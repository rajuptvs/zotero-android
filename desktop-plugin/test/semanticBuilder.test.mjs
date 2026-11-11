// The semantic index builder's bookkeeping (src/content/semanticBuilder.js) outside Zotero, on an
// in-memory SQLite database: the dirty mark survives an interrupted run, signatures follow content,
// and an index holds at most MAX_ROWS rows.
//   node --test desktop-plugin/test/*.test.mjs
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import test from "node:test";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const source = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "../src/content/semanticBuilder.js"), "utf8");

/** The Sqlite.sys.mjs calls the builder makes, over node:sqlite. */
function database() {
	let db = new DatabaseSync(":memory:");
	db.exec("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)");
	db.exec("CREATE TABLE items (itemKey TEXT PRIMARY KEY, signature TEXT NOT NULL, rows TEXT NOT NULL, vectors BLOB NOT NULL)");
	return {
		raw: db,
		async execute(sql, params = []) {
			let statement = db.prepare(sql);
			if (/^\s*SELECT/i.test(sql)) return statement.all(...params).map((r) => ({ getResultByName: (n) => r[n] }));
			statement.run(...params);
			return [];
		},
		async executeTransaction(fn) {
			db.exec("BEGIN");
			try {
				await fn();
				db.exec("COMMIT");
			}
			catch (e) {
				db.exec("ROLLBACK");
				throw e;
			}
		},
		async close() {},
	};
}

/** A builder in a fresh context with a library of `keys`, whose signatures the test controls. */
function setup(keys) {
	let published = [];
	let attachment = {
		key: "INDEX234",
		getFilePathAsync: async () => "/library/index.zip",
		setNote() {},
		saveTx: async () => {
			if (env.failSave) throw new Error("save failed");
		},
	};
	let env = { published, attachment, failSave: false, signatures: Object.fromEntries(keys.map((k) => [k, `${k}-1`])), onInputs: null };
	let prefs = {};
	let context = {
		setTimeout: () => 0,
		clearTimeout() {},
		IOUtils: {
			copy: async (from, to) => published.push(to),
			remove: async () => {},
			makeDirectory: async () => {},
		},
		PathUtils: { join: (...p) => p.join("/"), parent: (p) => p.split("/").slice(0, -1).join("/") },
		Zotero: {
			logError() {},
			DataDirectory: { dir: "/data" },
			DB: { executeTransaction: async (fn) => fn() },
			Sync: { Storage: { Local: { updateSyncStates: async () => {} } }, Runner: { syncInProgress: false } },
			Libraries: { userLibraryID: 1, userLibrary: { libraryVersion: 7 } },
			Promise: { delay: async () => {} },
			Utilities: { Internal: { md5: (s) => createHash("md5").update(s).digest("hex") } },
			Items: { get: (ids) => ids },
		},
		ZoteroAI: {
			version: "test",
			pref: (k, v) => (v === undefined ? prefs[k] : (prefs[k] = v)),
			randomToken: () => Math.random().toString(36).slice(2),
			plainText: (html) => html.replace(/<[^>]+>/g, ""),
			Semantic: {
				MODEL_ID: "m", REVISION: "r", FILES: [], INDEX_FILENAME: "index.zip",
				isEnabled: () => true,
				isModelReady: () => true,
				indexAttachment: async () => attachment,
				_changed() {},
				_dropIndex() {},
				call: async (op, data) => {
					env.written = data;
					return {};
				},
			},
			Tools: {},
		},
	};
	vm.createContext(context);
	vm.runInContext(source, context);
	let B = context.ZoteroAI.SemanticBuilder;
	B._db = database();
	B._manifest = async () => ({ builder: { id: B.builderId() } });
	B._items = async () => keys.map((key) => ({ key }));
	env.inputs = B._inputs.bind(B);
	B._inputs = async (item) => {
		await env.onInputs?.(item);
		return { item, title: "", texts: [], signature: env.signatures[item.key] };
	};
	B._embed = async () => ({ rows: [{ source: "metadata", key: null, text: "t" }], vectors: new Int8Array(B.DIM) });
	env.B = B;
	env.context = context;
	env.meta = async (key) => B._getMeta(key);
	return env;
}

/** A first full run that publishes, then the daily interval treated as passed. */
async function built(keys) {
	let env = setup(keys);
	await env.B.run({ write: true });
	assert.equal(env.B.status().state, "idle");
	assert.equal(env.published.length, 1);
	await env.B._setMeta("writtenAt", "0");
	return env;
}

