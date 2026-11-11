# Zotero AI MCP server (standalone)

The same Zotero tools as the Zotero AI desktop plugin and Android app, served over MCP from the
**Zotero Web API**, so agents (Claude Code, Codex, …) can use your library with Zotero closed: on
this computer, a home server or a VPS. It implements the contract in
[`../ai/spec/mcp.md`](../ai/spec/mcp.md) (part A), and `ai/conformance/run.py --standalone` checks
it against the desktop plugin and the Android app.

Python 3.10+, standard library only. Page images (`zotero_get_page_image`, `zotero_get_pdf_info`)
also need `pip install pypdfium2 pillow`, and semantic search (`zotero_semantic_search`) needs
`pip install onnxruntime tokenizers numpy` and `--semantic`; without them those tools answer
`unavailable`.

## Set up

1. Create a Zotero API key at <https://www.zotero.org/settings/keys/new> with **library access**,
   **notes access** and **write access** for your personal library. Keep it in a file only you
   can read (`chmod 600`).
2. Set the password for the approval page (agents never get it):
   ```bash
   python3 -m zotero_ai_mcp set-admin-password
   ```
3. Add an agent; its token is shown once:
   ```bash
   python3 -m zotero_ai_mcp add-client --name "Claude Code" --scope write
   ```
4. Run the server (it listens on `127.0.0.1:8770`):
   ```bash
   python3 -m zotero_ai_mcp serve --api-key-file ~/.config/zotero-api-key
   ```
5. Connect the agent, e.g.
   ```bash
   claude mcp add --transport http zotero http://127.0.0.1:8770/mcp --header "Authorization: Bearer zai_…"
   ```

If Zotero keeps your files on a **WebDAV** server, give the server the same details as Zotero's sync
settings, with the password in a file only you can read:
```bash
python3 -m zotero_ai_mcp serve --api-key-file ~/.config/zotero-api-key \
  --webdav-url https://dav.example.com/dav --webdav-username me --webdav-password-file ~/.config/zotero-webdav-password
```
Without WebDAV options, files come from Zotero storage. Downloaded PDFs are cached in the data
directory (up to 1 GB).

Run the commands from this directory (or put it on `PYTHONPATH`). State (agents, the password hash)
lives in `~/.config/zotero-ai-mcp/state.json`, readable only by you; `--data-dir` changes it.

## Semantic search

`zotero_semantic_search` finds items by meaning, using the search index the Zotero desktop plugin
builds and syncs as an attachment ([`../ai/spec/semantic.md`](../ai/spec/semantic.md)). This server
only reads that index; it doesn't build one. It's off unless you install the extra packages and
start the server with `--semantic`:
```bash
pip install onnxruntime tokenizers numpy
python3 -m zotero_ai_mcp serve --api-key-file ~/.config/zotero-api-key --semantic
```
At startup the embedding model (EmbeddingGemma 2, int8, about 346 MB) downloads from Hugging Face
in the background into `<data dir>/models/`, and each file is checked against its pinned sha256.
Until it's ready the tool answers `unavailable`. The index is fetched like PDFs (WebDAV or Zotero
storage), cached in the data directory and read again when the desktop replaces it. The model uses
about 400 MB of memory while in use, plus the index; both are freed after five minutes without a
search.

## Approving changes

Reads run at once. When an agent asks to change something (tags, collections, notes, adding items,
the trash), the change waits on **http://127.0.0.1:8770/admin** until you approve or decline it there;
the agent is told it's waiting and collects the result afterwards. The page also lists agents, lets
you revoke them, and lets a trusted agent apply changes without asking (except moving items to the
trash, which always asks).

To approve from your phone, reach the server over HTTPS through something you control (for example
`tailscale serve 8770`) and start it with `--allowed-host <that name>`. Never expose port 8770
directly to the internet.

## Remote clients (OAuth)

Web-based clients such as claude.ai and ChatGPT connectors, and agents on other machines, can sign
in with OAuth instead of a pasted token. It's off unless you start the server with `--oauth` and the
public HTTPS address the clients will use:
```bash
python3 -m zotero_ai_mcp serve --api-key-file ~/.config/zotero-api-key \
  --public-url https://zotero.example.com --oauth
```
Then add `https://zotero.example.com/mcp` as a custom connector in the client. The client registers
itself and opens a page on your server: sign in with the admin password, check the app's name and
the address it will send access to, choose **Read only** or **Read and request changes**, and
allow it. Nothing is approved automatically. The connection then appears under Agents on the admin
page like any other agent: its changes wait for your approval, you can let it approve changes
automatically, and **Revoke** ends it (its access tokens last an hour and are refreshed; both are
stored only as hashes). Pasted `zai_…` tokens keep working alongside.

