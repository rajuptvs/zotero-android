"""Semantic search (semantic.py) without the network: the golden checks of ai/conformance/semantic, the
model download (against a local server), the index attachment over WebDAV and zotero_semantic_search
on a hand-made library.

    cd mcp-server && python3 -m unittest discover -s tests -v

The golden checks and the tool tests need onnxruntime, tokenizers and numpy, and the model files in
ZOTERO_AI_MODEL_DIR (default ~/.cache/zotero-ai/models/<revision>); without them they're skipped.
"""
import hashlib
import io
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
import urllib.error
import warnings
import zipfile
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from zotero_ai_mcp import pdf, semantic  # noqa: E402
from zotero_ai_mcp.library import Library  # noqa: E402

GOLDEN = os.path.join(os.path.dirname(__file__), "..", "..", "ai", "conformance", "semantic")
MODEL_DIR = os.environ.get("ZOTERO_AI_MODEL_DIR") or os.path.expanduser(f"~/.cache/zotero-ai/models/{semantic.REVISION}")

try:
    semantic._deps()
    HAVE_DEPS = True
except semantic.SemanticError:
    HAVE_DEPS = False
HAVE_MODEL = HAVE_DEPS and all(os.path.exists(os.path.join(MODEL_DIR, f)) for f in semantic.SHA256)


def ready_semantic(data_dir):
    """A Semantic on the local model files (linked into data_dir), turned on and verified."""
    os.makedirs(os.path.join(data_dir, "models"))
    os.symlink(MODEL_DIR, os.path.join(data_dir, "models", semantic.REVISION))
    s = semantic.Semantic(data_dir, enabled=True, model_url="http://127.0.0.1:9/")  # never fetched: the files are there
    deadline = time.time() + 120
    while s.model == "downloading" and time.time() < deadline:
        time.sleep(0.1)
    assert s.model == "ready", s.status()
    return s


