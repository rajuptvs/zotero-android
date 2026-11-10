/* Shared helpers. Every plugin file adds to the ZoteroAI namespace. */

ZoteroAI = {
	id: null,
	version: null,
	rootURI: null,
	PREF_BRANCH: "extensions.zotero-ai.",

	pref(name, value) {
		if (value === undefined) return Zotero.Prefs.get(this.PREF_BRANCH + name, true);
		Zotero.Prefs.set(this.PREF_BRANCH + name, value, true);
		return value;
	},

	log(msg) {
		Zotero.debug("Zotero AI: " + msg);
	},

	escapeHTML(text) {
		return String(text ?? "")
			.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
	},

	/** Case-insensitive comparison used for every "title order" in the spec. */
	compareText(a, b) {
		let x = (a || "").toLowerCase(), y = (b || "").toLowerCase();
		return x < y ? -1 : x > y ? 1 : 0;
	},

	/** The first run of exactly four digits (the spec's `year`). */
	year(date) {
		let m = /(?<!\d)\d{4}(?!\d)/.exec(date || "");
		return m ? m[0] : "";
	},

	/** "2026-11-10 17:59:31" (Zotero's UTC SQL time) → "2026-11-10T17:59:31Z". */
	isoFromSQL(sql) {
		return sql ? sql.replace(" ", "T") + "Z" : "";
	},

	randomToken(bytes) {
		let a = new Uint8Array(bytes);
		crypto.getRandomValues(a);
		return this.base64url(a);
	},

	base64url(bytes) {
		let s = "";
		for (let b of bytes) s += String.fromCharCode(b);
		return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
	},

	base64urlDecode(text) {
		let s = text.replace(/-/g, "+").replace(/_/g, "/");
		while (s.length % 4) s += "=";
		return new TextDecoder().decode(Uint8Array.from(atob(s), (c) => c.charCodeAt(0)));
	},

	async sha256base64url(text) {
		let digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
		return this.base64url(new Uint8Array(digest));
	},

	/** Plain text of note HTML, whitespace collapsed (spec: zotero_get_notes `text`). */
	plainText(html) {
		let doc = new DOMParser().parseFromString(`<body>${html || ""}</body>`, "text/html");
		for (let el of doc.querySelectorAll("p, div, h1, h2, h3, h4, h5, h6, li, br, tr, blockquote, pre")) {
			el.append(" ");
		}
		return (doc.body.textContent || "").replace(/\s+/g, " ").trim();
	},
};
