/*
 * Index ZIPs (ai/spec/semantic.md, "Index file") for the semantic worker: writing them, and reading
 * them. An index comes from synced storage, so a read checks it while unzipping: only the three
 * entries, each inflated a few KB at a time with its bytes counted, and nothing sized from the
 * manifest allocated before the manifest's limits are checked. No Zotero APIs except IOUtils, so
 * the tests can load it in Node.
 */

export class WorkerError extends Error {
	constructor(code, message, file) {
		super(message);
		this.code = code;
		this.file = file;
	}
}

export const DAMAGED = "The search index is damaged or too large; rebuild it.";
export const LIMITS = {
	file: 512 * 1024 * 1024,
	manifest: 64 * 1024,
	chunks: 512 * 1024 * 1024,
	line: 64 * 1024, // one chunks.jsonl row, without its newline
	dim: 768,
	count: 200000,
	text: 800,
};
const NAMES = ["manifest.json", "chunks.jsonl", "vectors.i8"];
const SOURCES = ["metadata", "fulltext", "note"];
// Compressed bytes handed to the inflater at a time. Deflate expands at most ~1032×, so what it
// produces before the output is counted stays a few MB.
const SLICE = 4096;

/** The spec's one message for every damaged or oversized index; `detail` is for the log. */
function bad(detail) {
	let e = new WorkerError("index", DAMAGED);
	e.detail = detail;
	return e;
}

// region ZIP

const CRC_TABLE = (() => {
	let table = new Uint32Array(256);
	for (let i = 0; i < 256; i++) {
		let c = i;
		for (let k = 0; k < 8; k++) c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1;
		table[i] = c >>> 0;
	}
	return table;
})();

/** CRC-32 of `bytes`, continuing from the CRC of what came before them. */
export function crc32(bytes, crc = 0) {
	let c = (crc ^ 0xFFFFFFFF) >>> 0;
	for (let i = 0; i < bytes.length; i++) c = CRC_TABLE[(c ^ bytes[i]) & 0xFF] ^ (c >>> 8);
	return (c ^ 0xFFFFFFFF) >>> 0;
}

async function transform(bytes, stream) {
	let out = new Response(new Blob([bytes]).stream().pipeThrough(stream));
	return new Uint8Array(await out.arrayBuffer());
}

/**
 * The index ZIP's entries (stored or deflated, no ZIP64) → Map name → {method, crc, size, raw},
 * nothing inflated yet. Exactly the spec's three names, each once.
 */
export function zipEntries(bytes) {
	if (bytes.length > LIMITS.file) throw bad("file size");
	let view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
	let eocd = -1;
	for (let i = bytes.length - 22; i >= Math.max(0, bytes.length - 22 - 65535); i--) {
		if (view.getUint32(i, true) == 0x06054b50) { eocd = i; break; }
	}
	if (eocd < 0) throw bad("not a ZIP file");
	let entries = view.getUint16(eocd + 10, true);
	if (view.getUint16(eocd + 4, true) || view.getUint16(eocd + 6, true) || view.getUint16(eocd + 8, true) != entries
		|| entries != NAMES.length) {
		throw bad("entry count");
	}
	let p = view.getUint32(eocd + 16, true);
	let names = new TextDecoder("utf-8", { fatal: true });
	let files = new Map();
	for (let n = 0; n < entries; n++) {
		if (p + 46 > eocd || view.getUint32(p, true) != 0x02014b50) throw bad("central directory");
		let flags = view.getUint16(p + 8, true);
		let method = view.getUint16(p + 10, true);
		let crc = view.getUint32(p + 16, true);
		let compressed = view.getUint32(p + 20, true);
		let size = view.getUint32(p + 24, true);
		let nameLength = view.getUint16(p + 28, true);
		let extraLength = view.getUint16(p + 30, true);
		let commentLength = view.getUint16(p + 32, true);
		let local = view.getUint32(p + 42, true);
		if (p + 46 + nameLength > eocd) throw bad("central directory");
		let nameBytes = bytes.subarray(p + 46, p + 46 + nameLength);
		let name;
		try {
			name = names.decode(nameBytes);
		}
		catch (e) {
			throw bad("entry name");
		}
		p += 46 + nameLength + extraLength + commentLength;
		if (!NAMES.includes(name) || files.has(name)) throw bad(`entry ${name}`);
		if (flags & 1) throw bad(`${name} is encrypted`);
		if (method != 0 && method != 8) throw bad(`${name} compression`);
		if (compressed == 0xFFFFFFFF || size == 0xFFFFFFFF || local == 0xFFFFFFFF) throw bad(`${name} is ZIP64`);
		if (local + 30 > bytes.length || view.getUint32(local, true) != 0x04034b50) throw bad(`${name} local header`);
		let localName = view.getUint16(local + 26, true);
		let start = local + 30 + localName + view.getUint16(local + 28, true);
		if (localName != nameLength || start > bytes.length
			|| !bytes.subarray(local + 30, local + 30 + nameLength).every((b, i) => b == nameBytes[i])) {
			throw bad(`${name} local header`);
		}
		let raw = bytes.subarray(start, start + compressed);
		if (raw.length != compressed || (method == 0 && compressed != size)) throw bad(`${name} size`);
		files.set(name, { name, method, crc, size, raw });
	}
	return files;
}

