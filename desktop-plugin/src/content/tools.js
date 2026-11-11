/*
 * The Zotero tools the AI can call, run in-process against the local library. Semantics and result
 * shapes follow ai/spec/tools.md exactly (the Android app implements the same contract), and
 * ai/conformance/run.py diffs the two.
 */

class ZoteroAIToolError extends Error {
	constructor(code, message) {
		super(message);
		this.code = code;
	}
}

ZoteroAI.Tools = {
	get libraryID() {
		return Zotero.Libraries.userLibraryID;
	},

	/**
	 * Runs a tool. Never throws: failures come back as error objects. Writes run only with the
	 * capability that ZoteroAI.Approvals holds, i.e. after a decision there (approvals.js).
	 */
	async execute(name, args, capability) {
		try {
			let fn = this._tools[name];
			if (!fn) throw new ZoteroAIToolError("invalid_argument", `Unknown tool ${name}`);
			let checked = ZoteroAI.Spec.validateArgs(name, args);
			if (checked.error) return checked;
			if (ZoteroAI.Spec.isWrite(name) && !ZoteroAI.Approvals.authorizes(capability)) {
				throw new ZoteroAIToolError("declined", "Changes need the user's approval.");
			}
			return await fn.call(this, checked.args);
		}
		catch (e) {
			if (e instanceof ZoteroAIToolError) return { error: e.code, message: e.message };
			Zotero.logError(e);
			return { error: "failed", message: e.message || String(e) };
		}
	},

	/** One line describing what a write would change, for the approval card. */
	async describeWrite(name, args) {
		let titles = (keys) => {
			let names = (keys || []).map((key) => {
				let item = Zotero.Items.getByLibraryAndKey(this.libraryID, key);
				return item ? `“${this._title(item)}”` : key;
			});
			return names.length <= 3 ? names.join(", ") : names.slice(0, 3).join(", ") + ` and ${names.length - 3} more`;
		};
		let collections = (keys) => (keys || []).map((key) => {
			let c = Zotero.Collections.getByLibraryAndKey(this.libraryID, key);
			return c ? `“${c.name}”` : key;
		}).join(", ");
		// "remove …" starts the sentence when nothing is added.
		let sentence = (parts) => { let t = parts.join(" and "); return t.charAt(0).toUpperCase() + t.slice(1); };
		try {
			switch (name) {
				case "zotero_batch_update_tags": {
					let parts = [];
					let add = args.add_tags || [], remove = args.remove_tags || [];
					if (add.length) parts.push(`Add tag${add.length > 1 ? "s" : ""} ${add.join(", ")}`);
					if (remove.length) parts.push(`remove ${remove.join(", ")}`);
					return sentence(parts) + ` on ${titles(args.item_keys)}`;
				}
				case "zotero_set_item_collections": {
					let parts = [];
					if ((args.add_to || []).length) parts.push(`Add to ${collections(args.add_to)}`);
					if ((args.remove_from || []).length) parts.push(`remove from ${collections(args.remove_from)}`);
					return sentence(parts) + `: ${titles(args.item_keys)}`;
				}
				case "zotero_create_collection":
					return `Create collection “${args.name}”` + (args.parent_collection_key ? ` inside ${collections([args.parent_collection_key])}` : "");
				case "zotero_create_note":
					return `Create note “${args.title}”` + (args.parent_item_key ? ` on ${titles([args.parent_item_key])}` : " (standalone)");
				case "zotero_add_item_by_identifier":
					return `Add ${args.identifier} to the library` + (args.collection_key ? ` in ${collections([args.collection_key])}` : "");
				case "zotero_trash_items":
					return `Move to trash: ${titles(args.item_keys)}`;
			}
		}
		catch (e) {
			Zotero.logError(e);
		}
		return name;
	},

	// region Helpers

	_str(args, name) {
		let v = args[name];
		v = typeof v == "string" ? v.trim() : "";
		if (!v) throw new ZoteroAIToolError("invalid_argument", `Missing ${name}.`);
		return v;
	},

	_strOrNull(args, name) {
		let v = args[name];
		v = typeof v == "string" ? v.trim() : "";
		return v || null;
	},

	_strList(args, name) {
		let v = args[name];
		return Array.isArray(v) ? v.filter((x) => typeof x == "string") : [];
	},

	_limit(args, name, def, max) {
		let v = args[name];
		if (typeof v != "number" || !Number.isFinite(v)) return def;
		return Math.min(Math.max(Math.trunc(v), 1), max);
	},

	_item(key) {
		let item = Zotero.Items.getByLibraryAndKey(this.libraryID, key);
		if (!item) throw new ZoteroAIToolError("not_found", `No item with key ${key}.`);
		return item;
	},

	_collection(key) {
		let c = Zotero.Collections.getByLibraryAndKey(this.libraryID, key);
		if (!c || c.deleted) throw new ZoteroAIToolError("not_found", `No collection with key ${key}.`);
		return c;
	},

	_isRegular(item) {
		return item.isRegularItem() && !item.deleted;
	},

	async _regularItems() {
		let items = await Zotero.Items.getAll(this.libraryID, false, false);
		return items.filter((i) => this._isRegular(i));
	},

	_title(item) {
		if (item.isNote()) return (item.getNoteTitle() || "").trim();
		return (item.getField("title", false, true) || "").trim();
	},

	_field(item, name) {
		try {
			return String(item.getField(name, false, true) || "").trim();
		}
		catch (e) {
			return "";
		}
	},

	_year(item) {
		return ZoteroAI.year(this._field(item, "date"));
	},

	_tagNames(item) {
		return [...new Set(item.getTags().map((t) => t.tag))];
	},

	_sortedTags(item) {
		return this._tagNames(item).sort((a, b) => ZoteroAI.compareText(a, b) || (a < b ? -1 : a > b ? 1 : 0));
	},

	_titleOrder(a, b) {
		return ZoteroAI.compareText(this._title(a), this._title(b)) || (a.key < b.key ? -1 : a.key > b.key ? 1 : 0);
	},

	_creatorSummary(item) {
		let creators = item.getCreators();
		let primaryID = Zotero.CreatorTypes.getPrimaryIDForType(item.itemTypeID);
		let primary = creators.filter((c) => c.creatorTypeID == primaryID);
		if (!primary.length) primary = creators;
		let names = primary.map((c) => (c.lastName || "").trim() || (c.firstName || "").trim());
		if (names.length == 0) return "";
		if (names.length == 1) return names[0];
		if (names.length == 2) return `${names[0]} and ${names[1]}`;
		return `${names[0]} et al.`;
	},

	_summary(item) {
		return {
			key: item.key,
			itemType: item.itemType,
			title: this._title(item),
			creators: this._creatorSummary(item),
			year: this._year(item),
			tags: this._sortedTags(item),
		};
	},

	_searchable(item) {
		let out = [this._title(item).toLowerCase()];
		for (let c of item.getCreators()) {
			out.push((c.firstName || "").toLowerCase(), (c.lastName || "").toLowerCase());
		}
		out.push(this._year(item));
		for (let t of this._tagNames(item)) out.push(t.toLowerCase());
		return out;
	},

	_textAttachment(item) {
		let types = ["application/pdf", "application/epub+zip", "text/html"];
		let candidates = item.isAttachment() ? [item]
			: Zotero.Items.get(item.getAttachments(false)).filter((a) => !a.deleted);
		return candidates
			.filter((a) => types.includes(a.attachmentContentType))
			.sort((a, b) => (types.indexOf(a.attachmentContentType) - types.indexOf(b.attachmentContentType))
				|| this._titleOrder(a, b))[0] || null;
	},

	// endregion

	_tools: {
		async zotero_search_items(args) {
			let words = (typeof args.query == "string" ? args.query : "").toLowerCase().split(/\s+/).filter(Boolean);
			let tag = this._strOrNull(args, "tag");
			let collectionKey = this._strOrNull(args, "collection_key");
			let limit = this._limit(args, "limit", 20, 100);
			let items = (await this._regularItems()).filter((item) => {
				let texts = this._searchable(item);
				return words.every((w) => texts.some((t) => t.includes(w)));
			});
			if (tag != null) items = items.filter((i) => this._tagNames(i).includes(tag));
			if (collectionKey != null) {
				items = items.filter((i) => i.getCollections().some((id) => {
					let c = Zotero.Collections.get(id);
					return c && !c.deleted && c.key == collectionKey;
				}));
			}
			items.sort((a, b) => this._titleOrder(a, b));
			return { total: items.length, items: items.slice(0, limit).map((i) => this._summary(i)) };
		},

		async zotero_get_item_metadata(args) {
			let item = this._item(this._str(args, "item_key"));
			let creatorName = (c) => c.fieldMode == 1 ? (c.lastName || "").trim()
				: `${(c.firstName || "").trim()} ${(c.lastName || "").trim()}`.trim();
			let publication = ["publicationTitle", "proceedingsTitle", "bookTitle", "websiteTitle"]
				.map((f) => this._field(item, f)).find((v) => v) || "";
			let attachments = item.isRegularItem() ? Zotero.Items.get(item.getAttachments(false)).filter((a) => !a.deleted) : [];
			let notes = item.isRegularItem() ? Zotero.Items.get(item.getNotes(false)).filter((n) => !n.deleted) : [];
			return {
				key: item.key,
				itemType: item.itemType,
				title: this._title(item),
				creators: item.getCreators().map((c) => ({
					type: Zotero.CreatorTypes.getName(c.creatorTypeID),
					name: creatorName(c),
				})),
				date: this._field(item, "date"),
				year: this._year(item),
				publication,
				doi: this._field(item, "DOI"),
				url: this._field(item, "url"),
				abstract: this._field(item, "abstractNote"),
				tags: this._sortedTags(item),
				collections: item.getCollections().map((id) => Zotero.Collections.get(id))
					.filter((c) => c && !c.deleted)
					.sort((a, b) => ZoteroAI.compareText(a.name, b.name) || (a.key < b.key ? -1 : 1))
					.map((c) => ({ key: c.key, name: c.name })),
				attachments: attachments.sort((a, b) => this._titleOrder(a, b)).map((a) => ({
					key: a.key, title: this._title(a), contentType: a.attachmentContentType || "",
				})),
				notes: notes.sort((a, b) => this._titleOrder(a, b)).map((n) => ({ key: n.key, title: this._title(n) })),
			};
		},

		async zotero_get_item_fulltext(args) {
			let maxChars = this._limit(args, "max_chars", 20000, 100000);
			let attachment = this._textAttachment(this._item(this._str(args, "item_key")));
			if (!attachment) throw new ZoteroAIToolError("not_found", "This item has no PDF, EPUB or snapshot.");
			let file = Zotero.Fulltext.getItemCacheFile(attachment);
			let text = null;
			try {
				if (file && await IOUtils.exists(file.path)) text = await IOUtils.readUTF8(file.path);
			}
			catch (e) {
				Zotero.logError(e);
			}
			// Not indexed yet (e.g. just synced): extract it, as Zotero's indexer would.
			if (text == null && attachment.isPDFAttachment() && await attachment.fileExists()) {
				try {
					text = (await Zotero.PDFWorker.getFullText(attachment.id, null, true))?.text ?? null;
				}
				catch (e) {
					Zotero.logError(e);
				}
			}
			if (text == null) throw new ZoteroAIToolError("unavailable", "This attachment's text hasn't been indexed yet.");
			return {
				attachmentKey: attachment.key,
				totalChars: text.length,
				truncated: text.length > maxChars,
				text: text.slice(0, maxChars),
			};
		},

		async zotero_get_pdf_info(args) {
			let attachment = ZoteroAI.Pdf.attachment(this._item(this._str(args, "item_key")));
			let pageCount = await ZoteroAI.Pdf.pageCount(async () => (await ZoteroAI.Pdf.read(attachment)).bytes);
			return { attachmentKey: attachment.key, pageCount };
		},

		// The image travels beside the result, as MCP content parts under _content (mcp.md, chat.md).
		async zotero_get_page_image(args) {
			let attachment = ZoteroAI.Pdf.attachment(this._item(this._str(args, "item_key")));
			let image = await ZoteroAI.Pdf.render(async () => (await ZoteroAI.Pdf.read(attachment)).bytes, args.page, args.region, args.max_width);
			return {
				attachmentKey: attachment.key,
				page: args.page,
				pageCount: image.pageCount,
				width: image.width,
				height: image.height,
				mimeType: "image/jpeg",
				_content: [{ type: "image", data: image.data, mimeType: "image/jpeg" }],
			};
		},

		async zotero_get_pdf_file(args) {
			let attachment = ZoteroAI.Pdf.attachment(this._item(this._str(args, "item_key")));
			let { bytes, filename } = await ZoteroAI.Pdf.read(attachment, ZoteroAI.Pdf.MAX_RESOURCE_BYTES);
			return {
				attachmentKey: attachment.key,
				filename,
				size: bytes.length,
				_content: [{ type: "resource", resource: { uri: `zotero://attachment/${attachment.key}`, mimeType: "application/pdf", blob: ZoteroAI.Pdf.base64(bytes) } }],
			};
		},

		async zotero_get_collections() {
			let collections = Zotero.Collections.getByLibrary(this.libraryID, true).filter((c) => !c.deleted);
			collections.sort((a, b) => ZoteroAI.compareText(a.name, b.name) || (a.key < b.key ? -1 : 1));
			return {
				collections: collections.map((c) => ({
					key: c.key,
					name: c.name,
					parentKey: c.parentKey || null,
					itemCount: c.getChildItems(false, false).filter((i) => this._isRegular(i)).length,
				})),
			};
		},

		async zotero_get_collection_items(args) {
			let collection = this._collection(this._str(args, "collection_key"));
			let limit = this._limit(args, "limit", 50, 100);
			let items = collection.getChildItems(false, false).filter((i) => this._isRegular(i));
			items.sort((a, b) => this._titleOrder(a, b));
			return {
				collection: { key: collection.key, name: collection.name },
				total: items.length,
				items: items.slice(0, limit).map((i) => this._summary(i)),
			};
		},

		async zotero_get_tags(args) {
			let limit = this._limit(args, "limit", 200, 500);
			let counts = new Map();
			for (let item of await this._regularItems()) {
				for (let t of this._tagNames(item)) counts.set(t, (counts.get(t) || 0) + 1);
			}
			let sorted = [...counts.entries()].sort((a, b) => (b[1] - a[1])
				|| ZoteroAI.compareText(a[0], b[0]) || (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
			return { total: sorted.length, tags: sorted.slice(0, limit).map(([tag, count]) => ({ tag, count })) };
		},

		async zotero_get_recent(args) {
			let limit = this._limit(args, "limit", 10, 100);
			let items = await this._regularItems();
			items.sort((a, b) => (a.dateAdded < b.dateAdded ? 1 : a.dateAdded > b.dateAdded ? -1 : 0)
				|| (a.key < b.key ? -1 : 1));
			return {
				items: items.slice(0, limit).map((i) => ({ ...this._summary(i), dateAdded: ZoteroAI.isoFromSQL(i.dateAdded) })),
			};
		},

		async zotero_get_notes(args) {
			let parentKey = this._strOrNull(args, "item_key");
			let query = this._strOrNull(args, "query")?.toLowerCase() || null;
			let limit = this._limit(args, "limit", 20, 50);
			if (parentKey != null) this._item(parentKey);
			let all = await Zotero.Items.getAll(this.libraryID, false, false);
			let notes = all.filter((i) => i.isNote() && !i.deleted)
				.map((n) => ({ note: n, text: ZoteroAI.plainText(n.getNote()) }))
				.filter(({ note, text }) => (parentKey == null || note.parentKey == parentKey)
					&& (query == null || text.toLowerCase().includes(query)));
			notes.sort((a, b) => (a.note.dateModified < b.note.dateModified ? 1 : a.note.dateModified > b.note.dateModified ? -1 : 0)
				|| (a.note.key < b.note.key ? -1 : 1));
			return {
				total: notes.length,
				notes: notes.slice(0, limit).map(({ note, text }) => ({
					key: note.key,
					parentKey: note.parentKey || null,
					title: this._title(note),
					text: text.slice(0, 2000),
				})),
			};
		},

		async zotero_get_annotations(args) {
			let item = this._item(this._str(args, "item_key"));
			let attachments = item.isAttachment() ? [item]
				: Zotero.Items.get(item.getAttachments(false)).filter((a) => !a.deleted);
			attachments.sort((a, b) => (a.key < b.key ? -1 : 1));
			let out = [];
			for (let attachment of attachments) {
				if (!attachment.isFileAttachment()) continue;
				let annotations = attachment.getAnnotations(false).filter((a) => !a.deleted);
				annotations.sort((a, b) => ((a.annotationSortIndex || "") < (b.annotationSortIndex || "") ? -1
					: (a.annotationSortIndex || "") > (b.annotationSortIndex || "") ? 1 : 0) || (a.key < b.key ? -1 : 1));
				for (let a of annotations) {
					out.push({
						key: a.key,
						attachmentKey: attachment.key,
						type: a.annotationType || "",
						text: (a.annotationText || "").trim(),
						comment: (a.annotationComment || "").trim(),
						pageLabel: (a.annotationPageLabel || "").trim(),
						color: (a.annotationColor || "").trim(),
						tags: this._sortedTags(a),
					});
				}
			}
			return { annotations: out };
		},

		async zotero_batch_update_tags(args) {
			let keys = [...new Set(this._strList(args, "item_keys"))];
			let add = [...new Set(this._strList(args, "add_tags").map((t) => t.trim()).filter(Boolean))];
			let remove = new Set(this._strList(args, "remove_tags"));
			let items = keys.map((k) => this._item(k));
			let updated = [];
			for (let item of items) {
				let names = this._tagNames(item);
				let changed = false;
				for (let t of names) {
					if (remove.has(t)) { item.removeTag(t); changed = true; }
				}
				for (let t of add) {
					if (!names.includes(t)) { item.addTag(t, 0); changed = true; }
				}
				if (changed) {
					await item.saveTx();
					updated.push(item.key);
				}
			}
			return { updated: updated.sort() };
		},

		async zotero_set_item_collections(args) {
			let keys = [...new Set(this._strList(args, "item_keys"))];
			let items = keys.map((k) => this._item(k));
			let addTo = this._strList(args, "add_to").map((k) => this._collection(k));
			let removeFrom = this._strList(args, "remove_from").map((k) => this._collection(k));
			let updated = new Set();
			for (let item of items) {
				let changed = false;
				for (let c of addTo) {
					if (!item.inCollection(c.id)) { item.addToCollection(c.id); changed = true; }
				}
				for (let c of removeFrom) {
					if (item.inCollection(c.id)) { item.removeFromCollection(c.id); changed = true; }
				}
				if (changed) {
					await item.saveTx();
					updated.add(item.key);
				}
			}
			return { updated: [...updated].sort() };
		},

		async zotero_create_collection(args) {
			let name = this._str(args, "name");
			let parentKey = this._strOrNull(args, "parent_collection_key");
			if (parentKey != null) this._collection(parentKey);
			let collection = new Zotero.Collection({ libraryID: this.libraryID, name, parentKey: parentKey || undefined });
			await collection.saveTx();
			return { key: collection.key, name };
		},

		async zotero_create_note(args) {
			let parentKey = this._strOrNull(args, "parent_item_key");
			if (parentKey != null) this._item(parentKey);
			let note = new Zotero.Item("note");
			note.libraryID = this.libraryID;
			if (parentKey != null) note.parentKey = parentKey;
			// The line break keeps the heading on its own line: Zotero titles a note with its first line.
			note.setNote(`<div data-schema-version="9"><h1>${ZoteroAI.escapeHTML(this._str(args, "title"))}</h1>\n`
				+ ZoteroAI.Notes.toHTML(typeof args.content == "string" ? args.content : "") + "</div>");
			for (let t of this._strList(args, "tags")) note.addTag(t, 0);
			await note.saveTx();
			return { key: note.key };
		},

		async zotero_add_item_by_identifier(args) {
			let identifier = this._str(args, "identifier");
			let collectionKey = this._strOrNull(args, "collection_key");
			let collection = collectionKey != null ? this._collection(collectionKey) : null;
			let identifiers = Zotero.Utilities.extractIdentifiers(identifier);
			if (!identifiers.length) throw new ZoteroAIToolError("invalid_argument", `Not a DOI, ISBN, arXiv ID or PMID: ${identifier}`);
			let translate = new Zotero.Translate.Search();
			translate.setIdentifier(identifiers[0]);
			let translators = await translate.getTranslators();
			if (!translators.length) throw new ZoteroAIToolError("not_found", `Couldn't find metadata for ${identifier}.`);
			translate.setTranslator(translators);
			let items;
			try {
				items = await translate.translate({
					libraryID: this.libraryID,
					collections: collection ? [collection.id] : false,
					saveAttachments: true,
				});
			}
			catch (e) {
				throw new ZoteroAIToolError("not_found", `Couldn't find metadata for ${identifier}.`);
			}
			let item = items && items[0];
			if (!item) throw new ZoteroAIToolError("not_found", `Couldn't find metadata for ${identifier}.`);
			let tags = this._strList(args, "tags");
			if (tags.length) {
				for (let t of tags) item.addTag(t, 0);
				await item.saveTx();
			}
			return { key: item.key, title: this._title(item), itemType: item.itemType };
		},

		async zotero_trash_items(args) {
			let keys = [...new Set(this._strList(args, "item_keys"))];
			let items = keys.map((k) => this._item(k));
			await Zotero.Items.trashTx(items.map((i) => i.id));
			return { trashed: keys.sort() };
		},
	},
};
