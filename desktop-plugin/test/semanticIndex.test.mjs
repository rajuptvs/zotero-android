// The index reader's checks (ai/spec/semantic.md, "Index file"), on small synthetic ZIPs; the bombs
// are streamed through zlib, so nothing large is ever allocated:
//   node --test desktop-plugin/test/*.test.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import zlib from "node:zlib";
import { DAMAGED, LIMITS, crc32, inflate, parseIndex, readIndex, zipBytes, zipEntries } from "../src/content/semanticIndex.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const enc = new TextEncoder();

const MANIFEST = { format: "zotero-ai-index", version: 1, model: { revision: "r" }, dim: 4, count: 2, items: 1 };
const ROWS = [
	{ itemKey: "ABCD2345", source: "metadata", key: null, text: "first" },
	{ itemKey: "ABCD2345", source: "fulltext", key: "WXYZ6789", text: "x".repeat(2000) },
];
const jsonl = (rows) => rows.map((r) => JSON.stringify(r) + "\n").join("");

/** An index ZIP; `parts` replaces or adds entries ({name: bytes | {data, deflate}}), null drops one. */
function index({ manifest = MANIFEST, rows = ROWS, vectors = new Uint8Array(manifest.dim * manifest.count).fill(3), extra = {} } = {}) {
	let entries = {
		"manifest.json": { data: enc.encode(JSON.stringify(manifest)), deflate: false },
		"chunks.jsonl": { data: enc.encode(jsonl(rows)), deflate: true },
		"vectors.i8": { data: vectors, deflate: false },
		...extra,
	};
	return zipBytes(Object.entries(entries).filter(([, e]) => e).map(([name, e]) => ({ name, ...e })));
}

const damaged = (promise) => assert.rejects(promise, (e) => e.code == "index" && e.message == DAMAGED);

/** `bytes` zeros, deflated, by streaming one small buffer through zlib (nothing that size exists). */
function deflatedZeros(bytes) {
	let zeros = Buffer.alloc(1 << 20);
	let deflate = zlib.createDeflateRaw({ level: 9 });
	let out = [];
	deflate.on("data", (d) => out.push(d));
	return new Promise((resolve, reject) => {
		deflate.on("end", () => resolve(new Uint8Array(Buffer.concat(out))));
		deflate.on("error", reject);
		let left = bytes;
		let write = () => {
			while (left > 0) {
				let n = Math.min(left, zeros.length);
				left -= n;
				if (!deflate.write(n == zeros.length ? zeros : zeros.subarray(0, n))) return deflate.once("drain", write);
			}
			deflate.end();
		};
		write();
	});
}

/** A hand-made ZIP entry list → bytes; each entry {name, method, raw, size, crc}. */
function rawZip(entries) {
	let parts = [], central = [], offset = 0;
	for (let e of entries) {
		let name = enc.encode(e.name);
		let local = new Uint8Array(30 + name.length);
		let lv = new DataView(local.buffer);
		lv.setUint32(0, 0x04034b50, true);
		lv.setUint16(8, e.method, true);
		lv.setUint32(14, e.crc ?? 0, true);
		lv.setUint32(18, e.raw.length, true);
		lv.setUint32(22, e.size, true);
		lv.setUint16(26, name.length, true);
		local.set(name, 30);
		let cd = new Uint8Array(46 + name.length);
		let cv = new DataView(cd.buffer);
		cv.setUint32(0, 0x02014b50, true);
		cv.setUint16(10, e.method, true);
		cv.setUint32(16, e.crc ?? 0, true);
		cv.setUint32(20, e.raw.length, true);
		cv.setUint32(24, e.size, true);
		cv.setUint16(28, name.length, true);
		cv.setUint32(42, offset, true);
		cd.set(name, 46);
		parts.push(local, e.raw);
		central.push(cd);
		offset += local.length + e.raw.length;
	}
	let size = central.reduce((n, c) => n + c.length, 0);
	let end = new Uint8Array(22);
	let ev = new DataView(end.buffer);
	ev.setUint32(0, 0x06054b50, true);
	ev.setUint16(8, entries.length, true);
	ev.setUint16(10, entries.length, true);
	ev.setUint32(12, size, true);
	ev.setUint32(16, offset, true);
	return new Uint8Array(Buffer.concat([...parts, ...central, end]));
}

