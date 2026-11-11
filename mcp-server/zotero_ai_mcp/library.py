"""The ai/spec Zotero tools against the Zotero Web API (ai/spec/tools.md), for the standalone server.

A cached copy of My Library is kept in memory and brought up to date with `?since=<version>`
before each tool call (at most every few seconds). Results must equal the desktop plugin's and the
Android app's; ai/conformance/run.py compares them.
"""
import base64
import html
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET

from . import markdown_lite, pdf, semantic

API = "https://api.zotero.org"
NON_REGULAR = {"attachment", "note", "annotation"}
TEXT_TYPES = ["application/pdf", "application/epub+zip", "text/html"]
PDF_TOOLS = {"zotero_get_pdf_info", "zotero_get_page_image", "zotero_get_pdf_file"}
FILE_SLOTS = threading.BoundedSemaphore(2)


class ToolError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def utf16(s):
    """Sort key matching JavaScript/Kotlin string order (UTF-16 code units)."""
    return (s or "").encode("utf-16-be")


def compare_key(s):
    return utf16((s or "").lower())


def year(date):
    m = re.search(r"(?<!\d)\d{4}(?!\d)", date or "", re.ASCII)
    return m.group(0) if m else ""


def u16len(s):
    """Length in UTF-16 code units, as JavaScript and Kotlin count it."""
    return len(s.encode("utf-16-le", "surrogatepass")) // 2


def export_result(keys, fmt, found):
    """zotero_export_items' result from zotero.org's items (key → item with bibtex and csljson)."""
    for key in keys:
        if key not in found:
            raise ToolError("not_found", f"{key} hasn't synced to zotero.org yet.")
    if fmt == "bibtex":
        text = "\n\n".join(found[k].get("bibtex", "").strip() for k in keys)
    else:
        text = json.dumps([{**found[k].get("csljson", {}), "id": k} for k in keys], ensure_ascii=False, separators=(",", ":"))
    return {"format": fmt, "keys": keys, "text": text}


def u16slice(s, start, end):
    """UTF-16 code units start…end (may split a surrogate pair, as String.slice does)."""
    if start == 0 and u16len(s) <= end:
        return s
    return s.encode("utf-16-le", "surrogatepass")[2 * start:2 * end].decode("utf-16-le", "surrogatepass")


def u16cut(s, n):
    """The first n UTF-16 code units (may end in half a surrogate pair, as String.slice does)."""
    if u16len(s) <= n:
        return s
    return s.encode("utf-16-le", "surrogatepass")[:2 * n].decode("utf-16-le", "surrogatepass")


def note_title(note_html):
    """Zotero's noteToTitle rule (tools.md)."""
    trimmed = (note_html or "").strip()
    text = re.sub(r"(</(h\d|p|div)+>)", r"\1\n", trimmed)
    text = re.sub(r"<br\s*/?>", " ", text)
    text = re.sub(r"<[^>]*>", "", text)
    text = html.unescape(text)
    if re.search(r"^<[^>\n]+[^/]>\n", trimmed):
        text = text.strip()
    title = u16cut(text, 120)
    i = title.find("\n")
    return title[:i] if i > -1 else title


