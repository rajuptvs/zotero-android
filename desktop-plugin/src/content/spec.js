/* The tool definitions and prompts shared with the other Zotero AI clients (ai/spec, copied in at build). */

ZoteroAI.Spec = {
	tools: null,
	prompts: null,

	async load(rootURI) {
		let read = async (path) => JSON.parse(await (await fetch(rootURI + path)).text());
		this.tools = (await read("content/spec/tools.json")).tools;
		this.prompts = await read("content/spec/prompts.json");
	},

	tool(name) {
		return this.tools.find((t) => t.name == name) || null;
	},

	/** Tools the chat offers the model (MCP-only tools left out). */
	chatTools() {
		return this.tools.filter((t) => !t["x-mcp-only"]);
	},

	/** Tool definitions as sent to the Responses API (the x- fields stripped). */
	apiTools() {
		return this.chatTools().map((tool) => Object.fromEntries(Object.entries(tool).filter(([k]) => !k.startsWith("x-"))));
	},

	/** A tool the chat may call. */
	isKnown(name) {
		return this.chatTools().some((t) => t.name == name);
	},

	isWrite(name) {
		return this.tool(name)?.["x-write"] === true;
	},

	/**
	 * The MCP tool list for a client (ai/spec/mcp.md): read-scope clients get the read tools, write
	 * clients everything. Nullable properties aren't required, since MCP clients may omit them.
	 */
	mcpTools(scope) {
		return this.tools
			.filter((t) => scope == "write" || (!t["x-write"] && !t["x-write-scope"]))
			.map((t) => {
				let schema = JSON.parse(JSON.stringify(t.parameters || { type: "object", properties: {} }));
				let props = schema.properties || {};
				schema.required = (schema.required || []).filter((k) => ![].concat(props[k]?.type || []).includes("null"));
				return {
					name: t.name,
					description: t.description,
					inputSchema: schema,
					annotations: { readOnlyHint: t["x-write"] !== true, destructiveHint: t["x-always-ask"] === true },
				};
			});
	},

	/** Whether a call needs the user's decision (ai/spec/tools.md, "Write tools"). */
	shouldAsk(name, autoApprove) {
		let tool = this.tool(name);
		if (!tool || tool["x-write"] !== true) return false;
		return tool["x-always-ask"] === true || !autoApprove;
	},

	/**
	 * Checks arguments against the tool's schema (ai/spec/tools.md): returns {args} with omitted
	 * nullables filled in, or {error: "invalid_argument", message}.
	 */
	validateArgs(name, args) {
		let tool = this.tool(name);
		if (!tool) return { error: "invalid_argument", message: `Unknown tool ${name}` };
		let invalid = (message) => ({ error: "invalid_argument", message });
		if (args == null || typeof args != "object" || Array.isArray(args)) return invalid("Arguments must be an object.");
		let schema = tool.parameters || { properties: {} };
		let properties = schema.properties || {};
		let out = {};
		for (let key of Object.keys(args)) {
			if (!(key in properties)) return invalid(`Unknown argument ${key}.`);
		}
		for (let [key, prop] of Object.entries(properties)) {
			let types = [].concat(prop.type || []);
			let value = args[key];
			if (value === undefined) {
				if (types.includes("null")) {
					out[key] = null;
					continue;
				}
				if ((schema.required || []).includes(key)) return invalid(`Missing ${key}.`);
				continue;
			}
			if (!this._matches(value, types, prop)) return invalid(`${key} must be ${types.join(" or ")}.`);
			out[key] = value;
		}
		return { args: out };
	},

	_matches(value, types, prop) {
		return types.some((type) => {
			switch (type) {
				case "null": return value === null;
				case "string": return typeof value == "string";
				case "boolean": return typeof value == "boolean";
				case "integer": return Number.isInteger(value);
				case "number": return typeof value == "number" && Number.isFinite(value);
				case "object": return value !== null && typeof value == "object" && !Array.isArray(value);
				case "array": {
					if (!Array.isArray(value)) return false;
					let itemTypes = [].concat(prop.items?.type || []);
					return !itemTypes.length || value.every((v) => this._matches(v, itemTypes, prop.items));
				}
			}
			return false;
		});
	},
};
