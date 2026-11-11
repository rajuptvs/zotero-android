// Builds dist/zotero-ai-<version>.mcpb, the Claude Desktop extension: a zip of manifest.json and
// bridge.mjs. No dependencies (writes the zip itself), so it runs anywhere Node does.
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { deflateRawSync } from "node:zlib";

const root = dirname(fileURLToPath(import.meta.url));
const FILES = ["manifest.json", "bridge.mjs", "README.md"];
const manifest = JSON.parse(readFileSync(join(root, "manifest.json"), "utf8"));

const CRC = Array.from({ length: 256 }, (_, n) => {
  let c = n;
  for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  return c >>> 0;
});
const crc32 = (buf) => {
  let c = 0xffffffff;
  for (const b of buf) c = CRC[(c ^ b) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
};

// A plain zip (deflate, UTF-8 names, fixed 1980-01-01 timestamps so builds are reproducible).
const locals = [], centrals = [];
let offset = 0;
for (const name of FILES) {
  const data = readFileSync(join(root, name));
  const packed = deflateRawSync(data, { level: 9 });
  const nameBytes = Buffer.from(name, "utf8");
  const fields = (sig, central) => {
    const b = Buffer.alloc(central ? 46 : 30);
    let p = 0;
    const u16 = (v) => (p = b.writeUInt16LE(v, p)), u32 = (v) => (p = b.writeUInt32LE(v, p));
    u32(sig);
    if (central) u16(0x0314); // made by: Unix, zip 2.0
    u16(20); u16(0x0800); u16(8); u16(0); u16(0x21); // version, UTF-8 flag, deflate, time, date
    u32(crc32(data)); u32(packed.length); u32(data.length); u16(nameBytes.length); u16(0);
    if (central) {
      u16(0); u16(0); u16(0); u32((0o100644 << 16) >>> 0); u32(offset); // comment, disk, attrs, offset
    }
    return b;
  };
  const local = Buffer.concat([fields(0x04034b50, false), nameBytes, packed]);
  centrals.push(Buffer.concat([fields(0x02014b50, true), nameBytes]));
  locals.push(local);
  offset += local.length;
}
const central = Buffer.concat(centrals);
const end = Buffer.alloc(22);
end.writeUInt32LE(0x06054b50, 0);
end.writeUInt16LE(FILES.length, 8);
end.writeUInt16LE(FILES.length, 10);
end.writeUInt32LE(central.length, 12);
end.writeUInt32LE(offset, 16);

mkdirSync(join(root, "dist"), { recursive: true });
const out = join(root, "dist", `zotero-ai-${manifest.version}.mcpb`);
writeFileSync(out, Buffer.concat([...locals, central, end]));
console.log(`built ${out}`);
