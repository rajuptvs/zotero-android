/*
 * Builds the library's semantic search index (ai/spec/semantic.md, "Building") in the background:
 * chunks every regular item's metadata, full text and notes, embeds them in the worker one chunk at
 * a time, and keeps each item's rows in <data dir>/zotero-ai/semantic.sqlite, so a build resumes
 * where it stopped and later builds re-embed only items that changed. The index attachment is
 * written when a first build finishes and then at most once a day (or when the user asks). Every
 * stored change marks the index dirty in the same transaction; only a published index clears it.
 */

ZoteroAI.SemanticBuilder = {
	DIM: 512,
	// The most rows an index holds (semantic.md); items past that are left out.
	MAX_ROWS: 200000,
	WRITE_INTERVAL_MS: 24 * 60 * 60 * 1000,
	CHECK_INTERVAL_MS: 6 * 60 * 60 * 1000,
	// After library changes, wait this long so a burst of edits is one run.
	CHANGE_DELAY_MS: 10 * 60 * 1000,
	// Between chunks: short when the user is away, longer while they use the computer.
	PAUSE_IDLE_MS: 20,
	PAUSE_ACTIVE_MS: 400,
	USER_IDLE_MS: 60 * 1000,

	_db: null,
	_dbOpening: null,
	_run: null,
	_stop: false,
	_timer: null,
	_notifierID: null,
	_state: { state: "idle", done: 0, total: 0, message: null, finishedAt: null, leftOut: 0 },

	startup() {
		this._notifierID = Zotero.Notifier.registerObserver({
			notify: (event, type) => {
				if (type == "item" && ["add", "modify", "trash", "delete"].includes(event) && !this._run) this.schedule(this.CHANGE_DELAY_MS);
			},
		}, ["item"], "zotero-ai-semantic");
		if (ZoteroAI.Semantic.isEnabled()) this.schedule(30 * 1000);
	},

	shutdown() {
		this.stop();
		if (this._notifierID) Zotero.Notifier.unregisterObserver(this._notifierID);
		this._notifierID = null;
		let db = this._db;
		this._db = null;
		this._dbOpening = null;
		(this._run || Promise.resolve()).catch(() => {}).finally(() => db?.close().catch((e) => Zotero.logError(e)));
	},

	status() {
		return { ...this._state };
	},

	builderId() {
		return ZoteroAI.pref("semantic.builderId") || ZoteroAI.pref("semantic.builderId", ZoteroAI.randomToken(12));
	},

	/** Starts a run after `delay` ms (sooner wins), when semantic search is on. */
	schedule(delay) {
		if (!ZoteroAI.Semantic.isEnabled()) return;
		let at = Date.now() + delay;
		if (this._timer && this._timerAt <= at) return;
		clearTimeout(this._timer);
		this._timerAt = at;
		this._timer = setTimeout(() => {
			this._timer = null;
			this.run().catch((e) => Zotero.logError(e));
		}, delay);
	},

	stop() {
		clearTimeout(this._timer);
		this._timer = null;
		if (this._run) this._stop = true;
	},

	/** "Build now": a run that writes the index when it finishes. */
	buildNow() {
		return this.run({ write: true });
	},

	_set(changes) {
		Object.assign(this._state, changes);
		ZoteroAI.Semantic._changed();
	},

	async run({ write = false } = {}) {
		if (this._run) {
			if (write) this._writeAfter = true;
			return this._run;
		}
		this._stop = false;
		this._writeAfter = write;
		this._run = this._build().catch((e) => {
			Zotero.logError(e);
			this._set({ state: "error", message: e.message });
		}).finally(() => {
			this._run = null;
			if (ZoteroAI.Semantic.isEnabled() && !this._stop) this.schedule(this.CHECK_INTERVAL_MS);
		});
		return this._run;
	},

	async _build() {
		let S = ZoteroAI.Semantic;
		if (!S.isEnabled()) return;
		if (!S.isModelReady()) {
			this._set({ state: "waiting", message: "Waiting for the model to download." });
			return;
		}
		// One library, one builder: an index from another builder is only read.
		let attachment = await S.indexAttachment();
		if (attachment) {
			let manifest = await this._manifest(attachment);
			if (!manifest) {
				this._set({ state: "error", message: "Couldn't read the library's search index." });
				return;
			}
			if (manifest.builder?.id != this.builderId()) {
				this._set({ state: "other", message: "Another computer builds this library's index." });
				return;
			}
		}
		await this._openDB();
		this._set({ state: "running", done: 0, total: 0, message: null, leftOut: Number(await this._getMeta("leftOut")) || 0 });
		let items = await this._items();
		await this._update(items, (done) => this._set({ done, total: items.length }));
		if (this._stop) {
			this._set({ state: "stopped", message: null });
			return;
		}
		let dirty = this._isDirty(await this._getMeta("dirty"));
		let writtenAt = Number(await this._getMeta("writtenAt")) || 0;
		if (!attachment || this._writeAfter || (dirty && Date.now() - writtenAt >= this.WRITE_INTERVAL_MS)) {
			this._set({ state: "writing" });
			await this._writeAttachment(attachment);
		}
		else if (dirty) this.schedule(Math.max(0, writtenAt + this.WRITE_INTERVAL_MS - Date.now()));
		this._set({ state: "idle", finishedAt: new Date().toISOString(), message: null });
	},

	async _manifest(attachment) {
		try {
			return (await ZoteroAI.Semantic._loadIndex({ item: attachment })).manifest;
		}
		catch (e) {
			// Built with another model: rebuild it only if it's ours.
			if (e.message == ZoteroAI.Semantic.MESSAGES.otherModel) {
				let path = await attachment.getFilePathAsync();
				let data = path && await ZoteroAI.Semantic.call("readIndex", { path, maxText: 1 }).catch(() => null);
				return data?.manifest || null;
			}
			return null;
		}
	},

	/** Regular items of My Library, in key order. */
	async _items() {
		let items = await ZoteroAI.Tools._regularItems();
		return items.sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0));
	},

	/**
	 * Embeds the items whose inputs changed and (unless `keepOthers`) forgets stored items that
	 * aren't in `items`, marking the index dirty with each change. → whether anything changed.
	 */
	async _update(items, progress, { keepOthers = false } = {}) {
		let db = await this._openDB();
		let stored = new Map();
		for (let row of await db.execute("SELECT itemKey, signature FROM items")) {
			stored.set(row.getResultByName("itemKey"), row.getResultByName("signature"));
		}
		let changed = false;
		let keys = new Set(items.map((i) => i.key));
		let gone = keepOthers ? [] : [...stored.keys()].filter((k) => !keys.has(k));
		if (gone.length) {
			await db.executeTransaction(async () => {
				for (let key of gone) await db.execute("DELETE FROM items WHERE itemKey = ?", [key]);
				await this._markDirty();
			});
			changed = true;
		}
		let done = 0;
		for (let item of items) {
			if (this._stop) return changed;
			let inputs = await this._inputs(item);
			if (stored.get(item.key) != inputs.signature) {
				let { rows, vectors } = await this._embed(inputs);
				if (this._stop) return changed;
				await db.executeTransaction(async () => {
					await db.execute("INSERT OR REPLACE INTO items (itemKey, signature, rows, vectors) VALUES (?, ?, ?, ?)",
						[item.key, inputs.signature, JSON.stringify(rows), new Uint8Array(vectors.buffer)]);
					await this._markDirty();
				});
				changed = true;
			}
			progress(++done);
		}
		return changed;
	},

	// region Texts

	/**
	 * What an item's rows are made of, and a signature that changes whenever they would. Built from
	 * the content only, not versions or dates, so an edit that leaves the embedded texts alone (a
	 * collection move, a sync) doesn't re-embed the item.
	 */
	async _inputs(item) {
		let T = ZoteroAI.Tools;
		let md5 = (text) => Zotero.Utilities.Internal.md5(text, false);
		let title = T._title(item);
		let metadata = this.metadataText(item);
		let texts = [{ source: "metadata", key: null, text: metadata }];
		let parts = [item.key, md5(title + "\n" + metadata)];
		let attachment = T._textAttachment(item);
		if (attachment) {
			// The full-text cache file, or, before Zotero has indexed it, the attachment's own file.
			let file = Zotero.Fulltext.getItemCacheFile(attachment);
			let info = file && await IOUtils.stat(file.path).catch(() => null);
			let stamp = info ? `${info.size}:${info.lastModified}` : "-";
			if (!info) {
				let path = await attachment.getFilePathAsync();
				let own = path && await IOUtils.stat(path).catch(() => null);
				if (own) stamp = `file:${own.size}:${own.lastModified}`;
			}
			parts.push(attachment.key, stamp);
			texts.push({ source: "fulltext", key: attachment.key, attachment });
		}
		let notes = Zotero.Items.get(item.getNotes(false)).filter((n) => !n.deleted)
			.sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0));
		for (let note of notes) {
			let text = ZoteroAI.plainText(note.getNote());
			parts.push(note.key, md5(text));
			texts.push({ source: "note", key: note.key, text });
		}
		return { item, title, texts, signature: parts.join("|") };
	},

	/** The metadata chunk's text (spec: title, creators, year, publication, abstract, tags). */
	metadataText(item) {
		let T = ZoteroAI.Tools;
		let creators = item.getCreators()
			.map((c) => (c.fieldMode == 1 ? (c.lastName || "").trim() : `${(c.firstName || "").trim()} ${(c.lastName || "").trim()}`.trim()))
			.filter(Boolean).join(", ");
		let publication = ["publicationTitle", "proceedingsTitle", "bookTitle", "websiteTitle"]
			.map((f) => T._field(item, f)).find((v) => v) || "";
		let head = [T._title(item), creators, T._year(item), publication].filter(Boolean).map((p) => p + ".").join(" ");
		let tags = T._sortedTags(item);
		return [head, T._field(item, "abstractNote"), tags.length ? `Tags: ${tags.join(", ")}` : ""].filter(Boolean).join("\n\n");
	},

	/** → {rows: [{source, key, text}], vectors: Int8Array (rows × DIM)}. */
	async _embed(inputs) {
		let S = ZoteroAI.Semantic;
		let rows = [], vectors = [];
		for (let input of inputs.texts) {
			let text = input.text;
			if (input.attachment) text = await ZoteroAI.Tools._attachmentText(input.attachment, false);
			if (!text || !S.normalize(text)) continue;
			let { title, chunks } = await S.call("chunk", { title: inputs.title, text });
			for (let chunk of chunks) {
				if (this._stop) return { rows, vectors: new Int8Array(0) };
				let { embedding } = await S.call("embedDocument", { title, text: chunk });
				rows.push({ source: input.source, key: input.key, text: chunk });
				vectors.push(this.quantize(embedding));
				await this._pause();
			}
		}
		let out = new Int8Array(vectors.length * this.DIM);
		vectors.forEach((v, i) => out.set(v, i * this.DIM));
		return { rows, vectors: out };
	},

	/** A row of vectors.i8: the first DIM values, renormalised, × 127, rounded half away from zero. */
	quantize(embedding) {
		let n = 0;
		for (let k = 0; k < this.DIM; k++) n += embedding[k] * embedding[k];
		n = Math.sqrt(n);
		let out = new Int8Array(this.DIM);
		for (let k = 0; k < this.DIM; k++) {
			let x = embedding[k] / n * 127;
			out[k] = Math.sign(x) * Math.floor(Math.abs(x) + 0.5);
		}
		return out;
	},

	async _pause() {
		let idle = 0;
		try {
			idle = Cc["@mozilla.org/widget/useridleservice;1"].getService(Ci.nsIUserIdleService).idleTime;
		}
		catch (e) {}
		await Zotero.Promise.delay(idle >= this.USER_IDLE_MS ? this.PAUSE_IDLE_MS : this.PAUSE_ACTIVE_MS);
		while (Zotero.Sync.Runner.syncInProgress && !this._stop) await Zotero.Promise.delay(2000);
	},

	// endregion

	// region Index file

	/**
	 * Writes the index ZIP for `items` (default: everything stored) to `path`, at most MAX_ROWS rows.
	 * → {manifest, leftOut: items that didn't fit}.
	 */
	async writeIndex(path, items = null) {
		let db = await this._openDB();
		let keys = items ? new Set(items.map((i) => i.key)) : null;
		let rows = [], parts = [], itemCount = 0, leftOut = 0;
		for (let r of await db.execute("SELECT itemKey, rows, vectors FROM items ORDER BY itemKey")) {
			let itemKey = r.getResultByName("itemKey");
			if (keys && !keys.has(itemKey)) continue;
			let itemRows = JSON.parse(r.getResultByName("rows"));
			if (!itemRows.length) continue;
			if (rows.length + itemRows.length > this.MAX_ROWS) {
				leftOut++;
				continue;
			}
			itemCount++;
			for (let row of itemRows) rows.push({ itemKey, ...row });
			parts.push(new Int8Array(r.getResultByName("vectors")));
		}
		let vectors = new Int8Array(rows.length * this.DIM);
		let p = 0;
		for (let v of parts) {
			vectors.set(v, p);
			p += v.length;
		}
		let S = ZoteroAI.Semantic;
		let manifest = {
			format: "zotero-ai-index",
			version: 1,
			model: { id: S.MODEL_ID, revision: S.REVISION, sha256: Object.fromEntries(S.FILES.map((f) => [f.path, f.sha256])) },
			dim: this.DIM,
			count: rows.length,
			items: itemCount,
			builtAt: new Date().toISOString().replace(/\.\d+Z$/, "Z"),
			libraryVersion: Zotero.Libraries.userLibrary.libraryVersion,
			builder: { host: "desktop", version: ZoteroAI.version, id: this.builderId() },
		};
		await IOUtils.makeDirectory(PathUtils.parent(path), { createAncestors: true, ignoreExisting: true });
		await S.call("writeIndex", { path, manifest, rows, vectors }, { transfer: [vectors.buffer] });
		return { manifest, leftOut };
	},

	/**
	 * Writes the index and puts it in the library: a new standalone attachment the first time,
	 * after that the same attachment with its file replaced (marked for upload so it syncs). Clears
	 * the dirty mark it started from, unless something changed meanwhile. An empty index (which
	 * readers refuse) isn't published.
	 */
	async _writeAttachment(attachment) {
		let S = ZoteroAI.Semantic;
		let dir = PathUtils.join(Zotero.DataDirectory.dir, "zotero-ai", "index-tmp");
		let tmp = PathUtils.join(dir, S.INDEX_FILENAME);
		// Read before the rows, so a change stored after them leaves the index dirty.
		let dirty = await this._getMeta("dirty");
		let { manifest, leftOut } = await this.writeIndex(tmp);
		if (!manifest.count) {
			await IOUtils.remove(dir, { recursive: true, ignoreAbsent: true });
			return attachment;
		}
		let note = `<p>Built by Zotero AI on ${manifest.builtAt}: ${manifest.items} items, ${manifest.count} passages. Used by semantic search; don't edit.</p>`;
		try {
			if (!attachment) {
				attachment = await Zotero.Attachments.importFromFile({
					file: tmp,
					libraryID: Zotero.Libraries.userLibraryID,
					contentType: "application/zip",
					title: S.INDEX_TITLE,
				});
				attachment.addTag(S.INDEX_TAG, 0);
				attachment.setNote(note);
				await attachment.saveTx();
			}
			else {
				let path = await attachment.getFilePathAsync();
				if (!path) throw new Error("The index attachment's file is missing.");
				await IOUtils.copy(tmp, path);
				attachment.setNote(note);
				await attachment.saveTx();
				await Zotero.DB.executeTransaction(async () => {
					await Zotero.Sync.Storage.Local.updateSyncStates([attachment], "to_upload");
				});
			}
		}
		finally {
			await IOUtils.remove(dir, { recursive: true, ignoreAbsent: true });
		}
		await this._db.execute("UPDATE meta SET value = '0' WHERE key = 'dirty' AND value = ?", [dirty]);
		await this._setMeta("writtenAt", String(Date.now()));
		await this._setMeta("leftOut", String(leftOut));
		this._set({ leftOut });
		S._dropIndex();
		return attachment;
	},

	/** The test hook's build: those items (null = all) into `out`, no attachment. */
	async buildTo(out, itemKeys) {
		let S = ZoteroAI.Semantic;
		if (!S.isModelReady()) throw new Error("The model isn't downloaded.");
		let items = await this._items();
		if (itemKeys) {
			let wanted = new Set(itemKeys);
			items = items.filter((i) => wanted.has(i.key));
		}
		// Not alongside a background run (it would share the stop flag).
		if (this._run) {
			this._stop = true;
			await this._run;
		}
		this._stop = false;
		try {
			await this._update(items, () => {}, { keepOthers: true });
			return (await this.writeIndex(out, items)).manifest;
		}
		finally {
			this.schedule(this.CHANGE_DELAY_MS);
		}
	},

	// endregion

	// region Storage

	async _openDB() {
		if (this._db) return this._db;
		if (!this._dbOpening) {
			this._dbOpening = (async () => {
				let { Sqlite } = ChromeUtils.importESModule("resource://gre/modules/Sqlite.sys.mjs");
				let dir = PathUtils.join(Zotero.DataDirectory.dir, "zotero-ai");
				await IOUtils.makeDirectory(dir, { createAncestors: true, ignoreExisting: true });
				let db = await Sqlite.openConnection({ path: PathUtils.join(dir, "semantic.sqlite") });
				await db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)");
				await db.execute("CREATE TABLE IF NOT EXISTS items (itemKey TEXT PRIMARY KEY, signature TEXT NOT NULL, rows TEXT NOT NULL, vectors BLOB NOT NULL)");
				this._db = db;
				// Rows from another model (or chunking) are useless: start over.
				let model = `${ZoteroAI.Semantic.REVISION}/${this.DIM}/1`;
				if ((await this._getMeta("model")) != model) {
					await db.executeTransaction(async () => {
						await db.execute("DELETE FROM items");
						await this._setMeta("model", model);
						await this._markDirty();
					});
				}
				return db;
			})();
			this._dbOpening.catch(() => {
				this._dbOpening = null;
			});
		}
		return this._dbOpening;
	},

	async _getMeta(key) {
		let rows = await this._db.execute("SELECT value FROM meta WHERE key = ?", [key]);
		return rows.length ? rows[0].getResultByName("value") : null;
	},

	async _setMeta(key, value) {
		await this._db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", [key, value]);
	},

	/** Marks the index dirty with a new value, so a publication started earlier doesn't clear it. */
	async _markDirty() {
		await this._setMeta("dirty", ZoteroAI.randomToken(12));
	},

	_isDirty(value) {
		return value != null && value != "0";
	},

	// endregion
};
