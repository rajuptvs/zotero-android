/*
 * The chat panel's UI. One panel per window; it shows whichever ChatSession belongs to the selected
 * tab (the library, or a reader). Same controls and wording as AiChatPanel on Android.
 */

ZoteroAI.STRINGS = {
	title: "Ask AI",
	libraryTitle: "Ask AI · Library",
	signInTitle: "Chat about what you're reading",
	signInBody: "Ask questions about this document, get explanations of selected passages, and keep the conversation as a note that syncs to your other devices. Requests use your ChatGPT plan.",
	librarySignInTitle: "Manage your library with AI",
	librarySignInBody: "Find papers, summarize a collection, or organize items, in a chat that syncs to your other devices as a note. Requests use your ChatGPT plan.",
	continueWithChatGPT: "Continue with ChatGPT",
	signingIn: "Finish signing in in your browser…",
	planUsageDisabled: "ChatGPT plan usage wasn't enabled for Zotero. Sign in again and allow it to use your plan.",
	inputHint: "Ask about this document",
	libraryInputHint: "Ask about your library",
	send: "Send",
	stop: "Stop",
	newChat: "New chat",
	signOut: "Sign out of ChatGPT",
	differentAccount: "Sign in with a different account",
	usage: "ChatGPT usage",
	quoteLabel: "Text selection",
	empty: "Ask anything about this document. Select text to ask about a passage, or use + to send the page or a figure as an image.",
	libraryEmpty: "Ask about your library: find papers, summarize a collection, or organize items. You approve every change before it's made.",
	reading: "Reading the document…",
	thinking: "Thinking…",
	writing: "Writing the answer…",
	notSaved: "This chat isn't saved: the document has no parent item or the library is read-only.",
	cancel: "Cancel",
	model: "Model",
	reasoning: "Reasoning effort",
	history: "Chat history",
	historyEmpty: "No saved chats",
	autoAttach: "Attach selected text",
	autoApprove: "Approve changes automatically",
	close: "Close AI panel",
	copy: "Copy",
	saveAsNote: "Save as note",
	saved: "Saved",
	explainAgain: "Explain again",
	attach: "Attach",
	attachPage: "This page (image)",
	attachArea: "Select an area (image)",
	imagesUnsupported: "This model can't read images",
	approvalTitle: "Allow this change?",
	approve: "Approve",
	decline: "Decline",
};

