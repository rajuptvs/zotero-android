#!/usr/bin/env node
// MCP over stdio → Streamable HTTP, for clients that only run local (stdio) servers. Each JSON-RPC
// message read from stdin is POSTed to the server with a bearer token; replies (JSON or SSE) are
// written to stdout unchanged, one per line. Works with the Zotero AI desktop plugin
// (http://127.0.0.1:23119/zotero-ai/mcp) and the standalone server (…/mcp). No dependencies; Node 18+.
//
//   ZOTERO_AI_MCP_URL        server URL (default: the desktop plugin)
//   ZOTERO_AI_MCP_TOKEN      the zai_… token, or
//   ZOTERO_AI_MCP_TOKEN_FILE a file holding it (never pass the token on the command line)
//   ZOTERO_AI_MCP_TIMEOUT    seconds to wait for one reply (default 120)
import { readFileSync } from "node:fs";
import { createInterface } from "node:readline";

const log = (...parts) => console.error("[zotero-ai bridge]", ...parts);

function config() {
  if (process.argv.slice(2).some((a) => /zai_|^bearer/i.test(a))) {
    log("Don't put the token on the command line (other programs can read it there). Set ZOTERO_AI_MCP_TOKEN or ZOTERO_AI_MCP_TOKEN_FILE.");
    process.exit(2);
  }
  const raw = process.env.ZOTERO_AI_MCP_URL || process.argv[2] || "http://127.0.0.1:23119/zotero-ai/mcp";
  let url;
  try {
    url = new URL(raw);
  } catch {
    log(`ZOTERO_AI_MCP_URL isn't a URL: ${raw}`);
    process.exit(2);
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    log(`ZOTERO_AI_MCP_URL must be http(s): ${raw}`);
    process.exit(2);
  }
  if (url.protocol === "http:" && !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)) {
    log(`Warning: ${url.host} is plain http, so the token travels unencrypted.`);
  }
  let token = process.env.ZOTERO_AI_MCP_TOKEN || "";
  if (!token && process.env.ZOTERO_AI_MCP_TOKEN_FILE) {
    try {
      token = readFileSync(process.env.ZOTERO_AI_MCP_TOKEN_FILE, "utf8");
    } catch (e) {
      log(`Can't read ZOTERO_AI_MCP_TOKEN_FILE: ${e.code || e.message}`);
      process.exit(2);
    }
  }
  token = token.trim().replace(/^bearer\s+/i, "");
  if (!token) {
    log("No token. Set ZOTERO_AI_MCP_TOKEN or ZOTERO_AI_MCP_TOKEN_FILE (from Zotero's Connect an agent dialog or add-client).");
    process.exit(2);
  }
  if (!/^[\x21-\x7e]+$/.test(token)) {
    log("The token has spaces, line breaks or other characters a token can't have. Check ZOTERO_AI_MCP_TOKEN(_FILE): it should hold one zai_… token.");
    process.exit(2);
  }
  const seconds = Number(process.env.ZOTERO_AI_MCP_TIMEOUT || 120);
  return { url: url.href, token, timeoutMs: (seconds > 0 ? seconds : 120) * 1000 };
}

const { url, token, timeoutMs } = config();
let protocolVersion = null; // from the initialize result, sent on later requests
let sessionId = null; // only if the server uses sessions (ours don't)

const write = (message) => process.stdout.write(JSON.stringify(message) + "\n");

const isRequest = (msg) => msg && typeof msg === "object" && !Array.isArray(msg) && typeof msg.method === "string" && "id" in msg;

// Tells the client a request failed (so it doesn't wait forever) and says why on stderr.
function fail(msg, text) {
  text = text.split(token).join("…"); // never echo the token
  log(msg && msg.method ? `${msg.method}: ${text}` : text);
  if (isRequest(msg)) write({ jsonrpc: "2.0", id: msg.id, error: { code: -32000, message: text } });
}

function explain(status, body) {
  let detail = "";
  try {
    const j = JSON.parse(body);
    detail = j?.error?.message || j?.error_description || (typeof j?.error === "string" ? j.error : "");
  } catch {
    detail = body.trim().slice(0, 200);
  }
  const what = {
    401: "The server rejected the token (HTTP 401). Check ZOTERO_AI_MCP_TOKEN; it may have been revoked.",
    403: "The server refused the request (HTTP 403).",
    404: `Nothing at ${url} (HTTP 404). Check ZOTERO_AI_MCP_URL.`,
    503: "The server is turned off (HTTP 503).",
  }[status] || `The server answered HTTP ${status}.`;
  return detail ? `${what} ${detail}` : what;
}

