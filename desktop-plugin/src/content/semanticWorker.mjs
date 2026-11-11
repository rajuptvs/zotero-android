/*
 * The semantic search worker (ai/spec/semantic.md), a ChromeWorker started by semantic.js: the
 * tokenizer, the embedding model (onnxruntime-web on WASM; a ChromeWorker is cross-origin isolated,
 * so it gets threads), chunking, and reading and writing index ZIPs, all off the main thread.
 * Answers {id, op, ...} with {id, result} or {id, error: {code, message, file?}}. One op at a time.
 */

import * as ort from "./vendor/ort.wasm.min.mjs";
import { Tokenizer } from "./vendor/tokenizers.min.mjs";
import { readIndex, writeIndex, WorkerError } from "./semanticIndex.mjs";

const MAX_TOKENS = 2048;
const EOS = 1;
// Python's str.isspace(), which the reference normalisation (\s+ in Python) uses.
const SPACE = "\t\n\v\f\r\x1c-\x1f \x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000";
const IS_SPACE = new RegExp(`[${SPACE}]`);
const SPACES = new RegExp(`[${SPACE}]+`, "g");
const TRIM = new RegExp(`^[${SPACE}]+|[${SPACE}]+$`, "g");

let config = null; // {dir, sha256: {file: hex}, verified: {file: "size:mtime"}}
let tokenizer = null;
let session = null;

// region Model

async function sha256(bytes) {
	let digest = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
	return Array.from(digest, (b) => b.toString(16).padStart(2, "0")).join("");
}

/**
 * A model file's bytes. Hashed only when it isn't the size and modification time it had when it
 * last matched its sha256 in this Zotero session; a match is posted back as {verified}.
 */
async function readVerified(file) {
	let path = PathUtils.join(config.dir, ...file.split("/"));
	let bytes, stamp;
	try {
		let info = await IOUtils.stat(path);
		stamp = `${info.size}:${info.lastModified}`;
		bytes = await IOUtils.read(path);
	}
	catch (e) {
		throw new WorkerError("model_missing", `${file} is missing.`, file);
	}
	if (config.verified?.[file] != stamp) {
		if (await sha256(bytes) != config.sha256[file]) throw new WorkerError("model_hash", `${file} doesn't match its checksum.`, file);
		config.verified = { ...config.verified, [file]: stamp };
		postMessage({ verified: { file, stamp } });
	}
	return bytes;
}

async function getTokenizer() {
	if (!tokenizer) {
		let bytes = await readVerified("tokenizer.json");
		tokenizer = new Tokenizer(JSON.parse(new TextDecoder().decode(bytes)), {});
	}
	return tokenizer;
}

async function getSession() {
	if (!session) {
		let onnx = await readVerified("onnx/model_quantized.onnx");
		let data = await readVerified("onnx/model_quantized.onnx_data");
		// Half the cores, at most 4: enough for queries, and building leaves the computer usable.
		ort.env.wasm.numThreads = Math.max(1, Math.min(4, Math.floor((navigator.hardwareConcurrency || 2) / 2)));
		ort.env.wasm.wasmPaths = new URL("./vendor/", import.meta.url).href;
		// Handed over as bytes: resource:// serves it with a charset, which streaming compile rejects.
		ort.env.wasm.wasmBinary = await (await fetch(new URL("./vendor/ort-wasm-simd-threaded.wasm", import.meta.url))).arrayBuffer();
		session = await ort.InferenceSession.create(onnx, {
			executionProviders: ["wasm"],
			externalData: [{ path: "model_quantized.onnx_data", data }],
		});
	}
	return session;
}

/** Token ids as the `tokenizers` library gives them, cut to 2048 keeping the final EOS. */
function encode(tok, text) {
	let ids = tok.encode(text).ids;
	return ids.length > MAX_TOKENS ? [...ids.slice(0, MAX_TOKENS - 1), EOS] : ids;
}

async function embed(text) {
	let ids = encode(await getTokenizer(), text);
	let sess = await getSession();
	let n = ids.length;
	let empty = new ort.Tensor("float32", new Float32Array(0), [0, 512]);
	let inputs = {
		input_ids: new ort.Tensor("int64", BigInt64Array.from(ids, BigInt), [1, n]),
		attention_mask: new ort.Tensor("int64", new BigInt64Array(n).fill(1n), [1, n]),
		image_features: empty,
		video_features: empty,
		audio_features: empty,
	};
	let out = await sess.run(inputs, ["sentence_embedding"]);
	let embedding = Float32Array.from(out.sentence_embedding.data);
	for (let t of Object.values(out)) t.dispose?.();
	return embedding;
}

// endregion

// region Chunking

/**
 * The spec's text normalisation (NFC, whitespace runs → one space, trimmed), with the raw index
 * of every output character and the output positions where a run held a line break.
 */
function normalizeMapped(raw) {
	let out = [], map = [], breaks = new Set();
	let i = 0;
	while (i < raw.length && IS_SPACE.test(raw[i])) i++;
	while (i < raw.length) {
		if (IS_SPACE.test(raw[i])) {
			let start = i, lineBreak = false;
			while (i < raw.length && IS_SPACE.test(raw[i])) {
				if (raw[i] == "\n" || raw[i] == "\r") lineBreak = true;
				i++;
			}
			if (i == raw.length) break;
			if (lineBreak) breaks.add(out.length);
			out.push(" ");
			map.push(start);
		}
		else {
			out.push(raw[i]);
			map.push(i);
			i++;
		}
	}
	return { text: out.join(""), map, breaks };
}

function normalize(text) {
	return text.normalize("NFC").replace(SPACES, " ").replace(/^ | $/g, "");
}

