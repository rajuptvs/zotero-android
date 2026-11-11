/*
 * "Semantic search": turns it on or off on this computer and shows the model download, the
 * library's index and the background build (semantic.js, semanticBuilder.js).
 */

ZoteroAI.SemanticDialog = {
	_open: null, // {win, overlay, onKey, unlisten, status}

	open(win) {
		this.close();
		let doc = win.document;
		let overlay = doc.createElementNS("http://www.w3.org/1999/xhtml", "div");
		overlay.setAttribute("style", "position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,.35);display:flex;align-items:center;justify-content:center;");
		overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) this.close(); });
		let onKey = (e) => { if (e.key == "Escape") this.close(); };
		doc.addEventListener("keydown", onKey, true);
		doc.documentElement.append(overlay);
		let unlisten = ZoteroAI.Semantic.onChange(() => this.refresh());
		this._open = { win, overlay, onKey, unlisten, status: null };
		this.refresh();
	},

	close() {
		if (!this._open) return;
		this._open.unlisten();
		this._open.win.document.removeEventListener("keydown", this._open.onKey, true);
		this._open.overlay.remove();
		this._open = null;
	},

	/** Re-reads the status (at most one read at a time) and redraws. */
	async refresh() {
		let state = this._open;
		if (!state) return;
		if (state.reading) {
			state.again = true;
			return;
		}
		state.reading = true;
		try {
			do {
				state.again = false;
				state.status = await ZoteroAI.Semantic.status();
				if (this._open !== state) return;
				this.render();
			} while (state.again);
		}
		catch (e) {
			Zotero.logError(e);
		}
		finally {
			state.reading = false;
		}
	},

	render() {
		let state = this._open;
		if (!state?.status) return;
		let s = state.status;
		let doc = state.win.document;
		let h = (tag, style, text) => {
			let el = doc.createElementNS("http://www.w3.org/1999/xhtml", tag);
			if (style) el.setAttribute("style", style);
			if (text != null) el.textContent = text;
			return el;
		};
		let button = (label, onClick, primary) => {
			let b = h("button", primary
				? "padding:4px 12px;border-radius:6px;border:none;background:var(--accent-blue,#2f6fdf);color:#fff;"
				: "padding:4px 12px;border-radius:6px;border:1px solid var(--fill-quinary,#ccc);background:transparent;color:inherit;", label);
			b.setAttribute("type", "button");
			b.addEventListener("click", onClick);
			return b;
		};
		let row = (label, value) => {
			let r = h("div", "display:flex;gap:8px;margin:4px 0;");
			r.append(h("div", "width:70px;opacity:.7;flex:none;", label), h("div", "flex:1;", value));
			return r;
		};
		let mb = (bytes) => Math.round(bytes / 1048576);
		let when = (iso) => (iso ? new Date(iso).toLocaleString() : "");

		let card = h("div", "width:480px;max-width:calc(100vw - 32px);max-height:calc(100vh - 64px);overflow:auto;"
			+ "background:var(--material-background,#fff);color:var(--fill-primary,#000);border-radius:10px;padding:16px 18px;"
			+ "box-shadow:0 12px 40px rgba(0,0,0,.3);font:message-box;font-size:13px;");
		card.setAttribute("role", "dialog");
		card.setAttribute("aria-label", "Semantic search");
		let header = h("div", "display:flex;align-items:center;margin-bottom:8px;");
		header.append(h("div", "font-size:16px;font-weight:600;flex:1;", "Semantic search"), button("Close", () => this.close()));
		card.append(header, h("p", "margin:0 0 10px;line-height:1.45;", "Lets the AI find papers by meaning, not just by words. "
			+ "Turning it on downloads a 346 MB model to this computer and builds a search index of your library in the background. "
			+ "The index syncs to your other devices as an attachment, so it counts against your Zotero storage quota "
			+ "(tens of MB for a typical library, replaced at most once a day)."));

		let toggleRow = h("label", "display:flex;align-items:center;gap:8px;margin:6px 0 10px;font-weight:600;");
		let toggle = h("input");
		toggle.setAttribute("type", "checkbox");
		toggle.checked = s.enabled;
		toggle.addEventListener("change", () => ZoteroAI.Semantic.setEnabled(toggle.checked));
		toggleRow.append(toggle, h("span", null, "Use semantic search on this computer"));
		card.append(toggleRow);

		let model = {
			missing: "Not downloaded",
			downloading: s.download ? `Downloading… ${mb(s.download.received)} of ${mb(s.download.total)} MB` : "Downloading…",
			ready: "Downloaded",
			error: "Download failed",
		}[s.model];
		card.append(row("Model", model));
		if (s.modelError) card.append(h("div", "margin:0 0 4px 78px;color:var(--accent-red,#c00);", s.modelError));

		let index = s.index ? `${s.index.items} items, ${s.index.chunks} passages, built ${when(s.index.builtAt)}`
			: s.indexError || "None yet";
		card.append(row("Index", index));

		let b = s.build || {};
		let build = {
			idle: b.finishedAt ? `Up to date (checked ${when(b.finishedAt)})` : "Not started",
			waiting: b.message,
			running: b.total ? `Indexing item ${Math.min(b.done + 1, b.total)} of ${b.total}…` : "Starting…",
			writing: "Saving the index…",
			stopped: "Stopped",
			other: b.message,
			error: `Failed: ${b.message}`,
		}[b.state] || "";
		card.append(row("Build", s.enabled ? build : "Off"));
		if (s.enabled && b.leftOut) {
			card.append(h("div", "margin:0 0 4px 78px;", `${b.leftOut} ${b.leftOut == 1 ? "item was" : "items were"} left out: `
				+ `an index holds at most ${ZoteroAI.SemanticBuilder.MAX_ROWS.toLocaleString("en-US")} passages.`));
		}

		let actions = h("div", "display:flex;justify-content:flex-end;gap:8px;margin-top:12px;");
		let canBuild = s.enabled && s.model == "ready" && !["running", "writing", "other"].includes(b.state);
		let buildNow = button("Build now", () => {
			ZoteroAI.SemanticBuilder.buildNow();
			this.refresh();
		}, true);
		buildNow.disabled = !canBuild;
		if (!canBuild) buildNow.style.opacity = ".5";
		actions.append(buildNow);
		card.append(actions);
		state.overlay.replaceChildren(card);
	},
};