/**
 * Unzips an entry, passing its bytes to `onData` as they come out; stops as soon as they pass
 * `limit` or the entry's declared size, and checks the size and CRC at the end.
 */
export async function inflate(entry, limit, onData) {
	let { name, method, crc, size, raw } = entry;
	if (size > limit) throw bad(`${name} is too large`);
	let total = 0, sum = 0;
	let take = (data) => {
		total += data.length;
		if (total > size) throw bad(`${name} is larger than it says`);
		sum = crc32(data, sum);
		onData(data);
	};
	if (method == 0) take(raw);
	else {
		let stream = new DecompressionStream("deflate-raw");
		let writer = stream.writable.getWriter();
		let reader = stream.readable.getReader();
		// One slice in the inflater at a time: the next is written only once this one's output is read.
		let feeding = (async () => {
			for (let at = 0; at < raw.length; at += SLICE) await writer.write(raw.subarray(at, at + SLICE));
			await writer.close();
		})();
		feeding.catch(() => {});
		try {
			for (;;) {
				let { done, value } = await reader.read();
				if (done) break;
				take(value);
			}
			await feeding;
		}
		catch (e) {
			reader.cancel().catch(() => {});
			writer.abort().catch(() => {});
			if (e instanceof WorkerError) throw e;
			throw bad(`${name} doesn't unzip`);
		}
	}
	if (total != size || sum != crc) throw bad(`${name} size or CRC`);
}

/** The ZIP of [{name, data, deflate}] as bytes. */
export async function zipBytes(entries) {
	let now = new Date();
	let time = (now.getHours() << 11) | (now.getMinutes() << 5) | (now.getSeconds() >> 1);
	let date = ((now.getFullYear() - 1980) << 9) | ((now.getMonth() + 1) << 5) | now.getDate();
	let parts = [], central = [];
	let offset = 0;
	for (let entry of entries) {
		let name = new TextEncoder().encode(entry.name);
		let data = entry.deflate ? await transform(entry.data, new CompressionStream("deflate-raw")) : entry.data;
		let crc = crc32(entry.data);
		let header = (signature, extra) => {
			let h = new Uint8Array(extra + name.length);
			let v = new DataView(h.buffer);
			v.setUint32(0, signature, true);
			return { h, v };
		};
		let { h: local, v: lv } = header(0x04034b50, 30);
		lv.setUint16(4, 20, true);
		lv.setUint16(8, entry.deflate ? 8 : 0, true);
		lv.setUint16(10, time, true);
		lv.setUint16(12, date, true);
		lv.setUint32(14, crc, true);
		lv.setUint32(18, data.length, true);
		lv.setUint32(22, entry.data.length, true);
		lv.setUint16(26, name.length, true);
		local.set(name, 30);
		let { h: cd, v: cv } = header(0x02014b50, 46);
		cv.setUint16(4, 20, true);
		cv.setUint16(6, 20, true);
		cv.setUint16(10, entry.deflate ? 8 : 0, true);
		cv.setUint16(12, time, true);
		cv.setUint16(14, date, true);
		cv.setUint32(16, crc, true);
		cv.setUint32(20, data.length, true);
		cv.setUint32(24, entry.data.length, true);
		cv.setUint16(28, name.length, true);
		cv.setUint32(42, offset, true);
		cd.set(name, 46);
		parts.push(local, data);
		central.push(cd);
		offset += local.length + data.length;
	}
	let centralSize = central.reduce((n, c) => n + c.length, 0);
	if (offset + centralSize > 0xFFFFFFFF) throw new WorkerError("index", "The search index is too large.");
	let end = new Uint8Array(22);
	let ev = new DataView(end.buffer);
	ev.setUint32(0, 0x06054b50, true);
	ev.setUint16(8, entries.length, true);
	ev.setUint16(10, entries.length, true);
	ev.setUint32(12, centralSize, true);
	ev.setUint32(16, offset, true);
	let all = new Uint8Array(offset + centralSize + 22);
	let p = 0;
	for (let part of [...parts, ...central, end]) {
		all.set(part, p);
		p += part.length;
	}
	return all;
}

// endregion

// region Index

/**
 * Reads and validates index bytes. → {manifest, itemKeys, sources, keys, texts (cut to `maxText`,
 * at most 800), vectors (Int8Array)}, or just {manifest} when its format or version is another
 * one. The caller checks the model.
 */
