/*
 * Notes written by the chat, in the shared formats of ai/spec/chat.md, so a conversation started on
 * Android continues here and the other way round. Mirrors ChatNoteFormat / ChatMarkdown (Android).
 */

ZoteroAI.Notes = {
	TITLE_LABEL: "AI chat:",
	USER_LABEL: "You:",
	ASSISTANT_LABEL: "ChatGPT:",
	LIBRARY_CHAT_TAG: "ai-chat",
	LIBRARY_SCOPE_ATTR: 'data-zotero-ai-scope="library"',

	_marked: null,

	markdown() {
		if (!this._marked) {
			let esc = (t) => ZoteroAI.escapeHTML(t);
			// Same links as Android's renderer keeps.
			let safeHref = (href) => /^(https?:|mailto:)/i.test(href || "");
			this._marked = new marked.Marked({ gfm: true, breaks: false });
			this._marked.use({
				renderer: {
					// Never pass raw HTML from a model answer through.
					html(token) { return esc(token.text); },
					link(token) {
						let text = this.parser.parseInline(token.tokens);
						return safeHref(token.href)
							? `<a href="${esc(token.href)}"${token.title ? ` title="${esc(token.title)}"` : ""}>${text}</a>`
							: text;
					},
					image(token) { return esc(token.text || ""); },
				},
			});
		}
		return this._marked;
	},

	// Code (left alone) | $$…$$ | \[…\] | \(…\) | $…$ (not "$5 and $10").
	MATH_OR_CODE: /(```[\s\S]*?```|`[^`\n]*`)|\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|\$(?!\s)([^$\n]+?)(?<!\s)\$(?!\d)/g,

	/**
	 * Markdown → HTML the Zotero note editor keeps (and the chat panel renders). Math becomes
	 * Zotero's own math markup, set aside before Markdown parsing so `_`, `*` and `\` inside it
	 * survive (ai/spec/chat.md).
	 */
	toHTML(markdown) {
		let math = [];
		let protectedText = (markdown || "").replace(this.MATH_OR_CODE, (all, code, d1, d2, i1, i2) => {
			let display = d1 ?? d2, inline = i1 ?? i2;
			if (display != null) math.push([display.trim(), true]);
			else if (inline != null) math.push([inline.trim(), false]);
			else return all;
			return `ZAIMATH${math.length - 1}Z`;
		});
		let html = this.markdown().parse(protectedText);
		let esc = (t) => ZoteroAI.escapeHTML(t);
		math.forEach(([tex, display], index) => {
			let token = `ZAIMATH${index}Z`;
			html = display && html.includes(`<p>${token}</p>`)
				? html.replace(`<p>${token}</p>`, () => `<pre class="math">$$${esc(tex)}$$</pre>`)
				: html.replace(token, () => `<span class="math">$${esc(tex)}$</span>`);
		});
		return html;
	},

	isChatNote(title, html) {
		return (title || "").startsWith(this.TITLE_LABEL) || /data-zotero-ai-attachment="/.test(html)
			|| (html || "").includes(this.LIBRARY_SCOPE_ATTR);
	},

	/**
	 * The scope attribute is dropped when a note is opened in the note editor, so the title heading
	 * also marks a library chat (ai/spec/chat.md).
	 */
	isLibraryChat(html) {
		return (html || "").includes(this.LIBRARY_SCOPE_ATTR) || /^\s*(<div[^>]*>)?\s*<h1>\s*AI chat: Library —/.test(html || "");
	},

	attachmentKey(html) {
		let m = /data-zotero-ai-attachment="([^"]+)"/.exec(html || "");
		return m ? m[1] : null;
	},

	_turns(turns) {
		let esc = (t) => ZoteroAI.escapeHTML(t);
		let out = "";
		for (let turn of turns) {
			out += `<p><strong>${turn.role == "user" ? this.USER_LABEL : this.ASSISTANT_LABEL}</strong></p>\n`;
			for (let line of turn.text.trim().split("\n").filter((l) => l.trim())) {
				out += `<p>${esc(line)}</p>\n`;
			}
			for (let image of turn.images || []) {
				out += `<p>${esc(`[Image: ${image.label}]`)}</p>\n`;
			}
		}
		return out;
	},

	/** Reader conversation: a child note of the item, tied to the attachment. */
	encodeReader(documentTitle, attachmentKey, turns) {
		let esc = (t) => ZoteroAI.escapeHTML(t);
		// No line break before the heading: Zotero titles a note with its first line of text.
		return `<div data-schema-version="9" data-zotero-ai-attachment="${esc(attachmentKey)}">`
			+ `<h1>${esc(`${this.TITLE_LABEL} ${documentTitle}`.trim())}</h1>\n`
			+ this._turns(turns) + "</div>";
	},

	/** Library conversation: a standalone note. */
	encodeLibrary(turns) {
		let esc = (t) => ZoteroAI.escapeHTML(t);
		let first = (turns.find((t) => t.role == "user")?.text || "").split("\n").find((l) => l.trim())?.trim() || "";
		let short = first.length > 60 ? first.slice(0, 57).trimEnd() + "…" : first;
		return `<div data-schema-version="9" ${this.LIBRARY_SCOPE_ATTR}>`
			+ `<h1>${esc(`${this.TITLE_LABEL} Library — ${short}`.trim())}</h1>\n`
			+ this._turns(turns) + "</div>";
	},

	decode(html) {
		let turns = [];
		let role = null, lines = [];
		let flush = () => {
			if (role && lines.length) turns.push({ role, text: role == "assistant" ? this.joinMarkdownLines(lines) : lines.join("\n") });
			lines = [];
		};
		let doc = new DOMParser().parseFromString(`<body>${html || ""}</body>`, "text/html");
		for (let p of doc.querySelectorAll("p")) {
			let inner = p.innerHTML.trim();
			let label = /^<strong>\s*(You:|ChatGPT:)\s*<\/strong>$/.exec(inner);
			if (label) {
				flush();
				role = label[1] == this.USER_LABEL ? "user" : "assistant";
			}
			else if (role) {
				// Read text the way Android's Html.fromHtml does: whitespace runs collapse, <br> breaks.
				for (let br of p.querySelectorAll("br")) br.replaceWith("\u0000");
				let text = (p.textContent || "").replace(/[ \t\n\r\f]+/g, " ").replace(/ ?\u0000 ?/g, "\n").trim();
				if (text) lines.push(text);
			}
		}
		flush();
		return turns;
	},

	/**
	 * Notes keep one paragraph per line and lose blank lines, so put them back between Markdown
	 * blocks: keep table rows and fenced code together, separate everything else (ai/spec/chat.md).
	 */
	joinMarkdownLines(lines) {
		let out = "", inFence = false;
		lines.forEach((line, i) => {
			if (i > 0) out += inFence || (lines[i - 1].startsWith("|") && line.startsWith("|")) ? "\n" : "\n\n";
			out += line;
			if (line.startsWith("```")) inFence = !inFence;
		});
		return out;
	},

	/** A standalone "Save as note" note for one answer (ChatMarkdown.answerNote on Android). */
	answerNote({ question, passage, answerMarkdown, documentTitle, location, modelName }) {
		let esc = (t) => ZoteroAI.escapeHTML(t);
		let heading = (question || "").split("\n").find((l) => l.trim())?.trim() || "AI explanation";
		if (heading.length > 90) heading = heading.slice(0, 87).trimEnd() + "…";
		let html = `<div data-schema-version="9"><h2>${esc(heading)}</h2>\n`;
		if (passage && passage.trim()) html += `<blockquote><p>${esc(passage.trim())}</p></blockquote>\n`;
		html += this.toHTML(answerMarkdown);
		let source = `From an AI chat about “${documentTitle}”`;
		if (location) source += ` (${location})`;
		if (modelName) source += ` · ${modelName}`;
		html += `<p><em>${esc(source)}</em></p>\n</div>`;
		return html;
	},
};
