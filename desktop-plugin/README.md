# Zotero AI — desktop plugin

The desktop half of the [Zotero for Android AI panel](../README.md): an **Ask AI** panel for
Zotero 7–10, with the same library tools, approval cards, prompts and note formats as the Android
app, so conversations carry over between phone and computer through normal Zotero sync.

## Install

```bash
cd desktop-plugin
node scripts/build.mjs          # → dist/zotero-ai.xpi
```

In Zotero: **Tools → Plugins → ⚙ → Install Plugin From File…** and pick `dist/zotero-ai.xpi`.
Then click ✨ at the right end of the tab bar and **Continue with ChatGPT**.

Sign-in redirects back to Zotero's local server (`http://127.0.0.1:23119/auth/callback`), so
**Allow other applications on this computer to communicate with Zotero** must stay enabled
(Settings → Advanced, on by default).

## Use from agents (MCP)

**Tools → Zotero AI: Connect an agent…** turns on an MCP server at
`http://127.0.0.1:23119/zotero-ai/mcp` (Zotero 10+) and gives each agent its own token, with the
`claude mcp add` / `codex mcp add` command to paste. Changes an agent asks for wait for your
approval in Zotero. Details: [`../ai/spec/mcp.md`](../ai/spec/mcp.md).

## Semantic search

**Tools → Zotero AI: Semantic search…** turns on `zotero_semantic_search` (finding papers by
meaning) on this computer. It downloads the pinned EmbeddingGemma 2 model (346 MB, sha256-checked)
into `<Zotero data dir>/zotero-ai/models/`, then builds an index of My Library in the background:
metadata, full text and notes, one chunk at a time, slower while you use the computer and paused during
syncs. The first build takes a while (about 1 s per 512-token chunk with 4 threads); after that only
changed items are embedded again. Progress is kept in `<Zotero data dir>/zotero-ai/semantic.sqlite`,
so a build carries on after a restart. The index is saved as a standalone attachment, "Zotero AI
search index", when the first build finishes and then at most once a day, and syncs to your other
devices like any file, so it counts against your Zotero storage quota (tens of MB for a typical
library). An index holds at most 200,000 passages; items past that are left out, and the dialog says
so. If another computer already builds the library's index, this one only reads it. Details: [`../ai/spec/semantic.md`](../ai/spec/semantic.md).

The model runs in a ChromeWorker with onnxruntime-web (WASM, SIMD, up to 4 threads; a ChromeWorker is
cross-origin isolated, so threads work there, unlike in a content frame). The vendored runtime and
tokenizer add 15 MB to the unpacked plugin and 3.9 MB to the .xpi.

## What's where

| File | |
|---|---|
| `src/bootstrap.js` | Loads the scripts below into the plugin scope |
| `src/content/auth.js` | Sign in with ChatGPT (registration, PKCE, refresh, revocation); tokens in the login manager |
| `src/content/api.js` | Responses API: model list, streaming |
| `src/content/chat.js` | A conversation: context block, tool loop with approvals, notes, history |
| `src/content/tools.js` | The tools of `ai/spec/tools.json`, on Zotero's APIs |
| `src/content/pdf.js` | PDF pages as images, drawn with the pdf.js of Zotero's reader |
| `src/content/semantic.js` | Semantic search: model download, the worker, the index attachment, `zotero_semantic_search` |
| `src/content/semanticWorker.mjs` | The ChromeWorker: tokenizer, model, chunking, index ZIP read/write |
| `src/content/semanticBuilder.js` | Builds the index in the background and keeps the index attachment |
| `src/content/semanticDialog.js` | The Semantic search dialog |
| `src/content/vendor/` | marked, KaTeX, highlight.js, onnxruntime-web 1.31.1 (WASM build), @huggingface/tokenizers 0.2.0 |
| `src/content/notes.js` | Note formats and Markdown → note HTML |
| `src/content/panel.js` | The panel UI |
| `src/content/windows.js` | Puts the panel in the window, follows tabs, reader selection popup |
| `src/content/capture.js` | Page and area images from the reader |
| `src/content/approvals.js` | The only path that runs a write: chat approvals and MCP operations |
| `src/content/mcpServer.js`, `mcpClients.js` | The MCP endpoint and the agents allowed to use it |
| `src/content/approvalSheet.js`, `connectDialog.js` | The approval card and the Connect dialog |
| `src/content/testEndpoints.js` | Dev-only endpoints used by the cross-device tests (not in the release .xpi) |

The shared spec is copied in from `../ai/spec` at build time.

## Development

`scripts/dev-zotero.sh start|stop|restart|status` runs a headless Zotero 10 with the plugin
loaded unpacked from `build/`, on a separate profile and data directory (`~/.zotero-ai-test`),
with the dev endpoints enabled (`extensions.zotero-ai.testEndpoints`, plus a fresh secret per
launch in `~/.zotero-ai-test/test-token` that every dev request must send as
`X-Zotero-AI-Test-Token`). Never point it at your real data directory; the script refuses to.

Cross-device checks against an Android emulator running the debug app:

```bash
python3 ../ai/conformance/run.py                       # fast tier, about 1.5 min: every check, with test timings
python3 ../ai/conformance/run.py --only semantic,read  # just the sections you're working on
python3 ../ai/conformance/run.py --full                # before pushing, about 3 min: adds cross-device writes
                                                       # after sync, the real 20 s approval hold and a fresh
                                                       # semantic index of real items
```
Add `--standalone http://127.0.0.1:8770` (with `ZOTERO_AI_MCP_ADMIN_PASSWORD`) to check the
standalone server too. Each section's time is printed at the end; `CONFORMANCE_TIMES=1` stamps every
check.
