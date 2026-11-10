// Builds the plugin into build/ (unpacked, for development) and dist/zotero-ai.xpi.
// Copies the shared AI spec (../ai/spec) into the plugin so both clients ship the same file.
import { cpSync, mkdirSync, rmSync, existsSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const build = join(root, "build");
const dist = join(root, "dist");
rmSync(build, { recursive: true, force: true });
cpSync(join(root, "src"), build, { recursive: true });
cpSync(join(root, "..", "ai", "spec"), join(build, "content", "spec"), { recursive: true });
mkdirSync(dist, { recursive: true });
const xpi = join(dist, "zotero-ai.xpi");
if (existsSync(xpi)) rmSync(xpi);
// The dev-only test endpoints stay out of the release (bootstrap.js skips a missing script).
execFileSync("zip", ["-qr", xpi, ".", "-x", "content/testEndpoints.js"], { cwd: build });
console.log(`built ${build} and ${xpi}`);