def plain_text(note_html):
    """Note HTML → text, whitespace collapsed (as the clients do)."""
    text = re.sub(r"</?(p|div|h[1-6]|li|br|tr|blockquote|pre)\b[^>]*>", " ", note_html or "", flags=re.I)
    text = re.sub(r"<[^>]*>", "", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


class Library:
    def __init__(self, api_key, user_id=None, sync_interval=3.0):
        self.api_key = api_key
        self.user_id = user_id or self._get("/keys/current")["userID"]
        self.base = f"/users/{self.user_id}"
        self.sync_interval = sync_interval
        self.version = 0
        self.items = {}  # key → data
        self.collections = {}  # key → data
        self.last_sync = 0.0
        self.lock = threading.RLock()
        self.files = None  # pdf.Files, set by the server
        self.semantic = None  # semantic.Semantic, set by the server
        schema = self._get("/schema", base=API)
        self.fields = {}  # itemType → {baseField: field}
        self.type_fields = {}  # itemType → set of field names
        self.primary_creator = {}
        for t in schema["itemTypes"]:
            self.fields[t["itemType"]] = {f["baseField"]: f["field"] for f in t["fields"] if "baseField" in f}
            self.type_fields[t["itemType"]] = {f["field"] for f in t["fields"]}
            primary = [c["creatorType"] for c in t.get("creatorTypes", []) if c.get("primary")]
            self.primary_creator[t["itemType"]] = primary[0] if primary else None

    # region Web API

    def _request(self, method, path, body=None, headers=None, base=None, raw=False):
        url = (base or API) + path
        req = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode())
        req.add_header("Zotero-API-Key", self.api_key)
        req.add_header("Zotero-API-Version", "3")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
            if raw:
                return resp, data
            return json.loads(data) if data else None

    def _get(self, path, base=None):
        return self._request("GET", path, base=base)

    def _fetch_all(self, path, params):
        out, start = [], 0
        while True:
            q = urllib.parse.urlencode({**params, "start": start, "limit": 100, "format": "json"})
            resp, data = self._request("GET", f"{path}?{q}", raw=True)
            page = json.loads(data)
            out += page
            version = int(resp.headers.get("Last-Modified-Version", "0"))
            total = int(resp.headers.get("Total-Results", len(out)))
            start += len(page)
            if not page or start >= total:
                return out, version

    def sync(self, force=False):
        with self.lock:
            if not force and time.time() - self.last_sync < self.sync_interval:
                return
            since = self.version
            items, v1 = self._fetch_all(f"{self.base}/items", {"since": since, "includeTrashed": 1})
            collections, v2 = self._fetch_all(f"{self.base}/collections", {"since": since})
            # Requests aren't atomic: advance only to the oldest version seen, so a change made
            # between them is fetched again next time rather than skipped.
            for i in items:
                self.items[i["key"]] = i["data"]
            for c in collections:
                self.collections[c["key"]] = c["data"]
            if since:
                deleted = self._get(f"{self.base}/deleted?since={since}")
                for key in deleted.get("items", []):
                    self.items.pop(key, None)
                for key in deleted.get("collections", []):
                    self.collections.pop(key, None)
            self.version = max(self.version, min(v1, v2))
            self.last_sync = time.time()

    # endregion

    # region Item helpers

    def field(self, item, name):
        value = item.get(name)
        if value is None:
            mapped = self.fields.get(item.get("itemType"), {}).get(name)
            value = item.get(mapped) if mapped else None
        return str(value or "").strip()

    def is_regular(self, item):
        return item.get("itemType") not in NON_REGULAR and not item.get("deleted")

    def title(self, item):
        if item.get("itemType") == "note":
            return note_title(item.get("note", "")).strip()
        return self.field(item, "title")

    def tag_names(self, item):
        seen = []
        for t in item.get("tags", []):
            if t.get("tag") not in seen:
                seen.append(t.get("tag"))
        return seen

    def sorted_tags(self, item):
        return sorted(self.tag_names(item), key=lambda t: (compare_key(t), utf16(t)))

    def title_order(self, item):
        return (compare_key(self.title(item)), utf16(item["key"]))

    def creator_summary(self, item):
        creators = item.get("creators", [])
        primary = self.primary_creator.get(item.get("itemType"))
        chosen = [c for c in creators if c.get("creatorType") == primary] or creators
        names = [(c.get("lastName") or c.get("name") or "").strip() or (c.get("firstName") or "").strip() for c in chosen]
        if not names:
            return ""
        if len(names) == 1:
            return names[0]
        if len(names) == 2:
            return f"{names[0]} and {names[1]}"
        return f"{names[0]} et al."

    def summary(self, item):
        return {"key": item["key"], "itemType": item["itemType"], "title": self.title(item),
                "creators": self.creator_summary(item), "year": year(self.field(item, "date")), "tags": self.sorted_tags(item)}

    def searchable(self, item):
        out = [self.title(item).lower()]
        for c in item.get("creators", []):
            out += [(c.get("firstName") or "").lower(), (c.get("lastName") or "").lower(), (c.get("name") or "").lower()]
        out.append(year(self.field(item, "date")))
        out += [t.lower() for t in self.tag_names(item)]
        return out

    def get_item(self, key):
        item = self.items.get(key)
        if not item:
            raise ToolError("not_found", f"No item with key {key}.")
        return item

    def get_collection(self, key):
        c = self.collections.get(key)
        if not c or c.get("deleted"):
            raise ToolError("not_found", f"No collection with key {key}.")
        return c

    def children(self, key, item_type=None):
        return [i for i in self.items.values() if i.get("parentItem") == key and not i.get("deleted")
                and (item_type is None or i.get("itemType") == item_type)]

    def regular_items(self):
        return [i for i in self.items.values() if self.is_regular(i)]

    def in_collection(self, item, key):
        c = self.collections.get(key)
        return key in item.get("collections", []) and c is not None and not c.get("deleted")

    # endregion

    # region Arguments (same helpers as tools.js)

    @staticmethod
    def _str(args, name):
        v = args.get(name)
        v = v.strip() if isinstance(v, str) else ""
        if not v:
            raise ToolError("invalid_argument", f"Missing {name}.")
        return v

    @staticmethod
    def _str_or_none(args, name):
        v = args.get(name)
        v = v.strip() if isinstance(v, str) else ""
        return v or None

    @staticmethod
    def _str_list(args, name):
        v = args.get(name)
        return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []

    @staticmethod
    def _limit(args, name, default, maximum):
        v = args.get(name)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return default
        return min(max(int(v), 1), maximum)

    @staticmethod
    def _int_or_none(args, name):
        v = args.get(name)
        return None if isinstance(v, bool) or not isinstance(v, (int, float)) else int(v)

    @staticmethod
    def _offset(args):
        v = args.get("offset")
        return 0 if isinstance(v, bool) or not isinstance(v, (int, float)) else max(int(v), 0)

    @staticmethod
    def _unique(values):
        out = []
        for v in values:
            if v not in out:
                out.append(v)
        return out

    # endregion

    # region Tools

    def execute(self, name, args):
        """Runs a tool (arguments already validated). Never raises: failures are error objects.
        A page image or PDF comes back as extra MCP content parts under "_content"."""
        try:
            fn = getattr(self, "t_" + name.removeprefix("zotero_"), None)
            if not fn:
                raise ToolError("invalid_argument", f"Unknown tool {name}")
            with self.lock:
                self.sync()
                if name in PDF_TOOLS:
                    attachment = dict(self.pdf_attachment(self._str(args, "item_key")))
                elif name == "zotero_semantic_search":
                    attachment = self.index_attachment()
                else:
                    return fn(args)
            # Downloading, rendering and embedding can take a while: not under the library lock.
            return fn(args, attachment)
        except (ToolError, pdf.PdfError, semantic.SemanticError) as e:
            return {"error": e.code, "message": str(e)}
        except urllib.error.HTTPError as e:
            return {"error": "failed", "message": f"Zotero Web API: HTTP {e.code}"}
        except Exception as e:  # noqa: BLE001 — a tool result, not a crash
            return {"error": "failed", "message": str(e) or e.__class__.__name__}

    def t_search_items(self, args):
        words = [w for w in (args.get("query") if isinstance(args.get("query"), str) else "").lower().split() if w]
        tag = self._str_or_none(args, "tag")
        collection_key = self._str_or_none(args, "collection_key")
        item_type = self._str_or_none(args, "item_type")
        year_from, year_to = self._int_or_none(args, "year_from"), self._int_or_none(args, "year_to")
        limit, offset = self._limit(args, "limit", 20, 100), self._offset(args)
        mode = args.get("mode")
        if mode not in (None, "metadata", "everything"):
            raise ToolError("invalid_argument", "mode must be metadata or everything.")
        everything = mode == "everything"
        fulltext = {w: self.fulltext_matches(w) for w in words} if everything else {}
        children = self.children_by_parent() if everything else {}
        items = []
        for item in self.regular_items():
            texts = self.searchable(item) + (self.searchable_everything(item, children) if everything else [])
            if all(any(w in t for t in texts) or item["key"] in fulltext.get(w, ()) for w in words):
                items.append(item)
        if tag is not None:
            items = [i for i in items if tag in self.tag_names(i)]
        if collection_key is not None:
            items = [i for i in items if self.in_collection(i, collection_key)]
        if item_type is not None:
            items = [i for i in items if i.get("itemType") == item_type]
        if year_from is not None or year_to is not None:
            def in_range(i):
                y = year(self.field(i, "date"))
                return bool(y) and (year_from is None or int(y) >= year_from) and (year_to is None or int(y) <= year_to)
            items = [i for i in items if in_range(i)]
        items.sort(key=self.title_order)
        return {"total": len(items), "items": [self.summary(i) for i in items[offset:offset + limit]]}

    def children_by_parent(self):
        """parent key → its non-trashed child items (notes, attachments, annotations)."""
        out = {}
        for i in self.items.values():
            if i.get("parentItem") and not i.get("deleted"):
                out.setdefault(i["parentItem"], []).append(i)
        return out

    def searchable_everything(self, item, children):
        """The extra texts `mode: everything` looks in (tools.md, zotero_search_items)."""
        out = [str(item.get(f) or "").lower() for f in self.type_fields.get(item.get("itemType"), ())]
        for child in children.get(item["key"], []):
            if child.get("itemType") == "note":
                out.append(plain_text(child.get("note", "")).lower())
            elif child.get("itemType") == "attachment":
                out.append(self.title(child).lower())
                for a in children.get(child["key"], []):
                    if a.get("itemType") == "annotation":
                        out += [str(a.get("annotationText") or "").lower(), str(a.get("annotationComment") or "").lower()]
        return out

    def fulltext_matches(self, word):
        """Keys of regular items whose attachments zotero.org's full-text search finds `word` in."""
        try:
            resp, data = self._request("GET", f"{self.base}/items?" + urllib.parse.urlencode({"q": word, "qmode": "everything", "format": "keys"}), raw=True)
        except (urllib.error.URLError, OSError):
            raise ToolError("unavailable", "Searching full text needs zotero.org, which can't be reached.")
        found = set()
        # Only attachments: fields, notes and annotations are matched locally, the same way on every client.
        for key in data.decode("utf-8").split():
            attachment = self.items.get(key)
            if not attachment or attachment.get("itemType") != "attachment" or attachment.get("deleted"):
                continue
            item = self.items.get(attachment.get("parentItem") or "")
            if item and self.is_regular(item):
                found.add(item["key"])
        return found

    def t_get_item_metadata(self, args):
        item = self.get_item(self._str(args, "item_key"))
        regular = item.get("itemType") not in NON_REGULAR

        def creator_name(c):
            if "name" in c:
                return (c.get("name") or "").strip()
            return f"{(c.get('firstName') or '').strip()} {(c.get('lastName') or '').strip()}".strip()

        publication = next((v for v in (self.field(item, f) for f in ("publicationTitle", "proceedingsTitle", "bookTitle", "websiteTitle")) if v), "")
        collections = [self.collections[k] for k in item.get("collections", []) if k in self.collections and not self.collections[k].get("deleted")]
        collections.sort(key=lambda c: (compare_key(c["name"]), utf16(c["key"])))
        attachments = self.children(item["key"], "attachment") if regular else []
        notes = self.children(item["key"], "note") if regular else []
        return {
            "key": item["key"], "itemType": item["itemType"], "title": self.title(item),
            "creators": [{"type": c.get("creatorType"), "name": creator_name(c)} for c in item.get("creators", [])],
            "date": self.field(item, "date"), "year": year(self.field(item, "date")), "publication": publication,
            "doi": self.field(item, "DOI"), "url": self.field(item, "url"), "abstract": self.field(item, "abstractNote"),
            "citationKey": self.field(item, "citationKey"),
            "tags": self.sorted_tags(item),
            "collections": [{"key": c["key"], "name": c["name"]} for c in collections],
            "attachments": [{"key": a["key"], "title": self.title(a), "contentType": a.get("contentType") or ""} for a in sorted(attachments, key=self.title_order)],
            "notes": [{"key": n["key"], "title": self.title(n)} for n in sorted(notes, key=self.title_order)],
        }

    def text_attachment(self, item):
        candidates = [item] if item.get("itemType") == "attachment" else self.children(item["key"], "attachment")
        candidates = [a for a in candidates if a.get("contentType") in TEXT_TYPES]
        candidates.sort(key=lambda a: (TEXT_TYPES.index(a["contentType"]), *self.title_order(a)))
        return candidates[0] if candidates else None

    def t_get_item_fulltext(self, args):
        max_chars = self._limit(args, "max_chars", 20000, 100000)
        start = max(self._int_or_none(args, "start") or 0, 0)
        attachment = self.text_attachment(self.get_item(self._str(args, "item_key")))
        if not attachment:
            raise ToolError("not_found", "This item has no PDF, EPUB or snapshot.")
        try:
            text = self._get(f"{self.base}/items/{attachment['key']}/fulltext")["content"]
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise ToolError("unavailable", "This attachment's text hasn't been indexed yet.")
            raise
        total = u16len(text)
        part = u16slice(text, start, start + max_chars)
        return {"attachmentKey": attachment["key"], "totalChars": total, "truncated": start + u16len(part) < total, "text": part}

    def pdf_attachment(self, key):
        item = self.get_item(key)
        candidates = [item] if item.get("itemType") == "attachment" else self.children(item["key"], "attachment")
        candidates = sorted((a for a in candidates if a.get("contentType") == "application/pdf"), key=self.title_order)
        if not candidates:
            raise ToolError("not_found", "This item has no PDF.")
        return candidates[0]

    def _pdf_path(self, attachment):
        if not self.files:
            raise ToolError("unavailable", "This server isn't set up to read files.")
        return self.files.path(attachment)

    def t_get_pdf_info(self, args, attachment):
        path, _ = self._pdf_path(attachment)
        return {"attachmentKey": attachment["key"], "pageCount": pdf.page_count(path)}

    def t_get_page_image(self, args, attachment):
        path, _ = self._pdf_path(attachment)
        page_count, width, height, jpeg = pdf.render(path, args.get("page"), args.get("region"), args.get("max_width"))
        return {"attachmentKey": attachment["key"], "page": args.get("page"), "pageCount": page_count,
                "width": width, "height": height, "mimeType": "image/jpeg",
                "_content": [{"type": "image", "data": base64.b64encode(jpeg).decode("ascii"), "mimeType": "image/jpeg"}]}

    def t_get_pdf_file(self, args, attachment):
        path, filename = self._pdf_path(attachment)
        size = os.path.getsize(path)
        if size > pdf.MAX_RESOURCE_BYTES:
            raise ToolError("unavailable", f"The PDF is {size / 1048576:.1f} MB; the limit is 25 MB. Use zotero_get_page_image instead.")
        with FILE_SLOTS, open(path, "rb") as f:  # a few files read and encoded at a time
            blob = base64.b64encode(f.read()).decode("ascii")
        return {"attachmentKey": attachment["key"], "filename": filename, "size": size,
                "_content": [{"type": "resource", "resource": {"uri": f"zotero://attachment/{attachment['key']}", "mimeType": "application/pdf",
                                                               "blob": blob}}]}

    def index_attachment(self):
        """The semantic search index: the most recently modified standalone stored-file attachment
        tagged _zotero-ai-index that isn't in the trash (a copy), or None."""
        found = [i for i in self.items.values() if i.get("itemType") == "attachment" and i.get("linkMode") == "imported_file"
                 and not i.get("parentItem") and not i.get("deleted") and semantic.INDEX_TAG in self.tag_names(i)]
        found.sort(key=lambda i: utf16(i["key"]))
        # The most recently modified; ties: the lowest key (semantic.md).
        return dict(max(found, key=lambda i: i.get("dateModified", ""))) if found else None

    def t_semantic_search(self, args, attachment):
        query = semantic.normalize(args.get("query") if isinstance(args.get("query"), str) else "")
        if not query:
            raise ToolError("invalid_argument", "query is empty.")
        limit = self._limit(args, "limit", 10, 20)
        if not self.semantic:
            raise ToolError("unavailable", semantic.NO_INDEX if attachment is None else semantic.OFF)
        index, ranked = self.semantic.search(query, attachment, self.files)
        items = {}
        with self.lock:
            for row, score in ranked:
                chunk = index.chunk(row)
                item = self.items.get(chunk["itemKey"])
                if not item or not self.is_regular(item):  # deleted or trashed since the build
                    continue
                entry = items.get(item["key"])
                if entry is None:
                    if len(items) == limit:  # full: later rows can only add passages to listed items
                        continue
                    entry = items[item["key"]] = {**self.summary(item), "score": semantic.round3(score), "passages": []}
                if len(entry["passages"]) < semantic.PASSAGES_PER_ITEM:
                    entry["passages"].append({"source": chunk.get("source"), "key": chunk.get("key"),
                                              "text": u16cut(chunk["text"], semantic.PASSAGE_CHARS)})
        return {"index": index.info(), "items": list(items.values())}

    def t_export_items(self, args):
        keys = self._unique(self._str_list(args, "item_keys"))
        fmt = args.get("format")
        if not 1 <= len(keys) <= 50:
            raise ToolError("invalid_argument", "Give 1 to 50 item keys.")
        if fmt not in ("csl_json", "bibtex"):
            raise ToolError("invalid_argument", "format must be csl_json or bibtex.")
        for key in keys:
            item = self.items.get(key)
            if not item or not self.is_regular(item):
                raise ToolError("not_found", f"No item with key {key}.")
        try:
            found = self._get(f"{self.base}/items?" + urllib.parse.urlencode({"itemKey": ",".join(keys), "include": "bibtex,csljson", "limit": 50}))
        except (urllib.error.URLError, OSError) as e:
            if isinstance(e, urllib.error.HTTPError):
                raise
            raise ToolError("unavailable", "Exporting needs zotero.org, which can't be reached.")
        return export_result(keys, fmt, {i["key"]: i for i in found})

    def t_get_collections(self, args):
        collections = [c for c in self.collections.values() if not c.get("deleted")]
        collections.sort(key=lambda c: (compare_key(c["name"]), utf16(c["key"])))
        regular = self.regular_items()
        return {"collections": [{
            "key": c["key"], "name": c["name"], "parentKey": c.get("parentCollection") or None,
            "itemCount": sum(1 for i in regular if c["key"] in i.get("collections", [])),
        } for c in collections]}

    def t_get_collection_items(self, args):
        c = self.get_collection(self._str(args, "collection_key"))
        limit = self._limit(args, "limit", 50, 100)
        items = sorted((i for i in self.regular_items() if c["key"] in i.get("collections", [])), key=self.title_order)
        offset = self._offset(args)
        return {"collection": {"key": c["key"], "name": c["name"]}, "total": len(items), "items": [self.summary(i) for i in items[offset:offset + limit]]}

    def t_get_tags(self, args):
        limit = self._limit(args, "limit", 200, 500)
        counts = {}
        for item in self.regular_items():
            for t in self.tag_names(item):
                counts[t] = counts.get(t, 0) + 1
        ordered = sorted(counts.items(), key=lambda kv: (-kv[1], compare_key(kv[0]), utf16(kv[0])))
        offset = self._offset(args)
        return {"total": len(ordered), "tags": [{"tag": t, "count": n} for t, n in ordered[offset:offset + limit]]}

    def t_get_recent(self, args):
        limit = self._limit(args, "limit", 10, 100)
        items = self.regular_items()
        items.sort(key=lambda i: utf16(i["key"]))
        items.sort(key=lambda i: i.get("dateAdded", ""), reverse=True)
        return {"items": [{**self.summary(i), "dateAdded": i.get("dateAdded", "")} for i in items[:limit]]}

    def t_get_notes(self, args):
        parent_key = self._str_or_none(args, "item_key")
        query = self._str_or_none(args, "query")
        query = query.lower() if query else None
        limit, offset = self._limit(args, "limit", 20, 50), self._offset(args)
        if parent_key is not None:
            self.get_item(parent_key)
        notes = []
        for n in self.items.values():
            if n.get("itemType") != "note" or n.get("deleted"):
                continue
            text = plain_text(n.get("note", ""))
            if (parent_key is None or n.get("parentItem") == parent_key) and (query is None or query in text.lower()):
                notes.append((n, text))
        notes.sort(key=lambda nt: utf16(nt[0]["key"]))
        notes.sort(key=lambda nt: nt[0].get("dateModified", ""), reverse=True)
        return {"total": len(notes), "notes": [{"key": n["key"], "parentKey": n.get("parentItem") or None,
                                                 "title": self.title(n), "text": u16cut(text, 2000)} for n, text in notes[offset:offset + limit]]}

    def t_get_annotations(self, args):
        item = self.get_item(self._str(args, "item_key"))
        attachments = [item] if item.get("itemType") == "attachment" else self.children(item["key"], "attachment")
        attachments.sort(key=lambda a: utf16(a["key"]))
        out = []
        for a in attachments:
            if a.get("linkMode") == "linked_url":
                continue
            annotations = self.children(a["key"], "annotation")
            annotations.sort(key=lambda x: (utf16(x.get("annotationSortIndex") or ""), utf16(x["key"])))
            for x in annotations:
                out.append({"key": x["key"], "attachmentKey": a["key"], "type": x.get("annotationType") or "",
                            "text": (x.get("annotationText") or "").strip(), "comment": (x.get("annotationComment") or "").strip(),
                            "pageLabel": (x.get("annotationPageLabel") or "").strip(), "color": (x.get("annotationColor") or "").strip(),
                            "tags": self.sorted_tags(x)})
        return {"annotations": out}

    # Writes

    def _update(self, key, compute):
        """Applies compute(item data) → changes (or None for no change) with the item's version as a
        precondition; if someone else changed it meanwhile (412), re-reads it and computes again."""
        for attempt in (1, 2):
            item = self.items[key]
            changes = compute(item)
            if changes is None:
                return False
            try:
                self._request("PATCH", f"{self.base}/items/{key}", body=changes,
                              headers={"If-Unmodified-Since-Version": str(item["version"])})
                self.last_sync = 0  # pick the change up on the next read
                return True
            except urllib.error.HTTPError as e:
                if e.code != 412 or attempt == 2:
                    raise
                self.items[key] = self._get(f"{self.base}/items/{key}")["data"]
        return False

    def _create(self, kind, objects):
        result = self._request("POST", f"{self.base}/{kind}", body=objects, headers={"Zotero-Write-Token": uuid.uuid4().hex})
        self.last_sync = 0
        if result.get("failed"):
            raise ToolError("failed", next(iter(result["failed"].values())).get("message", "Zotero refused the change."))
        return result["successful"]["0"]

    def t_batch_update_tags(self, args):
        keys = self._unique(self._str_list(args, "item_keys"))
        add = self._unique([t.strip() for t in self._str_list(args, "add_tags") if t.strip()])
        remove = set(self._str_list(args, "remove_tags"))
        items = [self.get_item(k) for k in keys]

        def compute(item):
            kept = [t for t in item.get("tags", []) if t.get("tag") not in remove]
            names = self.tag_names(item)
            added = [{"tag": t} for t in add if t not in names]
            if len(kept) == len(item.get("tags", [])) and not added:
                return None
            return {"tags": kept + added}

        updated = [item["key"] for item in items if self._update(item["key"], compute)]
        return {"updated": sorted(updated, key=utf16)}

    def t_set_item_collections(self, args):
        keys = self._unique(self._str_list(args, "item_keys"))
        items = [self.get_item(k) for k in keys]
        add_to = [self.get_collection(k)["key"] for k in self._str_list(args, "add_to")]
        remove_from = [self.get_collection(k)["key"] for k in self._str_list(args, "remove_from")]

        def compute(item):
            # Add first, then remove, as the desktop does (a key in both ends up removed).
            current = list(item.get("collections", []))
            new = current + [k for k in add_to if k not in current]
            new = [k for k in new if k not in remove_from]
            return None if new == current else {"collections": new}

        updated = [item["key"] for item in items if self._update(item["key"], compute)]
        return {"updated": sorted(self._unique(updated), key=utf16)}

    def t_create_collection(self, args):
        name = self._str(args, "name")
        parent = self._str_or_none(args, "parent_collection_key")
        if parent is not None:
            self.get_collection(parent)
        key = self._create("collections", [{"name": name, "parentCollection": parent or False}])
        return {"key": key if isinstance(key, str) else key["key"], "name": name}

    def t_create_note(self, args):
        parent = self._str_or_none(args, "parent_item_key")
        if parent is not None:
            self.get_item(parent)
        content = args.get("content") if isinstance(args.get("content"), str) else ""
        note = (f'<div data-schema-version="9"><h1>{html.escape(self._str(args, "title"), quote=True).replace("&#x27;", "'")}</h1>\n'
                + markdown_lite.to_html(content) + "</div>")
        obj = {"itemType": "note", "note": note, "tags": [{"tag": t} for t in self._str_list(args, "tags")], "collections": []}
        if parent:
            obj["parentItem"] = parent
        created = self._create("items", [obj])
        return {"key": created if isinstance(created, str) else created["key"]}

    def t_add_item_by_identifier(self, args):
        identifier = self._str(args, "identifier")
        collection_key = self._str_or_none(args, "collection_key")
        if collection_key is not None:
            self.get_collection(collection_key)
        data = lookup_identifier(identifier)
        item_type = data.pop("itemType")
        allowed = self.type_fields.get(item_type, set())
        mapping = self.fields.get(item_type, {})
        obj = {"itemType": item_type, "creators": data.pop("creators", []), "tags": [{"tag": t} for t in self._str_list(args, "tags")],
               "collections": [collection_key] if collection_key else []}
        for k, v in data.items():
            field = k if k in allowed else mapping.get(k)
            if field and v:
                obj[field] = v
        created = self._create("items", [obj])
        key = created if isinstance(created, str) else created["key"]
        return {"key": key, "title": (obj.get("title") or "").strip(), "itemType": item_type}

    def t_trash_items(self, args):
        keys = self._unique(self._str_list(args, "item_keys"))
        items = [self.get_item(k) for k in keys]
        for item in items:
            self._update(item["key"], lambda data: None if data.get("deleted") else {"deleted": 1})
        return {"trashed": sorted(keys, key=utf16)}

    # endregion


