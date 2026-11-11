/*
 * "MCP servers": the user's own MCP servers whose tools the chat can use (mcpRemote.js,
 * ai/spec/mcp-client.md). Kept on this computer; header values go to the login manager.
 */

ZoteroAI.McpServersDialog = {
	_open: null, // {win, overlay, onKey, message}

	open(win) {
		this.close();
		let doc = win.document;
		let overlay = doc.createElementNS("http://www.w3.org/1999/xhtml", "div");
		overlay.setAttribute("style", "position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,.35);display:flex;align-items:center;justify-content:center;");
		overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) this.close(); });
		let onKey = (e) => { if (e.key == "Escape") this.close(); };
		doc.addEventListener("keydown", onKey, true);
		doc.documentElement.append(overlay);
		this._open = { win, overlay, onKey, message: null };
		this.render();
	},

	close() {
		if (!this._open) return;
		this._open.win.document.removeEventListener("keydown", this._open.onKey, true);
		this._open.overlay.remove();
		this._open = null;
	},

	render() {
		let state = this._open;
		if (!state) return;
		let doc = state.win.document;
		let R = ZoteroAI.McpRemote;
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
		let checkbox = (checked, label, onChange) => {
			let row = h("label", "display:flex;align-items:center;gap:4px;font-size:12px;");
			let box = h("input");
			box.setAttribute("type", "checkbox");
			box.checked = checked;
			box.addEventListener("change", () => onChange(box.checked));
			row.append(box, h("span", null, label));
			return row;
		};

		let card = h("div", "width:560px;max-width:calc(100vw - 32px);max-height:calc(100vh - 64px);overflow:auto;"
			+ "background:var(--material-background,#fff);color:var(--fill-primary,#000);border-radius:10px;padding:16px 18px;"
			+ "box-shadow:0 12px 40px rgba(0,0,0,.3);font:message-box;font-size:13px;");
		card.setAttribute("role", "dialog");
		card.setAttribute("aria-label", "MCP servers");
		let header = h("div", "display:flex;align-items:center;margin-bottom:8px;");
		header.append(h("div", "font-size:16px;font-weight:600;flex:1;", "MCP servers"), button("Close", () => this.close()));
		card.append(header, h("p", "margin:0 0 10px;line-height:1.45;", "The chat can use tools from your own MCP servers (https, or http on this computer). "
			+ "Every call asks first unless you allow read-only tools for a server."));

		let servers = R.servers();
		if (!servers.length) card.append(h("div", "opacity:.7;margin-bottom:8px;", "None yet."));
		for (let server of servers) {
			let row = h("div", "padding:6px 0;border-bottom:1px solid var(--fill-quinary,#eee);");
			let top = h("div", "display:flex;align-items:center;gap:8px;");
			let names = h("div", "flex:1;min-width:0;");
			names.append(h("div", "font-weight:600;", server.name), h("div", "font-size:11px;opacity:.75;word-break:break-all;", server.url));
			top.append(names,
				checkbox(server.enabled, "On", async (on) => { await R.save({ ...server, enabled: on }); this.render(); }),
				button("Test", async () => {
					state.message = `Connecting to ${server.name}…`;
					this.render();
					let turn = await R.toolsForTurn();
					let count = [...turn.index.values()].filter((e) => e.server.id == server.id).length;
					state.message = !server.enabled ? `${server.name} is turned off.` : count ? `${server.name}: ${count} tools` : `${server.name} isn't available.`;
					this.render();
				}),
				button("Remove", async () => { await R.remove(server.id); this.render(); }));
			row.append(top, checkbox(server.autoReadOnly, "Run read-only tools without asking", async (on) => {
				await R.save({ ...server, autoReadOnly: on });
				this.render();
			}));
			card.append(row);
		}
		if (state.message) card.append(h("div", "margin:8px 0;color:var(--accent-blue,#2f6fdf);", state.message));

		card.append(h("div", "font-weight:600;margin:12px 0 4px;", "Add a server"));
		let name = h("input", "width:100%;box-sizing:border-box;padding:4px;margin:2px 0;");
		name.setAttribute("placeholder", "Name");
		let url = h("input", "width:100%;box-sizing:border-box;padding:4px;margin:2px 0;");
		url.setAttribute("placeholder", "https://…/mcp");
		let auth = h("input", "width:100%;box-sizing:border-box;padding:4px;margin:2px 0;");
		auth.setAttribute("type", "password");
		auth.setAttribute("placeholder", "Authorization header (optional), e.g. Bearer …");
		card.append(name, url, auth, button("Add", async () => {
			try {
				await R.save({ name: name.value, url: url.value.trim() }, auth.value.trim() || null);
				state.message = null;
			}
			catch (e) {
				state.message = e.message;
			}
			this.render();
		}, true));
		state.overlay.replaceChildren(card);
	},
};
