/*
 * Puts the chat panel into Zotero's main windows: a pane to the right of the tabs, a toolbar
 * button, and "Ask AI" in the reader's text-selection popup. The panel follows the selected tab:
 * the library tab gets the library chat, each reader tab its own reader chat.
 */

// What the library chat is told while the user looks at a group library (same text on Android).
ZoteroAI.GROUP_VIEW = "a group library (the tools only reach My Library)";

ZoteroAI.Windows = {
	_windows: new Map(), // window → {panel, pane, splitter, button}
	_sessions: new Map(), // "library:<id>" | "reader:<tabID>" → ChatSession
	_texts: new Map(), // attachment item ID → full text promise
	_notifierID: null,

	startup() {
		ZoteroAI.Auth.init().catch((e) => Zotero.logError(e));
		this._notifierID = Zotero.Notifier.registerObserver({
			notify: (event, type, ids) => {
				if (type == "tab") {
					// A restored reader tab only gets its reader when it loads.
					if (["select", "load", "add"].includes(event)) this._refreshAll();
					// "close" passes the closed tabs' IDs as one nested array.
					else if (event == "close") for (let id of ids.flat()) this._dropSession("reader:" + id);
				}
				else this._notesChanged(event, ids);
			},
		}, ["tab", "item"], "zotero-ai");
		this._onPopup = (event) => this._renderSelectionPopup(event);
		Zotero.Reader.registerEventListener("renderTextSelectionPopup", this._onPopup, ZoteroAI.id);
	},

	/** A sync (or the note editor) changed notes: open chats pick up their note and history. */
	_notesChanged(event, ids) {
		if (!["add", "modify", "delete", "trash", "refresh"].includes(event)) return;
		let gone = event == "delete";
		let chatNotes = gone || Zotero.Items.get(ids).some((i) => i.isNote() && ZoteroAI.Notes.isChatNote(i.getNoteTitle(), i.getNote()));
		if (!chatNotes) return;
		clearTimeout(this._notesTimer);
		this._notesTimer = setTimeout(() => {
			for (let session of this._sessions.values()) {
				session.noteChanged();
				if (!session.state.isSending) session.refreshHistory().catch((e) => Zotero.logError(e));
			}
		}, 300);
	},

	shutdown() {
		clearTimeout(this._notesTimer);
		if (this._notifierID) Zotero.Notifier.unregisterObserver(this._notifierID);
		Zotero.Reader.unregisterEventListener?.("renderTextSelectionPopup", this._onPopup);
		for (let key of [...this._sessions.keys()]) this._dropSession(key);
		this._texts.clear();
		ZoteroAI.Auth.unregister();
	},

	add(win) {
		if (this._windows.has(win)) return;
		let doc = win.document;
		let deck = doc.getElementById("tabs-deck");
		if (!deck) return;
		let splitter = doc.createXULElement("splitter");
		splitter.id = "zotero-ai-splitter";
		splitter.setAttribute("resizebefore", "sibling");
		splitter.setAttribute("resizeafter", "none");
		let pane = doc.createXULElement("vbox");
		pane.id = "zotero-ai-pane";
		pane.setAttribute("width", ZoteroAI.pref("panelWidth") || 400);
		pane.style.minWidth = "300px";
		// Long answers (wide tables, code) must not widen the pane; it scrolls instead.
		pane.style.width = (ZoteroAI.pref("panelWidth") || 400) + "px";
		pane.style.maxWidth = "70vw";
		let panel = new ZoteroAI.Panel(doc, {
			close: () => this.setOpen(win, false),
			openConnect: () => ZoteroAI.ConnectDialog.open(win),
			openMcpServers: () => ZoteroAI.McpServersDialog.open(win),
			attachPage: () => this._capturePage(win),
			selectArea: () => this._captureArea(win),
		});
		panel.element.style.height = "100%";
		pane.append(panel.element);
		deck.after(splitter, pane);

		let button = doc.createXULElement("toolbarbutton");
		button.id = "zotero-ai-button";
		button.setAttribute("label", "✨ " + ZoteroAI.STRINGS.title);
		button.setAttribute("tooltiptext", ZoteroAI.STRINGS.title);
		button.className = "zotero-tb-button";
		button.addEventListener("command", () => this.setOpen(win, pane.hidden));
		let toolbar = doc.getElementById("zotero-tabs-toolbar");
		toolbar?.append(button);

		// Tools → Connect an agent… (MCP), reachable without opening the panel or signing in.
		let toolsMenuItem = doc.createXULElement("menuitem");
		toolsMenuItem.id = "zotero-ai-connect-agent";
		toolsMenuItem.setAttribute("label", "Zotero AI: Connect an agent…");
		toolsMenuItem.addEventListener("command", () => ZoteroAI.ConnectDialog.open(win));
		doc.getElementById("menu_ToolsPopup")?.append(toolsMenuItem);
		let semanticMenuItem = doc.createXULElement("menuitem");
		semanticMenuItem.id = "zotero-ai-semantic-search";
		semanticMenuItem.setAttribute("label", "Zotero AI: Semantic search…");
		semanticMenuItem.addEventListener("command", () => ZoteroAI.SemanticDialog.open(win));
		toolsMenuItem.after(semanticMenuItem);

		let entry = { panel, pane, splitter, button, toolsMenuItem, semanticMenuItem };
		this._windows.set(win, entry);
		this.setOpen(win, !!ZoteroAI.pref("panelOpen"));
		ZoteroAI.ApprovalSheet?.windowsChanged();
	},

	remove(win) {
		let entry = this._windows.get(win);
		if (!entry) return;
		if (!entry.pane.hidden) ZoteroAI.pref("panelWidth", entry.pane.getBoundingClientRect().width | 0);
		entry.panel.destroy();
		entry.pane.remove();
		entry.splitter.remove();
		entry.button.remove();
		entry.toolsMenuItem.remove();
		entry.semanticMenuItem.remove();
		this._windows.delete(win);
		if (ZoteroAI.ConnectDialog._open?.win === win) ZoteroAI.ConnectDialog.close();
		if (ZoteroAI.SemanticDialog._open?.win === win) ZoteroAI.SemanticDialog.close();
		ZoteroAI.ApprovalSheet?.windowsChanged();
	},

	isOpen(win) {
		let entry = this._windows.get(win);
		return !!entry && !entry.pane.hidden;
	},

	setOpen(win, open) {
		let entry = this._windows.get(win);
		if (!entry) return;
		entry.pane.hidden = !open;
		entry.splitter.hidden = !open;
		entry.button.checked = open;
		ZoteroAI.pref("panelOpen", open);
		if (open) {
			this._refresh(win);
			entry.panel.focusInput();
		}
	},

	/** The session the panel shows for the window's selected tab. */
	sessionFor(win) {
		let tabs = win.Zotero_Tabs;
		let tabID = tabs?.selectedID;
		let reader = tabID && Zotero.Reader.getByTabID(tabID);
		if (reader) return this._session("reader:" + tabID, () => this._readerScope(reader));
		// Library chats are about My Library, the only library the tools reach (ai/spec/tools.md).
		return this._session("library", () => this._libraryScope());
	},

	_session(key, scope) {
		let session = this._sessions.get(key);
		if (!session) {
			session = new ZoteroAI.ChatSession(scope());
			this._sessions.set(key, session);
		}
		return session;
	},

	_dropSession(key) {
		let session = this._sessions.get(key);
		if (!session) return;
		session.destroy();
		this._sessions.delete(key);
	},

	_refreshAll() {
		for (let win of this._windows.keys()) this._refresh(win);
	},

	_refresh(win) {
		let entry = this._windows.get(win);
		if (!entry || entry.pane.hidden) return;
		entry.panel.attach(this.sessionFor(win));
	},

	// region Scopes

	_libraryScope() {
		let libraryID = Zotero.Libraries.userLibraryID;
		return {
			kind: "library",
			libraryContext: () => {
				// The window the user is looking at now (on macOS the main window can be reopened).
				let pane = Zotero.getActiveZoteroPane();
				// Zotero 10 allows several selected rows; Zotero 7 has getCollectionTreeRow().
				let row = pane?.getCollectionTreeRows ? pane.getCollectionTreeRows()[0] : pane?.getCollectionTreeRow?.();
				let ctx = {
					libraryID,
					editable: Zotero.Libraries.get(libraryID)?.editable ?? false,
					collectionKey: null, collectionID: null, collectionName: null, viewName: null,
					selectedKeys: [],
				};
				if (!row) return ctx;
				let rowLibraryID = row.ref?.libraryID;
				if (rowLibraryID != null && rowLibraryID != libraryID) {
					ctx.viewName = ZoteroAI.GROUP_VIEW;
					return ctx;
				}
				let collection = row.isCollection() ? row.ref : null;
				ctx.collectionKey = collection?.key || null;
				ctx.collectionID = collection?.id || null;
				ctx.collectionName = collection?.name || null;
				// Same name as Android's root view.
				ctx.viewName = collection ? null : row.isLibrary() ? "All Items" : row.getName();
				ctx.selectedKeys = (pane.getSelectedItems() || []).filter((i) => i.libraryID == libraryID).map((i) => i.key);
				return ctx;
			},
		};
	},

	_readerScope(reader) {
		return {
			kind: "reader",
			reader,
			libraryID: Zotero.Items.get(reader.itemID)?.libraryID,
			readerContext: async (includeText) => {
				let attachment = Zotero.Items.get(reader.itemID);
				let parent = attachment?.parentItem || null;
				let ctx = {
					libraryID: attachment.libraryID,
					attachmentKey: attachment.key,
					parentKey: parent?.key || null,
					editable: Zotero.Libraries.get(attachment.libraryID)?.editable ?? false,
					documentTitle: (parent || attachment).getDisplayTitle(),
					itemContext: parent ? this._itemContext(parent) : `Title: ${attachment.getDisplayTitle()}`,
					location: this._location(reader),
					documentText: null,
				};
				if (includeText) ctx.documentText = await this._documentText(attachment);
				return ctx;
			},
		};
	},

	/** Same lines as ReadChatItemContextDbRequest on Android. */
	_itemContext(item) {
		let field = (name) => {
			// Base-mapped, so a conference paper's proceedings count as "Published in".
			try { return String(item.getField(name, false, true) || "").trim() || null; }
			catch (e) { return null; }
		};
		let creators = item.getCreators().map((c) => (c.fieldMode == 1 ? c.lastName : `${c.firstName || ""} ${c.lastName || ""}`).trim()).join(", ");
		let tags = item.getTags().map((t) => t.tag).sort();
		let lines = [`Title: ${item.getDisplayTitle()}`];
		if (creators) lines.push(`Authors: ${creators}`);
		if (field("date")) lines.push(`Date: ${field("date")}`);
		if (field("publicationTitle")) lines.push(`Published in: ${field("publicationTitle")}`);
		if (field("DOI")) lines.push(`DOI: ${field("DOI")}`);
		if (tags.length) lines.push(`Tags: ${tags.join(", ")}`);
		if (field("abstractNote")) lines.push(`Abstract: ${field("abstractNote")}`);
		return lines.join("\n");
	},

	/** "Page 3 (6%)", like the page indicator on Android. */
	_location(reader) {
		let stats = reader._internalReader?._state?.primaryViewStats;
		if (!stats || stats.pageIndex == null || !stats.pagesCount) return null;
		let page = stats.pageLabel || String(stats.pageIndex + 1);
		let prefix = stats.usePhysicalPageNumbers === false ? "Location" : "Page";
		return `${prefix} ${page} (${Math.round(stats.pageIndex / stats.pagesCount * 100)}%)`;
	},

	_documentText(attachment) {
		let cached = this._texts.get(attachment.id);
		if (!cached) {
			cached = this._loadText(attachment).catch((e) => {
				Zotero.logError(e);
				this._texts.delete(attachment.id);
				return null;
			});
			this._texts.set(attachment.id, cached);
		}
		return cached;
	},

	async _loadText(attachment) {
		let file = Zotero.Fulltext.getItemCacheFile(attachment);
		if (file && await IOUtils.exists(file.path)) {
			let text = await IOUtils.readUTF8(file.path);
			if (text.trim()) return text;
		}
		if (attachment.isPDFAttachment()) {
			let result = await Zotero.PDFWorker.getFullText(attachment.id, null, true);
			return result?.text || null;
		}
		return null;
	},

	// endregion

	// region Reader hooks

	_renderSelectionPopup({ reader, doc, params, append }) {
		let text = params?.annotation?.text || "";
		let win = reader._window;
		// Readers in their own window have no tab here and no panel next to them.
		if (!text.trim() || !win || !reader.tabID || !this._windows.has(win)) return;
		let session = this._sessions.get("reader:" + reader.tabID);
		// Select-to-chat: with the panel open, the selection is attached as it's made.
		if (session && this.isOpen(win) && session.state.autoAttachSelection) session.setQuote(text);
		let button = doc.createElement("button");
		button.textContent = "✨ " + ZoteroAI.STRINGS.title;
		button.className = "toolbar-button wide-button";
		button.addEventListener("click", () => this.askAbout(win, text));
		append(button);
	},

	/** Opens the panel on the selected reader with `text` attached. */
	askAbout(win, text) {
		this.setOpen(win, true);
		let session = this.sessionFor(win);
		session.setQuote(text);
		this._windows.get(win)?.panel.focusInput();
	},

	_selectedReader(win) {
		return Zotero.Reader.getByTabID(win.Zotero_Tabs.selectedID);
	},

	async _capturePage(win) {
		let reader = this._selectedReader(win);
		let where = this._location(reader)?.replace(/ \(.*$/, "") || "the current view";
		return ZoteroAI.Capture.capture(reader, null, where);
	},

	async _captureArea(win) {
		let reader = this._selectedReader(win);
		let where = this._location(reader)?.replace(/ \(.*$/, "") || "the current view";
		return ZoteroAI.Capture.selectArea(reader, `area on ${where}`);
	},

	// endregion
};
