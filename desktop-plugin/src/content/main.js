/* Lifecycle: load the spec, register endpoints and the reader hooks, and add the panel to windows. */

Object.assign(ZoteroAI, {
	async startup({ id, version, rootURI }) {
		this.id = id;
		this.version = version;
		this.rootURI = rootURI;
		await Zotero.initializationPromise;
		await this.Spec.load(rootURI);
		this.TestEndpoints?.register();
		this.McpServer.startup();
		this.Windows?.startup?.();
		for (let win of Zotero.getMainWindows()) {
			if (win.ZoteroPane) this.addToWindow(win);
		}
		this.ApprovalSheet.startup();
		this.log(`started ${version}`);
	},

	addToWindow(win) {
		this.Windows?.add?.(win);
	},

	removeFromWindow(win) {
		this.Windows?.remove?.(win);
	},

	shutdown() {
		this.TestEndpoints?.unregister();
		this.McpServer.shutdown();
		this.ApprovalSheet.shutdown();
		this.ConnectDialog.close();
		this.Windows?.shutdown?.();
		for (let win of Zotero.getMainWindows()) this.removeFromWindow(win);
	},
});