def write_index(path, chunks, rows, dim, **manifest):
    import numpy as np
    m = {"format": "zotero-ai-index", "version": 1,
         "model": {"id": semantic.MODEL_ID, "revision": semantic.REVISION, "sha256": semantic.SHA256},
         "dim": dim, "count": len(chunks), "items": len({c["itemKey"] for c in chunks}), "builtAt": "2026-11-11T10:00:00Z",
         "libraryVersion": 1, "builder": {"host": "test", "version": "1"}, **manifest}
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("manifest.json", json.dumps(m), zipfile.ZIP_STORED)
        z.writestr("chunks.jsonl", "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in chunks), zipfile.ZIP_DEFLATED)
        z.writestr("vectors.i8", np.asarray(rows, np.int8).tobytes(), zipfile.ZIP_STORED)


def quantize(vec, dim):
    import numpy as np
    v = np.asarray(vec, np.float64)[:dim]
    x = v / np.linalg.norm(v) * 127
    return (np.sign(x) * np.floor(np.abs(x) + 0.5)).astype(np.int8)


@unittest.skipUnless(HAVE_MODEL, "needs onnxruntime, tokenizers, numpy and the model files")
class Golden(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="zai-sem-")
        cls.s = ready_semantic(cls.dir)
        with open(os.path.join(GOLDEN, "golden.json"), encoding="utf-8") as f:
            cls.golden = json.load(f)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_golden(self):
        import numpy as np
        index = os.path.join(GOLDEN, "index.zip")
        for q in self.golden["queries"]:
            with self.subTest(query=q["query"]):
                self.assertEqual(semantic.query_text(q["query"]), q["text"])
                self.assertEqual(self.s.hook({"action": "tokenize", "text": q["text"]}), {"ids": q["ids"]})
                e = np.array(self.s.hook({"action": "embed", "text": q["text"]})["embedding"])
                g = np.array(q["embedding"])
                self.assertEqual(len(e), 768)
                self.assertGreaterEqual(float(e @ g / np.linalg.norm(e) / np.linalg.norm(g)), 0.9999)
                rows = self.s.hook({"action": "rank", "indexPath": index, "text": q["text"], "top": 20})["rows"]
                self.assertEqual(len(rows), 20)
                expected = q["rows"][:10]
                for i, (got, want) in enumerate(zip(rows[:10], expected)):
                    if got["row"] != want["row"]:  # a swap only between near-equal scores
                        other = next(r for r in expected if r["row"] == got["row"])
                        self.assertLess(abs(other["score"] - want["score"]), 1e-4, f"rank {i}")
                    self.assertAlmostEqual(got["score"], next(r["score"] for r in q["rows"] if r["row"] == got["row"]), delta=1e-4)
                self.assertEqual({r["row"] for r in rows[:10]}, {r["row"] for r in expected})

    def test_hook_errors(self):
        self.assertEqual(self.s.hook({"action": "nope"})["error"], "invalid_argument")
        self.assertEqual(self.s.hook({"action": "embed"})["error"], "invalid_argument")
        self.assertEqual(self.s.hook({"action": "rank", "indexPath": "/nonexistent.zip", "text": "x"}),
                         {"error": "unavailable", "message": semantic.DAMAGED})


class FakeFiles:
    def __init__(self, path):
        self.file, self.calls = path, 0

    def path(self, attachment, kind="pdf"):
        assert kind == "zip"
        self.calls += 1
        return self.file, "zotero-ai-index.zip"


def offline_library(items):
    """A Library with these items and no Web API."""
    lib = Library.__new__(Library)
    lib.items = {i["key"]: i for i in items}
    lib.collections, lib.lock, lib.last_sync, lib.sync_interval = {}, threading.RLock(), time.time() + 3600, 3600.0
    lib.fields, lib.type_fields, lib.primary_creator = {}, {}, {"journalArticle": "author"}
    lib.files = lib.semantic = None
    return lib


def article(key, title, **extra):
    return {"key": key, "itemType": "journalArticle", "title": title, "creators": [{"creatorType": "author", "lastName": "Doe"}],
            "date": "2024", "tags": [], "dateModified": "2026-01-01T00:00:00Z", **extra}


INDEX_ATTACHMENT = {"key": "IDX00001", "itemType": "attachment", "linkMode": "imported_file", "contentType": "application/zip",
                    "filename": "zotero-ai-index.zip", "md5": "0" * 32, "version": 5, "tags": [{"tag": semantic.INDEX_TAG}],
                    "dateModified": "2026-11-11T10:00:00Z"}


class Unavailable(unittest.TestCase):
    """The tool's errors before any model work (no dependencies needed)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="zai-sem-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def call(self, lib, query="attention"):
        return lib.execute("zotero_semantic_search", {"query": query, "limit": None})

    def test_errors(self):
        lib = offline_library([article("AAAA1111", "A")])
        self.assertEqual(self.call(lib, " \n "), {"error": "invalid_argument", "message": "query is empty."})
        self.assertEqual(self.call(lib), {"error": "unavailable", "message": semantic.NO_INDEX})
        lib.semantic = semantic.Semantic(self.dir)  # off
        self.assertEqual(self.call(lib), {"error": "unavailable", "message": semantic.NO_INDEX})
        lib.items["IDX00001"] = dict(INDEX_ATTACHMENT)
        self.assertEqual(self.call(lib), {"error": "unavailable", "message": semantic.OFF})
        lib.items["IDX00001"]["deleted"] = 1  # a trashed index doesn't count
        self.assertEqual(self.call(lib), {"error": "unavailable", "message": semantic.NO_INDEX})

    def test_index_attachment(self):
        lib = offline_library([dict(INDEX_ATTACHMENT), {**INDEX_ATTACHMENT, "key": "IDX00002", "dateModified": "2026-11-12T10:00:00Z"},
                               {**INDEX_ATTACHMENT, "key": "IDX00003", "dateModified": "2026-11-13T10:00:00Z", "parentItem": "AAAA1111"},
                               {**INDEX_ATTACHMENT, "key": "IDX00004", "dateModified": "2026-11-13T10:00:00Z", "tags": []}])
        self.assertEqual(lib.index_attachment()["key"], "IDX00002")

    def test_index_attachment_is_a_stored_file(self):
        lib = offline_library([dict(INDEX_ATTACHMENT), {**INDEX_ATTACHMENT, "key": "IDX00002", "dateModified": "2026-11-12T10:00:00Z",
                                                        "linkMode": "linked_file"}])
        self.assertEqual(lib.index_attachment()["key"], "IDX00001")  # a newer linked file doesn't count

    def test_too_large_download(self):
        class TooLargeFiles:
            def path(self, attachment, kind="pdf"):
                raise pdf.TooLarge()
        with self.assertRaises(semantic.SemanticError) as e:
            semantic.Semantic(self.dir).load_index(dict(INDEX_ATTACHMENT), TooLargeFiles())
        self.assertEqual(str(e.exception), semantic.DAMAGED)


ROW = {"itemKey": "AAAA1111", "source": "metadata", "key": None, "text": "x"}


def manifest(**over):
    return {"format": "zotero-ai-index", "version": 1,
            "model": {"id": semantic.MODEL_ID, "revision": semantic.REVISION, "sha256": semantic.SHA256},
            "dim": 2, "count": 1, "items": 1, "builtAt": "2026-11-11T10:00:00Z", "libraryVersion": 1,
            "builder": {"host": "test", "version": "1"}, **over}


def entries(rows=(ROW,), vectors=b"\x01\x02", **over):
    """The entries of a small index: [(name, bytes, compression)]."""
    jsonl = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode()
    return [("manifest.json", json.dumps(manifest(count=len(rows), **over)).encode(), zipfile.ZIP_STORED),
            ("chunks.jsonl", jsonl, zipfile.ZIP_DEFLATED), ("vectors.i8", vectors, zipfile.ZIP_STORED)]


def raw_zip(path, members):
    """Writes a ZIP by hand: members are (name, compressed bytes, method, crc, declared size), so a test
    can declare any size without producing that much data."""
    local, central = b"", b""
    for name, data, method, crc, size in members:
        header = struct.pack("<HHHHHIIIHH", 20, 0, method, 0, 0, crc, len(data), size, len(name), 0) + name.encode()
        central += b"PK\x01\x02" + struct.pack("<H", 20) + header[:26] + struct.pack("<HHHII", 0, 0, 0, 0, len(local)) + name.encode()
        local += b"PK\x03\x04" + header + data
    end = b"PK\x05\x06" + struct.pack("<HHHHIIH", 0, 0, len(members), len(members), len(central), len(local), 0)
    with open(path, "wb") as f:
        f.write(local + central + end)


def deflated(blocks):
    """(raw deflate stream, crc, size) of the given byte blocks, compressed as they come."""
    c, out, crc, size = zlib.compressobj(9, zlib.DEFLATED, -15), [], 0, 0
    for block in blocks:
        out.append(c.compress(block))
        crc, size = zlib.crc32(block, crc), size + len(block)
    out.append(c.flush())
    return b"".join(out), crc, size


def member(name, data, method):
    """A raw_zip member holding `data` as it is."""
    return name, deflated([data])[0] if method == zipfile.ZIP_DEFLATED else data, method, zlib.crc32(data), len(data)


class IndexFile(unittest.TestCase):
    """read_index's checks (semantic.md, "Index file"); no dependencies needed."""

    @classmethod
    def setUpClass(cls):
        # About 600 MiB of valid rows, deflated as they're made (never held unzipped).
        line = (json.dumps(ROW) + "\n").encode()
        cls.bomb = deflated(line * (1024 * 1024 // len(line)) for _ in range(600))

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="zai-sem-")
        self.path = os.path.join(self.dir, "index.zip")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, members):
        with zipfile.ZipFile(self.path, "w") as z, warnings.catch_warnings():
            warnings.simplefilter("ignore")  # a repeated name, on purpose
            for name, data, method in members:
                z.writestr(name, data, method)

    def assertDamaged(self, message=semantic.DAMAGED):
        tracemalloc.start()
        try:
            with self.assertRaises(semantic.SemanticError) as e:
                semantic.read_index(self.path)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual((e.exception.code, str(e.exception)), ("unavailable", message))
        self.assertLess(peak, 16 * 1024 * 1024)  # rejected without unzipping much

    def test_valid(self):
        long = {**ROW, "itemKey": "BBBB2222", "source": "fulltext", "key": "ATTBBBB2", "text": "é" * 900}
        self.write(entries([ROW, long], b"\x01\x02\x03\x04"))
        m, vectors, chunks = semantic.read_index(self.path)
        self.assertEqual((m["count"], m["dim"], bytes(vectors)), (2, 2, b"\x01\x02\x03\x04"))
        self.assertEqual(chunks, [("AAAA1111", "metadata", None, "x"), ("BBBB2222", "fulltext", "ATTBBBB2", "é" * 800)])

    def test_other_model(self):
        self.write(entries(model={"id": semantic.MODEL_ID, "revision": "0" * 40, "sha256": semantic.SHA256}))
        self.assertDamaged(semantic.OTHER_MODEL)

    def test_entries(self):
        good = entries()
        for members in (good + [("extra.txt", b"x", zipfile.ZIP_STORED)], good[:2], good + [good[0]],
                        [(n, d, zipfile.ZIP_BZIP2 if n == "chunks.jsonl" else c) for n, d, c in good]):
            with self.subTest(names=[n for n, _, _ in members]):
                self.write(members)
                self.assertDamaged()
        with open(self.path, "wb") as f:
            f.write(b"not a zip")
        self.assertDamaged()

    def test_manifest(self):
        self.write(entries(padding="x" * (64 * 1024)))
        self.assertDamaged()
        for over in ({"dim": 0}, {"dim": 769}, {"dim": "2"}, {"dim": True}, {"dim": 2.0}):
            with self.subTest(**over):
                self.write(entries(**over))
                self.assertDamaged()
        for count in (0, 200_001):
            with self.subTest(count=count):
                members = entries()
                members[0] = ("manifest.json", json.dumps(manifest(count=count)).encode(), zipfile.ZIP_STORED)
                self.write(members)
                self.assertDamaged()
        self.write([("manifest.json", b"[" * 10000, zipfile.ZIP_STORED)] + entries()[1:])
        self.assertDamaged()

    def test_vectors(self):
        for vectors in (b"\x01", b"\x01\x02\x03"):
            with self.subTest(size=len(vectors)):
                self.write(entries(vectors=vectors))
                self.assertDamaged()

    def test_rows(self):
        bad = [{**ROW, "itemKey": "AAAA111"}, {**ROW, "source": "web"}, {k: v for k, v in ROW.items() if k != "key"}, {**ROW, "key": 5},
               {**ROW, "text": None}, ["AAAA1111"]]
        for row in bad:
            with self.subTest(row=row):
                self.write(entries([row]))
                self.assertDamaged()
        for rows in ([], [ROW, ROW]):  # fewer or more lines than count
            with self.subTest(lines=len(rows)):
                members = entries(rows, vectors=b"\x01\x02" * len(rows))
                members[0] = ("manifest.json", json.dumps(manifest(count=1)).encode(), zipfile.ZIP_STORED)
                self.write(members)
                self.assertDamaged()
        members = entries()
        members[1] = ("chunks.jsonl", b"{not json\n", zipfile.ZIP_DEFLATED)
        self.write(members)
        self.assertDamaged()

    def test_long_line(self):
        fits = {**ROW, "text": ""}
        fits["text"] = "y" * (semantic.MAX_LINE_BYTES - len(json.dumps(fits)))
        self.write(entries([fits]))
        self.assertEqual(len(semantic.read_index(self.path)[2]), 1)
        self.write(entries([{**fits, "text": fits["text"] + "y"}]))
        self.assertDamaged()

    def test_zip_bombs(self):
        data, crc, size = self.bomb
        honest = [member(*e) for e in entries()]
        manifest_member, _, vectors_member = honest
        raw_zip(self.path, honest)
        self.assertEqual(semantic.read_index(self.path)[2], [("AAAA1111", "metadata", None, "x")])  # raw_zip writes valid files
        # Declared as it is (600 MiB): refused before unzipping.
        raw_zip(self.path, [manifest_member, ("chunks.jsonl", data, zipfile.ZIP_DEFLATED, crc, size), vectors_member])
        self.assertDamaged()
        # Declared small: unzipping stops there.
        raw_zip(self.path, [manifest_member, ("chunks.jsonl", data, zipfile.ZIP_DEFLATED, crc, 1000), vectors_member])
        self.assertDamaged()
        # Under the size limit but far more lines than rows: stops at the first extra line.
        raw_zip(self.path, [manifest_member, ("chunks.jsonl", data, zipfile.ZIP_DEFLATED, crc, 400 * 1024 * 1024), vectors_member])
        self.assertDamaged()
        # A manifest or vectors entry that unzips past its limit.
        raw_zip(self.path, [("manifest.json", data, zipfile.ZIP_DEFLATED, crc, size)] + honest[1:])
        self.assertDamaged()
        raw_zip(self.path, [manifest_member, ("chunks.jsonl", data, zipfile.ZIP_DEFLATED, crc, size), ("vectors.i8", data, zipfile.ZIP_DEFLATED, crc, size)])
        self.assertDamaged()

    def test_file_size(self):
        with open(self.path, "wb") as f:
            f.truncate(semantic.MAX_INDEX_BYTES + 1)  # sparse: takes no space
        self.assertDamaged()


@unittest.skipUnless(HAVE_MODEL, "needs onnxruntime, tokenizers, numpy and the model files")
class Tool(unittest.TestCase):
    """zotero_semantic_search on a hand-made index whose rows are known vectors."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="zai-sem-")
        cls.s = ready_semantic(cls.dir)
        q = cls.s.embed(semantic.query_text("making attention cheaper"))
        import numpy as np
        rng = np.random.default_rng(1)
        noise = lambda scale: quantize(np.asarray(q[:256]) + rng.normal(0, scale, 256), 256)
        # Rows in decreasing similarity to the query: A A A B C(trashed) D(missing) E …
        cls.chunks, rows = [], []
        for key, source, scale in [("AAAA1111", "fulltext", 0.0), ("AAAA1111", "note", 0.005), ("AAAA1111", "fulltext", 0.01),
                                   ("BBBB2222", "metadata", 0.02), ("CCCC3333", "metadata", 0.025), ("DDDD4444", "metadata", 0.03),
                                   ("EEEE5555", "metadata", 0.04), ("FFFF6666", "metadata", 0.08)]:
            cls.chunks.append({"itemKey": key, "source": source, "key": None if source == "metadata" else "ATT" + key[:5],
                               "text": key + " " + "é" * 900 if source == "fulltext" else key})
            rows.append(noise(scale))
        cls.path = os.path.join(cls.dir, "index.zip")
        write_index(cls.path, cls.chunks, rows, 256)
        cls.expected = semantic.Index(cls.path).rank(q, 10)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def library(self):
        lib = offline_library([article("AAAA1111", "Linear attention"), article("BBBB2222", "Sparse attention"),
                               article("CCCC3333", "Trashed", deleted=1), article("EEEE5555", "Flash"), article("FFFF6666", "Other"),
                               dict(INDEX_ATTACHMENT)])
        lib.semantic, lib.files = self.s, FakeFiles(self.path)
        return lib

    def test_results(self):
        self.assertEqual([r for r, _ in self.expected[:8]], list(range(8)))  # the rows really rank in order
        lib = self.library()
        result = lib.execute("zotero_semantic_search", {"query": "  making   attention cheaper ", "limit": 3})
        self.assertEqual(result["index"], {"builtAt": "2026-11-11T10:00:00Z", "items": 6, "chunks": 8})
        self.assertEqual([i["key"] for i in result["items"]], ["AAAA1111", "BBBB2222", "EEEE5555"])  # C trashed, D gone
        a = result["items"][0]
        self.assertEqual(a["score"], semantic.round3(self.expected[0][1]))
        self.assertEqual([p["source"] for p in a["passages"]], ["fulltext", "note"])  # at most 2
        self.assertEqual(len(a["passages"][0]["text"]), 800)
        self.assertEqual(a["passages"][1], {"source": "note", "key": "ATTAAAA1", "text": "AAAA1111"})
        self.assertEqual(set(a), {"key", "itemType", "title", "creators", "year", "tags", "score", "passages"})
        self.assertEqual(result["items"][1]["passages"], [{"source": "metadata", "key": None, "text": "BBBB2222"}])
        full = lib.execute("zotero_semantic_search", {"query": "making attention cheaper", "limit": 50})
        self.assertEqual([i["key"] for i in full["items"]], ["AAAA1111", "BBBB2222", "EEEE5555", "FFFF6666"])

    def test_index_cached_until_changed(self):
        self.s.index = None
        lib = self.library()
        lib.execute("zotero_semantic_search", {"query": "x", "limit": 1})
        lib.execute("zotero_semantic_search", {"query": "y", "limit": 1})
        self.assertEqual(lib.files.calls, 1)
        lib.items["IDX00001"]["md5"] = "1" * 32
        lib.execute("zotero_semantic_search", {"query": "z", "limit": 1})
        self.assertEqual(lib.files.calls, 2)
        self.assertEqual(self.s.status()["index"]["source"], "attachment")

    def test_override(self):
        lib = self.library()
        lib.files = None
        del lib.items["IDX00001"]
        self.assertEqual(self.s.hook({"action": "useIndex", "path": self.path})["index"]["source"], "override")
        try:
            self.assertEqual(len(lib.execute("zotero_semantic_search", {"query": "attention", "limit": 2})["items"]), 2)
        finally:
            self.assertIsNone(self.s.hook({"action": "useIndex", "path": None})["index"])
        self.assertEqual(lib.execute("zotero_semantic_search", {"query": "attention", "limit": 2})["message"], semantic.NO_INDEX)


class Serve(ThreadingHTTPServer):
    daemon_threads = True


def serve(files, fail_after=None):
    """A local HTTP server for `files` (path → bytes) with Range support; the first response is cut
    after `fail_after` bytes when set."""
    state = {"cut": fail_after, "ranges": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = files.get(self.path)
            if body is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start = 0
            rng = self.headers.get("Range")
            state["ranges"].append(rng)
            if rng:
                start = int(rng.split("=")[1].split("-")[0])
                if start >= len(body):
                    self.send_response(416)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(206)
            else:
                self.send_response(200)
            self.send_header("Content-Length", str(len(body) - start))
            self.end_headers()
            out = body[start:]
            if state["cut"] is not None:
                out, state["cut"] = out[:state["cut"]], None
                self.wfile.write(out)
                self.wfile.flush()
                self.close_connection = True
                return
            self.wfile.write(out)

    httpd = Serve(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}", state


class Download(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="zai-sem-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_resume_and_verify(self):
        body = os.urandom(3 * 1024 * 1024 + 5)
        httpd, url, state = serve({"/m/file.bin": body}, fail_after=1024 * 1024)
        try:
            s = semantic.Semantic(self.dir, model_url=url + "/m/")
            s._fetch("file.bin", hashlib.sha256(body).hexdigest())  # cut short, then resumed
            with open(os.path.join(s.model_dir, "file.bin"), "rb") as f:
                self.assertEqual(f.read(), body)
            self.assertIsNone(state["ranges"][0])
            self.assertTrue(state["ranges"][-1].startswith("bytes="))
            self.assertFalse(os.path.exists(os.path.join(s.model_dir, "file.bin.part")))
            with self.assertRaises(urllib.error.HTTPError):
                s._fetch("other.bin", "0" * 64)  # not there
        finally:
            httpd.shutdown()

    def test_mismatch_deleted(self):
        httpd, url, _ = serve({"/m/file.bin": b"tampered"})
        try:
            s = semantic.Semantic(self.dir, model_url=url + "/m/")
            with self.assertRaises(semantic.SemanticError):
                s._fetch("file.bin", "0" * 64)
            self.assertEqual(os.listdir(s.model_dir), [])
            os.makedirs(s.model_dir, exist_ok=True)
            with open(os.path.join(s.model_dir, "file.bin"), "wb") as f:  # a stored file that changed
                f.write(b"old")
            good = hashlib.sha256(b"tampered").hexdigest()
            s._fetch("file.bin", good)
            self.assertEqual(semantic.sha256_file(os.path.join(s.model_dir, "file.bin")), good)
        finally:
            httpd.shutdown()

    def test_status_without_deps_or_network(self):
        s = semantic.Semantic(self.dir, model_url="http://127.0.0.1:9/")
        self.assertEqual(s.status(), {"enabled": False, "model": "missing", "modelError": None, "index": None, "build": None})
        s.hook({"action": "enable", "on": True})
        deadline = time.time() + 30
        while s.model == "downloading" and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(s.model, "error")
        self.assertTrue(s.model_error)
        self.assertEqual(s.hook({"action": "tokenize", "text": "x"})["error"], "unavailable")


class WebDavIndex(unittest.TestCase):
    """Files.path(kind="zip") fetches the index from WebDAV the way Zotero stores it (ITEMKEY.zip)."""

    def test_fetch(self):
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as z:
            z.writestr("manifest.json", "{}")
        outer = io.BytesIO()
        with zipfile.ZipFile(outer, "w") as z:
            z.writestr("zotero-ai-index.zip", inner.getvalue())
        prop = b"<properties version='1'><mtime>1</mtime><hash>" + b"a" * 32 + b"</hash></properties>"
        httpd, url, _ = serve({"/zotero/IDX00001.prop": prop, "/zotero/IDX00001.zip": outer.getvalue(),
                               "/zotero/PDF00001.prop": prop, "/zotero/PDF00001.zip": outer.getvalue()})
        d = tempfile.mkdtemp(prefix="zai-sem-")
        try:
            files = pdf.Files(None, d, {"url": url, "username": "u", "password": "p"})
            path, name = files.path(dict(INDEX_ATTACHMENT), kind="zip")
            with open(path, "rb") as f:
                self.assertEqual(f.read(), inner.getvalue())
            with self.assertRaises(pdf.PdfError) as e:  # a zip isn't a PDF
                files.path({"key": "PDF00001", "filename": "zotero-ai-index.zip"})
            self.assertEqual(str(e.exception), "This file isn't a PDF.")
        finally:
            httpd.shutdown()
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
