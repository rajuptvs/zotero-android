# Zotero AI MCP server (standalone)

The same Zotero tools as the Zotero AI desktop plugin and Android app, served over MCP from the
**Zotero Web API**, so agents (Claude Code, Codex, …) can use your library with Zotero closed: on
this computer, a home server or a VPS. It implements the contract in
[`../ai/spec/mcp.md`](../ai/spec/mcp.md) (part A), and `ai/conformance/run.py --standalone` checks
it against the desktop plugin and the Android app.

Python 3.10+, standard library only. Page images (`zotero_get_page_image`, `zotero_get_pdf_info`)
also need `pip install pypdfium2 pillow`; without them those tools answer `unavailable`.

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

## Approving changes

Reads run at once. When an agent asks to change something (tags, collections, notes, adding items,
the trash), the change waits on **http://127.0.0.1:8770/admin** until you approve or decline it there;
the agent is told it's waiting and collects the result afterwards. The page also lists agents, lets
you revoke them, and lets a trusted agent apply changes without asking (except moving items to the
trash, which always asks).

To approve from your phone, reach the server over HTTPS through something you control (for example
`tailscale serve 8770`) and start it with `--allowed-host <that name>`. Never expose port 8770
directly to the internet.

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