function deliver(msg, reply) {
  if (msg && msg.method === "initialize" && reply && reply.id === msg.id && reply.result?.protocolVersion) {
    protocolVersion = reply.result.protocolVersion;
  }
  write(reply);
}

// Reads a text/event-stream body; each "message" event's data is one JSON-RPC message.
async function readEvents(body, msg) {
  const decoder = new TextDecoder();
  let buffer = "", data = [], event = "message", answered = false;
  const dispatch = () => {
    if (data.length && event === "message") {
      try {
        const reply = JSON.parse(data.join("\n"));
        if (isRequest(msg) && reply && reply.id === msg.id && ("result" in reply || "error" in reply)) answered = true;
        deliver(msg, reply);
      } catch {
        log("Skipped an event that isn't JSON.");
      }
    }
    data = [];
    event = "message";
  };
  for await (const chunk of body) {
    buffer += decoder.decode(chunk, { stream: true });
    let i;
    while ((i = buffer.search(/\r\n|\r|\n/)) >= 0) {
      if (buffer[i] === "\r" && i === buffer.length - 1) break; // maybe half of \r\n
      const line = buffer.slice(0, i);
      buffer = buffer.slice(i + (buffer.startsWith("\r\n", i) ? 2 : 1));
      if (line === "") dispatch();
      else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
      else if (line.startsWith("event:")) event = line.slice(6).trim();
      // id:, retry: and comments don't matter here
    }
  }
  dispatch();
  return answered;
}

async function forward(msg) {
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), timeoutMs);
  try {
    const headers = {
      "Content-Type": "application/json",
      Accept: "application/json, text/event-stream",
      Authorization: `Bearer ${token}`,
    };
    if (protocolVersion) headers["MCP-Protocol-Version"] = protocolVersion;
    if (sessionId) headers["Mcp-Session-Id"] = sessionId;
    const res = await fetch(url, { method: "POST", headers, body: JSON.stringify(msg), signal: abort.signal });
    if (res.headers.get("mcp-session-id")) sessionId = res.headers.get("mcp-session-id");
    const type = (res.headers.get("content-type") || "").split(";")[0].trim().toLowerCase();
    if (!res.ok) {
      if (res.status === 404 && sessionId) sessionId = null;
      return fail(msg, explain(res.status, await res.text()));
    }
    if (type === "text/event-stream") {
      const answered = await readEvents(res.body, msg);
      if (isRequest(msg) && !answered) fail(msg, "The server closed the stream without answering.");
      return;
    }
    const text = await res.text();
    if (!text.trim()) {
      if (isRequest(msg)) fail(msg, `The server sent no reply (HTTP ${res.status}).`);
      return; // notifications and responses: 202, no body
    }
    let reply;
    try {
      reply = JSON.parse(text);
    } catch {
      return fail(msg, "The server's reply isn't JSON.");
    }
    for (const r of Array.isArray(reply) ? reply : [reply]) deliver(msg, r);
  } catch (e) {
    if (e.name === "AbortError") fail(msg, `No reply within ${timeoutMs / 1000} s.`);
    else fail(msg, `Can't reach ${url}: ${e.cause?.code || e.cause?.message || e.message}`);
  } finally {
    clearTimeout(timer);
  }
}

const inFlight = new Set();
const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
input.on("line", (line) => {
  if (!line.trim()) return;
  let msg;
  try {
    msg = JSON.parse(line);
  } catch {
    log("Skipped a line from the client that isn't JSON.");
    return write({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "Parse error" } });
  }
  // Each message is its own request, so a long call (a write waiting for approval) doesn't hold up the rest.
  const p = forward(msg).finally(() => inFlight.delete(p));
  inFlight.add(p);
});
input.on("close", async () => {
  await Promise.allSettled([...inFlight]);
  if (sessionId) {
    await fetch(url, { method: "DELETE", headers: { Authorization: `Bearer ${token}`, "Mcp-Session-Id": sessionId }, signal: AbortSignal.timeout(2000) }).catch(() => {});
  }
  process.stdout.write("", () => process.exit(0)); // after stdout has drained
});