export async function parseIndex(bytes, { maxText = LIMITS.text } = {}) {
	let files = zipEntries(bytes);
	let utf8 = new TextDecoder("utf-8", { fatal: true });
	let manifestParts = [];
	await inflate(files.get("manifest.json"), LIMITS.manifest, (data) => manifestParts.push(data));
	let manifest;
	try {
		manifest = JSON.parse(utf8.decode(join(manifestParts)));
	}
	catch (e) {
		throw bad("manifest.json");
	}
	if (!manifest || typeof manifest != "object" || Array.isArray(manifest)) throw bad("manifest.json");
	// The format and model are checked by the caller, so it can say which one differs.
	if (manifest.format != "zotero-ai-index" || manifest.version != 1) return { manifest };
	let { dim, count } = manifest;
	if (!Number.isInteger(dim) || dim < 1 || dim > LIMITS.dim || !Number.isInteger(count) || count < 1 || count > LIMITS.count) {
		throw bad("manifest.json dim or count");
	}
	let vectorsEntry = files.get("vectors.i8");
	if (vectorsEntry.size != count * dim) throw bad("vectors.i8 size");
	let chunksEntry = files.get("chunks.jsonl");
	if (chunksEntry.size > LIMITS.chunks) throw bad("chunks.jsonl is too large");

	let keep = Math.min(maxText || LIMITS.text, LIMITS.text);
	let itemKeys = new Array(count), sources = new Array(count), keys = new Array(count), texts = new Array(count);
	let rows = 0;
	let line = (data) => {
		if (rows >= count) throw bad("chunks.jsonl has too many rows");
		let row;
		try {
			row = JSON.parse(utf8.decode(data));
		}
		catch (e) {
			throw bad(`chunks.jsonl row ${rows}`);
		}
		if (!row || typeof row != "object" || typeof row.itemKey != "string" || row.itemKey.length != 8
			|| !SOURCES.includes(row.source) || !(row.key === null || typeof row.key == "string") || typeof row.text != "string") {
			throw bad(`chunks.jsonl row ${rows}`);
		}
		itemKeys[rows] = row.itemKey;
		sources[rows] = row.source;
		keys[rows] = row.key;
		texts[rows] = row.text.slice(0, keep);
		rows++;
	};
	// Split into lines as the bytes come out; a line can span pieces.
	let pending = [], pendingBytes = 0;
	let hold = (piece) => {
		pendingBytes += piece.length;
		if (pendingBytes > LIMITS.line) throw bad("chunks.jsonl line too long");
		pending.push(piece);
	};
	await inflate(chunksEntry, LIMITS.chunks, (data) => {
		let start = 0;
		for (let nl; (nl = data.indexOf(10, start)) >= 0; start = nl + 1) {
			hold(data.subarray(start, nl));
			line(join(pending));
			pending = [];
			pendingBytes = 0;
		}
		if (start < data.length) hold(data.subarray(start));
	});
	if (pending.length) line(join(pending));
	if (rows != count) throw bad("chunks.jsonl row count");

	let vectors = new Uint8Array(count * dim);
	let at = 0;
	await inflate(vectorsEntry, count * dim, (data) => {
		vectors.set(data, at);
		at += data.length;
	});
	return { manifest, itemKeys, sources, keys, texts, vectors: new Int8Array(vectors.buffer) };
}

function join(parts) {
	if (parts.length == 1) return parts[0];
	let out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
	let p = 0;
	for (let part of parts) {
		out.set(part, p);
		p += part.length;
	}
	return out;
}

/** Reads and validates an index file (parseIndex). */
export async function readIndex({ path, maxText }) {
	let bytes;
	try {
		let info = await IOUtils.stat(path);
		if (info.size > LIMITS.file) throw bad("file size");
		bytes = await IOUtils.read(path);
	}
	catch (e) {
		if (e instanceof WorkerError) throw e;
		throw bad("can't read the file");
	}
	return parseIndex(bytes, { maxText });
}

/** Writes an index ZIP to `path` (atomically, via a temporary file). → {size}. */
export async function writeIndex({ path, manifest, rows, vectors }) {
	let jsonl = rows.map((r) => JSON.stringify({ itemKey: r.itemKey, source: r.source, key: r.key, text: r.text }) + "\n").join("");
	let enc = new TextEncoder();
	let all = await zipBytes([
		{ name: "manifest.json", data: enc.encode(JSON.stringify(manifest, null, 1)), deflate: false },
		{ name: "chunks.jsonl", data: enc.encode(jsonl), deflate: true },
		{ name: "vectors.i8", data: new Uint8Array(vectors.buffer, vectors.byteOffset, vectors.byteLength), deflate: false },
	]);
	await IOUtils.write(path, all, { tmpPath: path + ".tmp" });
	return { size: all.length };
}

// endregion
