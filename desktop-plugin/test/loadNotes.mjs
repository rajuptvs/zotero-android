// Loads src/content/notes.js outside Zotero, for tests of its pure functions.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

export function loadNotes() {
	let context = { ZoteroAI: {} };
	vm.createContext(context);
	let root = join(dirname(fileURLToPath(import.meta.url)), "..");
	vm.runInContext(readFileSync(join(root, "src/content/notes.js"), "utf8"), context);
	return context.ZoteroAI.Notes;
}
