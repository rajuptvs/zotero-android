/*
 * Semantic search (ai/spec/semantic.md): the pinned model's download, the worker that runs it
 * (semanticWorker.mjs), the library's index attachment, and zotero_semantic_search. The index is
 * built by semanticBuilder.js. Everything stays off until the user turns it on for this computer.
 */

ZoteroAI.Semantic = {
	MODEL_ID: "onnx-community/embeddinggemma-2-ONNX",
	REVISION: "daa72c51243991dfcaf9f9137d2c573d8f7790c0",
	FILES: [
		{ path: "onnx/model_quantized.onnx", size: 495165, sha256: "d06edd601f851c633a2519304cbeb8dc6170d7ceb61b436625c17fb9b6e74953" },
		{ path: "onnx/model_quantized.onnx_data", size: 313724928, sha256: "278a7ff1248c3618e4bd11a607fc54f7bdc7778854230f3956d3f86bd9db4f3b" },
		{ path: "tokenizer.json", size: 32170510, sha256: "4d777ef5bdc1aa36227abdfb77c3e49e7b9c892d16e1b6bda41c393504828be4" },
	],
	INDEX_TAG: "_zotero-ai-index",
	INDEX_TITLE: "Zotero AI search index",
	INDEX_FILENAME: "zotero-ai-index.zip",
	QUERY_PREFIX: "task: search result | query: ",
	TOP_ROWS: 200,
	MAX_PASSAGES: 2,
	PASSAGE_CHARS: 800,
	// The worker (and the model in it) goes after this long without work; the index after INDEX_IDLE_MS.
	WORKER_IDLE_MS: 3 * 60 * 1000,
	INDEX_IDLE_MS: 30 * 60 * 1000,
	QUERY_TIMEOUT_MS: 60 * 1000,
	DOWNLOAD_TIMEOUT_MS: 60 * 1000,
	RESOURCE_HOST: "zotero-ai",

	MESSAGES: {
		empty: "query is empty.",
		noIndex: "There's no search index yet. Turn on semantic search in the Zotero desktop plugin to build one.",
		off: "Semantic search is off on this device. Turn it on in the Zotero AI settings.",
		otherModel: "The search index was built with a different model; rebuild it.",
		damaged: "The search index is damaged or too large; rebuild it.",
	},

	// Python's str.isspace(), which the reference normalisation (\s+ in Python) uses.
	WHITESPACE: /[\t\n\v\f\r\x1c-\x1f \x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+/g,

	_model: { state: "missing", error: null, received: 0, total: 0 },
	// Model files that matched their sha256 in this Zotero session: path → "size:mtime" then.
	// The worker trusts a file still at that size and time, so a restart doesn't hash 346 MB again.
	_verified: new Map(),
	_download: null, // {promise, abort}
	_listeners: new Set(),
	_override: null,
	_index: null, // {signature, manifest, itemKeys, sources, keys, texts, vectors, norms, dim}
	_indexTimer: null,
	_indexLoading: null,
	_lastIndexInfo: null,

	async startup() {
		let res = Services.io.getProtocolHandler("resource").QueryInterface(Ci.nsIResProtocolHandler);
		res.setSubstitution(this.RESOURCE_HOST, Services.io.newURI(ZoteroAI.rootURI + "content/"));
		await this._checkModel();
		if (this.isEnabled() && this._model.state != "ready") this._startDownload();
		ZoteroAI.SemanticBuilder.startup();
	},

	shutdown() {
		ZoteroAI.SemanticBuilder.shutdown();
		this._download?.abort.abort();
		this._terminateWorker();
		this._dropIndex();
		clearTimeout(this._indexTimer);
		this._listeners.clear();
		let res = Services.io.getProtocolHandler("resource").QueryInterface(Ci.nsIResProtocolHandler);
		res.setSubstitution(this.RESOURCE_HOST, null);
	},

	normalize(text) {
		// Not trim(): it also drops U+FEFF, which isn't whitespace to Python.
		return String(text ?? "").normalize("NFC").replace(this.WHITESPACE, " ").replace(/^ | $/g, "");
	},

	isEnabled() {
		return ZoteroAI.pref("semantic.enabled") === true;
	},

	/** Turns semantic search on or off on this computer. On: downloads the model, then builds. */
	async setEnabled(on) {
		ZoteroAI.pref("semantic.enabled", !!on);
		if (on) {
			if (this._model.state != "ready") this._startDownload();
			ZoteroAI.SemanticBuilder.schedule(0);
		}
		else {
			this._download?.abort.abort();
			ZoteroAI.SemanticBuilder.stop();
			this._terminateWorker();
		}
		this._changed();
	},

	onChange(fn) {
		this._listeners.add(fn);
		return () => this._listeners.delete(fn);
	},

	_changed() {
		for (let fn of this._listeners) {
			try {
				fn();
			}
			catch (e) {
				Zotero.logError(e);
			}
		}
	},

	/** The status the test hook and the dialog show (the hook contract in the semantic brief). */
	async status() {
		let index = null, indexError = null;
		try {
			let source = await this._indexSource();
			if (source) {
				let loaded = await this._loadIndex(source);
				index = { builtAt: loaded.manifest.builtAt ?? null, items: loaded.manifest.items ?? null,
					chunks: loaded.manifest.count, source: source.override ? "override" : "attachment" };
			}
		}
		catch (e) {
			indexError = e.message;
		}
		this._lastIndexInfo = { index, indexError };
		return {
			enabled: this.isEnabled(),
			model: this._model.state,
			modelError: this._model.error,
			download: this._model.state == "downloading" ? { received: this._model.received, total: this._model.total } : null,
			index,
			indexError,
			build: ZoteroAI.SemanticBuilder.status(),
		};
	},

	// region Model

	modelDir() {
		return PathUtils.join(Zotero.DataDirectory.dir, "zotero-ai", "models", this.REVISION);
	},

	_modelPath(file) {
		return PathUtils.join(this.modelDir(), ...file.path.split("/"));
	},

	isModelReady() {
		return this._model.state == "ready";
	},

	/** Ready when every file is there at its size; the worker checks the sha256 when it loads them. */
	async _checkModel() {
		for (let file of this.FILES) {
			let info = await IOUtils.stat(this._modelPath(file)).catch(() => null);
			if (!info || info.size != file.size) {
				if (this._model.state == "ready") this._setModel({ state: "missing" });
				return false;
			}
		}
		this._setModel({ state: "ready", error: null });
		return true;
	},

	_setModel(changes) {
		Object.assign(this._model, changes);
		this._changed();
	},

	_startDownload() {
		if (this._download) return this._download.promise;
		let abort = new AbortController();
		let promise = (async () => {
			try {
				let total = this.FILES.reduce((n, f) => n + f.size, 0);
				this._setModel({ state: "downloading", error: null, received: 0, total });
				let done = 0;
				for (let file of this.FILES) {
					await this._downloadFile(file, abort.signal, (bytes) => {
						this._model.received = done + bytes;
						this._throttledChange();
					});
					done += file.size;
				}
				await this._checkModel();
				ZoteroAI.SemanticBuilder.schedule(0);
			}
			catch (e) {
				if (abort.signal.aborted) this._setModel({ state: "missing", error: null });
				else {
					Zotero.logError(e);
					this._setModel({ state: "error", error: `Couldn't download the model: ${e.message}` });
				}
			}
			finally {
				this._download = null;
			}
		})();
		this._download = { promise, abort };
		return promise;
	},

	_throttledChange() {
		if (this._changeTimer) return;
		this._changeTimer = setTimeout(() => {
			this._changeTimer = null;
			this._changed();
		}, 500);
	},

	/**
	 * One model file from Hugging Face into <name>.part (resumed with a Range request when part of it
	 * is there), then checked against its sha256 and moved into place; a mismatch is deleted.
	 */
	async _downloadFile(file, signal, progress) {
		let path = this._modelPath(file);
		let info = await IOUtils.stat(path).catch(() => null);
		if (info?.size == file.size) {
			progress(file.size);
			return;
		}
		if (info) await IOUtils.remove(path);
		await IOUtils.makeDirectory(PathUtils.parent(path), { createAncestors: true, ignoreExisting: true });
		let part = path + ".part";
		let have = (await IOUtils.stat(part).catch(() => null))?.size || 0;
		if (have > file.size) {
			await IOUtils.remove(part);
			have = 0;
		}
		if (have < file.size) {
			let url = `https://huggingface.co/${this.MODEL_ID}/resolve/${this.REVISION}/${file.path}`;
			let response = await fetch(url, { headers: have ? { Range: `bytes=${have}-` } : {}, signal });
			if (have && response.status == 200) {
				// The server ignored the range: start over.
				await IOUtils.remove(part);
				have = 0;
			}
			else if (!response.ok) throw new Error(`HTTP ${response.status} for ${file.path}`);
			let reader = response.body.getReader();
			let buffered = [], bufferedBytes = 0;
			let flush = async () => {
				if (!bufferedBytes) return;
				let chunk = new Uint8Array(bufferedBytes);
				let p = 0;
				for (let b of buffered) {
					chunk.set(b, p);
					p += b.length;
				}
				buffered = [];
				bufferedBytes = 0;
				await IOUtils.write(part, chunk, { mode: "appendOrCreate" });
			};
			progress(have);
			for (;;) {
				let { done, value } = await reader.read();
				if (done) break;
				buffered.push(value);
				bufferedBytes += value.length;
				have += value.length;
				if (have > file.size) throw new Error(`${file.path} is larger than expected.`);
				if (bufferedBytes >= 4 * 1024 * 1024) await flush();
				progress(have);
			}
			await flush();
		}
		let sha256 = await this.call("hashFile", { path: part });
		if (have != file.size || sha256.hex != file.sha256) {
			await IOUtils.remove(part);
			throw new Error(`${file.path} didn't match its checksum.`);
		}
		await IOUtils.move(part, path);
		let moved = await IOUtils.stat(path);
		this._verified.set(file.path, `${moved.size}:${moved.lastModified}`);
		progress(file.size);
	},

	/** A model file failed its check in the worker: delete it, and download again if still on. */
	async _modelFileBad(path) {
		let file = this.FILES.find((f) => f.path == path);
		this._verified.delete(path);
		if (file) await IOUtils.remove(this._modelPath(file), { ignoreAbsent: true }).catch((e) => Zotero.logError(e));
		this._terminateWorker();
		this._setModel({ state: "missing", error: `${path} was damaged and has been deleted.` });
		if (this.isEnabled()) this._startDownload();
	},

	// endregion

	// region Worker

	_worker: null,
	_jobs: [],
	_urgent: [],
	_current: null,
	_nextId: 1,
	_idleTimer: null,

	/**
	 * Runs an op in the worker. One at a time; `urgent` ops (queries) go before queued build work.
	 * Rejects with an Error whose `code` is the worker's.
	 */
	call(op, data = {}, { urgent = false, transfer = [] } = {}) {
		return new Promise((resolve, reject) => {
			(urgent ? this._urgent : this._jobs).push({ op, data, transfer, resolve, reject });
			this._pump();
		});
	},

	_pump() {
		if (this._current) return;
		let job = this._urgent.shift() || this._jobs.shift();
		clearTimeout(this._idleTimer);
		if (!job) {
			this._idleTimer = setTimeout(() => this._terminateWorker(), this.WORKER_IDLE_MS);
			return;
		}
		let id = this._nextId++;
		this._current = { id, job };
		try {
			this._ensureWorker().postMessage({ id, op: job.op, ...job.data }, job.transfer);
		}
		catch (e) {
			this._current = null;
			job.reject(e);
			this._pump();
		}
	},

	_ensureWorker() {
		if (this._worker) return this._worker;
		let worker = new ChromeWorker(`resource://${this.RESOURCE_HOST}/semanticWorker.mjs`, { type: "module" });
		worker.onmessage = (event) => {
			let m = event.data;
			if (m.verified) {
				this._verified.set(m.verified.file, m.verified.stamp);
				return;
			}
			if (!this._current || m.id != this._current.id) return;
			let { job } = this._current;
			this._current = null;
			if (m.error) {
				let error = new Error(m.error.message);
				error.code = m.error.code;
				error.detail = m.error.detail;
				if (m.error.code == "model_hash") this._modelFileBad(m.error.file);
				else if (m.error.code == "model_missing") this._checkModel().then(() => this.isEnabled() && !this.isModelReady() && this._startDownload());
				job.reject(error);
			}
			else job.resolve(m.result);
			this._pump();
		};
		worker.onerror = (event) => {
			Zotero.logError(`Zotero AI: semantic worker: ${event.message}`);
			event.preventDefault?.();
			let current = this._current;
			this._current = null;
			this._terminateWorker();
			current?.job.reject(new Error(event.message || "The semantic search worker stopped."));
			this._pump();
		};
		worker.postMessage({ id: 0, op: "configure", dir: this.modelDir(),
			sha256: Object.fromEntries(this.FILES.map((f) => [f.path, f.sha256])), verified: Object.fromEntries(this._verified) });
		this._worker = worker;
		return worker;
	},

	_terminateWorker() {
		clearTimeout(this._idleTimer);
		if (!this._worker) return;
		this._worker.terminate();
		this._worker = null;
		let current = this._current;
		this._current = null;
		current?.job.reject(new Error("The semantic search worker stopped."));
		this._pump();
	},

	// endregion

	// region Index

	/** The library's index: the most recently modified non-trashed standalone attachment with the tag. */
	async indexAttachment() {
		let search = new Zotero.Search();
		search.libraryID = Zotero.Libraries.userLibraryID;
		search.addCondition("tag", "is", this.INDEX_TAG);
		search.addCondition("itemType", "is", "attachment");
		let items = Zotero.Items.get(await search.search())
			.filter((i) => i.isAttachment() && !i.parentID && !i.deleted
				&& i.attachmentLinkMode == Zotero.Attachments.LINK_MODE_IMPORTED_FILE);
		items.sort((a, b) => (a.dateModified < b.dateModified ? 1 : a.dateModified > b.dateModified ? -1 : 0)
			|| (a.key < b.key ? -1 : a.key > b.key ? 1 : 0));
		return items[0] || null;
	},

	/** Where the index comes from: {override, path} or {item}; null when there is none. */
	async _indexSource() {
		if (this._override) return { override: true, path: this._override };
		let item = await this.indexAttachment();
		return item ? { item } : null;
	},

	useIndex(path) {
		this._override = path || null;
		this._dropIndex();
	},

	/** The index file on this computer, downloaded through Zotero's file sync if needed. */
	async _indexPath(source) {
		if (source.override) {
			if (!await IOUtils.exists(source.path)) throw new ZoteroAIToolError("unavailable", "The search index file doesn't exist.");
			return source.path;
		}
		let item = source.item;
		let path = await item.getFilePathAsync();
		if (path) return path;
		if (!Zotero.Sync.Storage.Local.getEnabledForLibrary(item.libraryID)) {
			throw new ZoteroAIToolError("unavailable", "The search index isn't on this computer, and file syncing is off.");
		}
		let timer;
		try {
			await Promise.race([
				Zotero.Sync.Runner.downloadFile(item),
				new Promise((resolve, reject) => {
					timer = setTimeout(() => reject(new Error("timeout")), this.DOWNLOAD_TIMEOUT_MS);
				}),
			]);
		}
		catch (e) {
			Zotero.logError(e);
		}
		finally {
			clearTimeout(timer);
		}
		path = await item.getFilePathAsync();
		if (!path) throw new ZoteroAIToolError("unavailable", "Couldn't download the search index.");
		return path;
	},

	/** The index, read once and kept until its file changes (or it goes unused for INDEX_IDLE_MS). */
	async _loadIndex(source) {
		let path = await this._indexPath(source);
		let info = await IOUtils.stat(path);
		let signature = `${source.item?.key || "override"}:${path}:${info.lastModified}:${info.size}`;
		if (this._index?.signature != signature) {
			if (this._indexLoading?.signature != signature) {
				let promise = this._readIndex(path, signature);
				this._indexLoading = { signature, promise };
				promise.finally(() => {
					if (this._indexLoading?.promise === promise) this._indexLoading = null;
				}).catch(() => {});
			}
			this._index = await this._indexLoading.promise;
		}
		clearTimeout(this._indexTimer);
		this._indexTimer = setTimeout(() => this._dropIndex(), this.INDEX_IDLE_MS);
		return this._index;
	},

	async _readIndex(path, signature) {
		let data;
		try {
			data = await this.call("readIndex", { path, maxText: this.PASSAGE_CHARS }, { urgent: true });
		}
		catch (e) {
			if (e.code != "index") throw new ZoteroAIToolError("unavailable", "The search index can't be read.");
			Zotero.logError(`Zotero AI: search index ${path}: ${e.detail || e.message}`);
			throw new ZoteroAIToolError("unavailable", this.MESSAGES.damaged);
		}
		let m = data.manifest;
		if (m.format != "zotero-ai-index" || m.version != 1 || !this._sameModel(m.model)) {
			throw new ZoteroAIToolError("unavailable", this.MESSAGES.otherModel);
		}
		let { dim, count } = m;
		let norms = new Float64Array(count);
		for (let i = 0; i < count; i++) {
			let s = 0;
			for (let k = i * dim, end = k + dim; k < end; k++) s += data.vectors[k] * data.vectors[k];
			norms[i] = Math.sqrt(s);
		}
		return { signature, path, dim, count, ...data, norms };
	},

	_sameModel(model) {
		let sha = model?.sha256;
		return model?.revision == this.REVISION && sha && typeof sha == "object"
			&& Object.keys(sha).length == this.FILES.length && this.FILES.every((f) => sha[f.path] == f.sha256);
	},

	_dropIndex() {
		this._index = null;
		this._indexLoading = null;
	},

	/** Step 3: every row's score against a 768-d query embedding; → [{row, score}] best first. */
	rank(index, embedding, top) {
		let { dim, count, vectors, norms } = index;
		let q = new Float64Array(dim);
		let n = 0;
		for (let k = 0; k < dim; k++) {
			q[k] = embedding[k];
			n += q[k] * q[k];
		}
		n = Math.sqrt(n);
		for (let k = 0; k < dim; k++) q[k] /= n;
		let scores = new Float64Array(count);
		for (let i = 0; i < count; i++) {
			let s = 0;
			for (let k = 0, base = i * dim; k < dim; k++) s += vectors[base + k] * q[k];
			scores[i] = norms[i] ? s / norms[i] : 0;
		}
		let order = Array.from({ length: count }, (_, i) => i);
		order.sort((a, b) => scores[b] - scores[a] || a - b);
		return order.slice(0, top).map((row) => ({ row, score: scores[row] }));
	},

	/** The query embedding (768 floats) for already normalised text. */
	async embedQuery(query) {
		let timer;
		try {
			let result = await Promise.race([
				this.call("embed", { text: this.QUERY_PREFIX + query }, { urgent: true }),
				new Promise((resolve, reject) => {
					timer = setTimeout(() => reject(new Error("Embedding the query took too long.")), this.QUERY_TIMEOUT_MS);
				}),
			]);
			return result.embedding;
		}
		finally {
			clearTimeout(timer);
		}
	},

	/** zotero_semantic_search (semantic.md). */
	async search(args) {
		let query = this.normalize(typeof args.query == "string" ? args.query : "");
		if (!query) throw new ZoteroAIToolError("invalid_argument", this.MESSAGES.empty);
		let limit = ZoteroAI.Tools._limit(args, "limit", 10, 20);
		let source = await this._indexSource();
		if (!source) throw new ZoteroAIToolError("unavailable", this.MESSAGES.noIndex);
		if (!this.isEnabled() || !this.isModelReady()) throw new ZoteroAIToolError("unavailable", this.MESSAGES.off);
		let index = await this._loadIndex(source);
		let embedding;
		try {
			embedding = await this.embedQuery(query);
		}
		catch (e) {
			Zotero.logError(e);
			if (!this.isModelReady()) throw new ZoteroAIToolError("unavailable", this.MESSAGES.off);
			throw new ZoteroAIToolError("unavailable", `Semantic search failed: ${e.message}`);
		}
		let T = ZoteroAI.Tools;
		let items = [], byKey = new Map();
		for (let { row, score } of this.rank(index, embedding, this.TOP_ROWS)) {
			let key = index.itemKeys[row];
			let entry = byKey.get(key);
			if (entry === undefined) {
				// Full: later rows can only add passages to listed items.
				if (items.length >= limit) continue;
				let item = Zotero.Items.getByLibraryAndKey(T.libraryID, key);
				entry = item && T._isRegular(item)
					? { ...T._summary(item), score: Math.floor(score * 1000 + 0.5) / 1000, passages: [] }
					: null;
				byKey.set(key, entry);
				if (entry) items.push(entry);
			}
			if (entry && entry.passages.length < this.MAX_PASSAGES) {
				entry.passages.push({ source: index.sources[row], key: index.keys[row], text: index.texts[row].slice(0, this.PASSAGE_CHARS) });
			}
		}
		let m = index.manifest;
		return { index: { builtAt: m.builtAt ?? null, items: m.items ?? null, chunks: m.count }, items };
	},

	// endregion
};
