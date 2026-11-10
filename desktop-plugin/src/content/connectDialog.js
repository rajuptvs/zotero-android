/*
 * "Connect an agent": turns the MCP server on and off and manages the agents allowed to use it
 * (mcpClients.js). A token is shown once, with the commands that add it to Claude Code and Codex.
 * Works whether or not the user is signed in to ChatGPT.
 */

ZoteroAI.ConnectDialog = {
	_open: null, // {win, overlay}

	open(win) {
		this.close();
		let doc = win.document;
		let overlay = doc.createElementNS("http://www.w3.org/1999/xhtml", "div");
		overlay.setAttribute("style", "position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,.35);display:flex;align-items:center;justify-content:center;");
		overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) this.close(); });
		let onKey = (e) => { if (e.key == "Escape") this.close(); };
		doc.addEventListener("keydown", onKey, true);
		doc.documentElement.append(overlay);
		this._open = { win, overlay, onKey, created: null };
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
		let copyable = (text) => {
			let row = h("div", "display:flex;gap:6px;align-items:flex-start;margin:4px 0;");
			let code = h("code", "flex:1;font-size:11px;background:var(--fill-quinary,#f2f2f2);padding:5px;border-radius:4px;word-break:break-all;user-select:text;", text);
			let copy = button("Copy", () => {
				Zotero.Utilities.Internal.copyTextToClipboard(text);
				copy.textContent = "Copied";
			});
			row.append(code, copy);
			return row;
		};

		let card = h("div", "width:560px;max-width:calc(100vw - 32px);max-height:calc(100vh - 64px);overflow:auto;"
			+ "background:var(--material-background,#fff);color:var(--fill-primary,#000);border-radius:10px;padding:16px 18px;"
			+ "box-shadow:0 12px 40px rgba(0,0,0,.3);font:message-box;font-size:13px;");
		card.setAttribute("role", "dialog");
		card.setAttribute("aria-label", "Connect an agent");
		let header = h("div", "display:flex;align-items:center;margin-bottom:8px;");
		header.append(h("div", "font-size:16px;font-weight:600;flex:1;", "Connect an agent"), button("Close", () => this.close()));
		card.append(header,
			h("p", "margin:0 0 10px;line-height:1.45;", "Let AI agents such as Claude Code or Codex use your library through MCP while Zotero is running. "
				+ "They can search and read; changes they ask for wait for your approval here, and moving items to the trash always asks."));

		let server = ZoteroAI.McpServer;
		if (!server.isSupported()) {
			card.append(h("p", "color:var(--accent-red,#c00);", "This needs Zotero 10 or later."));
			state.overlay.replaceChildren(card);
			return;
		}
		let enabledRow = h("label", "display:flex;align-items:center;gap:8px;margin:6px 0 10px;font-weight:600;");
		let toggle = h("input");
		toggle.setAttribute("type", "checkbox");
		toggle.checked = ZoteroAI.pref("mcp.enabled") === true;
		toggle.addEventListener("change", () => {
			server.setEnabled(toggle.checked);
			this.render();
		});
		enabledRow.append(toggle, h("span", null, "Let agents use this library (MCP)"));
		card.append(enabledRow);

		let url = server.url();
		if (!url) {
			card.append(h("p", "color:var(--accent-red,#c00);", "Zotero's local server isn't running, so agents can't connect. "
				+ "It's controlled by the hidden pref extensions.zotero.httpServer.enabled (Settings → Advanced → Config Editor); restart Zotero after changing it."));
		}
		else if (toggle.checked) {
			card.append(h("div", "font-size:12px;opacity:.8;margin-bottom:8px;", `Address: ${url}`));
		}

		// The token for an agent that was just added, shown once.
		if (state.created && url) {
			let { client, token } = state.created;
			let box = h("div", "border:1px solid var(--accent-green,#2a8);border-radius:8px;padding:8px 10px;margin:8px 0;");
			box.append(h("div", "font-weight:600;margin-bottom:4px;", `Token for ${client.name} (shown once; copy it now)`), copyable(token),
				h("div", "margin-top:6px;", "Claude Code:"),
				copyable(`claude mcp add --transport http zotero ${url} --header "Authorization: Bearer ${token}"`),
				h("div", "margin-top:6px;", "Codex:"),
				copyable(`export ZOTERO_AI_MCP_TOKEN=${token}`),
				copyable(`codex mcp add zotero --url ${url} --bearer-token-env-var ZOTERO_AI_MCP_TOKEN`),
				h("div", "font-size:12px;opacity:.75;margin-top:6px;", "Other clients: send the token as \"Authorization: Bearer …\". "
					+ "Clients that only speak stdio can use npx mcp-remote with --header."),
				button("Done", () => { state.created = null; this.render(); }));
			card.append(box);
		}

		card.append(h("div", "font-weight:600;margin:10px 0 4px;", "Agents"));
		let clients = ZoteroAI.McpClients.list();
		if (!clients.length) card.append(h("div", "opacity:.7;", "None yet."));
		for (let client of clients) {
			let row = h("div", "display:flex;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid var(--fill-quinary,#eee);");
			row.append(h("div", "flex:1;", client.name),
				h("div", "font-size:12px;opacity:.8;", client.scope == "write" ? "Read and request changes" : "Read only"));
			if (client.scope == "write") {
				let auto = h("label", "display:flex;align-items:center;gap:4px;font-size:12px;");
				let box = h("input");
				box.setAttribute("type", "checkbox");
				box.checked = client.autoApprove;
				box.addEventListener("change", () => ZoteroAI.McpClients.setAutoApprove(client.id, box.checked));
				auto.append(box, h("span", null, "Approve changes automatically (except trash)"));
				row.append(auto);
			}
			row.append(button("Revoke", () => {
				ZoteroAI.McpClients.revoke(client.id);
				this.render();
			}));
			card.append(row);
		}

		let add = h("div", "display:flex;gap:6px;align-items:center;margin-top:10px;flex-wrap:wrap;");
		let name = h("input", "flex:1;min-width:140px;padding:4px;");
		name.setAttribute("placeholder", "Agent name, e.g. Claude Code");
		let scope = h("select", "padding:3px;");
		for (let [value, label] of [["write", "Read and request changes"], ["read", "Read only"]]) {
			let o = h("option", null, label);
			o.setAttribute("value", value);
			scope.append(o);
		}
		add.append(name, scope, button("Add agent", async () => {
			state.created = await ZoteroAI.McpClients.create({ name: name.value, scope: scope.value });
			if (ZoteroAI.pref("mcp.enabled") !== true) server.setEnabled(true);
			this.render();
		}, true));
		card.append(add,
			h("div", "font-size:12px;opacity:.7;margin-top:10px;line-height:1.4;", "Revoking an agent stops its token at once and declines anything it's waiting on. "
				+ "Approvals protect against an agent's mistakes or instructions hidden in documents; they don't protect against programs on this computer that run as you."));
		state.overlay.replaceChildren(card);
	},
};