ZoteroAI.Panel = class {
	/**
	 * @param doc   the main window's document
	 * @param hooks {close(), attachPage(), selectArea()} — the capture hooks resolve with an image or null
	 */
	constructor(doc, hooks) {
		this.doc = doc;
		this.hooks = hooks;
		this.session = null;
		this._unsubscribe = null;
		this._rendered = {};
		this.element = this._build();
	}

	destroy() {
		clearTimeout(this._streamTimer);
		this._unsubscribe?.();
		this.element.remove();
	}

	attach(session) {
		if (this.session === session) return;
		this._unsubscribe?.();
		this.session = session;
		this._rendered = {};
		this._streamBlocks = [];
		this._menu.hidden = true;
		this._input.value = session?.state.input || "";
		this._unsubscribe = session?.onChange((state) => this.render(state));
		if (session) this.render(session.state);
	}

	focusInput() {
		this._input.focus();
	}

	// region Building

	_h(tag, attrs = {}, ...children) {
		let el = this.doc.createElementNS("http://www.w3.org/1999/xhtml", tag);
		for (let [k, v] of Object.entries(attrs)) {
			if (v == null || v === false) continue;
			if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
			else if (k == "class") el.className = v;
			else if (k == "text") el.textContent = v;
			else el.setAttribute(k, v === true ? "" : v);
		}
		for (let c of children.flat()) {
			if (c == null || c === false) continue;
			el.append(typeof c == "string" ? this.doc.createTextNode(c) : c);
		}
		return el;
	}

	_build() {
		let S = ZoteroAI.STRINGS;
		let h = this._h.bind(this);
		this._title = h("div", { class: "zai-title" });
		this._historySelect = h("select", { class: "zai-history", title: S.history, "aria-label": S.history, onchange: () => {
			let key = this._historySelect.value;
			this._historySelect.value = "";
			if (key) this.session?.openChat(key);
		} });
		this._accountButton = h("button", { class: "zai-icon", title: "ChatGPT account", "aria-label": "ChatGPT account", text: "⋯",
			onclick: (e) => { e.stopPropagation(); this._toggleMenu(); } });
		this._menu = h("div", { class: "zai-menu", hidden: true });
		let header = h("div", { class: "zai-header" },
			this._title,
			this._historySelect,
			h("button", { class: "zai-icon", title: S.newChat, "aria-label": S.newChat, text: "＋", onclick: () => this.session?.newChat() }),
			this._accountButton,
			h("button", { class: "zai-icon", title: S.close, "aria-label": S.close, text: "✕", onclick: () => this.hooks.close() }),
			this._menu);

		this._signIn = h("div", { class: "zai-signin" });
		this._messages = h("div", { class: "zai-messages", role: "log" });
		this._streaming = h("div", { class: "zai-msg zai-assistant zai-streaming", hidden: true });
		this._status = h("div", { class: "zai-status", hidden: true, role: "status" });
		this._approval = h("div", { class: "zai-approval", hidden: true });
		this._error = h("div", { class: "zai-error", hidden: true, role: "alert" });
		this._notice = h("div", { class: "zai-notice", hidden: true, text: S.notSaved });
		this._scroller = h("div", { class: "zai-scroller" }, this._messages, this._streaming, this._approval, this._status);

		this._quote = h("div", { class: "zai-quote", hidden: true });
		this._images = h("div", { class: "zai-images", hidden: true });
		this._quick = h("div", { class: "zai-chips", hidden: true });
		this._input = h("textarea", { class: "zai-input", rows: 2, onkeydown: (e) => {
			if (e.key == "Enter" && !e.shiftKey && !e.isComposing) {
				e.preventDefault();
				this._send();
			}
		}, oninput: () => {
			this.session?.setInput(this._input.value);
			this._sendButton.disabled = !this._canSend(this.session.state);
		} });
		this._attachButton = h("button", { class: "zai-icon", title: S.attach, "aria-label": S.attach, text: "+",
			onclick: (e) => { e.stopPropagation(); this._toggleAttachMenu(); } });
		this._attachMenu = h("div", { class: "zai-menu zai-attach-menu", hidden: true },
			h("button", { text: S.attachPage, onclick: () => this._capture(this.hooks.attachPage) }),
			h("button", { text: S.attachArea, onclick: () => this._capture(this.hooks.selectArea) }));
		this._sendButton = h("button", { class: "zai-send", text: S.send, onclick: () => this._send() });
		this._stopButton = h("button", { class: "zai-send zai-stop", text: S.stop, hidden: true, onclick: () => this.session?.stop() });
		this._modelSelect = h("select", { class: "zai-model", title: S.model, "aria-label": S.model,
			onchange: () => this.session?.selectModel(this._modelSelect.value) });
		this._effortSelect = h("select", { class: "zai-effort", title: S.reasoning, "aria-label": S.reasoning,
			onchange: () => this.session?.selectReasoningEffort(this._effortSelect.value) });
		this._composer = h("div", { class: "zai-composer" },
			this._quote, this._images, this._quick, this._input,
			h("div", { class: "zai-row" }, this._attachButton, this._modelSelect, this._effortSelect,
				h("div", { class: "zai-spacer" }), this._stopButton, this._sendButton),
			this._attachMenu);

		let root = h("div", { class: "zai-panel" },
			h("style", { text: ZoteroAI.PANEL_CSS }),
			header, this._signIn, this._scroller, this._notice, this._error, this._composer);
		root.addEventListener("click", (e) => {
			// Links in answers (finished or streaming) open in the browser, never in this window.
			let link = e.target.closest?.("a[href]");
			if (link) {
				e.preventDefault();
				if (/^(https?:|mailto:)/i.test(link.getAttribute("href"))) Zotero.launchURL(link.href);
			}
			this._menu.hidden = true;
			this._attachMenu.hidden = true;
		});
		return root;
	}

	// endregion

	// region Rendering

	render(state) {
		let S = ZoteroAI.STRINGS;
		let r = this._rendered;
		let signedIn = state.auth.status == "signedIn" && state.auth.planUsageEnabled;
		this._title.textContent = state.isLibrary ? S.libraryTitle : S.title;

		if (r.auth !== state.auth || r.error !== state.error) this._renderSignIn(state, signedIn);
		this._scroller.hidden = !signedIn;
		this._composer.hidden = !signedIn;
		this._historySelect.hidden = !signedIn;
		this._notice.hidden = !signedIn || state.canSave || !state.turns.length;

		this._error.hidden = !state.error;
		this._error.textContent = state.error || "";

		if (r.history !== state.history) this._renderHistory(state);
		if (r.models !== state.models || r.model !== state.selectedModel || r.effort !== state.reasoningEffort) this._renderModels(state);
		if (r.turns !== state.turns || r.saved !== state.savedAnswers || r.isSending !== state.isSending) this._renderMessages(state);
		if (r.streaming !== state.streamingText || r.liveActivity !== state.liveActivity) this._renderStreaming(state);
		if (r.approval !== state.pendingApproval) this._renderApproval(state);
		this._renderStatus(state);
		if (r.quote !== state.quote || r.images !== state.images || r.model !== state.selectedModel) this._renderAttachments(state);

		this._input.placeholder = state.isLibrary ? S.libraryInputHint : S.inputHint;
		if (r.input !== state.input && this._input.value != state.input) this._input.value = state.input;
		this._attachButton.hidden = state.isLibrary;
		this._sendButton.hidden = state.isSending;
		this._stopButton.hidden = !state.isSending;
		this._sendButton.disabled = !this._canSend(state);

		let scroll = r.turns !== state.turns || r.streaming !== state.streamingText || r.approval !== state.pendingApproval;
		this._rendered = {
			auth: state.auth, error: state.error, history: state.history, models: state.models, model: state.selectedModel,
			effort: state.reasoningEffort, turns: state.turns, saved: state.savedAnswers, isSending: state.isSending,
			streaming: state.streamingText, liveActivity: state.liveActivity, approval: state.pendingApproval,
			quote: state.quote, images: state.images, input: state.input,
		};
		if (scroll) this._scroller.scrollTop = this._scroller.scrollHeight;
	}

	_canSend(state) {
		return !state.isSending && !!state.selectedModel && !!this._input.value.trim();
	}

	_renderSignIn(state, signedIn) {
		let S = ZoteroAI.STRINGS;
		let h = this._h.bind(this);
		this._signIn.replaceChildren();
		this._signIn.hidden = signedIn;
		if (signedIn) return;
		let status = state.auth.status;
		this._signIn.append(
			h("h2", { text: state.isLibrary ? S.librarySignInTitle : S.signInTitle }),
			h("p", { text: state.isLibrary ? S.librarySignInBody : S.signInBody }));
		if (status == "signedIn" && !state.auth.planUsageEnabled) this._signIn.append(h("p", { class: "zai-warning", text: S.planUsageDisabled }));
		if (status == "signingIn") {
			this._signIn.append(
				h("p", { class: "zai-muted", text: S.signingIn }),
				h("button", { class: "zai-secondary", text: S.cancel, onclick: () => this.session.cancelSignIn() }));
		}
		else {
			this._signIn.append(h("button", { class: "zai-primary", text: S.continueWithChatGPT, onclick: () => this.session.signIn() }));
		}
	}

	_renderHistory(state) {
		let S = ZoteroAI.STRINGS;
		let h = this._h.bind(this);
		this._historySelect.replaceChildren(
			h("option", { value: "", text: "🕘 " + S.history }),
			...(state.history.length
				? state.history.map((e) => h("option", { value: e.noteKey, text: e.title }))
				: [h("option", { value: "", disabled: true, text: S.historyEmpty })]));
		this._historySelect.value = "";
	}

	_renderModels(state) {
		let h = this._h.bind(this);
		this._modelSelect.replaceChildren(...state.models.map((m) => h("option", { value: m.slug, text: m.displayName })));
		if (state.selectedModel) this._modelSelect.value = state.selectedModel.slug;
		let efforts = state.selectedModel?.reasoningEfforts || [];
		this._effortSelect.hidden = !efforts.length;
		this._effortSelect.replaceChildren(...efforts.map((e) => h("option", { value: e, text: e })));
		if (state.reasoningEffort) this._effortSelect.value = state.reasoningEffort;
	}

	_renderMessages(state) {
		let S = ZoteroAI.STRINGS;
		let h = this._h.bind(this);
		this._messages.replaceChildren();
		if (!state.turns.length) {
			this._messages.append(h("div", { class: "zai-empty" },
				h("p", { text: state.isLibrary ? S.libraryEmpty : S.empty }),
				state.isLibrary ? h("div", { class: "zai-chips" }, ZoteroAI.LIBRARY_SUGGESTIONS.map((s) =>
					h("button", { class: "zai-chip", text: s, onclick: () => this.session.send(s) }))) : null));
			return;
		}
		let lastAssistant = state.turns.map((t) => t.role).lastIndexOf("assistant");
		state.turns.forEach((turn, index) => {
			if (turn.role == "user") {
				let m = /^About this passage:\n"([\s\S]*?)"\n+([\s\S]*)$/.exec(turn.text);
				this._messages.append(h("div", { class: "zai-msg zai-user" },
					m ? h("blockquote", { text: m[1] }) : null,
					h("div", { class: "zai-text", text: m ? m[2] : turn.text }),
					(turn.images || []).length ? h("div", { class: "zai-images" }, turn.images.map((img) =>
						h("img", { src: img.dataUrl, alt: img.label || "image", title: img.label || "" }))) : null));
				return;
			}
			let saved = state.savedAnswers.includes(index);
			let body = h("div", { class: "zai-markdown" });
			this._setMarkdown(body, turn.text);
			this._messages.append(h("div", { class: "zai-msg zai-assistant" },
				this._activity(turn.activity),
				body,
				h("div", { class: "zai-actions" },
					h("button", { text: S.copy, onclick: (e) => this._copy(turn.text, e.target) }),
					state.canSave ? h("button", { text: saved ? "✓ " + S.saved : S.saveAsNote, disabled: saved,
						onclick: () => this.session.saveAnswerAsNote(index) }) : null,
					!state.isLibrary && index == lastAssistant && !state.isSending
						? h("button", { text: S.explainAgain, onclick: () => this.session.explainAgain() }) : null)));
		});
	}

	_activity(lines) {
		if (!lines || !lines.length) return null;
		return this._h("ul", { class: "zai-activity" }, lines.map((l) => this._h("li", { text: l })));
	}

	/**
	 * The answer being written, split into blocks (Notes.splitBlocks): finished blocks keep their
	 * nodes (and typeset math) and only the last one is rendered again, at most every 50 ms.
	 */
	_renderStreaming(state) {
		let text = state.streamingText;
		if (!text || !text.trim()) {
			// Finished or not started: no delay, so the bubble never lingers next to the saved turn.
			clearTimeout(this._streamTimer);
			this._streamTimer = null;
			this._paintStreaming(state);
			return;
		}
		if (this._streamTimer) return;
		let wait = Math.max(0, 50 - (Date.now() - (this._streamPaintedAt || 0)));
		this._streamTimer = setTimeout(() => {
			this._streamTimer = null;
			this._streamPaintedAt = Date.now();
			if (this.session) this._paintStreaming(this.session.state);
		}, wait);
	}

	_paintStreaming(state) {
		let text = state.streamingText || "";
		let activity = state.isSending ? state.liveActivity : [];
		this._streaming.hidden = !text.trim() && !activity.length;
		if (this._streaming.hidden) {
			this._streamBlocks = [];
			this._streaming.replaceChildren();
			return;
		}
		let old = this._streamBlocks || [];
		let sources = ZoteroAI.Notes.splitBlocks(text) || [text];
		this._streamBlocks = sources.map((src, i) => {
			// The last block may still be streaming: color its code once a later block exists.
			let last = i == sources.length - 1;
			if (old[i]?.src === src && old[i].highlighted === !last) return old[i];
			let node = this._h("div", { class: "zai-markdown" });
			this._setMarkdown(node, src, { highlight: !last });
			return { src, node, highlighted: !last };
		});
		this._streaming.replaceChildren(...[this._activity(activity)].filter(Boolean), ...this._streamBlocks.map((b) => b.node));
	}

	_renderApproval(state) {
		let S = ZoteroAI.STRINGS;
		let h = this._h.bind(this);
		let p = state.pendingApproval;
		this._approval.hidden = !p;
		this._approval.replaceChildren();
		if (!p) return;
		// A double-click on Approve mustn't also approve the next card that appears under it.
		let shownAt = Date.now();
		let settled = () => Date.now() - shownAt > 400;
		let auto = h("input", { type: "checkbox", id: "zai-auto-approve" });
		auto.checked = state.autoApprove;
		auto.addEventListener("change", () => this.session.setAutoApprove(auto.checked));
		this._approval.append(
			h("div", { class: "zai-approval-title", text: S.approvalTitle }),
			h("div", { class: "zai-approval-body", text: p.description }),
			h("div", { class: "zai-row" },
				h("button", { class: "zai-primary", text: S.approve, onclick: () => settled() && this.session.approvePending() }),
				h("button", { class: "zai-secondary", text: S.decline, onclick: () => settled() && this.session.declinePending() })),
			h("label", { class: "zai-check" }, auto, S.autoApprove));
	}

	_renderStatus(state) {
		let S = ZoteroAI.STRINGS;
		let phase = state.phase;
		let text = null;
		if (state.isSending && !state.pendingApproval) {
			if (phase && typeof phase == "object") text = phase.tool + "…";
			else if (phase == "reading") text = S.reading;
			else if (phase == "writing") text = null;
			else text = S.thinking;
		}
		this._status.hidden = !text;
		this._status.textContent = text || "";
	}

	_renderAttachments(state) {
		let S = ZoteroAI.STRINGS;
		let h = this._h.bind(this);
		this._quote.hidden = !state.quote;
		this._quote.replaceChildren();
		if (state.quote) {
			this._quote.append(
				h("div", { class: "zai-quote-label", text: S.quoteLabel }),
				h("div", { class: "zai-quote-text", text: state.quote }),
				h("button", { class: "zai-icon zai-remove", title: "Remove", "aria-label": "Remove text selection", text: "✕",
					onclick: () => this.session.setQuote(null) }));
		}
		this._images.hidden = !state.images.length;
		this._images.replaceChildren(...state.images.map((img, i) => h("div", { class: "zai-thumb" },
			h("img", { src: img.dataUrl, alt: img.label || "image", title: img.label || "" }),
			h("button", { class: "zai-icon zai-remove", title: "Remove", "aria-label": "Remove image", text: "✕",
				onclick: () => this.session.removeImage(i) }))));
		if (state.images.length && state.selectedModel && !state.selectedModel.supportsImages) {
			this._images.append(h("div", { class: "zai-warning", text: S.imagesUnsupported }));
		}
		// Quick actions apply to a selected passage, as on Android.
		this._quick.hidden = state.isLibrary || !state.quote;
		this._quick.replaceChildren(...ZoteroAI.QUICK_ACTIONS.map((a) =>
			h("button", { class: "zai-chip", text: a.label, onclick: () => this.session.send(a.prompt) })));
	}

	// endregion

	// region Actions

	_send() {
		if (!this.session || !this._canSend(this.session.state)) return;
		this.session.setInput(this._input.value);
		this._input.value = "";
		this.session.send();
	}

	async _capture(hook) {
		this._attachMenu.hidden = true;
		try {
			let image = await hook();
			if (image) this.session?.addImage(image);
		}
		catch (e) {
			Zotero.logError(e);
			this.session?.set({ error: "Couldn't capture the page." });
		}
	}

	_copy(text, button) {
		Zotero.Utilities.Internal.copyTextToClipboard(text);
		let old = button.textContent;
		button.textContent = "✓ " + ZoteroAI.STRINGS.copy;
		setTimeout(() => { button.textContent = old; }, 1500);
	}

	/**
	 * The main window is an XML document, where innerHTML would use the XML parser and throw on
	 * <br>, <hr> or &nbsp;. Parse as HTML and import the nodes; fall back to plain text.
	 */
	_setMarkdown(el, markdown, { highlight = true } = {}) {
		try {
			let doc = new DOMParser().parseFromString(`<body>${ZoteroAI.Notes.toHTML(markdown)}</body>`, "text/html");
			this._typesetMath(doc);
			if (highlight) this._highlightCode(doc);
			el.replaceChildren(...[...doc.body.childNodes].map((n) => this.doc.importNode(n, true)));
		}
		catch (e) {
			Zotero.logError(e);
			el.textContent = markdown;
		}
	}

	/**
	 * Turns the note math markup from Notes.toHTML (<span class="math">$…$</span>,
	 * <pre class="math">$$…$$</pre>) into MathML, which Zotero draws natively. Anything KaTeX
	 * can't parse stays as its TeX source.
	 */
	_typesetMath(doc) {
		for (let node of doc.querySelectorAll(".math")) {
			let display = node.localName == "pre";
			let tex = node.textContent.replace(display ? /^\$\$|\$\$$/g : /^\$|\$$/g, "");
			try {
				let mathml = katex.renderToString(tex, { output: "mathml", displayMode: display, throwOnError: true, trust: false, maxSize: 20, maxExpand: 1000 });
				let parsed = new DOMParser().parseFromString(`<body>${mathml}</body>`, "text/html").body.firstElementChild;
				parsed.classList.add(display ? "zai-math-block" : "zai-math");
				node.replaceWith(doc.importNode(parsed, true));
			}
			catch (e) {
				// Unsupported TeX: keep the source.
			}
		}
	}

	/** Syntax colors for fenced code with a known language (highlight.js, BSD-3-Clause). */
	_highlightCode(doc) {
		for (let el of doc.querySelectorAll("pre:not(.math) > code[class*='language-']")) {
			let lang = (el.className.match(/language-([\w+-]+)/) || [])[1];
			if (!lang || !hljs.getLanguage(lang) || el.textContent.length > 20000) continue;
			try {
				// hljs escapes the code; the result is spans with hljs-* classes.
				el.innerHTML = hljs.highlight(el.textContent, { language: lang, ignoreIllegals: true }).value;
			}
			catch (e) {}
		}
	}

	_toggleAttachMenu() {
		this._menu.hidden = true;
		this._attachMenu.hidden = !this._attachMenu.hidden;
	}

	_toggleMenu() {
		this._attachMenu.hidden = true;
		if (!this._menu.hidden) {
			this._menu.hidden = true;
			return;
		}
		let S = ZoteroAI.STRINGS;
		let h = this._h.bind(this);
		let state = this.session?.state;
		if (!state) return;
		let signedIn = state.auth.status == "signedIn";
		let check = (label, checked, onChange) => {
			let box = h("input", { type: "checkbox" });
			box.checked = checked;
			box.addEventListener("change", () => onChange(box.checked));
			return h("label", { class: "zai-check", onclick: (e) => e.stopPropagation() }, box, label);
		};
		this._menu.replaceChildren(
			signedIn && state.auth.email ? h("div", { class: "zai-muted", text: state.auth.email }) : null,
			state.isLibrary
				? check(S.autoApprove, state.autoApprove, (v) => this.session.setAutoApprove(v))
				: check(S.autoAttach, state.autoAttachSelection, (v) => this.session.setAutoAttachSelection(v)),
			signedIn ? h("button", { text: S.usage, onclick: () => Zotero.launchURL(ZoteroAI.Auth.USAGE_URL) }) : null,
			h("button", { text: "MCP servers for the chat…", onclick: () => this.hooks.openMcpServers() }),
			h("button", { text: "Connect an agent (MCP)…", onclick: () => this.hooks.openConnect() }),
			h("button", { text: S.differentAccount, onclick: () => this.session.signIn({ newAccount: true }) }),
			signedIn ? h("button", { text: S.signOut, onclick: () => this.session.signOut() }) : null);
		this._menu.hidden = false;
	}

	// endregion
};

