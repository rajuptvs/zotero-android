# Zotero AI stdio bridge and Claude Desktop extension

`bridge.mjs` lets MCP clients that only start local (stdio) servers use a Zotero AI MCP server over
HTTP: the **desktop plugin** (`http://127.0.0.1:23119/zotero-ai/mcp`) or the **standalone server**
(`…/mcp`, see [`../mcp-server`](../mcp-server/README.md)). It reads JSON-RPC messages from stdin,
POSTs each one to the server with your token, and writes the replies to stdout unchanged (text, page
images and PDF files alike). Node 18 or later, no dependencies.

## Use it from any stdio client

Give the bridge the URL and the token through the environment, never on the command line:

| Variable | |
|---|---|
| `ZOTERO_AI_MCP_URL` | Server URL (default: the desktop plugin) |
| `ZOTERO_AI_MCP_TOKEN` | The `zai_…` token |
| `ZOTERO_AI_MCP_TOKEN_FILE` | Or a file holding the token (`chmod 600`) |
| `ZOTERO_AI_MCP_TIMEOUT` | Seconds to wait for one reply (default 120) |

For example, in a client's JSON config:
```json
{
  "mcpServers": {
    "zotero": {
      "command": "node",
      "args": ["/path/to/mcp-bridge/bridge.mjs"],
      "env": { "ZOTERO_AI_MCP_URL": "http://127.0.0.1:23119/zotero-ai/mcp", "ZOTERO_AI_MCP_TOKEN_FILE": "/home/me/.config/zotero-ai-token" }
    }
  }
}
```

Problems (wrong token, server off, no reply in time) come back to the client as JSON-RPC errors on
the request that failed, and are explained on stderr. Notifications get no reply. The bridge also
reads Server-Sent Events replies, so it works with other Streamable HTTP servers, but it doesn't
open the optional GET stream for server-initiated messages.

## Claude Desktop extension (.mcpb)

```bash
node build.mjs          # → dist/zotero-ai-<version>.mcpb
```
Open the file with Claude Desktop (or drag it onto Settings → Extensions). It asks for the server URL
and the token; the token is stored as a secret and handed to the bridge in its environment.

`manifest.json` follows the MCPB manifest 0.3; check it with `npx @anthropic-ai/mcpb validate manifest.json`.