const stored = (name, data) => ({ name, method: 0, raw: data, size: data.length, crc: crc32(data) });
const goodEntries = () => [
	stored("manifest.json", enc.encode(JSON.stringify(MANIFEST))),
	stored("chunks.jsonl", enc.encode(jsonl(ROWS))),
	stored("vectors.i8", new Uint8Array(8)),
];

/** Counts what every DecompressionStream puts out, to show a bomb is stopped early. */
async function countingInflater(fn) {
	let Original = globalThis.DecompressionStream;
	let produced = 0;
	globalThis.DecompressionStream = class {
		constructor(format) {
			let inner = new Original(format);
			this.writable = inner.writable;
			this.readable = inner.readable.pipeThrough(new TransformStream({
				transform(chunk, controller) {
					produced += chunk.length;
					controller.enqueue(chunk);
				},
			}));
		}
	};
	try {
		await fn();
	}
	finally {
		globalThis.DecompressionStream = Original;
	}
	return produced;
}

test("reads an index it wrote, keeping 800 characters of each text", async () => {
	let data = await parseIndex(await index());
	assert.deepEqual(Array.from(data.itemKeys), ["ABCD2345", "ABCD2345"]);
	assert.deepEqual(Array.from(data.keys), [null, "WXYZ6789"]);
	assert.equal(data.texts[1].length, 800);
	assert.equal(data.vectors.length, 8);
	assert.equal((await parseIndex(await index(), { maxText: 5000 })).texts[1].length, 800);
});

test("reads the golden conformance index", async () => {
	let data = await parseIndex(new Uint8Array(readFileSync(join(here, "../../ai/conformance/semantic/index.zip"))));
	assert.equal(data.vectors.length, data.manifest.count * data.manifest.dim);
});

test("another format comes back as just its manifest", async () => {
	let data = await parseIndex(await index({ manifest: { ...MANIFEST, version: 2 } }));
	assert.deepEqual(Object.keys(data), ["manifest"]);
});

test("only the three entries, each once", async () => {
	await damaged(parseIndex(await index({ extra: { "other.txt": { data: enc.encode("x"), deflate: false } } })));
	await damaged(parseIndex(await index({ extra: { "vectors.i8": null } })));
	await damaged(parseIndex(rawZip([...goodEntries().slice(0, 2), stored("chunks.jsonl", enc.encode(jsonl(ROWS)))])));
	await damaged(parseIndex(rawZip([...goodEntries().slice(0, 2), stored("Vectors.i8", new Uint8Array(8))])));
	await parseIndex(rawZip(goodEntries()));
	await damaged(parseIndex(enc.encode("not a zip at all, just some words")));
});

test("manifest limits: dim 1–768, count 1–200,000", async () => {
	for (let [dim, count] of [[0, 2], [769, 1], [4, 0], [1, LIMITS.count + 1], [2.5, 2], ["4", 2]]) {
		let manifest = { ...MANIFEST, dim, count };
		await damaged(parseIndex(await index({ manifest, vectors: new Uint8Array(8) })), `dim ${dim} count ${count}`);
	}
	await damaged(parseIndex(await index({ manifest: { ...MANIFEST, x: "y".repeat(LIMITS.manifest) } })));
	await damaged(parseIndex(await index({ extra: { "manifest.json": { data: enc.encode("[1]"), deflate: false } } })));
});

test("vectors must be exactly count × dim", async () => {
	await damaged(parseIndex(await index({ vectors: new Uint8Array(7) })));
	await damaged(parseIndex(await index({ vectors: new Uint8Array(9) })));
	await damaged(parseIndex(await index({ extra: { "vectors.i8": { data: new Uint8Array(9), deflate: true } } })));
});

