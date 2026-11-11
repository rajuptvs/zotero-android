/*
 * Dev-only endpoints on Zotero's local server. Not in the release .xpi; in a dev build they're
 * registered only when the pref extensions.zotero-ai.testEndpoints is true and Zotero was started
 * with ZOTERO_AI_TEST_TOKEN set (both done by scripts/dev-zotero.sh), and every request must send
 * that token in the X-Zotero-AI-Test-Token header. They let
 * ai/conformance/run.py call the same tools as on Android, and let tests drive the UI headlessly.
 *   POST /zotero-ai/test/tool        {"name", "args"}  → the tool's result (writes approved by a stand-in)
 *   POST /zotero-ai/test/policy      {}                → shouldAsk for every tool
 *   POST /zotero-ai/test/sync        {}                → runs a Zotero sync
 *   GET  /zotero-ai/test/status                        → plugin and account status
 *   POST /zotero-ai/test/eval        {"code"}          → runs async JS in the plugin scope (UI driving)
 *   POST /zotero-ai/test/screenshot  {}                → {"png": base64} of the main window
 *   POST /zotero-ai/test/signin      {}                → starts ChatGPT sign-in, returns the authorize URL
 *   POST /zotero-ai/test/describe    {"name", "args"}  → the approval card text for a write
 *   POST /zotero-ai/test/notes       {"html"}          → how a conversation note is read back
 *   POST /zotero-ai/test/markdown    {"markdown"}      → answer → note HTML
 *   POST /zotero-ai/test/semantic    {"action", ...}   → semantic search: status, enable, tokenize, embed,
 *                                                        rank, useIndex, build (ai/conformance)
 */

