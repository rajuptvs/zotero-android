/*
 * One conversation, in the reader (about an open document) or over the library. Runs the tool loop
 * of ai/spec/chat.md and saves conversations as notes in the shared format. Mirrors AiChatViewModel
 * on Android; the panel (panel.js) renders `state` and calls the methods below.
 */

ZoteroAI.QUICK_ACTIONS = [
	{ label: "Explain", prompt: "Explain this passage in the context of the paper." },
	{ label: "Simplify", prompt: "Explain this passage in simple terms, as if to a smart newcomer to the field." },
	{ label: "Translate", prompt: "Translate this passage into English (or, if it is English, into plain everyday English) and briefly note any terms that don't translate cleanly." },
	{ label: "Define terms", prompt: "Define the technical terms and symbols in this passage, as they're used in this paper." },
];

ZoteroAI.LIBRARY_SUGGESTIONS = [
	"What did I add recently?",
	"Which papers in this collection have no tags? Suggest tags for them.",
	"Summarize the papers I've selected.",
	"Find my papers about reinforcement learning.",
];

ZoteroAI.TOOL_LABELS = {
	zotero_search_items: "Searching your library",
	zotero_get_item_metadata: "Reading item details",
	zotero_get_item_fulltext: "Reading a paper",
	zotero_get_collections: "Looking at your collections",
	zotero_get_collection_items: "Listing a collection",
	zotero_get_tags: "Looking at your tags",
	zotero_get_recent: "Checking recent additions",
	zotero_get_notes: "Reading notes",
	zotero_get_annotations: "Reading annotations",
	zotero_batch_update_tags: "Updating tags",
	zotero_set_item_collections: "Updating collections",
	zotero_create_collection: "Creating a collection",
	zotero_create_note: "Creating a note",
	zotero_add_item_by_identifier: "Adding an item",
	zotero_trash_items: "Moving items to the trash",
};