# region Identifier lookup (DOI via doi.org, arXiv via export.arxiv.org)

METADATA_HOSTS = {"doi.org", "api.crossref.org", "data.crossref.org", "api.datacite.org", "data.datacite.org"}
CSL_TYPES = {"article-journal": "journalArticle", "paper-conference": "conferencePaper", "book": "book",
             "chapter": "bookSection", "report": "report", "thesis": "thesis", "dataset": "dataset",
             "webpage": "webpage", "posted-content": "preprint", "article": "preprint"}


def lookup_identifier(identifier):
    arxiv = re.search(r"(?:arxiv[:/]|arxiv\.org/(?:abs|pdf)/)?(\d{4}\.\d{4,5})(?:v\d+)?", identifier, re.I)
    doi = re.search(r"10\.\d{4,9}/[^\s]+", identifier)
    if doi and not doi.group(0).lower().startswith("10.48550/arxiv."):
        return _doi(doi.group(0).rstrip(".,;"))
    if arxiv:
        return _arxiv(arxiv.group(1))
    if re.search(r"\b(?:97[89])?\d{9}[\dXx]\b", identifier.replace("-", "")) or re.search(r"\bpmid\b|\b\d{7,8}\b", identifier, re.I):
        raise ToolError("unavailable", "The standalone server can add items by DOI or arXiv ID only.")
    raise ToolError("invalid_argument", f"Not a DOI, ISBN, arXiv ID or PMID: {identifier}")


