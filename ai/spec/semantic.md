# Semantic search (spec v1)

`zotero_semantic_search` finds items by meaning ("papers about making attention cheaper"), not by
words. One host builds an **index** of the library with an embedding model; the index syncs like
any other Zotero file; every client embeds the question with the **same model file** and searches
that index, so every client returns the same items.

## Model (pinned)

- `onnx-community/embeddinggemma-2-ONNX` at revision `daa72c51243991dfcaf9f9137d2c573d8f7790c0`
  (EmbeddingGemma 2, Apache 2.0), int8 text model, downloaded from
  `https://huggingface.co/onnx-community/embeddinggemma-2-ONNX/resolve/<revision>/<file>`:

  | File | sha256 |
  |---|---|
  | `onnx/model_quantized.onnx` | `d06edd601f851c633a2519304cbeb8dc6170d7ceb61b436625c17fb9b6e74953` |
  | `onnx/model_quantized.onnx_data` | `278a7ff1248c3618e4bd11a607fc54f7bdc7778854230f3956d3f86bd9db4f3b` |
  | `tokenizer.json` | `4d777ef5bdc1aa36227abdfb77c3e49e7b9c892d16e1b6bda41c393504828be4` |

  A file whose sha256 differs is deleted and never used. Files are fetched at runtime (never
  bundled) only after the user turns semantic search on for that device; the download is 346 MB.
- Runtime: ONNX Runtime on CPU everywhere (Python `onnxruntime`, `onnxruntime-web` WASM in the
  desktop plugin, `onnxruntime-android`), so vectors agree to cosine ≥ 0.9999 across hosts.
- Inputs: `input_ids` and `attention_mask` (int64, shape `[1, n]`), plus empty float32 tensors of
  shape `[0, 512]` for `image_features`, `video_features` and `audio_features`. Output:
  `sentence_embedding` (768 floats, already L2-normalised).
- Tokenizer: `tokenizer.json` as the Hugging Face `tokenizers` library applies it (BOS and EOS are
  added by its post-processor). Every client must produce exactly the same ids (conformance checks
  it); at most 2048 tokens (longer input is cut, keeping the final EOS).
- Text before tokenizing: Unicode NFC, runs of whitespace → one space, trimmed, where whitespace is
  exactly the characters Python's `str.isspace()` accepts: U+0009–U+000D, U+001C–U+001F, U+0020,
  U+0085, U+00A0, U+1680, U+2000–U+200A, U+2028, U+2029, U+202F, U+205F and U+3000 (not U+FEFF;
  JavaScript's `\s` and `trim()` and Kotlin's `trim()` use other sets).
  - Query: `task: search result | query: {text}`
  - Document chunk: `title: {item title, or "none"} | text: {chunk text}`

## Index file

A ZIP named `zotero-ai-index.zip`, stored as a **standalone stored-file attachment** in My Library
titled `Zotero AI search index`, content type `application/zip`, tagged `_zotero-ai-index`. It
syncs through Zotero storage or WebDAV like any attachment. Readers use the most recently modified
non-trashed stored-file attachment with that tag and no parent item (ties: the lowest key). It isn't a regular item, so no other tool
lists it.

Entries (`chunks.jsonl` may be deflated; `vectors.i8` is stored uncompressed):
- `manifest.json`:
  ```json
  {"format": "zotero-ai-index", "version": 1,
   "model": {"id": "onnx-community/embeddinggemma-2-ONNX", "revision": "daa72c51…",
             "sha256": {"onnx/model_quantized.onnx": "…", "onnx/model_quantized.onnx_data": "…", "tokenizer.json": "…"}},
   "dim": 512, "count": 4567, "items": 123, "builtAt": "2026-11-11T10:00:00Z",
   "libraryVersion": 1234, "builder": {"host": "desktop", "version": "1.2.0", "id": "<random id of the building install>"}}
  ```
  A reader refuses an index whose `format`, `version`, `model.revision` or `model.sha256` differ
  from its own (`unavailable`, "The search index was built with a different model; rebuild it.").
- `chunks.jsonl`: one JSON object per row, in row order:
  `{"itemKey": "…", "source": "metadata" | "fulltext" | "note", "key": "<attachment or note key>" | null, "text": "…"}`.
- `vectors.i8`: `count × dim` signed bytes, row-major: each row is the chunk's embedding cut to its
  first `dim` values, renormalised, multiplied by 127 and rounded to the nearest integer.

