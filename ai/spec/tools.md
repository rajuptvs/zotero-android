# Zotero AI tool contract (spec v1)

Every client (Android app, desktop plugin) implements the tools in `tools.json` with **exactly** the
behaviour and result shapes below, so the same request gives the same answer on every device.
`ai/conformance/` checks this.

## General rules

- Scope: the user's personal library ("My Library"). Group libraries are out of scope in v1.
- "Regular item": any item that is not an attachment, note or annotation, and not in the trash.
- Collections in the trash are treated as nonexistent everywhere (lists, filters, `not_found`).
- Results are JSON objects. Keys are always present (use `null` / `[]`, never omit). No extra keys.
- Strings are returned as stored in Zotero, trimmed. Dates are the raw Zotero `date` field.
- Errors: return `{"error": "<code>", "message": "<human text>"}` with codes
  `not_found`, `invalid_argument`, `unavailable`, `declined` (user rejected a write), `failed`.
- Limits: clamp to the documented range; `null` means the default.
- Sorting must be deterministic as specified. "Title order" = case-insensitive (Unicode lowercase)
  comparison of the title, then of the key.

## Shapes

`ItemSummary`:
```json
{"key": "2MIWXEWJ", "itemType": "preprint", "title": "Attention Is All You Need",
 "creators": "Vaswani et al.", "year": "2023", "tags": ["transformers"]}
```
- `title`: the item's base title field (`title`, or the type's mapped equivalent such as `caseName`
  or `subject`), trimmed. For notes: Zotero's `noteToTitle` rule — add `\n` after every closing
  `</h1-6>`, `</p>`, `</div>`; `<br>` → space; strip tags; decode entities; if the HTML starts with a
  lone opening tag on its own line, trim; take the first 120 characters and cut at the first `\n`.
- `creators`: computed from the creators of the item type's **primary** creator type (e.g. `author`),
  falling back to all creators if there are none of that type, in creator order: last name of the
  first if one; "A and B" if two; "A et al." if more; `""` if none. Use the creator's `lastName`, or
  `name` for single-field creators.
- `year`: the first run of exactly 4 consecutive digits in `date`, else `""`.
- `tags`: tag names (manual and automatic), sorted case-insensitively.

## Tools

### zotero_search_items
Split `query` on whitespace (lowercase). An item matches when **every** word is a substring of at
least one of: title, any creator's firstName/lastName/name, `year`, any tag name. Then apply `tag`
(exact, case-sensitive) and `collection_key` (direct membership) filters. Default limit 20.
Result: `{"total": <matches before limit>, "items": [ItemSummary, ...]}` in title order.

### zotero_get_item_metadata
```json
{"key": "...", "itemType": "...", "title": "...",
 "creators": [{"type": "author", "name": "Ashish Vaswani"}],
 "date": "...", "year": "...", "publication": "...", "doi": "...", "url": "...", "abstract": "...",
 "tags": ["..."], "collections": [{"key": "...", "name": "..."}],
 "attachments": [{"key": "...", "title": "...", "contentType": "application/pdf"}],
 "notes": [{"key": "...", "title": "..."}]}
```
- `creators`: in order; `name` = "firstName lastName" (or single `name`).
- `publication`: `publicationTitle`, else `proceedingsTitle`, else `bookTitle`, else `websiteTitle`, else `""`.
- Missing text fields are `""`. `collections` sorted by name; `attachments`/`notes` in title order.

### zotero_get_item_fulltext
Resolve `item_key` to an attachment: the key itself if it's a PDF/EPUB/HTML attachment, else the
item's first such attachment (PDF first, then EPUB, then HTML; then title order). Text comes from
Zotero's full-text index, the same content the Zotero Web API serves at `items/<key>/fulltext`.
Default `max_chars` 20000, max 100000.
Result: `{"attachmentKey": "...", "totalChars": 12345, "truncated": true, "text": "..."}`.
Error `unavailable` if the attachment has not been indexed.

### zotero_get_collections
`{"collections": [{"key": "...", "name": "...", "parentKey": null, "itemCount": 3}]}` sorted by
name (case-insensitive) then key. `itemCount` = regular items directly in the collection.

### zotero_get_collection_items
`{"collection": {"key": "...", "name": "..."}, "total": n, "items": [ItemSummary]}` in title order.
Default limit 50.

### zotero_get_tags
`{"total": n, "tags": [{"tag": "...", "count": 4}]}` sorted by count desc, then tag
case-insensitively. Counts regular items only. Default limit 200.

### zotero_get_recent
`{"items": [ItemSummary + "dateAdded": "2026-11-10T18:00:31Z"]}` by `dateAdded` desc, then key.
Default limit 10.

### zotero_get_notes
`{"total": n, "notes": [{"key": "...", "parentKey": "..." | null, "title": "...", "text": "..."}]}`
- `text`: note HTML converted to plain text, whitespace collapsed, first 2000 chars.
- Trashed notes excluded. Sorted by `dateModified` desc, then key. Default limit 20.

### zotero_get_annotations
`{"annotations": [{"key": "...", "attachmentKey": "...", "type": "highlight", "text": "...",
"comment": "...", "pageLabel": "3", "color": "#ffd400", "tags": ["..."]}]}` across all the item's
attachments (or the one attachment), sorted by attachment key then Zotero's annotation sort index.

### Write tools
Writes need the user's approval in the client UI before running. They change only the local
library; normal Zotero sync propagates them.

- Whether to ask is one rule on every client: `shouldAsk(tool, autoApprove)` is true for tools with
  `x-always-ask` (trash), and otherwise for `x-write` tools unless `autoApprove` is on. Reads never ask.
- Arguments are checked against the tool's schema before anything is asked or run: a plain object,
  required keys present (an omitted nullable becomes `null`), correct types (integers are whole
  numbers, array items have the item type), no unknown keys. Otherwise `invalid_argument`.
- Tools marked `x-mcp-only` (`zotero_await_approval`) exist only on the MCP server (`mcp.md`); chat
  clients never offer them to the model.

- `zotero_batch_update_tags` → `{"updated": ["KEY1", ...]}` keys whose tags changed, sorted.
  Adding an existing tag or removing a missing one is not a change. Unknown keys → `not_found`
  and nothing is changed.
- `zotero_set_item_collections` → `{"updated": [...]}` same rules; unknown item or collection key →
  `not_found`.
- `zotero_create_collection` → `{"key": "NEWKEY", "name": "..."}`.
- `item_keys` lists are de-duplicated before use.
- `zotero_create_note` → `{"key": "NEWKEY"}`. `content` may be empty (a note that's just its
  title). The note HTML is
  `<div data-schema-version="9"><h1>{title}</h1>\n{markdown rendered to HTML}</div>` (title
  HTML-escaped; a newline after `</h1>` so every client derives the note title from the heading
  alone) and carries `tags`.
- `zotero_add_item_by_identifier` → `{"key": "NEWKEY", "title": "...", "itemType": "..."}`.
- `zotero_trash_items` → `{"trashed": [...]}` sorted.
