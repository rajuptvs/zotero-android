"""Semantic search for the standalone server (ai/spec/semantic.md): the pinned embedding model, the
library's search index and the ranking.

It needs `pip install onnxruntime tokenizers numpy`; without them zotero_semantic_search answers
`unavailable` and everything else works. `--semantic` turns it on: the model downloads into
<data dir>/models/<revision>/ in the background. The index comes from the library's index
attachment (built by the desktop plugin; this server doesn't build one), fetched like PDFs are.
"""
import hashlib
import http.client
import json
import math
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import zipfile
import zlib

from . import pdf

MODEL_ID = "onnx-community/embeddinggemma-2-ONNX"
REVISION = "daa72c51243991dfcaf9f9137d2c573d8f7790c0"
SHA256 = {
    "onnx/model_quantized.onnx": "d06edd601f851c633a2519304cbeb8dc6170d7ceb61b436625c17fb9b6e74953",
    "onnx/model_quantized.onnx_data": "278a7ff1248c3618e4bd11a607fc54f7bdc7778854230f3956d3f86bd9db4f3b",
    "tokenizer.json": "4d777ef5bdc1aa36227abdfb77c3e49e7b9c892d16e1b6bda41c393504828be4",
}
MODEL_URL = f"https://huggingface.co/{MODEL_ID}/resolve/{REVISION}/"
MAX_MODEL_FILE_BYTES = 2 * 1024 * 1024 * 1024
MAX_TOKENS = 2048
EMBEDDING_DIM = 768
INDEX_TAG = "_zotero-ai-index"
# Index file limits (semantic.md, "Index file"): the file, then each entry unzipped.
MAX_INDEX_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_CHUNKS_BYTES = 512 * 1024 * 1024
MAX_LINE_BYTES = 64 * 1024  # one chunks.jsonl row, without its newline
MAX_ROWS = 200_000
MAX_VECTORS_BYTES = MAX_ROWS * EMBEDDING_DIM
ENTRIES = ("chunks.jsonl", "manifest.json", "vectors.i8")
SOURCES = ("metadata", "fulltext", "note")
WALK_ROWS = 200
PASSAGES_PER_ITEM = 2
PASSAGE_CHARS = 800
IDLE_SECONDS = 300  # the model and index are freed after this long unused
RETRY_SECONDS = 600  # a failed model download is tried again on use after this long

NO_INDEX = "There's no search index yet. Turn on semantic search in the Zotero desktop plugin to build one."
OFF = "Semantic search is off on this device. Turn it on in the Zotero AI settings."
OTHER_MODEL = "The search index was built with a different model; rebuild it."
DAMAGED = "The search index is damaged or too large; rebuild it."
DEPS = "Semantic search needs onnxruntime, tokenizers and numpy on the server (pip install onnxruntime tokenizers numpy)."


class SemanticError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _deps():
    try:
        import numpy
        import onnxruntime
        import tokenizers
        return numpy, onnxruntime, tokenizers
    except ImportError:
        raise SemanticError("unavailable", DEPS)