ZoteroAI.PANEL_CSS = `
.zai-panel {
	display: flex; flex-direction: column; height: 100%; width: 100%; min-width: 0; position: relative; contain: inline-size;
	background: var(--material-sidepane, Canvas); color: var(--fill-primary, CanvasText);
	font: message-box; font-size: 13px;
}
.zai-panel button { font: inherit; cursor: pointer; }
.zai-header { display: flex; align-items: center; gap: 4px; padding: 6px 8px; border-bottom: 1px solid var(--fill-quinary, #ddd); position: relative; }
.zai-title { font-weight: 600; flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.zai-history { max-width: 150px; font-weight: normal; font-size: 12px; }
.zai-icon { border: none; background: transparent; padding: 2px 6px; border-radius: 4px; color: inherit; min-width: 24px; }
.zai-icon:hover { background: var(--fill-quinary, #eee); }
.zai-menu { position: absolute; right: 8px; top: 34px; z-index: 10; display: flex; flex-direction: column; gap: 2px; padding: 6px;
	background: var(--material-background, Canvas); border: 1px solid var(--fill-quinary, #ccc); border-radius: 6px; box-shadow: 0 4px 16px rgba(0,0,0,.15); min-width: 220px; }
.zai-menu[hidden] { display: none; }
.zai-menu button { text-align: left; border: none; background: transparent; padding: 5px 8px; border-radius: 4px; color: inherit; }
.zai-menu button:hover { background: var(--fill-quinary, #eee); }
.zai-attach-menu { top: auto; bottom: 64px; left: 8px; right: auto; }
.zai-signin { padding: 24px 16px; display: flex; flex-direction: column; gap: 8px; align-items: flex-start; }
.zai-signin h2 { margin: 0; font-size: 16px; }
.zai-signin p { margin: 0; line-height: 1.45; }
.zai-panel [hidden] { display: none !important; }
.zai-scroller { flex: 1; overflow-y: auto; padding: 8px 10px; display: flex; flex-direction: column; gap: 8px; }
.zai-messages { display: flex; flex-direction: column; gap: 10px; }
.zai-msg { border-radius: 10px; padding: 8px 10px; line-height: 1.45; overflow-wrap: anywhere; }
.zai-user { align-self: flex-end; max-width: 88%; background: var(--accent-blue10, #e3ecfb); }
.zai-user .zai-text { white-space: pre-wrap; }
.zai-user blockquote, .zai-markdown blockquote { margin: 0 0 6px; padding-left: 8px; border-left: 3px solid var(--accent-blue, #2f6fdf); opacity: .85; white-space: pre-wrap; }
.zai-assistant { background: var(--fill-senary, #f4f4f4); }
.zai-markdown p { margin: 0 0 6px; }
.zai-markdown pre { overflow-x: auto; background: var(--fill-quinary, #eaeaea); padding: 6px; border-radius: 4px; }
.zai-markdown code { font-family: monospace; }
.zai-markdown table { border-collapse: collapse; display: block; overflow-x: auto; max-width: 100%; }
.zai-markdown img { max-width: 100%; }
.zai-markdown math { font-size: 1.1em; }
.zai-markdown .hljs-keyword,.zai-markdown .hljs-selector-tag,.zai-markdown .hljs-doctag{color:#cf222e}.zai-markdown .hljs-string,.zai-markdown .hljs-regexp,.zai-markdown .hljs-meta .hljs-string{color:#0a3069}.zai-markdown .hljs-number,.zai-markdown .hljs-literal,.zai-markdown .hljs-attr,.zai-markdown .hljs-attribute,.zai-markdown .hljs-variable,.zai-markdown .hljs-template-variable,.zai-markdown .hljs-property{color:#0550ae}.zai-markdown .hljs-comment,.zai-markdown .hljs-quote,.zai-markdown .hljs-meta{color:#6e7781;font-style:italic}.zai-markdown .hljs-title,.zai-markdown .hljs-section,.zai-markdown .hljs-function .hljs-title{color:#8250df}.zai-markdown .hljs-built_in,.zai-markdown .hljs-type,.zai-markdown .hljs-symbol{color:#953800}
@media (prefers-color-scheme: dark) { .zai-markdown .hljs-keyword,.zai-markdown .hljs-selector-tag,.zai-markdown .hljs-doctag{color:#ff7b72}.zai-markdown .hljs-string,.zai-markdown .hljs-regexp,.zai-markdown .hljs-meta .hljs-string{color:#a5d6ff}.zai-markdown .hljs-number,.zai-markdown .hljs-literal,.zai-markdown .hljs-attr,.zai-markdown .hljs-attribute,.zai-markdown .hljs-variable,.zai-markdown .hljs-template-variable,.zai-markdown .hljs-property{color:#79c0ff}.zai-markdown .hljs-comment,.zai-markdown .hljs-quote,.zai-markdown .hljs-meta{color:#8b949e;font-style:italic}.zai-markdown .hljs-title,.zai-markdown .hljs-section,.zai-markdown .hljs-function .hljs-title{color:#d2a8ff}.zai-markdown .hljs-built_in,.zai-markdown .hljs-type,.zai-markdown .hljs-symbol{color:#ffa657} }
.zai-math-block { display: block; overflow-x: auto; margin: 6px 0; }
.zai-markdown td, .zai-markdown th { border: 1px solid var(--fill-quinary, #ccc); padding: 2px 6px; }
.zai-activity { margin: 0 0 6px; padding-left: 16px; font-size: 12px; opacity: .75; }
.zai-actions { display: flex; gap: 4px; margin-top: 4px; flex-wrap: wrap; }
.zai-actions button { border: 1px solid var(--fill-quinary, #ccc); background: transparent; border-radius: 12px; padding: 1px 10px; font-size: 12px; color: inherit; }
.zai-status { font-size: 12px; opacity: .75; font-style: italic; }
.zai-approval { border: 1px solid var(--accent-orange, #e08a00); border-radius: 10px; padding: 10px; display: flex; flex-direction: column; gap: 6px; }
.zai-approval-title { font-weight: 600; }
.zai-approval-body { white-space: pre-wrap; }
.zai-error { color: var(--accent-red, #c00); padding: 4px 10px; font-size: 12px; }
.zai-notice, .zai-warning { font-size: 12px; opacity: .8; padding: 2px 10px; }
.zai-muted { opacity: .7; font-size: 12px; padding: 2px 8px; }
.zai-empty { opacity: .9; }
.zai-empty p { line-height: 1.45; }
.zai-composer { border-top: 1px solid var(--fill-quinary, #ddd); padding: 6px 8px; display: flex; flex-direction: column; gap: 6px; position: relative; }
.zai-quote { position: relative; border-left: 3px solid var(--accent-blue, #2f6fdf); padding: 2px 24px 2px 8px; }
.zai-quote-label { font-size: 11px; opacity: .7; }
.zai-quote-text { max-height: 4.5em; overflow: hidden; white-space: pre-wrap; }
.zai-remove { position: absolute; top: 0; right: 0; }
.zai-images { display: flex; gap: 6px; flex-wrap: wrap; }
.zai-images img { max-height: 72px; max-width: 120px; border-radius: 4px; border: 1px solid var(--fill-quinary, #ccc); }
.zai-thumb { position: relative; }
.zai-thumb .zai-remove { background: rgba(0,0,0,.55); color: #fff; padding: 0 4px; min-width: 0; }
.zai-chips { display: flex; gap: 6px; flex-wrap: wrap; }
.zai-chip { border: 1px solid var(--fill-quinary, #ccc); background: transparent; border-radius: 14px; padding: 3px 10px; color: inherit; text-align: left; }
.zai-chip:hover { background: var(--fill-quinary, #eee); }
.zai-input { resize: none; width: 100%; box-sizing: border-box; font: inherit; padding: 6px; border-radius: 6px; border: 1px solid var(--fill-quinary, #ccc); background: var(--material-background, Field); color: inherit; }
.zai-row { display: flex; align-items: center; gap: 6px; }
.zai-spacer { flex: 1; }
.zai-model, .zai-effort { max-width: 140px; }
.zai-send, .zai-primary { border: none; border-radius: 6px; padding: 4px 14px; background: var(--accent-blue, #2f6fdf); color: #fff; }
.zai-send:disabled { opacity: .5; cursor: default; }
.zai-stop { background: var(--accent-red, #c33); }
.zai-secondary { border: 1px solid var(--fill-quinary, #ccc); border-radius: 6px; padding: 4px 14px; background: transparent; color: inherit; }
.zai-check { display: flex; align-items: center; gap: 6px; padding: 4px 8px; font-size: 12px; }
`;
