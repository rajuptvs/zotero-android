/* Copies pixels of the reader so a page or a figure can be sent to the model as an image. */

ZoteroAI.Capture = {
	MAX_SIDE: 1600,

	/** The reader's <browser>/<iframe> for a reader tab. */
	readerFrame(reader) {
		return reader?._iframe || null;
	},

	/**
	 * Captures what the reader shows, or only `rect` (CSS pixels relative to the reader frame).
	 * Returns {dataUrl, label}.
	 */
	async capture(reader, rect, label) {
		let frame = this.readerFrame(reader);
		if (!frame) throw new Error("No reader is open.");
		let win = frame.ownerGlobal;
		let box = frame.getBoundingClientRect();
		let r = rect || { x: 0, y: 0, width: box.width, height: box.height };
		let bitmap = await frame.browsingContext.currentWindowGlobal.drawSnapshot(
			new win.DOMRect(r.x, r.y, r.width, r.height), win.devicePixelRatio || 1, "rgb(255,255,255)");
		let scale = Math.min(1, this.MAX_SIDE / Math.max(bitmap.width, bitmap.height));
		let canvas = win.document.createElement("canvas");
		canvas.width = Math.round(bitmap.width * scale);
		canvas.height = Math.round(bitmap.height * scale);
		canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
		return { dataUrl: canvas.toDataURL("image/jpeg", 0.85), label };
	},

	/**
	 * Lets the user drag a rectangle over the reader; resolves with the captured image, or null if
	 * they cancel (Escape or a click without dragging).
	 */
	selectArea(reader, label) {
		let frame = this.readerFrame(reader);
		if (!frame) return Promise.resolve(null);
		let doc = frame.ownerDocument;
		let box = frame.getBoundingClientRect();
		let html = (tag) => doc.createElementNS("http://www.w3.org/1999/xhtml", tag);
		let overlay = html("div");
		Object.assign(overlay.style, {
			position: "fixed", left: box.left + "px", top: box.top + "px", width: box.width + "px", height: box.height + "px",
			background: "rgba(0,0,0,0.15)", cursor: "crosshair", zIndex: 10000,
		});
		let hint = html("div");
		hint.textContent = "Drag over a figure or passage to send it · Esc to cancel";
		Object.assign(hint.style, {
			position: "absolute", left: "50%", top: "12px", transform: "translateX(-50%)", padding: "6px 14px",
			borderRadius: "16px", background: "#333", color: "#fff", font: "13px sans-serif",
		});
		let marquee = html("div");
		Object.assign(marquee.style, { position: "absolute", border: "2px solid #2f6fdf", background: "rgba(47,111,223,0.12)", display: "none" });
		overlay.append(hint, marquee);
		doc.documentElement.append(overlay);
		return new Promise((resolve) => {
			let start = null;
			let finish = async (rect) => {
				overlay.remove();
				doc.removeEventListener("keydown", onKey, true);
				if (!rect) return resolve(null);
				// Let the overlay disappear before copying the pixels underneath it.
				await new Promise((r) => setTimeout(r, 100));
				try {
					resolve(await this.capture(reader, rect, label));
				}
				catch (e) {
					Zotero.logError(e);
					resolve(null);
				}
			};
			let onKey = (e) => {
				if (e.key == "Escape") { e.preventDefault(); finish(null); }
			};
			doc.addEventListener("keydown", onKey, true);
			overlay.addEventListener("mousedown", (e) => {
				start = { x: e.clientX - box.left, y: e.clientY - box.top };
				marquee.style.display = "block";
			});
			overlay.addEventListener("mousemove", (e) => {
				if (!start) return;
				let x = e.clientX - box.left, y = e.clientY - box.top;
				Object.assign(marquee.style, {
					left: Math.min(start.x, x) + "px", top: Math.min(start.y, y) + "px",
					width: Math.abs(x - start.x) + "px", height: Math.abs(y - start.y) + "px",
				});
			});
			overlay.addEventListener("mouseup", (e) => {
				if (!start) return finish(null);
				let x = e.clientX - box.left, y = e.clientY - box.top;
				let rect = { x: Math.min(start.x, x), y: Math.min(start.y, y), width: Math.abs(x - start.x), height: Math.abs(y - start.y) };
				finish(rect.width > 12 && rect.height > 12 ? rect : null);
			});
		});
	},
};
