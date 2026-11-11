/*
 * The Zotero AI MCP server (ai/spec/mcp.md): the ai/spec tools for any MCP client (Claude Code,
 * Codex, …) while Zotero runs, at POST http://127.0.0.1:<port>/zotero-ai/mcp on Zotero's own local
 * server. Streamable HTTP with JSON responses only: Zotero's server writes each response once and
 * can't stream. Each client authenticates with its own bearer token (mcpClients.js); writes wait
 * for the user's decision in Zotero (approvals.js, approvalSheet.js).
 */

ZoteroAI.McpServer = {
	PATH: "/zotero-ai/mcp",
	PROTOCOLS: ["2025-11-25", "2025-06-18"],
	READ_ONLY: Object.freeze({ error: "declined", message: "This connection is read-only." }),
	_registered: false,
	_debugLog: null,

	/** Zotero 10 or later, with its local server. */
	isSupported() {
		return !!Zotero.Server?.Endpoints && parseInt(Zotero.version) >= 10;
	},

	isEnabled() {
		return this._registered && ZoteroAI.pref("mcp.enabled") === true;
	},

	/** The endpoint URL, or null if Zotero's local server isn't running. */
	url() {
		try {
			return `http://127.0.0.1:${Zotero.Server.port}${this.PATH}`;
		}
		catch (e) {
			return null;
		}
	},

	startup() {
		this._redactDebugLog();
		if (ZoteroAI.pref("mcp.enabled") === true) this._register();
	},

	shutdown() {
		this._unregister();
		ZoteroAI.Approvals.declineAll();
		this._restoreDebugLog();
	},

	setEnabled(on) {
		ZoteroAI.pref("mcp.enabled", !!on);
		if (on) this._register();
		else {
			this._unregister();
			ZoteroAI.Approvals.declineAll();
		}
	},

	_register() {
		if (!this.isSupported()) return;
		let server = this;
		let Endpoint = function () {};
		Endpoint.prototype = {
			supportedMethods: ["POST", "GET"],
			// No supportedDataTypes: requests without JSON get our 415 instead of Zotero's text 400.
			// Exactly one parameter, so Zotero passes the headers (server.js, _processEndpoint).
			async init(request) {
				try {
					return await server._handle(request);
				}
				catch (e) {
					Zotero.logError(e);
					return server._json(500, { jsonrpc: "2.0", id: null, error: { code: -32603, message: "Internal error" } });
				}
			},
		};
		Zotero.Server.Endpoints[this.PATH] = Endpoint;
		this._registered = true;
	},

	_unregister() {
		if (Zotero.Server?.Endpoints?.[this.PATH]) delete Zotero.Server.Endpoints[this.PATH];
		this._registered = false;
	},

	// region HTTP

	async _handle(request) {
		if (request.method == "GET") return [405, { Allow: "POST" }, ""];
		let headers = request.headers;
		// Zotero drops browser requests itself, except ones carrying its connector headers.
		if ("origin" in headers) return this._json(403, this._error(null, -32000, "Requests from web pages aren't allowed."));
		let client = await ZoteroAI.McpClients.authenticate(headers.authorization);
		if (!client) return this._json(401, this._error(null, -32000, "Missing or invalid token. Add this agent in Zotero (Ask AI → Connect an agent)."));
		let contentType = (headers["content-type"] || "").split(";")[0].trim().toLowerCase();
		if (contentType != "application/json") return this._json(415, this._error(null, -32000, "Send JSON (Content-Type: application/json)."));
		let version = headers["mcp-protocol-version"];
		if (version && !this.PROTOCOLS.includes(version)) return this._json(400, this._error(null, -32000, `Unsupported MCP-Protocol-Version ${version}.`));
		let msg = request.data;
		if (!msg || typeof msg != "object" || Array.isArray(msg) || msg.jsonrpc !== "2.0") {
			return this._json(400, this._error(null, -32600, "Invalid request."));
		}
		if (typeof msg.method != "string") {
			// A response to us (we never send requests) or garbage.
			if ("id" in msg && ("result" in msg || "error" in msg)) return [202, null, ""];
			return this._json(400, this._error(msg.id ?? null, -32600, "Invalid request."));
		}
		if (!("id" in msg)) return [202, null, ""]; // notification
		let reply = await this._dispatch(client, msg.method, msg.params || {});
		if (reply.error) return this._json(200, { jsonrpc: "2.0", id: msg.id, error: reply.error });
		return this._json(200, { jsonrpc: "2.0", id: msg.id, result: reply.result });
	},

	_error(id, code, message) {
		return { jsonrpc: "2.0", id, error: { code, message } };
	},

	_json(status, value) {
		let body = JSON.stringify(value);
		let headers = {
			"Content-Type": "application/json",
			// Zotero sends no length for bodies and writes UTF-8, so count bytes, not characters.
			"Content-Length": String(new TextEncoder().encode(body).byteLength),
		};
		return [status, headers, body, { logFilter: (s) => s.replace(/zai_[A-Za-z0-9_-]+/g, "zai_[redacted]") }];
	},

	// endregion

	// region JSON-RPC

	async _dispatch(client, method, params) {
		switch (method) {
			case "initialize": {
				let asked = params.protocolVersion;
				return {
					result: {
						protocolVersion: this.PROTOCOLS.includes(asked) ? asked : this.PROTOCOLS[0],
						capabilities: { tools: { listChanged: false } },
						serverInfo: { name: "zotero-ai", version: ZoteroAI.version },
						instructions: ZoteroAI.Spec.prompts.mcp,
					},
				};
			}
			case "ping":
				return { result: {} };
			case "tools/list":
				return { result: { tools: ZoteroAI.Spec.mcpTools(client.scope) } };
			case "tools/call":
				return this._call(client, params);
		}
		return { error: { code: -32601, message: `Method not found: ${method}` } };
	},

	async _call(client, params) {
		let name = params.name;
		let args = params.arguments ?? {};
		let tool = ZoteroAI.Spec.tool(name);
		if (!tool) return { error: { code: -32602, message: `Unknown tool ${name}` } };
		// A page image or PDF comes as extra content parts (_content) after the JSON result.
		let toolResult = ({ _content = [], ...value }) => ({
			result: { content: [{ type: "text", text: JSON.stringify(value) }, ..._content], isError: "error" in value },
		});
		let writes = tool["x-write"] === true || tool["x-write-scope"] === true;
		if (writes && client.scope != "write") return toolResult(this.READ_ONLY);

		if (name == "zotero_await_approval") {
			let checked = ZoteroAI.Spec.validateArgs(name, args);
			if (checked.error) return toolResult(checked);
			let op = ZoteroAI.Approvals.get(checked.args.approval_id, client.id);
			if (!op) return toolResult({ error: "not_found", message: "No change with that approval_id." });
			await ZoteroAI.Approvals.wait(op, ZoteroAI.Approvals.timing.holdMs);
			return toolResult(ZoteroAI.Approvals.envelope(op));
		}
		if (tool["x-write"] === true) {
			let { op, result } = await ZoteroAI.Approvals.request({ client, name, args });
			if (!op) return toolResult(result);
			await ZoteroAI.Approvals.wait(op, ZoteroAI.Approvals.timing.holdMs);
			return toolResult(ZoteroAI.Approvals.envelope(op));
		}
		return toolResult(await ZoteroAI.Tools.execute(name, args));
	},

	// endregion

	// region Debug log

	/**
	 * Zotero logs every request header (server.js, handleRequest) when debug output is on, before
	 * any endpoint runs. Hide MCP tokens there by filtering what reaches Zotero.Debug.log.
	 */
	_redactDebugLog() {
		if (this._debugLog) return;
		let original = Zotero.Debug.log;
		let wrapper = function (message, level, maxDepth, stack) {
			if (typeof message == "string" && message.includes("zai_")) {
				message = message.replace(/zai_[A-Za-z0-9_-]+/g, "zai_[redacted]");
			}
			// Account for this extra frame, as Zotero.debug does for its own.
			if (stack === true) stack = 1;
			else if (stack >= 0) stack++;
			return original.call(this, message, level, maxDepth, stack);
		};
		Zotero.Debug.log = wrapper;
		this._debugLog = { original, wrapper };
	},

	_restoreDebugLog() {
		if (!this._debugLog) return;
		// Only if nothing else has wrapped it since.
		if (Zotero.Debug.log === this._debugLog.wrapper) Zotero.Debug.log = this._debugLog.original;
		this._debugLog = null;
	},

	// endregion
};