def normalize(text):
    """Unicode NFC, runs of whitespace → one space, trimmed."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()


def query_text(query):
    return f"task: search result | query: {normalize(query)}"


def round3(v):
    return math.floor(v * 1000 + 0.5) / 1000


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


# region Index file

def read_index(path):
    """Reads an index file, checking it while it unzips and before allocating anything sized from it.
    → (manifest, vectors as bytes, [(itemKey, source, key, text cut to PASSAGE_CHARS)] per row).
    Raises SemanticError."""
    try:
        if os.path.getsize(path) > MAX_INDEX_BYTES:
            raise _damaged()
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            # Only the three entries, once each; stored or deflated (other methods can't be unzipped in bounded steps).
            if sorted(e.filename for e in entries) != list(ENTRIES) \
                    or any(e.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) for e in entries):
                raise _damaged()
            entry = {e.filename: e for e in entries}
            manifest = json.loads(_unzip(archive, entry["manifest.json"], MAX_MANIFEST_BYTES))
            if not isinstance(manifest, dict) or manifest.get("format") != "zotero-ai-index" or manifest.get("version") != 1 \
                    or not isinstance(manifest.get("model"), dict) or manifest["model"].get("revision") != REVISION \
                    or manifest["model"].get("sha256") != SHA256:
                raise SemanticError("unavailable", OTHER_MODEL)
            count, dim = manifest.get("count"), manifest.get("dim")
            if not all(isinstance(v, int) and not isinstance(v, bool) for v in (count, dim)) \
                    or not 1 <= count <= MAX_ROWS or not 1 <= dim <= EMBEDDING_DIM:
                raise _damaged()
            vectors = _unzip(archive, entry["vectors.i8"], min(count * dim, MAX_VECTORS_BYTES))
            if len(vectors) != count * dim:
                raise _damaged()
            if entry["chunks.jsonl"].file_size > MAX_CHUNKS_BYTES:
                raise _damaged()
            chunks, total = [], 0
            with archive.open(entry["chunks.jsonl"]) as f:
                # A line at a time, counting bytes; a line longer than MAX_LINE_BYTES is never held whole.
                while line := f.readline(min(MAX_LINE_BYTES + 2, MAX_CHUNKS_BYTES + 1 - total)):
                    total += len(line)
                    if total > MAX_CHUNKS_BYTES or len(chunks) == count or len(line.rstrip(b"\n")) > MAX_LINE_BYTES:
                        raise _damaged()
                    chunks.append(_chunk(line))
            if len(chunks) != count:
                raise _damaged()
    except SemanticError:
        raise
    except (OSError, zipfile.BadZipFile, KeyError, ValueError, zlib.error, EOFError, RuntimeError, NotImplementedError, RecursionError):
        raise _damaged()
    return manifest, vectors, chunks


def _damaged():
    return SemanticError("unavailable", DAMAGED)


def _unzip(archive, entry, cap):
    """An entry's bytes, stopping as soon as more than `cap` come out."""
    if entry.file_size > cap:
        raise _damaged()
    out = bytearray()
    with archive.open(entry) as f:
        while block := f.read(1024 * 1024):
            out += block
            if len(out) > cap:
                raise _damaged()
    return out


def _chunk(line):
    """One chunks.jsonl row → (itemKey, source, key, text cut to PASSAGE_CHARS)."""
    row = json.loads(line)
    if not isinstance(row, dict) or not isinstance(row.get("itemKey"), str) or len(row["itemKey"]) != 8 \
            or row.get("source") not in SOURCES or "key" not in row or not (row["key"] is None or isinstance(row["key"], str)) \
            or not isinstance(row.get("text"), str):
        raise _damaged()
    return row["itemKey"], row["source"], row["key"], row["text"][:PASSAGE_CHARS]


class Index:
    """An index file read into memory: its manifest, the rows as signed bytes and each row's chunk
    (its text cut to PASSAGE_CHARS, all a passage shows)."""

    def __init__(self, path):
        np = _deps()[0]
        manifest, vectors, chunks = read_index(path)
        count, dim = manifest["count"], manifest["dim"]
        self.manifest, self.count, self.dim, self.chunks = manifest, count, dim, chunks
        self.rows = np.frombuffer(vectors, np.int8).reshape(count, dim)
        self.norms = np.empty(count, np.float64)
        for start in range(0, count, 8192):  # a block at a time, so float copies stay small
            block = self.rows[start:start + 8192].astype(np.float64)
            self.norms[start:start + 8192] = np.sqrt((block * block).sum(axis=1))
        self.norms[self.norms == 0] = 1.0  # an all-zero row scores 0

    def info(self):
        return {"builtAt": self.manifest.get("builtAt"), "items": self.manifest.get("items"), "chunks": self.count}

    def chunk(self, row):
        item_key, source, key, text = self.chunks[row]
        return {"itemKey": item_key, "source": source, "key": key, "text": text}

    def rank(self, embedding, top):
        """→ [(row, score)] for the best `top` rows: score = (q · r) / |r| with q the embedding cut to
        dim and renormalised; equal scores by row number."""
        np = _deps()[0]
        q = np.asarray(embedding, np.float64)[:self.dim]
        q = q / np.linalg.norm(q)
        scores = np.empty(self.count, np.float64)
        for start in range(0, self.count, 8192):
            scores[start:start + 8192] = self.rows[start:start + 8192].astype(np.float64) @ q
        scores /= self.norms
        order = np.argsort(-scores, kind="stable")[:top]
        return [(int(i), float(scores[i])) for i in order]

# endregion


