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
 */

ZoteroAI.TestEndpoints = {
	PATHS: ["/zotero-ai/test/tool", "/zotero-ai/test/sync", "/zotero-ai/test/status",
		"/zotero-ai/test/eval", "/zotero-ai/test/screenshot", "/zotero-ai/test/signin",
		"/zotero-ai/test/describe", "/zotero-ai/test/notes", "/zotero-ai/test/markdown", "/zotero-ai/test/policy"],

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
