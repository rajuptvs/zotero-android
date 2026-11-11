# Zotero AI MCP server (spec v1)

The Zotero tools of `tools.json`, served over MCP so any agent (Claude Code, Codex, …) can use the
library. Part A is the contract every host implements; part B is the Zotero desktop plugin, the
only host today. A later standalone server on the Zotero Web API implements part A with its own
approval UI. `ai/conformance/run.py` checks a host against part A (`class Mcp`, with a pluggable
"human" that decides approvals).

## A. Contract (any host)

### Transport
- MCP Streamable HTTP, **JSON responses only** (no SSE, no sessions): one `POST` per JSON-RPC
  message. `GET` → 405 with `Allow: POST`.
- Protocol versions `2025-11-25` and `2025-06-18`. `initialize` echoes the client's version if
  supported, else answers `2025-11-25`. A later request whose `MCP-Protocol-Version` header names an
  unsupported version gets 400; a missing header is accepted.
- `Content-Type` must be `application/json` (parameters ignored), else 415. The body must be one
  JSON-RPC 2.0 object (`jsonrpc: "2.0"`); arrays (batches) and anything else get `-32600` (400).
- Notifications and responses from the client get 202 with no body. Unknown methods `-32601`.
- Requests carrying an `Origin` header are refused (403, or the connection is dropped).
- Methods: `initialize` (`capabilities: {tools: {listChanged: false}}`, `serverInfo`,
  `instructions` = `prompts.json` → `mcp`), `ping`, `tools/list`, `tools/call`.

### Auth
- Every request carries `Authorization: Bearer <token>`; missing, wrong or revoked → 401 with a
  JSON-RPC error body and no `WWW-Authenticate` (no OAuth). Never in a URL.
- A token belongs to one client with a scope: `read` (read tools only) or `write` (may request
  changes), and an `autoApprove` flag set only in the host's own UI.

### Tools
- The catalog is derived from `tools.json` (conformance recomputes it):
  `{name, description, inputSchema, annotations: {readOnlyHint: !x-write, destructiveHint: x-always-ask}}`,
  where `inputSchema` is `parameters` with nullable properties removed from `required`.
- `read` clients get tools without `x-write` / `x-write-scope`; `write` clients get all of them,
  including `zotero_await_approval`. A `read` client calling a write tool or
  `zotero_await_approval` gets `{"error":"declined","message":"This connection is read-only."}` and
  nothing is queued.
- Arguments are validated as in `tools.md` ("Write tools"); omitted `params.arguments` means `{}`.
  An unknown tool name is a JSON-RPC `-32602`; everything else is a tool result.
- Tool result: `{content: [{type: "text", text: JSON.stringify(result)}], isError: "error" in result}`,
  where `result` is exactly what `tools.md` specifies (equal to the chat clients' results).
- A page image adds `{type: "image", data: <base64>, mimeType: "image/jpeg"}` after the text part;
  the PDF file adds `{type: "resource", resource: {uri: "zotero://attachment/<attachmentKey>",
  mimeType: "application/pdf", blob: <base64>}}`.

### Writes and approvals
- Whether a write asks is `shouldAsk(tool, client.autoApprove)` (`tools.md`); trash always asks.
- A write that asks becomes an **operation** decided by a human in a UI the host controls. No MCP
  tool can approve anything. The operation stores the tool and its canonical arguments; those
  exact arguments are what's described and what runs, once. Before running, the host checks again
  that the client still exists with write scope and the server is still on.
- `tools/call` on a write waits up to **20 s** for the outcome, then returns (with `isError: false`):
  ```json
  {"status": "pending_approval", "approval_id": "op_…", "description": "Add tag to-read on “…”",
   "expires_at": "2026-11-10T18:05:00Z", "message": "…"}
  ```
  `status` (`pending_approval` | `running`), `approval_id`, `description` (= the chat clients'
  `describeWrite` text) and `expires_at` are normative; `message` is a hint for the model.
- `zotero_await_approval {approval_id}` waits up to 20 s the same way and returns the pending shape
  or the final result. It returns the stored result every time until the operation is pruned (10
  minutes after it finished). Another client's or an unknown id → `not_found`.
- Outcomes: the tool's result; declined → `{"error":"declined","message":"The user declined this change."}`
  (byte-equal to the chat); unanswered for **5 min** →
  `{"error":"declined","message":"Nobody answered the approval request in Zotero."}`; a late
  decision is ignored.
- **Retries** (at most once within a window, not exactly once): an identical call (same client,
  tool and canonical arguments) joins an operation that is still open, and gets the result of one
  that **succeeded** within the last **2 min**. That covers a lost response, but also means an
  intentional identical write within 2 min returns the earlier result. Declined, expired or failed
  operations don't replay: an identical call asks again. Replay state doesn't survive a restart.
- Limits: at most 5 open operations per client and 10 in all; beyond that
  `{"error":"unavailable","message":"Too many changes are waiting for approval in Zotero."}`.
- Revoking a client, turning the server off, or shutting down declines that client's (or all)
  open operations.

## B. Desktop host (Zotero 10 plugin)

- Endpoint `POST http://127.0.0.1:<port>/zotero-ai/mcp` on Zotero's own local server (port 23119
  by default; the server runs while `extensions.zotero.httpServer.enabled` is true). Off until the
  user turns it on in **Tools → Zotero AI: Connect an agent…** (pref `extensions.zotero-ai.mcp.enabled`);
  Zotero 10 or later only.
- Zotero's server specifics: it answers HTTP/1.0, one response per connection, and can't stream —
  hence JSON-only. It drops requests from browsers (`Mozilla/` user agent or an `Origin` header)
  before the endpoint runs, unless they carry Zotero connector headers; the endpoint then refuses
  any `Origin` itself (403). Malformed JSON is answered by Zotero with a plain-text 400. It logs
  every request header when debug output is on; the plugin hides `zai_…` tokens in that log.
- Tokens: `zai_` + 32 random bytes, shown once in the Connect dialog together with ready-to-paste
  `claude mcp add --transport http … --header "Authorization: Bearer …"` and
  `codex mcp add … --bearer-token-env-var ZOTERO_AI_MCP_TOKEN` commands. Only the SHA-256 is stored
  (pref `extensions.zotero-ai.mcp.clients`).
- Approval UI: a card in the main window, oldest request first. It never takes focus, and Approve
  does nothing until the card has been visible in a focused window for a second and the user has
  pointed at it or tabbed into it. It shows the client, tool, description, item count and the
  arguments as text. Auto-approve per client is set only in the Connect dialog.
- Clients that can't send headers or send an `Origin` (some Electron apps) can use
  `npx mcp-remote <url> --header "Authorization: Bearer …"`.

### Threat model
Approvals protect the library from an agent's mistakes and from instructions hidden in documents or
tool results. They don't protect against programs running as the user on the same computer, which
can read the plugin's prefs and Zotero's database directly. Loopback-only, per-client tokens, the
`Origin` refusal and Zotero's Host check keep web pages out.
