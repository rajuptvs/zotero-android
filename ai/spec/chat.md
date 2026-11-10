# Zotero AI chat contract (spec v1)

Shared behaviour for the chat in every client, so a conversation started on one device continues
on another and looks the same.

## Model access

Sign in with ChatGPT, open-source plan-usage flow (see the Android `ai/auth` package for a
reference implementation): dynamic registration with `agent_name_hint` = client name
("Zotero for Android", "Zotero Desktop AI"), PKCE, loopback redirect
`http://127.0.0.1:<port>/auth/callback`, scopes
`openid profile email offline_access resource.invoke chatgpt.tokens.use.direct`,
`resource=https://api.openai.com/v1`. Inference: `POST https://api.openai.com/v1/responses` with
`store: false`, `stream: true`, the full conversation in `input` every turn, no
`previous_response_id`, no `temperature`/`top_p`/`metadata`/`user`, no system-role items.

## Requests

- `instructions`: from `prompts.json` (`reader` when chatting about an open document, `library`
  otherwise).
- First `input` item: a `developer` message with the context block (below).
- Then the conversation. User turns with images use content parts
  `[{"type":"input_text",...}, {"type":"input_image","image_url":"data:image/jpeg;base64,..."}]`.
- `tools`: every tool in `tools.json` (strip the `x-` fields). Tool loop: on a `function_call`
  output item, run the tool (asking for approval if `x-write`), append the `function_call` item and
  a `function_call_output` item (`output` = the result JSON string), and request again. Stop after
  12 tool rounds in one turn.
- `reasoning.effort`: the user's choice for the model, if the model lists reasoning levels.

## Approvals

- Every `x-write` call shows an approval card with the client's `describeWrite` text (identical on
  all clients; the conformance suite checks it). Declining returns
  `{"error":"declined","message":"The user declined this change."}` to the model.
- "Approve changes automatically" is one setting per device, read when each change is decided
  (not when the chat was opened). `zotero_trash_items` always asks, even with it on.

## Conversations across devices

- A chat remembers its note's HTML as it last loaded or wrote it. Before sending a question, if
  the note changed (another device added turns), the chat reloads from the note first, so the
  question continues the whole conversation. If the note is gone, the chat starts a new note.
- When saving, if the note changed while the answer was being written, the conversation is saved
  as a new note instead of overwriting (both versions are kept).
- Library chats are always about My Library (the only library the tools reach) and are saved there.

## Context block

```
The user is reading this item in Zotero:          (reader) / The user is browsing their Zotero library. (library)
Title: ...
Authors: ...
Date: ...
Published in: ...
DOI: ...
Tags: ...
Abstract: ...
Item key: XXXXXXXX                                 (reader; the parent item, else the attachment)
Current collection: Name (KEY)                    (library, if a collection is open)
Current view: All Items                            (library, otherwise: the view's name, e.g. Unfiled
                                                    Items; for a group library exactly
                                                    "a group library (the tools only reach My Library)")
Selected items: KEY1 “Title 1”, KEY2               (library, if any; up to 50; title omitted if empty)

Full text of the document[ (truncated)]:
<document>
...first 200000 characters...
</document>

They are currently at Page 3 (13%).                (reader, if known: "Page {label} ({percent}%)",
                                                    "Location" when the reader has no page numbers)
```

- Item lines follow Android's `ReadChatItemContextDbRequest`: `Authors` lists every creator in
  order ("First Last", or the single-field name); fields are base-mapped (a conference paper's
  proceedings title is "Published in"); empty lines are left out.
- If the document's text isn't available, the `Full text…` block is replaced by
  `The document's text isn't available, so answer from the conversation and any images.`
- Inside `<document>`, any `<document>` or `</document>` in the text is written as `< document>` /
  `</ document>`, so the document can't close its own wrapper.

## Notes written by the chat

Conversation note (one per conversation):
```html
<div data-schema-version="9" data-zotero-ai-attachment="ATTACHMENTKEY"><h1>AI chat: {document title}</h1>
<p><strong>You:</strong></p>
<p>line</p>
<p><strong>ChatGPT:</strong></p>
<p>line</p>
...
</div>
```
- Reader chats: child note of the item, `data-zotero-ai-attachment` = the attachment's key.
- Library chats: standalone note with `data-zotero-ai-scope="library"` instead of the attachment
  attribute, title `AI chat: Library — {first question, max 60 chars}`, tag `ai-chat`.
  Desktop's note editor drops unknown attributes (`data-zotero-ai-*`) when a note is opened, so a
  standalone note is a library chat if it has the scope attribute **or** its HTML starts with an
  `<h1>` beginning `AI chat: Library —` (optionally inside the wrapping `<div>`). Clients write the
  attribute but must not require it. For the same reason a reader chat without the attachment
  attribute belongs to every attachment of its parent.
- Each message line is its own `<p>`, HTML-escaped. Images are recorded as `<p>[Image: label]</p>`.
- Decoding: blank lines don't survive, so assistant messages are rejoined with a blank line between
  consecutive lines, except between two lines that both start with `|` (table rows) and inside a
  fenced code block (from a line starting with three backticks to the next). User messages are
  rejoined with single newlines.
- Tool activity is not recorded.

"Save as note" note: see `ChatMarkdown.answerNote` — `<div data-schema-version="9"><h2>{question}</h2>`
`<blockquote><p>{passage}</p></blockquote>` (if any), the answer's Markdown as HTML, then
`<p><em>From an AI chat about “{title}” ({location}) · {model}</em></p></div>`.
Math in the answer becomes Zotero note math, which the note editor typesets: `$$…$$` and `\[…\]`
→ `<pre class="math">$$…$$</pre>` when alone in a paragraph (otherwise inline), `\(…\)` and
`$…$` → `<span class="math">$…$</span>`. A `$…$` must not start or end with whitespace, can't span
lines, and its closing `$` can't be followed by a digit (so "$5 and $10" stays text). Math is set
aside before Markdown parsing; code spans and fenced code are left alone.

## UI parity checklist

Both clients provide: reader panel beside the document, library panel, selected-text chip,
quick actions (Explain, Simplify, Translate, Define terms), attach page / area as image,
Markdown answers with math typeset by KaTeX (from the note math markup above; KaTeX options `trust: false, maxSize: 20, maxExpand: 1000`, unparseable TeX shown as source), status line (Reading the document… / Thinking… / Writing the answer… /
Running {tool}…), stop, model + reasoning picker, Copy / Save as note / Explain again, history menu,
new chat, approval cards for writes (Approve / Decline, showing what will change), sign-in /
sign-out / switch account / usage link.

## Streaming blocks

While an answer streams, clients split it with `splitBlocks` (desktop `Notes.splitBlocks`, Android
`ChatMarkdown.splitBlocks`; adapted from T3 Code's MIT-licensed `markdownStreamBlocks.ts`) and
render each block separately, so finished blocks (and their typeset math) never re-render:
- cut only at a blank line that closes every open block (code fences tracked; a following list item
  or indented line continues the block; a number alone on the last line is a pending list item);
- never cut inside a math span (`MATH_OR_CODE`) or after a math opener (`$$`, `\[`, `\(`) that
  isn't closed yet;
- return null (render the whole text) for reference definitions, footnotes, raw HTML and `:::`.
The blocks join back into the text, and blocks before the last never change as text is appended.
`ai/spec/blocks.json` holds the expected splits; desktop (`node --test desktop-plugin/test/*.test.mjs`)
and Android (`ChatMarkdownBlocksTest`) both check them at every streamed prefix.