test("chunks: exactly count lines, each a valid row", async () => {
	let row = ROWS[0];
	for (let rows of [
		[row],
		[row, row, row],
		[row, { ...row, itemKey: "SHORT" }],
		[row, { ...row, source: "pdf" }],
		[row, { ...row, key: 5 }],
		[row, { ...row, text: null }],
		[row, [1, 2]],
	]) {
		await damaged(parseIndex(await index({ rows })), JSON.stringify(rows));
	}
	let noFinalNewline = enc.encode(jsonl(ROWS).trimEnd());
	await parseIndex(await index({ extra: { "chunks.jsonl": { data: noFinalNewline, deflate: true } } }));
	let blankLine = enc.encode(jsonl([row]) + "\n" + jsonl([row]));
	await damaged(parseIndex(await index({ extra: { "chunks.jsonl": { data: blankLine, deflate: true } } })));
	let badUtf8 = new Uint8Array([...enc.encode(jsonl([row])), 0x7B, 0xFF, 0x7D, 0x0A]);
	await damaged(parseIndex(await index({ extra: { "chunks.jsonl": { data: badUtf8, deflate: true } } })));
});

test("a chunks.jsonl line over 64 KiB is refused, one at the limit isn't", async () => {
	let fits = { ...ROWS[0], text: "" };
	fits.text = "y".repeat(LIMITS.line - JSON.stringify(fits).length);
	await parseIndex(await index({ rows: [fits, ROWS[1]] }));
	await damaged(parseIndex(await index({ rows: [{ ...fits, text: fits.text + "y" }, ROWS[1]] })));
});

test("a bad CRC or declared size is refused", async () => {
	let entries = goodEntries();
	entries[1].crc ^= 1;
	await damaged(parseIndex(rawZip(entries)));
	entries = goodEntries();
	let deflated = new Uint8Array(zlib.deflateRawSync(entries[1].raw));
	entries[1] = { ...entries[1], method: 8, raw: deflated, size: entries[1].size - 1 };
	await damaged(parseIndex(rawZip(entries)));
});

test("a deflate bomb in chunks.jsonl stops at its declared size", async () => {
	let bomb = await deflatedZeros(256 << 20);
	let entries = goodEntries();
	entries[1] = { name: "chunks.jsonl", method: 8, raw: bomb, size: 1000, crc: 0 };
	let produced = await countingInflater(() => damaged(parseIndex(rawZip(entries))));
	assert.ok(produced < 16 << 20, `inflated ${produced} bytes`);
});

test("a deflate bomb in manifest.json stops at 64 KiB", async () => {
	let bomb = await deflatedZeros(256 << 20);
	let entries = goodEntries();
	entries[0] = { name: "manifest.json", method: 8, raw: bomb, size: LIMITS.manifest, crc: 0 };
	let produced = await countingInflater(() => damaged(parseIndex(rawZip(entries))));
	assert.ok(produced < 16 << 20, `inflated ${produced} bytes`);
});

test("entries declared over their limit aren't inflated at all", async () => {
	let bomb = await deflatedZeros(1 << 20);
	let entries = goodEntries();
	entries[1] = { name: "chunks.jsonl", method: 8, raw: bomb, size: LIMITS.chunks + 1, crc: 0 };
	let produced = await countingInflater(() => damaged(parseIndex(rawZip(entries))));
	assert.equal(produced, 0);
	entries = goodEntries();
	entries[0] = { name: "manifest.json", method: 8, raw: bomb, size: LIMITS.manifest + 1, crc: 0 };
	assert.equal(await countingInflater(() => damaged(parseIndex(rawZip(entries)))), 0);
});

test("inflate counts against the limit while streaming", async () => {
	let bomb = await deflatedZeros(64 << 20);
	let seen = 0;
	await damaged(inflate({ name: "x", method: 8, raw: bomb, size: 1 << 20, crc: 0 }, 1 << 20, (d) => { seen += d.length; }));
	assert.ok(seen <= 1 << 20);
});

test("a file over 512 MiB isn't read", async () => {
	let read = false;
	globalThis.IOUtils = {
		stat: async () => ({ size: LIMITS.file + 1 }),
		read: async () => { read = true; return new Uint8Array(0); },
	};
	try {
		await damaged(readIndex({ path: "/x.zip" }));
		assert.equal(read, false);
	}
	finally {
		delete globalThis.IOUtils;
	}
	assert.throws(() => zipEntries({ length: LIMITS.file + 1 }), (e) => e.message == DAMAGED);
});
