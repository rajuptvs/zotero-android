/*
 * Agents allowed to use the MCP server (ai/spec/mcp.md). Each gets its own revocable token; only
 * the token's SHA-256 is stored. The list in memory is authoritative; the pref is its copy on disk.
 */

ZoteroAI.McpClients = {
	PREF: "mcp.clients",
	_clients: null,

	_all() {
		if (!this._clients) {
			try {
				this._clients = JSON.parse(ZoteroAI.pref(this.PREF) || "[]");
			}
			catch (e) {
				Zotero.logError(e);
				this._clients = [];
			}
		}
		return this._clients;
	},

	_save() {
		ZoteroAI.pref(this.PREF, JSON.stringify(this._all().map(({ lastUsedAt, ...c }) => c)));
	},

	/** The clients, without their token hashes. */
	list() {
		return this._all().map(({ tokenHash, ...c }) => ({ ...c }));
	},

	get(id) {
		return this._all().find((c) => c.id == id) || null;
	},

	/** Adds a client and returns its token, which is shown once and never stored. */
	async create({ name, scope }) {
		if (!["read", "write"].includes(scope)) throw new Error("Unknown scope");
		let token = "zai_" + ZoteroAI.randomToken(32);
		let client = {
			id: "c_" + ZoteroAI.randomToken(6),
			name: (name || "").trim().slice(0, 60) || "Agent",
			tokenHash: await this._hash(token),
			scope,
			autoApprove: false,
			createdAt: new Date().toISOString(),
		};
		this._all().push(client);
		this._save();
		return { client: this.list().find((c) => c.id == client.id), token };
	},

	revoke(id) {
		this._clients = this._all().filter((c) => c.id != id);
		this._save();
		ZoteroAI.Approvals.clientRevoked?.(id);
	},

	setAutoApprove(id, on) {
		let client = this.get(id);
		if (!client) return;
		client.autoApprove = !!on && client.scope == "write";
		this._save();
	},

	/** The client a request's `Authorization: Bearer zai_…` header belongs to, or null. */
	async authenticate(header) {
		let m = /^Bearer\s+(zai_[A-Za-z0-9_-]+)\s*$/.exec(header || "");
		if (!m) return null;
		let hash = await this._hash(m[1]);
		let found = null;
		// Compare every hash in full, so timing doesn't reveal how close a guess was.
		for (let client of this._all()) {
			if (this._equal(client.tokenHash, hash)) found = client;
		}
		if (found) found.lastUsedAt = new Date().toISOString();
		return found;
	},

	async _hash(token) {
		let digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(token));
		return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
	},

	_equal(a, b) {
		if (typeof a != "string" || a.length != b.length) return false;
		let diff = 0;
		for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
		return diff === 0;
	},
};