ZoteroAI.TestEndpoints = {
	PATHS: ["/zotero-ai/test/tool", "/zotero-ai/test/sync", "/zotero-ai/test/status",
		"/zotero-ai/test/eval", "/zotero-ai/test/screenshot", "/zotero-ai/test/signin",
		"/zotero-ai/test/describe", "/zotero-ai/test/notes", "/zotero-ai/test/markdown", "/zotero-ai/test/policy",
		"/zotero-ai/test/mcp-client", "/zotero-ai/test/approvals", "/zotero-ai/test/mcp-remote",
		"/zotero-ai/test/pdf-render", "/zotero-ai/test/semantic"],

	register() {
		let token = Services.env.get("ZOTERO_AI_TEST_TOKEN");
		if (!ZoteroAI.pref("testEndpoints") || !token || token.length < 32) return;
		let json = (status, body) => [status, "application/json", JSON.stringify(body)];
		let endpoint = (methods, handler) => {
			let E = function () {};
			E.prototype = {
				supportedMethods: methods,
				supportedDataTypes: ["application/json"],
				permitBookmarklet: false,
				async init(request) {
					if (request.headers?.["x-zotero-ai-test-token"] !== token) return json(403, { error: "forbidden" });
					try {
						return json(200, await handler(request.data || {}));
					}
					catch (e) {
						Zotero.logError(e);
						return json(500, { error: "failed", message: String(e && e.message || e) });
					}
				},
			};
			return E;
		};
		let E = Zotero.Server.Endpoints;
		// Writes go through the approval service with a test stand-in for the user's "Approve".
		E["/zotero-ai/test/tool"] = endpoint(["POST"], async (data) => {
			if (!ZoteroAI.Spec.isWrite(data.name)) return ZoteroAI.Tools.execute(data.name, data.args || {});
			let run = await ZoteroAI.Approvals.run({ origin: { kind: "test" }, name: data.name, args: data.args || {}, present: async () => true });
			return run.result;
		});
		// Renders a PDF sent by the conformance suite (same code as zotero_get_page_image).
		E["/zotero-ai/test/pdf-render"] = endpoint(["POST"], async (data) => {
			let bytes = Uint8Array.from(atob(data.pdf || ""), (c) => c.charCodeAt(0));
			try {
				let image = await ZoteroAI.Pdf.render(bytes, data.page, data.region ?? null, data.max_width ?? null);
				return { result: { pageCount: image.pageCount, width: image.width, height: image.height }, image: image.data };
			}
			catch (e) {
				if (e instanceof ZoteroAIToolError) return { result: { error: e.code, message: e.message } };
				throw e;
			}
		});
		// MCP test setup: turn the server on/off, add or revoke clients, set auto-approve.
		E["/zotero-ai/test/mcp-client"] = endpoint(["POST"], async (data) => {
			if ("enable" in data) ZoteroAI.McpServer.setEnabled(data.enable);
			if (data.revoke) ZoteroAI.McpClients.revoke(data.revoke);
			if (data.autoApprove) ZoteroAI.McpClients.setAutoApprove(data.autoApprove.id, data.autoApprove.on);
			let created = data.create ? await ZoteroAI.McpClients.create(data.create) : null;
			return { created, url: ZoteroAI.McpServer.url(), enabled: ZoteroAI.McpServer.isEnabled() };
		});
		// Plays the human for MCP approvals: list waiting requests, decide one, or shut down/restart.
		E["/zotero-ai/test/approvals"] = endpoint(["POST"], async (data) => {
			if (data.decide) return { decided: ZoteroAI.Approvals.decide(data.decide.id, data.decide.approved) };
			if (data.timing) Object.assign(ZoteroAI.Approvals.timing, data.timing);
			if (data.shutdown) {
				ZoteroAI.McpServer.shutdown();
				ZoteroAI.McpServer.startup();
				return { restarted: true };
			}
			return { pending: ZoteroAI.Approvals.pending().map((op) => ({ id: op.id, clientId: op.clientId, tool: op.tool, description: op.description })) };
		});
		// The chat's MCP client (ai/spec/mcp-client.md), compared with Android by run.py.
		E["/zotero-ai/test/mcp-remote"] = endpoint(["POST"], async (data) => {
			let R = ZoteroAI.McpRemote;
			switch (data.action) {
				case "set":
					for (let s of R.servers()) await R.remove(s.id);
					for (let s of data.servers || []) await R.save(s, s.authorization ?? null);
					return { servers: R.servers().length };
				case "tools": {
					let turn = await R.toolsForTurn();
					return { tools: turn.tools, unavailable: turn.unavailable,
						shouldAsk: Object.fromEntries([...turn.index].map(([name, entry]) => [name, R.shouldAsk(entry)])) };
				}
				case "describe":
				case "call": {
					let turn = await R.toolsForTurn();
					let entry = turn.index.get(data.name);
					if (!entry) return { error: "unknown tool" };
					return data.action == "describe" ? { description: R.describe(entry, data.args || {}) } : await R.call(entry, data.args || {});
				}
			}
			return { error: "unknown action" };
		});
		// Semantic search (ai/spec/semantic.md): golden checks and index builds for the conformance suite.
		E["/zotero-ai/test/semantic"] = endpoint(["POST"], async (data) => {
			let S = ZoteroAI.Semantic;
			try {
				switch (data.action) {
					case "status":
						return await S.status();
					case "enable":
						await S.setEnabled(!!data.on);
						return await S.status();
					case "tokenize":
						return { ids: (await S.call("tokenize", { text: String(data.text ?? "") }, { urgent: true })).ids };
					case "embed":
						return { embedding: Array.from((await S.call("embed", { text: String(data.text ?? "") }, { urgent: true })).embedding) };
					case "rank": {
						let index = await S._readIndex(data.indexPath, null);
						let embedding = (await S.call("embed", { text: String(data.text ?? "") }, { urgent: true })).embedding;
						return { rows: S.rank(index, embedding, data.top || 20).map(({ row, score }) => ({ row, itemKey: index.itemKeys[row], score })) };
					}
					case "useIndex":
						S.useIndex(data.path ?? null);
						return await S.status();
					case "build": {
						let manifest = await ZoteroAI.SemanticBuilder.buildTo(data.out, data.itemKeys ?? null);
						return { path: data.out, manifest };
					}
				}
				return { error: "invalid_argument", message: `Unknown action ${data.action}` };
			}
			catch (e) {
				return { error: e.code || "failed", message: String(e?.message || e) };
			}
		});
		// The approval rule for every tool, with auto-approve off and on (compared with Android).
		E["/zotero-ai/test/policy"] = endpoint(["POST"], async () => Object.fromEntries(ZoteroAI.Spec.tools.map((t) => [t.name, {
			ask: ZoteroAI.Spec.shouldAsk(t.name, false),
			askWithAutoApprove: ZoteroAI.Spec.shouldAsk(t.name, true),
		}])));
		E["/zotero-ai/test/sync"] = endpoint(["POST"], async () => {
			await Zotero.Sync.Runner.sync({ background: true });
			return { synced: true };
		});
		E["/zotero-ai/test/status"] = endpoint(["GET"], async () => ({
			version: ZoteroAI.version,
			specVersion: 1,
			userID: Zotero.Users.getCurrentUserID() || null,
			signedIn: ZoteroAI.Auth ? !!(await ZoteroAI.Auth.credentials?.()) : false,
		}));
		E["/zotero-ai/test/eval"] = endpoint(["POST"], async (data) => {
			// eslint-disable-next-line no-new-func
			let fn = new Function("ZoteroAI", "Zotero", "win", `return (async () => { ${data.code} })();`);
			let result = await fn(ZoteroAI, Zotero, Zotero.getMainWindow());
			return { result: result === undefined ? null : result };
		});
		E["/zotero-ai/test/screenshot"] = endpoint(["POST"], async () => {
			let win = Zotero.getMainWindow();
			let bitmap = await win.browsingContext.currentWindowGlobal.drawSnapshot(null, 1, "rgb(255,255,255)");
			let canvas = win.document.createElement("canvas");
			canvas.width = bitmap.width;
			canvas.height = bitmap.height;
			canvas.getContext("2d").drawImage(bitmap, 0, 0);
			return { png: canvas.toDataURL("image/png").split(",")[1] };
		});
		E["/zotero-ai/test/describe"] = endpoint(["POST"], async (data) => ({
			description: await ZoteroAI.Tools.describeWrite(data.name, data.args || {}),
		}));
		E["/zotero-ai/test/markdown"] = endpoint(["POST"], async (data) => ({ html: ZoteroAI.Notes.toHTML(data.markdown) }));
		E["/zotero-ai/test/notes"] = endpoint(["POST"], async (data) => ({
			turns: ZoteroAI.Notes.decode(data.html).map((t) => ({ role: t.role, text: t.text })),
			isLibraryChat: ZoteroAI.Notes.isLibraryChat(data.html),
			attachmentKey: ZoteroAI.Notes.attachmentKey(data.html),
		}));
		E["/zotero-ai/test/signin"] = endpoint(["POST"], async () => ({ authorizeUrl: await ZoteroAI.Auth.startSignInForTest() }));
	},

	unregister() {
		for (let path of this.PATHS) delete Zotero.Server.Endpoints[path];
	},
};
