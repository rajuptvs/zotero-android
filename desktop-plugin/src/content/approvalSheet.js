/*
 * The card where the user decides on changes requested by MCP clients (approvals.js). It shows in
 * the main window, independent of the chat panel, one request at a time, oldest first.
 *
 * It must never approve by accident: it doesn't take focus (the user may be typing elsewhere), and
 * Approve does nothing until the card has been visible in a focused window for a second and the
 * user has pointed at it or tabbed into it. Everything on it is text (textContent), since the
 * agent chooses the tag names, titles and arguments it shows.
 */

ZoteroAI.ApprovalSheet = {
	ARM_MS: 1000,
	MAX_ARGS_CHARS: 2000,
	_card: null, // {win, el, opId, armed, interacted, timer}
	_unsubscribe: null,

	startup() {
		this._unsubscribe = ZoteroAI.Approvals.onChange(() => this.render());
		this.render();
	},

	shutdown() {
		this._unsubscribe?.();
		this._remove();
	},

	/** A main window opened or closed: put the card in the current one. */
	windowsChanged() {
		if (this._card && (this._card.win.closed || !Zotero.getMainWindows().includes(this._card.win))) this._remove();
		this.render();
	},

	render() {
		let pending = ZoteroAI.Approvals.pending();
		if (!pending.length) {
			this._remove();
			return;
		}
		let win = Zotero.getMainWindow();
		if (!win) return;
		let op = pending[0];
		if (this._card && this._card.win === win && this._card.opId == op.id) {
			this._card.countEl.textContent = pending.length > 1 ? `1 of ${pending.length}` : "";
			return;
		}
		let isNew = !this._card;
		this._remove();
		this._show(win, op, pending.length);
		if (isNew) {
			try { win.getAttention(); }
			catch (e) {}
		}
	},

	_show(win, op, count) {
		let doc = win.document;
		let h = (tag, style, text) => {
			let el = doc.createElementNS("http://www.w3.org/1999/xhtml", tag);
			if (style) el.setAttribute("style", style);
			if (text != null) el.textContent = text;
			return el;
		};
		let args = JSON.parse(op.argsJSON);
		let items = Array.isArray(args.item_keys) ? args.item_keys.length : null;
		let argsText = JSON.stringify(args, null, 2);
		if (argsText.length > this.MAX_ARGS_CHARS) argsText = argsText.slice(0, this.MAX_ARGS_CHARS) + "\n…";

		let el = h("div", "position:fixed;right:16px;bottom:16px;z-index:10001;width:380px;max-width:calc(100vw - 32px);"
			+ "background:var(--material-background,#fff);color:var(--fill-primary,#000);border:2px solid var(--accent-orange,#e08a00);"
			+ "border-radius:10px;box-shadow:0 8px 28px rgba(0,0,0,.25);padding:12px 14px;font:message-box;font-size:13px;");
		el.setAttribute("role", "alertdialog");
		el.setAttribute("aria-label", "Allow this change?");
		let countEl = h("span", "float:right;opacity:.7;font-size:12px;", count > 1 ? `1 of ${count}` : "");
		let title = h("div", "font-weight:600;margin-bottom:6px;");
		title.append(countEl, h("span", null, `${op.clientName} wants to change your library`));
		let description = h("div", "white-space:pre-wrap;margin-bottom:6px;", op.description);
		let meta = h("div", "opacity:.75;font-size:12px;margin-bottom:6px;",
			`Tool: ${op.tool}` + (items != null ? ` · ${items} item${items == 1 ? "" : "s"}` : ""));
		let pre = h("pre", "max-height:160px;overflow:auto;background:var(--fill-quinary,#f2f2f2);padding:6px;border-radius:4px;"
			+ "font-size:11px;white-space:pre-wrap;word-break:break-word;margin:0 0 8px;", argsText);
		let hint = h("div", "font-size:12px;opacity:.75;min-height:1em;margin-bottom:6px;", "");
		let row = h("div", "display:flex;gap:8px;justify-content:flex-end;");
		let decline = h("button", "padding:4px 14px;border-radius:6px;border:1px solid var(--fill-quinary,#ccc);background:transparent;color:inherit;", "Decline");
		let approve = h("button", "padding:4px 14px;border-radius:6px;border:none;background:var(--accent-blue,#2f6fdf);color:#fff;", "Approve");
		decline.setAttribute("type", "button");
		approve.setAttribute("type", "button");
		row.append(decline, approve);
		el.append(title, description, meta, pre, hint, row);
		doc.documentElement.append(el);

		let card = { win, el, countEl, opId: op.id, armed: false, interacted: false, timer: null };
		let arm = () => {
			clearTimeout(card.timer);
			card.armed = false;
			if (doc.hasFocus()) card.timer = setTimeout(() => { card.armed = true; }, this.ARM_MS);
		};
		let disarm = () => {
			clearTimeout(card.timer);
			card.armed = false;
		};
		let interact = () => { card.interacted = true; };
		el.addEventListener("pointermove", interact);
		el.addEventListener("pointerdown", interact);
		el.addEventListener("focusin", interact);
		win.addEventListener("focus", arm);
		win.addEventListener("blur", disarm);
		card.cleanup = () => {
			win.removeEventListener("focus", arm);
			win.removeEventListener("blur", disarm);
			clearTimeout(card.timer);
		};
		approve.addEventListener("click", () => {
			if (!card.armed || !card.interacted) {
				hint.textContent = "Take a second to read the change, then approve.";
				return;
			}
			ZoteroAI.Approvals.decide(op.id, true);
		});
		decline.addEventListener("click", () => ZoteroAI.Approvals.decide(op.id, false));
		this._card = card;
		arm();
	},

	_remove() {
		if (!this._card) return;
		this._card.cleanup?.();
		this._card.el.remove();
		this._card = null;
	},
};