def _doi(doi):
    req = urllib.request.Request(f"https://doi.org/{urllib.parse.quote(doi)}", headers={"Accept": "application/vnd.citationstyles.csl+json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            # Metadata comes from the registration agencies only, never wherever a DOI points.
            host = urllib.parse.urlparse(resp.geturl()).hostname or ""
            if host not in METADATA_HOSTS:
                raise ToolError("not_found", f"Couldn't find metadata for {doi}.")
            csl = json.loads(resp.read(2 * 1024 * 1024))
    except (urllib.error.HTTPError, ValueError):
        raise ToolError("not_found", f"Couldn't find metadata for {doi}.")
    parts = (csl.get("issued", {}).get("date-parts") or [[]])[0]
    date = "-".join(f"{p:02d}" if i else str(p) for i, p in enumerate(parts) if p)
    creators = [{"creatorType": "author", "firstName": a.get("given", ""), "lastName": a.get("family", "")} if a.get("family")
                else {"creatorType": "author", "name": a.get("literal") or a.get("name", "")} for a in csl.get("author", [])]
    container = csl.get("container-title")
    return {"itemType": CSL_TYPES.get(csl.get("type"), "journalArticle"), "title": csl.get("title", ""), "creators": creators,
            "date": date, "publicationTitle": container[0] if isinstance(container, list) and container else container or "",
            "volume": csl.get("volume", ""), "issue": csl.get("issue", ""), "pages": csl.get("page", ""), "DOI": csl.get("DOI", doi),
            "url": csl.get("URL", ""), "publisher": csl.get("publisher", ""), "abstractNote": re.sub(r"<[^>]+>", "", csl.get("abstract", ""))}


def _arxiv(arxiv_id):
    with urllib.request.urlopen(f"https://export.arxiv.org/api/query?id_list={arxiv_id}", timeout=30) as resp:
        feed = ET.fromstring(resp.read(2 * 1024 * 1024))
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entry = feed.find("a:entry", ns)
    if entry is None or entry.find("a:title", ns) is None:
        raise ToolError("not_found", f"Couldn't find metadata for arXiv:{arxiv_id}.")
    text = lambda tag: re.sub(r"\s+", " ", (entry.findtext(f"a:{tag}", "", ns) or "")).strip()
    creators = []
    for author in entry.findall("a:author", ns):
        name = (author.findtext("a:name", "", ns) or "").strip()
        first, _, last = name.rpartition(" ")
        creators.append({"creatorType": "author", "firstName": first, "lastName": last} if first else {"creatorType": "author", "name": name})
    return {"itemType": "preprint", "title": text("title"), "creators": creators, "date": text("published")[:10],
            "abstractNote": text("summary"), "url": f"https://arxiv.org/abs/{arxiv_id}", "DOI": f"10.48550/arXiv.{arxiv_id}",
            "repository": "arXiv", "archiveID": f"arXiv:{arxiv_id}"}

# endregion
