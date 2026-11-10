/* The tool definitions and prompts shared with the other Zotero AI clients (ai/spec, copied in at build). */

ZoteroAI.Spec = {
	tools: null,
	prompts: null,

	async load(rootURI) {
		let read = async (path) => JSON.parse(await (await fetch(rootURI + path)).text());
		this.tools = (await read("content/spec/tools.json")).tools;
		this.prompts = await read("content/spec/prompts.json");
	},

	/** Tool definitions as sent to the Responses API (the x- fields stripped). */
	apiTools() {
		return this.tools.map((tool) => Object.fromEntries(Object.entries(tool).filter(([k]) => !k.startsWith("x-"))));
	},

	isKnown(name) {
		return this.tools.some((t) => t.name == name);
	},

	isWrite(name) {
		return this.tools.some((t) => t.name == name && t["x-write"] === true);
	},
};