test("an interrupted run leaves the index dirty, and an unchanged resume publishes it", async () => {
	let env = await built(["AAAAAAAA", "BBBBBBBB"]);
	assert.equal(await env.meta("dirty"), "0");
	env.signatures.AAAAAAAA = "AAAAAAAA-2";
	env.onInputs = (item) => {
		if (item.key == "BBBBBBBB") env.B._stop = true;
	};
	await env.B.run();
	assert.equal(env.B.status().state, "stopped");
	assert.equal(env.published.length, 1);
	assert.notEqual(await env.meta("dirty"), "0");

	env.onInputs = null;
	await env.B.run();
	assert.equal(env.published.length, 2, "the resumed run publishes the stored change");
	assert.equal(await env.meta("dirty"), "0");
	await env.B.run();
	assert.equal(env.published.length, 2, "nothing left to publish");
});

test("a run that fails after storing a change leaves the index dirty", async () => {
	let env = await built(["AAAAAAAA", "BBBBBBBB"]);
	env.signatures.AAAAAAAA = "AAAAAAAA-2";
	env.signatures.BBBBBBBB = "BBBBBBBB-2";
	let embed = env.B._embed;
	env.B._embed = async (inputs) => {
		if (inputs.item.key == "BBBBBBBB") throw new Error("worker stopped");
		return embed(inputs);
	};
	await env.B.run();
	assert.equal(env.B.status().state, "error");
	assert.notEqual(await env.meta("dirty"), "0");
	env.B._embed = embed;
	env.signatures.BBBBBBBB = "BBBBBBBB-1";
	await env.B.run();
	assert.equal(env.published.length, 2);
	assert.equal(await env.meta("dirty"), "0");
});

test("deleting an item marks the index dirty in the same transaction", async () => {
	let env = await built(["AAAAAAAA", "BBBBBBBB"]);
	env.B._items = async () => [{ key: "AAAAAAAA" }];
	env.B._stop = true;
	await env.B._update([{ key: "AAAAAAAA" }], () => {});
	assert.notEqual(await env.meta("dirty"), "0");
	env.B._stop = false;
	await env.B.run();
	assert.equal(env.published.length, 2);
});

test("a failed publication, or a change stored during one, keeps the index dirty", async () => {
	let env = await built(["AAAAAAAA"]);
	env.signatures.AAAAAAAA = "AAAAAAAA-2";
	env.failSave = true;
	await env.B.run();
	assert.equal(env.B.status().state, "error");
	assert.notEqual(await env.meta("dirty"), "0");

	env.failSave = false;
	let writeIndex = env.B.writeIndex.bind(env.B);
	env.B.writeIndex = async (...args) => {
		let out = await writeIndex(...args);
		await env.B._markDirty(); // what a concurrent _update would do
		return out;
	};
	await env.B.run();
	assert.notEqual(await env.meta("dirty"), "0");
	env.B.writeIndex = writeIndex;
	await env.B._setMeta("writtenAt", "0");
	await env.B.run();
	assert.equal(await env.meta("dirty"), "0");
});

test("an index holds at most MAX_ROWS rows; the items past that are counted", async () => {
	let env = await built(["AAAAAAAA", "BBBBBBBB", "CCCCCCCC"]);
	assert.equal(env.B.status().leftOut, 0);
	env.B.MAX_ROWS = 2;
	await env.B.run({ write: true });
	assert.equal(env.written.manifest.count, 2);
	assert.equal(env.written.rows.length, 2);
	assert.equal(env.B.status().leftOut, 1);
	assert.equal(await env.meta("leftOut"), "1");
});

test("an empty index isn't published", async () => {
	let env = setup([]);
	env.context.ZoteroAI.Semantic.indexAttachment = async () => null;
	await env.B.run({ write: true });
	assert.equal(env.published.length, 0);
	assert.equal(env.B.status().state, "idle");
});

test("signatures follow content, not versions or dates", async () => {
	let env = setup([]);
	let T = env.context.ZoteroAI.Tools;
	Object.assign(T, {
		_title: (i) => i.title,
		_field: (i, f) => i.fields[f] || "",
		_year: () => "2020",
		_sortedTags: () => [],
		_textAttachment: () => null,
	});
	let note = (key, html, version) => ({ key, deleted: false, version, dateModified: `d${version}`, getNote: () => html });
	let item = (over = {}) => ({
		key: "AAAAAAAA", version: 1, dateModified: "2026-01-01", title: "A title", fields: { abstractNote: "An abstract" },
		getCreators: () => [], notes: [note("NNNNNNNN", "<p>note</p>", 1)], ...over,
		getNotes() { return this.notes; },
	});
	let sig = async (i) => (await env.inputs(i)).signature;
	let base = await sig(item());
	assert.equal(await sig(item({ version: 9, dateModified: "2026-02-02" })), base);
	assert.equal(await sig(item({ notes: [note("NNNNNNNN", "<p>note</p>", 5)] })), base);
	assert.notEqual(await sig(item({ title: "Another title" })), base);
	assert.notEqual(await sig(item({ fields: { abstractNote: "Changed" } })), base);
	assert.notEqual(await sig(item({ notes: [note("NNNNNNNN", "<p>edited</p>", 1)] })), base);
	assert.notEqual(await sig(item({ notes: [] })), base);
});
