/* Zotero AI — bootstrap. Loads the plugin's scripts into this scope and hands lifecycle events to them. */

var ZoteroAI;
var { setTimeout, clearTimeout, setInterval, clearInterval } = ChromeUtils.importESModule("resource://gre/modules/Timer.sys.mjs");

function install() {}

function uninstall() {}

async function startup({ id, version, rootURI }) {
	// Web APIs the plugin uses from its own (non-window) scope.
	Cu.importGlobalProperties(["fetch", "AbortController", "TextDecoder", "TextEncoder", "URL", "URLSearchParams", "crypto", "btoa", "atob", "DOMParser", "XMLSerializer"]);
	Services.scriptloader.loadSubScript(rootURI + "content/vendor/marked.umd.js");
	Services.scriptloader.loadSubScript(rootURI + "content/vendor/katex.min.js");
	for (let file of ["util", "spec", "notes", "approvals", "tools", "auth", "api", "chat", "capture", "panel", "mcpClients", "mcpServer", "approvalSheet", "connectDialog", "windows", "main"]) {
		Services.scriptloader.loadSubScript(rootURI + `content/${file}.js`);
	}
	// Dev builds only (scripts/dev-zotero.sh); the release .xpi doesn't contain it.
	try {
		Services.scriptloader.loadSubScript(rootURI + "content/testEndpoints.js");
	}
	catch (e) {}
	await ZoteroAI.startup({ id, version, rootURI });
}

function onMainWindowLoad({ window }) {
	ZoteroAI?.addToWindow(window);
}

function onMainWindowUnload({ window }) {
	ZoteroAI?.removeFromWindow(window);
}

function shutdown() {
	ZoteroAI?.shutdown();
	ZoteroAI = undefined;
}
