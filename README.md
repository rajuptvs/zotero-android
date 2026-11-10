# Zotero for Android — with an AI reading assistant

This is a fork of the official [Zotero for Android](https://github.com/zotero/zotero-android) app. It adds an **AI chat panel to the reader**, so you can ask questions about the paper you're reading — about a selected passage, a figure, or the whole document — while keeping the PDF on screen. It signs in with your **ChatGPT account** (no API key), and every conversation is saved as a Zotero note, so it follows you to your other devices.

Everything else is the stock Zotero app. Work happens on the [`ai-chat-panel`](https://github.com/rajuptvs/zotero-android/tree/ai-chat-panel) branch.

<p align="center">
  <img src="docs/ai-chat/fold-unfolded-figure.png" width="900" alt="Foldable, unfolded: 'Attention Is All You Need' with Figure 1 on the left and the AI explaining the architecture on the right">
  <br><em>Unfolded foldable: the paper and the chat side by side. Here the Transformer diagram (Figure 1) was sent as an image and explained.</em>
</p>

## What it adds

**Chat next to the document**
- An ✨ **Ask AI** button in the reader toolbar opens the panel. The PDF stays visible: side by side on wide screens (unfolded foldables, tablets), PDF on top and chat below on a phone. The divider can be dragged.
- Answers stream in and are rendered as Markdown (lists, bold, code, tables), with a status line (*Reading the document… / Thinking… / Writing the answer…*) and a Stop button.
- The model is told what you're reading: title, authors, venue, DOI, tags, abstract, the full text of the document and the page you're on.

**Select to chat**
- Text you select while the panel is open is attached as a **Text selection** chip. **Ask AI** is also in the text-selection menu, next to Highlight and Underline.
- One-tap actions for a selection: **Explain**, **Simplify**, **Translate**, **Define terms**.

**Chat with images from the PDF**
- **+ → This page (image)** sends what's on screen.
- **+ → Select an area (image)** lets you drag a box around a figure, chart, table or equation and send just that.

**Keep what's useful**
- **Save as note** under any answer saves it as its own Zotero note on the paper: your question as the heading, the passage it was about, the formatted answer, and where it came from.
- **Explain again** asks for the last answer again, intuition first.
- Copy any answer.

**Synced conversations**
- Each conversation is stored as a child note on the item (tied to that attachment), so it syncs through Zotero and shows up on your other devices — and stays readable in Zotero desktop. A history menu lists past chats for the document.

**Foldables**
- Unfolding gives the side-by-side layout; folding switches to the stacked one. The app keeps the same document, page, panel and conversation across folds. (Stock Zotero closes the open document when you fold or unfold.)

**Model choice**
- Pick any model your ChatGPT plan offers, and its reasoning effort, from the menu under the message box.

<table>
  <tr>
    <td align="center"><img src="docs/ai-chat/phone-reader-ai.png" width="260" alt="Phone: reader with the Ask AI panel under the PDF"><br><em>Phone: Ask AI under the PDF</em></td>
    <td align="center"><img src="docs/ai-chat/phone-chat-synced.png" width="260" alt="Phone: the same conversation, synced from the foldable"><br><em>Phone: the chat started on the foldable, synced via Zotero</em></td>
    <td align="center"><img src="docs/ai-chat/fold-closed-selection.png" width="260" alt="Folded: text selected, Ask AI in the selection menu, quick actions"><br><em>Folded: select text → Ask AI, with quick actions</em></td>
    <td align="center"><img src="docs/ai-chat/fold-closed-chat.png" width="260" alt="Folded: an answer with Save as note and Explain again"><br><em>Folded: Save as note / Explain again</em></td>
  </tr>
</table>

## Sign in with ChatGPT

The panel uses OpenAI's [Sign in with ChatGPT](https://developers.openai.com/siwc/token-sharing-open-source) flow for open-source apps, so requests run on your ChatGPT Plus/Pro plan instead of an API key:

- Tap **Continue with ChatGPT**; you sign in and approve in the browser, which hands control back to the app through a local `127.0.0.1` callback (OAuth with PKCE; no client secret is involved).
- Tokens are encrypted with a key kept in the Android Keystore, refreshed automatically, revoked on sign-out, and excluded from device backups.
- Usage counts against your ChatGPT plan; the menu links to your ChatGPT usage page. "Sign in with a different account" switches accounts.

## Building

Same as upstream, see [CONTRIBUTING.md](CONTRIBUTING.md). In short:

```bash
git clone --recursive https://github.com/rajuptvs/zotero-android.git
cd zotero-android && git checkout ai-chat-panel
for s in translators translation pdf-worker citation_proc csl_locales styles; do python3 scripts/bundle_$s.py; done
for s in utilities reader document-worker; do bash scripts/bundle_$s.sh; done
./gradlew assembleDevDebug
```

No PSPDFKit key is needed: PDFs, EPUBs and snapshots all open in Zotero's web-based reader.

## Known limitations

- Images you send aren't stored in the synced note; they appear there as `[Image: …]`.
- Inline math in the panel is shown as raw `$…$` (notes typeset it).
- Sign in with ChatGPT for open-source apps is an OpenAI preview; its availability and limits may change.

## Where the code is

| Area | Files |
|---|---|
| ChatGPT sign-in, tokens | `app/src/main/java/org/zotero/android/ai/auth/` |
| Responses API client | `app/src/main/java/org/zotero/android/ai/api/` |
| Note format, Markdown → note HTML | `app/src/main/java/org/zotero/android/ai/chat/` |
| Chat panel UI and logic, page/area capture | `app/src/main/java/org/zotero/android/screens/reader/sidebar/chat/` |
| Split layout, foldable session restore | `screens/reader/ReaderAiSplitLayout.kt`, `screens/reader/ReaderSessionRestore.kt` |

---

Zotero is a project of the [Corporation for Digital Scholarship](https://digitalscholar.org/). This fork is not affiliated with or endorsed by Zotero. Licensed under the [GNU AGPL v3](COPYING), like upstream.
