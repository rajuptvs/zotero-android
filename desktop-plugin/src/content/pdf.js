/*
 * PDF files and page images (ai/spec/tools.md, "PDF pages and files"). Pages are drawn with the
 * pdf.js that Zotero's reader uses, in a hidden content frame for each request.
 */

ZoteroAI.Pdf = {
	MAX_FILE_BYTES: 200 * 1024 * 1024,
	MAX_RESOURCE_BYTES: 25 * 1024 * 1024,
	MAX_SIDE: 2000,
	JPEG_QUALITY: 0.85,

	/** The clipped region (fractions) and image size for a page of pageWidth × pageHeight points. */
	plan(pageWidth, pageHeight, region, maxWidth) {
		let x0 = 0, y0 = 0, x1 = 1, y1 = 1;
		if (region != null) {
			if (!Array.isArray(region) || region.length != 4 || !(region[2] > 0) || !(region[3] > 0)) {
				throw new ZoteroAIToolError("invalid_argument", "region must be [x, y, width, height] with width and height above 0.");
			}
			let [x, y, w, h] = region;
			x0 = Math.max(0, x);
			y0 = Math.max(0, y);
			x1 = Math.min(1, x + w);
			y1 = Math.min(1, y + h);
			if (x1 <= x0 || y1 <= y0) throw new ZoteroAIToolError("invalid_argument", "region is outside the page.");
		}
		let width = typeof maxWidth == "number" && Number.isFinite(maxWidth)
			? Math.min(Math.max(Math.trunc(maxWidth), 200), this.MAX_SIDE) : 1200;
		let W = (x1 - x0) * pageWidth, H = (y1 - y0) * pageHeight;
		let height = Math.floor(width * H / W + 0.5);
		if (height > this.MAX_SIDE) {
			height = this.MAX_SIDE;
			width = Math.floor(this.MAX_SIDE * W / H + 0.5);
		}
		return { x0, y0, x1, y1, width: Math.max(1, width), height: Math.max(1, height) };
	},

	/** The item's PDF: the key itself if it's one, else its first PDF attachment in title order. */
	attachment(item) {
		let candidates = item.isAttachment() ? [item]
			: Zotero.Items.get(item.getAttachments(false)).filter((a) => !a.deleted);
		let pdf = candidates.filter((a) => a.attachmentContentType == "application/pdf")
			.sort((a, b) => ZoteroAI.Tools._titleOrder(a, b))[0];
		if (!pdf) throw new ZoteroAIToolError("not_found", "This item has no PDF.");
		return pdf;
	},

	/**
	 * → {bytes, filename}; downloads the file through Zotero's file sync if it isn't here yet.
	 * Files over `maxBytes` are refused before they're read.
	 */
	async read(attachment, maxBytes = this.MAX_FILE_BYTES) {
		let path = await attachment.getFilePathAsync();
		if (!path) {
			if (attachment.attachmentLinkMode == Zotero.Attachments.LINK_MODE_LINKED_FILE) {
				throw new ZoteroAIToolError("unavailable", "This PDF is a linked file that isn't on this computer.");
			}
			if (!Zotero.Sync.Storage.Local.getEnabledForLibrary(attachment.libraryID)) {
				throw new ZoteroAIToolError("unavailable", "This PDF isn't on this computer, and file syncing is off.");
			}
			// One download per attachment, however many calls wait for it.
			let key = attachment.libraryKey;
			if (!this._downloads.has(key)) {
				this._downloads.set(key, Zotero.Sync.Runner.downloadFile(attachment)
					.catch((e) => Zotero.logError(e))
					.finally(() => this._downloads.delete(key)));
			}
			await this._downloads.get(key);
			path = await attachment.getFilePathAsync();
			if (!path) throw new ZoteroAIToolError("unavailable", "Couldn't download this PDF.");
		}
		let info = await IOUtils.stat(path);
		if (info.size > maxBytes) {
			throw new ZoteroAIToolError("unavailable", maxBytes == this.MAX_RESOURCE_BYTES
				? `The PDF is ${(info.size / 1048576).toFixed(1)} MB; the limit is 25 MB. Use zotero_get_page_image instead.`
				: "The PDF is too large.");
		}
		let bytes = await IOUtils.read(path);
		// A PDF starts with %PDF- within its first 1024 bytes.
		if (!String.fromCharCode(...bytes.subarray(0, 1024)).includes("%PDF-")) {
			throw new ZoteroAIToolError("unavailable", "This file isn't a PDF.");
		}
		return { bytes, filename: attachment.attachmentFilename || PathUtils.filename(path) };
	},

	_downloads: new Map(),

	/** `pdf` is the PDF's bytes, or an async function returning them (called in turn, see _withRenderer). */
	async pageCount(pdf) {
		return this._withRenderer(pdf, async (call, pageCount) => pageCount);
	},

	/** → {pageCount, width, height, data (base64 JPEG)} for a 1-based page. */
	async render(pdf, page, region, maxWidth) {
		return this._withRenderer(pdf, async (call, pageCount) => {
			if (!(page >= 1 && page <= pageCount)) {
				throw new ZoteroAIToolError("invalid_argument", `page must be between 1 and ${pageCount}.`);
			}
			let size = await call("size", { page }); // crop box, rotation applied
			let r = this.plan(size.width, size.height, region, maxWidth);
			let { data } = await call("render", {
				page,
				x: r.x0 * size.width,
				y: r.y0 * size.height,
				scaleX: r.width / ((r.x1 - r.x0) * size.width),
				scaleY: r.height / ((r.y1 - r.y0) * size.height),
				width: r.width,
				height: r.height,
				quality: this.JPEG_QUALITY,
			});
			return { pageCount, width: r.width, height: r.height, data };
		});
	},

	base64(bytes) {
		let chunks = [];
		for (let i = 0; i < bytes.length; i += 0x8000) chunks.push(String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000)));
		return btoa(chunks.join(""));
	},

	// region Renderer frame

	TIMEOUT_MS: 60 * 1000,

	/**
	 * The page script: pdf.js runs here, in a frame with an ordinary content principal (like
	 * Zotero's reader), never with chrome privileges. It answers {to: "renderer", id, op, ...}
	 * messages with {to: "host", id, ...}.
	 */
	RENDERER_SCRIPT: `
		import * as pdfjs from "resource://zotero/reader/pdf/build/pdf.mjs";
		pdfjs.GlobalWorkerOptions.workerSrc = "resource://zotero/reader/pdf/build/pdf.worker.mjs";
		let pdf = null;
		let base64 = (bytes) => {
			let chunks = [];
			for (let i = 0; i < bytes.length; i += 0x8000) chunks.push(String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000)));
			return btoa(chunks.join(""));
		};
		addEventListener("message", async (event) => {
			let m = event.data;
			if (!m || m.to != "renderer") return;
			let reply = (data) => postMessage({ to: "host", id: m.id, ...data }, "*");
			try {
				if (m.op == "open") {
					try {
						// The reader's data files: without them scanned (JBIG2, CCITT, JPEG 2000) images and
						// CMap-encoded (e.g. CJK) text would be left out.
						let web = "resource://zotero/reader/pdf/web/";
						pdf = await pdfjs.getDocument({
							data: m.bytes,
							isEvalSupported: false,
							cMapUrl: web + "cmaps/",
							cMapPacked: true,
							standardFontDataUrl: web + "standard_fonts/",
							wasmUrl: web + "wasm/",
							iccUrl: web + "iccs/",
						}).promise;
					}
					catch (e) {
						return reply({ error: "open" });
					}
					return reply({ pageCount: pdf.numPages });
				}
				let page = await pdf.getPage(m.page);
				if (m.op == "size") {
					let v = page.getViewport({ scale: 1 });
					return reply({ width: v.width, height: v.height });
				}
				let canvas = document.createElement("canvas");
				canvas.width = m.width;
				canvas.height = m.height;
				let ctx = canvas.getContext("2d", { alpha: false });
				ctx.fillStyle = "#fff";
				ctx.fillRect(0, 0, m.width, m.height);
				// The print intent draws without requestAnimationFrame, so a minimised window can't stall it.
				await page.render({
					canvasContext: ctx,
					viewport: page.getViewport({ scale: 1, offsetX: -m.x, offsetY: -m.y }),
					transform: [m.scaleX, 0, 0, m.scaleY, 0, 0],
					intent: "print",
				}).promise;
				let blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", m.quality));
				page.cleanup();
				reply({ data: base64(new Uint8Array(await blob.arrayBuffer())) });
			}
			catch (e) {
				reply({ error: String(e && e.message || e) });
			}
		});
		postMessage({ to: "host", id: "ready" }, "*");
	`,

	_queue: Promise.resolve(),

	/**
	 * Opens the PDF in a fresh renderer frame and runs fn(call, pageCount). One PDF at a time (each
	 * holds the file and a pdf.js worker), within TIMEOUT_MS; a file is read only when its turn comes.
	 */
	_withRenderer(pdf, fn) {
		let run = this._queue.then(async () => this._renderIn(typeof pdf == "function" ? await pdf() : pdf, fn));
		this._queue = run.catch(() => {});
		return run;
	},

	async _renderIn(bytes, fn) {
		let win = Zotero.getMainWindow();
		if (!win) throw new ZoteroAIToolError("unavailable", "Zotero's main window isn't open.");
		let browser = win.document.createXULElement("browser");
		browser.setAttribute("type", "content");
		browser.setAttribute("remote", "false");
		browser.setAttribute("disableglobalhistory", "true");
		browser.setAttribute("style", "position: fixed; width: 1px; height: 1px; opacity: 0; pointer-events: none;");
		win.document.documentElement.appendChild(browser);
		let timer;
		try {
			// A blank page with the origin of Zotero's reader files, so it can load pdf.js and its worker.
			let principal = Services.scriptSecurityManager.createContentPrincipal(Services.io.newURI("resource://zotero/"), {});
			let docShell = browser.docShell;
			// createAboutBlankContentViewer before Firefox 121 (Zotero 7).
			(docShell.createAboutBlankDocumentViewer || docShell.createAboutBlankContentViewer).call(docShell, principal, principal);
			let frame = browser.contentWindow;
			let waiting = new Map();
			let nextId = 1;
			frame.addEventListener("message", (event) => {
				let id = event.data?.to == "host" ? event.data.id : null;
				let pending = waiting.get(id);
				if (!pending) return;
				waiting.delete(id);
				// Copy out of the page's compartment.
				pending(JSON.parse(JSON.stringify(event.data)));
			});
			let failed;
			let ready = new Promise((resolve, reject) => {
				waiting.set("ready", resolve);
				failed = () => reject(new ZoteroAIToolError("unavailable", "The PDF renderer didn't load."));
				frame.addEventListener("error", failed, { once: true });
			});
			let call = async (op, data, transfer = []) => {
				let id = nextId++;
				let reply = new Promise((resolve) => waiting.set(id, resolve));
				frame.postMessage({ to: "renderer", id, op, ...data }, "*", transfer);
				let result = await reply;
				if (result.error) throw new Error(`renderer ${op}: ${result.error}`);
				return result;
			};
			let script = browser.contentDocument.createElement("script");
			script.type = "module";
			script.textContent = this.RENDERER_SCRIPT;
			script.addEventListener("error", failed, { once: true });
			browser.contentDocument.documentElement.appendChild(script);
			let work = (async () => {
				await ready;
				// Handed over, not copied.
				let opened = await call("open", { bytes }, [bytes.buffer]).catch(() => null);
				if (!opened) throw new ZoteroAIToolError("unavailable", "This PDF can't be opened (it may be damaged or password-protected).");
				return fn(call, opened.pageCount);
			})();
			let timeout = new Promise((resolve, reject) => {
				timer = setTimeout(() => reject(new ZoteroAIToolError("unavailable", "Drawing the page took too long.")), this.TIMEOUT_MS);
			});
			return await Promise.race([work, timeout]);
		}
		finally {
			clearTimeout(timer);
			// Removing the frame ends its pdf.js worker too.
			browser.remove();
		}
	},

	// endregion
};