The address must be the server's own origin (no path) and reach it through HTTPS you control, with
the original `Host` header passed through:
- **Tailscale Funnel** (public, so claude.ai's servers can reach it): `tailscale funnel --bg 8770`,
  then `--public-url https://<machine>.<tailnet>.ts.net`. Plain `tailscale serve` is enough for your
  own devices but not for web connectors.
- **Caddy**: `zotero.example.com { reverse_proxy 127.0.0.1:8770 }`.
- **nginx**: `proxy_pass http://127.0.0.1:8770;` with `proxy_set_header Host $host;`,
  `proxy_set_header X-Forwarded-For $remote_addr;` and `proxy_set_header X-Forwarded-Proto https;`.

Exposed this way, anyone on the internet can reach the sign-in page, so use a long admin password.
After five wrong passwords in ten minutes from one address, sign-in pauses for that address. The
address is the connection's, or the last `X-Forwarded-For` entry when the connection comes from
127.0.0.1 (your proxy; Caddy and Tailscale add it, nginx needs the line above). Sign-ins at
`http://127.0.0.1:8770` count separately, so you can always sign in there. Under Docker the proxy's
connections don't come from 127.0.0.1 inside the container, so all of them count as one address;
if someone keeps that address's sign-in paused while you connect an app, open the same
`/oauth/authorize?…` address with `http://127.0.0.1:8770` in place of your public one, sign in and
approve it there.
Registrations and open connection pages are limited per address the same way. A connection with **Read only** can still read your whole library, so
approve only apps you just started connecting yourself. The protocol details are in
[`../ai/spec/mcp.md`](../ai/spec/mcp.md) ("OAuth (optional)").

## Docker

[`compose.yaml`](compose.yaml) runs the server in a container (Python slim with PDFium, as a
non-root user). State and the PDF cache live in a volume. The API key, admin password and WebDAV
password come in as secret files, not environment variables:
```bash
mkdir -p secrets && chmod 700 secrets
cp ~/.config/zotero-api-key secrets/zotero-api-key
printf '%s' 'a long admin password' > secrets/admin-password
chmod 600 secrets/*
docker compose up -d
docker compose exec zotero-ai-mcp python -m zotero_ai_mcp add-client --name "Claude Code"
```
The container listens on all its interfaces, but the port is published on `127.0.0.1:8770` only, and
the server still accepts only `127.0.0.1:8770`, `localhost:8770`, `--public-url` and `--allowed-host`
as the `Host`. Keep the same port inside and outside the container. The admin password is set from
the secret on every start (`--admin-password-file`); to set it by hand instead, drop that option
and run `docker compose run --rm zotero-ai-mcp set-admin-password`. The container runs as uid/gid
1000 so it can read secrets you own. If your ids differ, run `ZOTERO_UID=$(id -u) ZOTERO_GID=$(id -g) docker compose build`.
For OAuth or WebDAV, uncomment the options in `compose.yaml`; for semantic search add `--semantic`
(the image has its packages; the model downloads into the volume on first start).

Without compose: `docker build -f mcp-server/Dockerfile -t zotero-ai-mcp .` from the repository root.

## Clients that only run stdio servers, and Claude Desktop

[`../mcp-bridge`](../mcp-bridge/README.md) has a small Node bridge (`bridge.mjs`) that turns this
server (or the desktop plugin) into a stdio server, and a Claude Desktop extension (`.mcpb`) built
on it that asks for the URL and token.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
These start the server on a free port with a fake library (no network) and cover the OAuth flow and
the stdio bridge (when Node is installed). `test_semantic.py` runs the semantic search golden checks
(`ai/conformance/semantic`) when onnxruntime, tokenizers and numpy are installed and the model files
are in `ZOTERO_AI_MODEL_DIR` (default `~/.cache/zotero-ai/models/<revision>`), and skips them
otherwise.

## Keep the API key away from agents

The server holds a Zotero API key with write access. An agent that can run shell commands as the
same user (Claude Code, Codex) could read that key file and call the Zotero Web API directly,
skipping approvals. If you let such agents work unattended, run the server as a different OS user
(or on another machine) so the key file isn't readable by them.

The same goes for the WebDAV password file: an agent running as you could read it.

## Differences from the desktop plugin
- Library data comes from Zotero's servers, so it reflects what has synced.
- `zotero_add_item_by_identifier` supports DOIs (doi.org metadata) and arXiv IDs; ISBNs and PMIDs
  return `unavailable` (Zotero's translators only run inside Zotero).
- Notes it creates use simpler Markdown formatting (`markdown_lite.py`).
- Only My Library, like the other clients.
- PDFs that are linked files (not stored by Zotero) can't be read; page images are drawn with
  PDFium instead of pdf.js, so they can differ slightly from the desktop's.