ZoteroAI.ChatSession = class {
	static MAX_DOCUMENT_CHARS = 200000;
	static MAX_TOOL_ROUNDS = 12;
	static all = new Set();

	/**
	 * @param scope {kind: "reader", readerContext: async (includeText) => ctx}
	 *           or {kind: "library", libraryContext: () => ctx}
	 */
	constructor(scope) {
		this.scope = scope;
		this.listeners = new Set();
		this.state = {
			isLibrary: scope.kind == "library",
			auth: ZoteroAI.Auth.state(),
			models: [],
			selectedModel: null,
			reasoningEffort: null,
			turns: [],
			streamingText: null,
			liveActivity: [],
			input: "",
			quote: null,
			images: [],
			isSending: false,
			phase: null, // "reading" | "thinking" | "writing" | {tool: label}
			pendingApproval: null,
			noteKey: null,
			canSave: true,
			savedAnswers: [],
			history: [],
			autoAttachSelection: ZoteroAI.pref("autoAttachSelection") !== false,
			autoApprove: ZoteroAI.pref("autoApproveChanges") === true,
			error: null,
		};
		this._abort = null;
		this._approval = null;
		this._documentTitle = "";
		this._itemContext = "";
		this._noteHTML = null; // the note as this session last loaded or wrote it
		this._conversation = 0; // bumped by New chat / opening another chat
		this._modelsFor = null; // account the model list was loaded for
		ZoteroAI.ChatSession.all.add(this);
		this._unsubscribe = ZoteroAI.Auth.onChange((auth) => {
			this.set({ auth });
			if (auth.status == "signedIn" && auth.planUsageEnabled) {
				// Another account may have signed in from a different panel.
				if (!this.state.models.length || this._modelsFor != auth.email) this.loadModels();
			}
			else this.stop();
		});
		if (this.state.auth.status == "signedIn" && this.state.auth.planUsageEnabled) this.loadModels();
		this._loadDocumentAndHistory().catch((e) => Zotero.logError(e));
	}

	destroy() {
		this.stop();
		this._unsubscribe();
		this.listeners.clear();
		ZoteroAI.ChatSession.all.delete(this);
	}

	onChange(listener) {
		this.listeners.add(listener);
		return () => this.listeners.delete(listener);
	}

	set(patch) {
		Object.assign(this.state, patch);
		for (let l of this.listeners) {
			try { l(this.state); }
			catch (e) { Zotero.logError(e); }
		}
	}

	// region Sign-in

	signIn({ newAccount = false } = {}) {
		let requestConsent = this.state.auth.status == "signedIn" && !this.state.auth.planUsageEnabled;
		this.set({ error: null, models: [], selectedModel: null });
		ZoteroAI.Auth.signIn((url) => Zotero.launchURL(url), { newAccount, requestConsent })
			.catch((e) => {
				if (e.message != "Sign-in was restarted.") this.set({ error: e.message || "Sign-in failed." });
			});
	}

	cancelSignIn() {
		ZoteroAI.Auth.cancelSignIn();
	}

	async signOut() {
		this.stop();
		let revoked = await ZoteroAI.Auth.signOut();
		this.set({
			models: [],
			selectedModel: null,
			error: revoked ? null : "Signed out on this computer, but ChatGPT didn't confirm the session was revoked. You can disconnect Zotero in ChatGPT settings.",
		});
	}

	// endregion

	// region Composer

	selectModel(slug) {
		let model = this.state.models.find((m) => m.slug == slug);
		if (!model) return;
		ZoteroAI.pref("model", slug);
		this.set({ selectedModel: model, reasoningEffort: this._effortFor(model) });
	}

	selectReasoningEffort(effort) {
		let model = this.state.selectedModel;
		if (!model) return;
		ZoteroAI.pref("effort." + model.slug, effort);
		this.set({ reasoningEffort: effort });
	}

	// Both settings are global: every open chat follows the change.
	setAutoAttachSelection(enabled) {
		ZoteroAI.pref("autoAttachSelection", enabled);
		for (let session of ZoteroAI.ChatSession.all) session.set({ autoAttachSelection: enabled });
	}

	setAutoApprove(enabled) {
		ZoteroAI.pref("autoApproveChanges", enabled);
		for (let session of ZoteroAI.ChatSession.all) session.set({ autoApprove: enabled });
	}

	setInput(text) {
		this.state.input = text; // no re-render while typing
	}

	setQuote(quote) {
		let q = (quote || "").trim();
		this.set({ quote: q || null });
	}

	addImage(image) {
		this.set({ images: [...this.state.images, image] });
	}

	removeImage(index) {
		this.set({ images: this.state.images.filter((_, i) => i != index) });
	}

	explainAgain() {
		this.send("Explain that again differently: start with the intuition in plain words (an analogy helps), then walk through the details step by step.");
	}

	// endregion

	// region Approval

	approvePending() {
		this._approval?.(true);
	}

	declinePending() {
		this._approval?.(false);
	}

	/** Shows the approval card; ZoteroAI.Approvals decides whether to ask at all. */
	async _askApproval(name, description) {
		let answer = await new Promise((resolve) => {
			this._approval = resolve;
			this.set({ pendingApproval: { toolName: name, description } });
		});
		this._approval = null;
		this.set({ pendingApproval: null });
		return answer;
	}

	// endregion

	// region Conversations

	stop() {
		let partial = this.state.streamingText;
		if (this._abort) {
			this._abort.cancelled = true;
			this._abort.cancel?.();
		}
		this._approval?.(false);
		if (this.state.isSending) {
			this.set({
				isSending: false,
				phase: null,
				streamingText: null,
				turns: partial && partial.trim() ? [...this.state.turns, { role: "assistant", text: partial + "\n\n*(stopped)*" }] : this.state.turns,
			});
		}
	}

	newChat() {
		this.stop();
		this._conversation++;
		this._noteHTML = null;
		this.set({ turns: [], streamingText: null, noteKey: null, error: null, quote: null, images: [], savedAnswers: [], phase: null, liveActivity: [] });
	}

	async openChat(noteKey) {
		this.stop();
		let note = (await this._chatNotes()).find((n) => n.key == noteKey);
		if (!note) return;
		this._conversation++;
		this._noteHTML = note.getNote();
		this.set({ turns: ZoteroAI.Notes.decode(this._noteHTML), noteKey: note.key, streamingText: null, error: null, savedAnswers: [], phase: null });
	}

	/** This chat's note, if it still exists. */
	_note() {
		let libraryID = this._libraryID();
		let note = this.state.noteKey && libraryID != null && Zotero.Items.getByLibraryAndKey(libraryID, this.state.noteKey);
		return note && !note.deleted ? note : null;
	}

	_libraryID() {
		return this.scope.kind == "reader" ? this.scope.libraryID : this.scope.libraryContext().libraryID;
	}

	/**
	 * Called when notes change (e.g. a sync brought in turns from another device): an idle chat
	 * shows the note as it is now, so the next question continues the whole conversation.
	 */
	noteChanged() {
		if (this.state.isSending || !this.state.noteKey) return;
		let note = this._note();
		if (!note) {
			this._noteHTML = null;
			this.set({ noteKey: null });
			return;
		}
		let html = note.getNote();
		if (html == this._noteHTML) return;
		this._noteHTML = html;
		this.set({ turns: ZoteroAI.Notes.decode(html), savedAnswers: [] });
	}

	async refreshHistory() {
		let notes = await this._chatNotes();
		this.set({ history: notes.map((n) => this._historyEntry(n)) });
	}

	// endregion

	async send(textOverride) {
		let question = (textOverride ?? this.state.input).trim();
		let model = this.state.selectedModel;
		if (!model) {
			this.loadModels();
			return;
		}
		if (!question || this.state.isSending) return;
		this.noteChanged();
		let quote = this.state.quote;
		let userText = quote ? `About this passage:\n"${quote}"\n\n${question}` : question;
		let turns = [...this.state.turns, { role: "user", text: userText, images: this.state.images }];
		this.set({
			turns,
			input: textOverride != null ? this.state.input : "",
			quote: null,
			images: [],
			isSending: true,
			error: null,
			liveActivity: [],
			phase: this.state.isLibrary ? "thinking" : "reading",
		});
		let abort = { cancelled: false, cancel: null };
		this._abort = abort;
		let conversation = this._conversation;
		let effort = model.reasoningEfforts.length ? this.state.reasoningEffort : null;
		try {
			let context = await this._buildContext();
			let answer = await this._runTurn(model.slug, effort, context, turns, abort);
			if (abort.cancelled) return;
			let finalTurns = [...turns, answer];
			this.set({ turns: finalTurns, streamingText: null, liveActivity: [] });
			await this._saveChat(finalTurns, conversation);
		}
		catch (e) {
			if (!abort.cancelled) {
				Zotero.logError(e);
				this.set({ error: e.message || "Request failed.", streamingText: null });
			}
		}
		finally {
			if (this._abort === abort) {
				this._abort = null;
				this.set({ isSending: false, phase: null });
			}
		}
	}

	/**
	 * One user turn: ask the model, run the tools it calls (asking before writes), feed the results
	 * back, and repeat until it answers (ai/spec/chat.md).
	 */
	async _runTurn(model, effort, context, turns, abort) {
		// The user's own MCP servers, offered as function tools (ai/spec/mcp-client.md).
		let remote = { tools: [], index: new Map(), unavailable: [] };
		try {
			remote = await ZoteroAI.McpRemote.toolsForTurn();
		}
		catch (e) {
			Zotero.logError(e);
		}
		if (remote.tools.length) context += "\n" + ZoteroAI.McpRemote.CONTEXT_LINE;
		let tools = [...ZoteroAI.Spec.apiTools(), ...remote.tools];
		// Text from other services (tool definitions, earlier results) is in the context: Zotero
		// writes always ask, and after any MCP result so do "read-only" MCP tools (mcp-client.md).
		let mcpSeen = ZoteroAI.McpRemote.usedIn(turns);
		let tainted = remote.tools.length > 0 || mcpSeen;
		let input = [{ role: "developer", content: context }];
		for (let turn of turns) {
			if (turn.role == "assistant") for (let item of turn.trace || []) input.push(item);
			input.push(ZoteroAI.API.inputItem(turn));
		}
		let instructions = this.state.isLibrary ? ZoteroAI.Spec.prompts.library : ZoteroAI.Spec.prompts.reader;
		let trace = [], activity = remote.unavailable.map((name) => `${name} isn't available`);
		let answer = "";
		for (let round = 0; ; round++) {
			if (abort.cancelled) return null;
			let calls = [];
			let roundText = "";
			await ZoteroAI.API.stream({
				model, instructions, input, tools, reasoningEffort: effort, abort,
				onEvent: (event) => {
					if (abort.cancelled) return;
					if (event.type == "phase") {
						this.set({ phase: event.phase, streamingText: this.state.streamingText ?? "" });
					}
					else if (event.type == "text") {
						roundText += event.delta;
						this.set({ streamingText: answer ? `${answer}\n\n${roundText}` : roundText });
					}
					else if (event.type == "item" && event.item.type == "function_call") {
						calls.push(event.item);
					}
				},
			});
			if (roundText.trim()) answer = answer ? `${answer}\n\n${roundText.trim()}` : roundText.trim();
			if (!calls.length) break;
			if (round + 1 > ZoteroAI.ChatSession.MAX_TOOL_ROUNDS) {
				answer += "\n\n*(Stopped: too many tool calls in one answer.)*";
				break;
			}
			if (roundText.trim()) {
				let message = { role: "assistant", content: roundText };
				input.push(message);
				trace.push(message);
			}
			for (let call of calls) {
				if (abort.cancelled) return null;
				// Items aren't stored server-side (store=false), so send calls back without ids.
				let functionCall = { type: "function_call", call_id: call.call_id, name: call.name, arguments: call.arguments || "{}" };
				input.push(functionCall);
				trace.push(functionCall);
				let args = {};
				try { args = JSON.parse(call.arguments || "{}"); }
				catch (e) {}
				let label = ZoteroAI.TOOL_LABELS[call.name] || call.name;
				this.set({ phase: { tool: label } });
				let result;
				let output = null; // set directly for MCP-client calls
				let entry = remote.index.get(call.name);
				if (entry) {
					let description = ZoteroAI.McpRemote.describe(entry, args);
					this.set({ phase: { tool: `${entry.tool.name} on ${entry.server.name}` } });
					if (!ZoteroAI.McpRemote.shouldAsk(entry, mcpSeen) || await this._askApproval(call.name, description)) {
						output = await ZoteroAI.McpRemote.call(entry, args);
						tainted = true;
						mcpSeen = true;
						activity.push(`${entry.tool.name} on ${entry.server.name}` + (JSON.parse(output).isError ? " (failed)" : ""));
					}
					else {
						output = JSON.stringify(ZoteroAI.Approvals.DECLINED);
						activity.push("Declined: " + description);
					}
				}
				else if (!ZoteroAI.Spec.isKnown(call.name)) {
					result = { error: "invalid_argument", message: `Unknown tool ${call.name}` };
				}
				else if (ZoteroAI.Spec.isWrite(call.name)) {
					let run = await ZoteroAI.Approvals.run({
						origin: { kind: "chat", tainted },
						name: call.name,
						args,
						present: (description) => this._askApproval(call.name, description),
					});
					result = run.result;
					if (run.description == null) activity.push(label + " (failed)");
					else if (run.approved) activity.push((result.error ? "✗ " : "✓ ") + run.description);
					else activity.push("Declined: " + run.description);
				}
				else {
					result = await ZoteroAI.Tools.execute(call.name, args);
					activity.push(label + (result.error ? " (failed)" : ""));
				}
				if (abort.cancelled) return null;
				this.set({ liveActivity: [...activity] });
				let outputItem = { type: "function_call_output", call_id: call.call_id, output: output ?? JSON.stringify(result) };
				input.push(outputItem);
				trace.push(outputItem);
			}
			if (abort.cancelled) return null;
			this.set({ phase: "thinking" });
		}
		return { role: "assistant", text: answer, trace, activity };
	}

	/** Keeps one answer as its own note, with the question and passage it answered. */
	async saveAnswerAsNote(index) {
		let answer = this.state.turns[index];
		if (!answer || answer.role != "assistant") return;
		let question = this.state.turns.slice(0, index).reverse().find((t) => t.role == "user");
		let [passage, questionText] = this._splitQuestion(question?.text || "");
		try {
			let target = await this._noteTarget();
			if (!target) {
				this.set({ error: "Notes can't be added here." });
				return;
			}
			let note = new Zotero.Item("note");
			note.libraryID = target.libraryID;
			if (target.parentKey) note.parentKey = target.parentKey;
			note.setNote(ZoteroAI.Notes.answerNote({
				question: questionText,
				passage,
				answerMarkdown: answer.text,
				documentTitle: this._documentTitle || "your library",
				location: target.location,
				modelName: this.state.selectedModel?.displayName,
			}));
			if (target.collectionID) note.addToCollection(target.collectionID);
			await note.saveTx();
			this.set({ savedAnswers: [...this.state.savedAnswers, index] });
		}
		catch (e) {
			Zotero.logError(e);
			this.set({ error: "The answer couldn't be saved as a note." });
		}
	}

	async _noteTarget() {
		if (this.scope.kind == "reader") {
			let ctx = await this.scope.readerContext(false);
			if (!ctx.parentKey || !ctx.editable) return null;
			return { libraryID: ctx.libraryID, parentKey: ctx.parentKey, location: ctx.location?.replace(/ \(.*$/, "") || null };
		}
		let ctx = this.scope.libraryContext();
		if (!ctx.editable) return null;
		return { libraryID: ctx.libraryID, parentKey: null, collectionID: ctx.collectionID || null, location: null };
	}

	/** Separates the quoted passage from the question in a user message. */
	_splitQuestion(text) {
		// Blank lines are lost when a conversation comes back from a note, so allow any newlines.
		let m = /^About this passage:\n"([\s\S]*?)"\n+([\s\S]*)$/.exec(text);
		return m ? [m[1], m[2]] : [null, text];
	}

	_effortFor(model) {
		if (!model.reasoningEfforts.length) return null;
		let saved = ZoteroAI.pref("effort." + model.slug);
		if (saved && model.reasoningEfforts.includes(saved)) return saved;
		if (model.defaultReasoningEffort && model.reasoningEfforts.includes(model.defaultReasoningEffort)) return model.defaultReasoningEffort;
		return model.reasoningEfforts[Math.floor(model.reasoningEfforts.length / 2)];
	}

	async loadModels() {
		if (this._loadingModels) return;
		this._loadingModels = true;
		try {
			let account = ZoteroAI.Auth.state().email;
			let models = await ZoteroAI.API.models();
			this._modelsFor = account;
			let saved = ZoteroAI.pref("model");
			let selected = models.find((m) => m.slug == saved) || models[0] || null;
			this.set({
				models,
				selectedModel: selected,
				reasoningEffort: selected ? this._effortFor(selected) : null,
				error: models.length ? this.state.error : "No ChatGPT models are available for this account.",
			});
		}
		catch (e) {
			Zotero.logError(e);
			this.set({ error: e.message || "Couldn't load ChatGPT models." });
		}
		finally {
			this._loadingModels = false;
		}
	}

	/** Title, metadata and the most recent conversation (possibly from another device). */
	async _loadDocumentAndHistory() {
		if (this.scope.kind == "reader") {
			let ctx = await this.scope.readerContext(false);
			this._documentTitle = ctx.documentTitle;
			this._itemContext = ctx.itemContext;
			this.set({ canSave: !!ctx.parentKey && ctx.editable });
		}
		else {
			this.set({ canSave: this.scope.libraryContext().editable });
		}
		let notes = await this._chatNotes();
		this.set({ history: notes.map((n) => this._historyEntry(n)) });
		// Reader chats pick up where they left off; library chats start fresh.
		if (this.scope.kind == "reader" && notes.length && !this.state.turns.length) {
			let html = notes[0].getNote();
			let turns = ZoteroAI.Notes.decode(html);
			if (turns.length) {
				this._noteHTML = html;
				this.set({ turns, noteKey: notes[0].key });
			}
		}
	}

	_historyEntry(note) {
		let first = ZoteroAI.Notes.decode(note.getNote()).find((t) => t.role == "user")?.text || "";
		let [, question] = this._splitQuestion(first);
		let title = question.split("\n").find((l) => l.trim())?.trim() || "Untitled chat";
		if (title.length > 60) title = title.slice(0, 57) + "…";
		return { noteKey: note.key, title };
	}

	/** Conversation notes for this scope, most recently modified first. */
	async _chatNotes() {
		let byModified = (a, b) => (a.dateModified < b.dateModified ? 1 : a.dateModified > b.dateModified ? -1 : 0);
		if (this.scope.kind == "reader") {
			let ctx = await this.scope.readerContext(false);
			if (!ctx.parentKey) return [];
			let parent = Zotero.Items.getByLibraryAndKey(ctx.libraryID, ctx.parentKey);
			if (!parent) return [];
			return Zotero.Items.get(parent.getNotes(false))
				.filter((n) => !n.deleted && ZoteroAI.Notes.isChatNote(n.getNoteTitle(), n.getNote()))
				.filter((n) => {
					let att = ZoteroAI.Notes.attachmentKey(n.getNote());
					return !att || att == ctx.attachmentKey;
				})
				.sort(byModified);
		}
		let ctx = this.scope.libraryContext();
		let all = await Zotero.Items.getAll(ctx.libraryID, true, false);
		return all.filter((i) => i.isNote() && !i.deleted && ZoteroAI.Notes.isLibraryChat(i.getNote())).sort(byModified);
	}

	async _saveChat(turns, conversation) {
		let libraryID, parentKey = null, html, tags = [];
		if (this.scope.kind == "reader") {
			let ctx = await this.scope.readerContext(false);
			if (!ctx.parentKey || !ctx.editable) return;
			libraryID = ctx.libraryID;
			parentKey = ctx.parentKey;
			html = ZoteroAI.Notes.encodeReader(this._documentTitle, ctx.attachmentKey, turns);
		}
		else {
			let ctx = this.scope.libraryContext();
			if (!ctx.editable) return;
			libraryID = ctx.libraryID;
			html = ZoteroAI.Notes.encodeLibrary(turns);
			tags = [ZoteroAI.Notes.LIBRARY_CHAT_TAG];
		}
		try {
			let note = this.state.noteKey && Zotero.Items.getByLibraryAndKey(libraryID, this.state.noteKey);
			if (note && (note.deleted || note.getNote() != this._noteHTML)) {
				// The note changed on another device while we were answering: keep both versions.
				note = null;
			}
			let isNew = !note;
			if (isNew) {
				note = new Zotero.Item("note");
				note.libraryID = libraryID;
				if (parentKey) note.parentKey = parentKey;
				for (let t of tags) note.addTag(t, 0);
			}
			note.setNote(html);
			await note.saveTx();
			// New chat / another chat opened meanwhile: this conversation is saved, but isn't shown.
			if (conversation !== this._conversation) return;
			this._noteHTML = note.getNote();
			this.set({ noteKey: note.key });
			if (isNew) await this.refreshHistory();
		}
		catch (e) {
			Zotero.logError(e);
			this.set({ error: "The conversation couldn't be saved to a note." });
		}
	}

	/** The context block (ai/spec/chat.md) for this scope. */
	async _buildContext() {
		if (this.scope.kind == "reader") {
			let ctx = await this.scope.readerContext(true);
			let out = "The user is reading this item in Zotero:\n";
			out += (this._itemContext || `Title: ${this._documentTitle}`) + "\n";
			out += `Item key: ${ctx.parentKey || ctx.attachmentKey}\n`;
			let text = ctx.documentText;
			if (!text || !text.trim()) {
				out += "\nThe document's text isn't available, so answer from the conversation and any images.\n";
			}
			else {
				let truncated = text.length > ZoteroAI.ChatSession.MAX_DOCUMENT_CHARS;
				// The document can't close its own wrapper (ai/spec/chat.md).
				let body = text.slice(0, ZoteroAI.ChatSession.MAX_DOCUMENT_CHARS).replace(/<(\/?)document>/gi, "<$1 document>");
				out += `\nFull text of the document${truncated ? " (truncated)" : ""}:\n<document>\n${body}\n</document>\n`;
			}
			// Kept after the document so the long, stable prefix can be cached between requests.
			if (ctx.location) out += `\nThey are currently at ${ctx.location}.`;
			return out;
		}
		let ctx = this.scope.libraryContext();
		let out = "The user is browsing their Zotero library.\n";
		if (ctx.collectionKey) out += `Current collection: ${ctx.collectionName} (${ctx.collectionKey})\n`;
		else if (ctx.viewName) out += `Current view: ${ctx.viewName}\n`;
		let selected = ctx.selectedKeys.slice(0, 50);
		if (selected.length) {
			out += "Selected items: " + selected.map((key) => {
				let item = Zotero.Items.getByLibraryAndKey(ctx.libraryID, key);
				let title = item ? ZoteroAI.Tools._title(item) : "";
				return title ? `${key} “${title}”` : key;
			}).join(", ") + "\n";
		}
		return out;
	}
};