function trim(text) {
	return text.replace(TRIM, "");
}

/** Each token's start offset in `text` (UTF-16), plus the end; byte-fallback tokens are decoded. */
function tokenOffsets(tok, text) {
	let tokens = tok.encode(text, { add_special_tokens: false }).tokens;
	let starts = new Array(tokens.length + 1);
	let pos = 0, bytes = [];
	let decoder = new TextDecoder("utf-8", { fatal: false });
	for (let k = 0; k < tokens.length; k++) {
		starts[k] = pos;
		let m = /^<0x([0-9A-F]{2})>$/.exec(tokens[k]);
		if (m) {
			bytes.push(parseInt(m[1], 16));
			// Advance once the bytes make whole characters.
			let lead = bytes[0];
			let need = lead < 0x80 ? 1 : lead >= 0xF0 ? 4 : lead >= 0xE0 ? 3 : lead >= 0xC0 ? 2 : 1;
			if (bytes.length >= need) {
				pos += decoder.decode(new Uint8Array(bytes)).length;
				bytes = [];
			}
		}
		else {
			if (bytes.length) {
				pos += decoder.decode(new Uint8Array(bytes)).length;
				bytes = [];
			}
			pos += tokens[k].length;
		}
	}
	if (bytes.length) pos += decoder.decode(new Uint8Array(bytes)).length;
	starts[tokens.length] = pos;
	if (pos != text.length) return null;
	return { tokens, starts };
}

/**
 * Cuts a text into the spec's windows: at most `maxTokens` with the document prefix, BOS and EOS,
 * overlapping by `overlap`, each ending at the last sentence end or line break within its final
 * `overlap` tokens when there is one (else at a word start). → {title, chunks: [raw text]}.
 */
async function chunk({ title, text, maxTokens = 512, overlap = 64, titleTokens = 32 }) {
	let tok = await getTokenizer();
	let count = (s) => tok.encode(s, { add_special_tokens: false }).ids.length;
	let t = normalize(title || "");
	let tt = t && tokenOffsets(tok, t);
	if (tt && tt.tokens.length > titleTokens) t = trim(t.slice(0, tt.starts[titleTokens]));
	t = t || "none";
	let raw = (text || "").normalize("NFC");
	let n = normalizeMapped(raw);
	if (!n.text) return { title: t, chunks: [] };
	let prefix = `title: ${t} | text:`;
	let fits = (s) => count(`${prefix} ${s}`) + 2 <= maxTokens;
	let rawSlice = (a, b) => trim(raw.slice(n.map[a], n.map[b - 1] + 1));
	if (fits(n.text)) return { title: t, chunks: [rawSlice(0, n.text.length)] };

	let offsets = tokenOffsets(tok, n.text);
	if (!offsets) throw new WorkerError("chunk", "Couldn't map tokens to the text.");
	let { tokens, starts } = offsets;
	let total = tokens.length;
	let budget = maxTokens - 2 - count(prefix);
	if (budget <= overlap * 2) throw new WorkerError("chunk", "The title leaves no room for text.");
	// A token that starts a word (its text begins with the space).
	let wordStart = (k) => k == 0 || k == total || tokens[k][0] == "▁";
	let goodEnd = (k) => {
		if (k == total) return true;
		if (!wordStart(k)) return false;
		let p = starts[k];
		return n.breaks.has(p) || (p > 0 && ".?!".includes(n.text[p - 1]));
	};
	let chunks = [];
	let s = 0;
	while (s < total) {
		let limit = Math.min(s + budget, total);
		let e = limit;
		if (limit < total) {
			e = -1;
			for (let k = limit; k > limit - overlap && k > s; k--) {
				if (goodEnd(k)) { e = k; break; }
			}
			if (e < 0) {
				for (let k = limit; k > s + overlap; k--) {
					if (wordStart(k)) { e = k; break; }
				}
			}
			if (e < 0) e = limit;
		}
		// Re-tokenising a slice can differ at its edges; step back a word until it fits.
		while (e > s + overlap && !fits(trim(n.text.slice(starts[s], starts[e])))) {
			let k = e - 1;
			while (k > s + overlap && !wordStart(k)) k--;
			e = k;
		}
		let piece = rawSlice(starts[s], starts[e]);
		if (piece) chunks.push(piece);
		if (e >= total) break;
		let next = e - overlap;
		while (next < e && !wordStart(next)) next++;
		s = next < e && next > s ? next : e;
	}
	return { title: t, chunks };
}

// endregion

const OPS = {
	configure(m) {
		let changed = !config || config.dir != m.dir;
		config = { dir: m.dir, sha256: m.sha256, verified: m.verified || {} };
		if (changed) {
			tokenizer = null;
			session?.release?.();
			session = null;
		}
		return {};
	},
	async tokenize(m) {
		return { ids: encode(await getTokenizer(), m.text) };
	},
	async embed(m) {
		return { embedding: await embed(m.text) };
	},
	async embedDocument(m) {
		return { embedding: await embed(`title: ${m.title} | text: ${normalize(m.text)}`) };
	},
	chunk,
	readIndex,
	writeIndex,
	async hashFile(m) {
		return { hex: await sha256(await IOUtils.read(m.path)) };
	},
};

onmessage = async (event) => {
	let m = event.data;
	try {
		let result = await OPS[m.op](m);
		let transfer = [];
		if (result?.embedding) transfer.push(result.embedding.buffer);
		if (result?.vectors) transfer.push(result.vectors.buffer);
		postMessage({ id: m.id, result }, transfer);
	}
	catch (e) {
		postMessage({ id: m.id, error: { code: e.code || "failed", message: String(e?.message || e), file: e.file || null, detail: e.detail || null } });
	}
};
