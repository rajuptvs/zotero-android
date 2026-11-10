// Streaming block splits (ai/spec/blocks.json), checked at every streamed prefix:
//   node --test desktop-plugin/test/*.test.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { loadNotes } from "./loadNotes.mjs";

const Notes = loadNotes();
// Arrays made inside the vm context have another Array prototype; compare plain copies.
const split = (text) => JSON.parse(JSON.stringify(Notes.splitBlocks(text)));
const fixtures = JSON.parse(readFileSync(join(dirname(fileURLToPath(import.meta.url)), "../../ai/spec/blocks.json"), "utf8"));

for (const { name, text, blocks } of fixtures.cases) {
	test(`splits as specified: ${name}`, () => {
		assert.deepEqual(split(text), blocks);
	});

	test(`every prefix joins back and finished blocks never change: ${name}`, () => {
		let previous = null;
		for (let end = 0; end <= text.length; end++) {
			const prefix = text.slice(0, end);
			const blocks = split(prefix);
			if (blocks === null) {
				previous = null;
				continue;
			}
			assert.equal(blocks.join(""), prefix);
			if (previous) {
				const finished = previous.slice(0, -1);
				assert.deepEqual(blocks.slice(0, finished.length), finished, `prefix ${end}: ${JSON.stringify(prefix)}`);
			}
			previous = blocks;
		}
	});
}