class Semantic:
    """The model (downloaded in the background once turned on), one inference session (freed after
    IDLE_SECONDS unused) and the index in use."""

    def __init__(self, data_dir, enabled=False, model_url=MODEL_URL):
        self.model_dir = os.path.join(data_dir, "models", REVISION)
        self.model_url = model_url
        self.enabled = False
        self.model = "missing"  # missing | downloading | ready | error
        self.model_error = None
        self.failed_at = 0.0
        self.state_lock = threading.Lock()
        self.run_lock = threading.Lock()  # the session: loading, running, freeing
        self.index_lock = threading.Lock()  # loading the index
        self.tokenizer = self.session = None
        self.index = None  # (identity, Index)
        self.index_info = None  # status of the index last used (kept when it's freed)
        self.override = None  # an index file used instead of the library's (test hook)
        self.last_used = 0.0
        threading.Thread(target=self._free_when_idle, daemon=True).start()
        if enabled:
            self.set_enabled(True)

    # region Model files

    def set_enabled(self, on):
        with self.state_lock:
            self.enabled = on
            start = on and self.model in ("missing", "error")
            if start:
                self.model, self.model_error = "downloading", None
        if start:
            threading.Thread(target=self._download, daemon=True).start()
        if not on:
            self.free()

    def _download(self):
        try:
            _deps()  # no point fetching 346 MB the server can't run
            for name, sha in SHA256.items():
                self._fetch(name, sha)
            state, error = "ready", None
        except SemanticError as e:
            state, error = "error", str(e)
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            state, error = "error", f"Couldn't download the model: {getattr(e, 'reason', None) or e}"
        with self.state_lock:
            self.model, self.model_error = state, error
            if state == "error":
                self.failed_at = time.time()

    def _fetch(self, name, sha):
        """One model file into model_dir, resuming a partial download; a file whose sha256 differs is deleted."""
        path = os.path.join(self.model_dir, name)
        if os.path.exists(path):
            if sha256_file(path) == sha:
                return
            os.remove(path)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        part = path + ".part"
        for attempt in range(3):
            offset = os.path.getsize(part) if os.path.exists(part) else 0
            req = urllib.request.Request(self.model_url + name, headers={"User-Agent": "zotero-ai-mcp"})
            if offset:
                req.add_header("Range", f"bytes={offset}-")
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    if offset and resp.status != 206:  # the server sent the whole file
                        offset = 0
                    length = resp.headers.get("Content-Length")
                    with open(part, "ab" if offset else "wb") as out:
                        total = offset
                        while data := resp.read(1024 * 1024):
                            total += len(data)
                            if total > MAX_MODEL_FILE_BYTES:
                                raise SemanticError("unavailable", f"The model file {name} is larger than expected.")
                            out.write(data)
                    if length and length.isdigit() and total - offset < int(length):  # the connection dropped
                        raise ConnectionError("the download was cut short")
                break
            except urllib.error.HTTPError as e:
                if e.code == 416 and offset:  # already complete
                    break
                raise
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError):
                if attempt == 2:
                    raise
                time.sleep(2)
        if sha256_file(part) != sha:
            os.remove(part)
            raise SemanticError("unavailable", f"The downloaded {name} doesn't match its sha256, so it was deleted.")
        os.replace(part, path)

    def require_model(self):
        """Raises unless semantic search is on and the model is ready."""
        with self.state_lock:
            if self.enabled and self.model == "error" and time.time() - self.failed_at > RETRY_SECONDS:
                retry = True
                self.model, self.model_error = "downloading", None
            else:
                retry = False
            ready = self.enabled and self.model == "ready"
            error = self.model_error
        if retry:
            threading.Thread(target=self._download, daemon=True).start()
        if not ready:
            raise SemanticError("unavailable", DEPS if error == DEPS else OFF)

    # endregion

    # region Inference

    def _load(self):
        """The tokenizer and session, loaded on first use (call with run_lock held)."""
        np, ort, tokenizers = _deps()
        if self.session is None:
            tokenizer = tokenizers.Tokenizer.from_file(os.path.join(self.model_dir, "tokenizer.json"))
            tokenizer.enable_truncation(MAX_TOKENS)  # keeps the final EOS
            options = ort.SessionOptions()
            options.log_severity_level = 3
            self.session = ort.InferenceSession(os.path.join(self.model_dir, "onnx/model_quantized.onnx"), options,
                                                providers=["CPUExecutionProvider"])
            self.tokenizer = tokenizer
        self.last_used = time.time()
        return np

    def tokenize(self, text):
        self.require_model()
        with self.run_lock:
            self._load()
            return self.tokenizer.encode(text).ids

    def embed(self, text):
        """The 768-value embedding of `text` as given (no prefix added, not cut to dim)."""
        self.require_model()
        with self.run_lock:
            np = self._load()
            ids = np.array([self.tokenizer.encode(text).ids], np.int64)
            empty = np.zeros((0, 512), np.float32)
            out = self.session.run(["sentence_embedding"], {"input_ids": ids, "attention_mask": np.ones_like(ids), "image_features": empty,
                                                            "video_features": empty, "audio_features": empty})[0][0]
            self.last_used = time.time()
        return [float(v) for v in out]

    def free(self):
        with self.run_lock:
            self.session = self.tokenizer = None
        with self.index_lock:
            if not self.override:
                self.index = None

    def _free_when_idle(self):
        while True:
            time.sleep(30)
            if (self.session is not None or self.index is not None) and time.time() - self.last_used > IDLE_SECONDS:
                self.free()

    # endregion

    # region Index and search

    def load_index(self, attachment, files):
        """The index to search: the override, or the library's index attachment (fetched through
        `files`, read again when its version or md5 changes)."""
        with self.index_lock:
            if self.override:
                return self.index[1]
            if attachment is None:
                raise SemanticError("unavailable", NO_INDEX)
            identity = (attachment["key"], attachment.get("version"), attachment.get("md5"))
            if not self.index or self.index[0] != identity:
                if not files:
                    raise SemanticError("unavailable", "This server isn't set up to read files.")
                try:
                    path, _ = files.path(attachment, kind="zip")
                except pdf.TooLarge:
                    raise _damaged()
                self.index = None  # the old one can go before the new one is read
                index = Index(path)
                self.index = (identity, index)
                self.index_info = {**index.info(), "source": "attachment"}
            self.last_used = time.time()
            return self.index[1]

    def use_index(self, path):
        """Searches the index file at `path` instead of the library's (None = back to the library's)."""
        index = Index(path) if path is not None else None
        with self.index_lock:
            self.override = path
            self.index = ("override", index) if index else None
            self.index_info = {**index.info(), "source": "override"} if index else None

    def search(self, query, attachment, files):
        """→ (Index, [(row, score)] for the best WALK_ROWS rows) for a normalised query."""
        if not self.override and attachment is None:
            raise SemanticError("unavailable", NO_INDEX)
        self.require_model()
        index = self.load_index(attachment, files)
        return index, index.rank(self.embed(query_text(query)), WALK_ROWS)

    # endregion

    def status(self):
        return {"enabled": self.enabled, "model": self.model, "modelError": self.model_error, "index": self.index_info, "build": None}

    def hook(self, body):
        """The conformance test hook (ai/conformance, semantic): one JSON request → one JSON reply."""
        if not isinstance(body, dict):
            return {"error": "invalid_argument", "message": "Send a JSON object."}
        action = body.get("action")
        text = body.get("text")
        try:
            if action == "status":
                return self.status()
            if action == "enable":
                self.set_enabled(body.get("on") is True)
                return self.status()
            if action in ("tokenize", "embed", "rank") and not isinstance(text, str):
                raise SemanticError("invalid_argument", "text must be a string.")
            if action == "tokenize":
                return {"ids": self.tokenize(text)}
            if action == "embed":
                return {"embedding": self.embed(text)}
            if action == "rank":
                if not isinstance(body.get("indexPath"), str):
                    raise SemanticError("invalid_argument", "indexPath must be a string.")
                index = Index(body["indexPath"])
                top = body.get("top")
                top = 20 if isinstance(top, bool) or not isinstance(top, int) else max(top, 1)
                return {"rows": [{"row": row, "itemKey": index.chunk(row)["itemKey"], "score": score}
                                 for row, score in index.rank(self.embed(text), top)]}
            if action == "useIndex":
                path = body.get("path")
                if path is not None and not isinstance(path, str):
                    raise SemanticError("invalid_argument", "path must be a string or null.")
                self.use_index(path)
                return self.status()
            raise SemanticError("invalid_argument", f"Unknown action {action}.")
        except SemanticError as e:
            return {"error": e.code, "message": str(e)}