The index comes from synced storage, so readers treat it as untrusted and check it **while
unzipping**, before allocating anything sized from it:
- The ZIP holds exactly `manifest.json`, `chunks.jsonl` and `vectors.i8` (no other or repeated
  names); the file is at most 512 MiB; `manifest.json` at most 64 KiB unzipped.
- `dim` is an integer 1–768, `count` an integer 1–200,000; `vectors.i8` unzips to exactly
  `count × dim` bytes; `chunks.jsonl` to at most 512 MiB, exactly `count` lines of at most 64 KiB
  each (without the newline), each an object with
  an 8-character `itemKey`, `source` one of the three values, `key` a string or null, and `text` a
  string. Unzipped bytes are counted as they're produced; a reader stops as soon as a limit is passed.
- Readers keep only the first 800 characters of each chunk's text (all a passage shows).
- Anything else → `unavailable` ("The search index is damaged or too large; rebuild it.").
The builder never writes more than 200,000 rows (items past that are left out and the dialog says so).

## Building (the desktop plugin in v1)

Off until the user turns it on (it downloads the model and uses CPU in the background). One
library has one builder; a builder that finds an index from another builder (a different
`builder.id`, which each install keeps in its prefs) only reads it.

For every regular item (not in the trash):
- one `metadata` chunk: `{title}. {all creators "First Last", comma-separated}. {year}. {publication}.`
  then, if any, a blank line and the abstract, then a blank line and `Tags: {tags, comma-separated}`
  (empty parts left out);
- `fulltext` chunks of the text `zotero_get_item_fulltext` reads (the same attachment, the whole
  text), `key` = that attachment;
- `note` chunks of each child note's text (the `zotero_get_notes` conversion), `key` = the note.

Long texts are cut into windows of at most 512 tokens including the document prefix, BOS and EOS
(title cut to 32 tokens), overlapping by 64 tokens, each window ending at the last sentence end
(`.`, `?`, `!` followed by whitespace) or line break within its final 64 tokens when there is one.
Chunk text is stored as cut. The builder works through items in the background, keeps per-item
progress so it can stop and resume, re-embeds only items that changed (the item, its notes or
its full text), and replaces the index file at most once a day (and when a first build finishes, or
when the user asks for a build). Pages aren't tracked in v1.

## zotero_semantic_search

Arguments: `query` (string), `limit` (1–20, default 10).

1. Normalise `query`; empty → `invalid_argument` ("query is empty."). No index in the library →
   `unavailable` ("There's no search index yet. Turn on semantic search in the Zotero desktop
   plugin to build one."). Semantic search off on this device, or the model not downloaded yet →
   `unavailable` ("Semantic search is off on this device. Turn it on in the Zotero AI settings.").
   The index can't be downloaded or read → `unavailable`.
2. Embed the query; take its first `dim` values and renormalise → `q`.
3. Score every row `r` (as signed bytes): `score = (q · r) / |r|`. Rank rows by score, highest
   first; equal scores by row number.
4. Walk all of the best 200 rows in rank order. Skip rows whose item isn't a regular item in this
   client's library now (deleted or trashed since the build). A row of an item not listed yet adds
   the item (its score is that row's, its first passage that row's chunk) if fewer than `limit` items
   are listed, and is ignored otherwise; a row of a listed item adds a passage if the item has fewer
   than 2. So a listed item can take its second passage from any of the 200 rows.
5. `score` is rounded to 3 decimals (`floor(v × 1000 + 0.5) / 1000`). Passage `text` is the chunk
   text cut to 800 characters (UTF-16 code units).

Result:
```json
{"index": {"builtAt": "2026-11-11T10:00:00Z", "items": 123, "chunks": 4567},
 "items": [{"key": "…", "itemType": "…", "title": "…", "creators": "…", "year": "…", "tags": [],
            "score": 0.734,
            "passages": [{"source": "fulltext", "key": "ATTACHKEY", "text": "…"}]}]}
```
`items` entries are `ItemSummary` (`tools.md`) plus `score` and `passages`. Items added after the
index was built aren't found; `index.builtAt` tells the model how fresh it is.

## Conformance

`ai/conformance/semantic/` holds a golden index (a public corpus with made-up item keys), its
queries, their token ids and embeddings from the Python reference, and the expected ranked rows.
Each client has a test hook that, for a given index file and text, returns the token ids, the
query embedding, and the ranked rows (`[{row, itemKey, score}]` for the best 20, scores unrounded).
Checks: identical token ids; query embedding cosine ≥ 0.9999 to the golden one; identical
top-10 rows (a swap allowed only where scores differ by < 1e-4). On the real library, the tool's
results from an index the desktop built are compared across clients like any other tool (scores
within 0.002).
