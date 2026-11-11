"""PDF files and page images for the standalone server (ai/spec/tools.md, "PDF pages and files").

Files come from where Zotero syncs them: a WebDAV server (`ITEMKEY.zip`, as Zotero stores them)
when one is configured, else Zotero storage through the Web API. They're cached under the data
directory. Rendering needs pypdfium2 and Pillow (`pip install pypdfium2 pillow`); without them the
PDF tools answer `unavailable` and everything else works.
"""
import base64
import http.client
import io
import math
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib

MAX_FILE_BYTES = 200 * 1024 * 1024  # largest PDF this server downloads
MAX_RESOURCE_BYTES = 25 * 1024 * 1024  # zotero_get_pdf_file
CACHE_BYTES = 1024 * 1024 * 1024
MAX_SIDE = 2000
JPEG_QUALITY = 85

_render_lock = threading.Lock()  # PDFium isn't thread-safe

# What Files.path fetches: how the file starts, its extension, what it's called in messages, its
# largest size. A zip is the semantic search index (ai/spec/semantic.md caps it at 512 MiB).
KINDS = {"pdf": (b"%PDF-", ".pdf", "PDF", MAX_FILE_BYTES), "zip": (b"PK\x03\x04", ".zip", "ZIP file", 512 * 1024 * 1024)}


class PdfError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class TooLarge(PdfError):
    def __init__(self, message="The file is too large."):
        super().__init__("unavailable", message)


def _pdfium():
    try:
        import pypdfium2
        import PIL.Image  # noqa: F401 — needed for JPEG output
        return pypdfium2
    except ImportError:
        raise PdfError("unavailable", "Page images need pypdfium2 and Pillow on the server (pip install pypdfium2 pillow).")


# region Page geometry (identical on every client)

def round_half_up(v):
    return math.floor(v + 0.5)


def plan(page_width, page_height, region, max_width):
    """The clipped region (fractions) and the image size for a page of page_width × page_height points."""
    if region is None:
        x0, y0, x1, y1 = 0.0, 0.0, 1.0, 1.0
    else:
        if not isinstance(region, list) or len(region) != 4 or region[2] <= 0 or region[3] <= 0:
            raise PdfError("invalid_argument", "region must be [x, y, width, height] with width and height above 0.")
        x, y, w, h = region
        x0, y0, x1, y1 = max(0.0, x), max(0.0, y), min(1.0, x + w), min(1.0, y + h)
        if x1 <= x0 or y1 <= y0:
            raise PdfError("invalid_argument", "region is outside the page.")
    if isinstance(max_width, bool) or not isinstance(max_width, (int, float)):
        max_width = 1200
    width = min(max(int(max_width), 200), MAX_SIDE)
    W, H = (x1 - x0) * page_width, (y1 - y0) * page_height
    if not math.isfinite(width * H / W):  # a region so thin that the ratio overflows
        return (x0, y0, x1, y1), 1, MAX_SIDE
    height = round_half_up(width * H / W)
    if height > MAX_SIDE:
        height = MAX_SIDE
        width = round_half_up(MAX_SIDE * W / H)
    return (x0, y0, x1, y1), max(1, width), max(1, height)

# endregion


# region Rendering

def _open(path):
    pdfium = _pdfium()
    try:
        return pdfium.PdfDocument(path)
    except pdfium.PdfiumError:
        raise PdfError("unavailable", "This PDF can't be opened (it may be damaged or password-protected).")


def page_count(path):
    with _render_lock:
        doc = _open(path)
        try:
            return len(doc)
        finally:
            doc.close()


def render(path, page, region, max_width):
    """→ (pageCount, width, height, JPEG bytes) for a 1-based page of the PDF at `path` (or bytes)."""
    with _render_lock:
        doc = _open(path)
        try:
            count = len(doc)
            if not 1 <= page <= count:
                raise PdfError("invalid_argument", f"page must be between 1 and {count}.")
            pg = doc[page - 1]
            try:
                pw, ph = pg.get_size()  # crop box, rotation applied
                (x0, y0, x1, y1), width, height = plan(pw, ph, region, max_width)
                image = _draw(pg, pw, ph, (x0, y0, x1, y1), width, height)
            finally:
                pg.close()
        finally:
            doc.close()
    if image.size != (width, height):
        image = image.resize((width, height))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=JPEG_QUALITY)
    return count, width, height, out.getvalue()


def _draw(pg, pw, ph, box, width, height):
    """The region as a PIL image of about width × height (resized to exactly that by the caller)."""
    x0, y0, x1, y1 = box
    W, H = (x1 - x0) * pw, (y1 - y0) * ph
    # PDFium scales both axes alike: take the smaller scale, so the bitmap is never larger than
    # the image, and stretch afterwards.
    scale = min(width / W, height / H)
    if W * scale >= 1 and H * scale >= 1:
        crop = (x0 * pw, (1 - y1) * ph, (1 - x1) * pw, y0 * ph)  # left, bottom, right, top
        try:
            return _bitmap_image(pg.render(scale=scale, crop=crop, fill_color=(255, 255, 255, 255), may_draw_forms=False))
        except ValueError:  # rounding left nothing to draw
            pass
    # A sliver thinner than a pixel: draw the page small and cut the sliver out of it.
    scale = min(MAX_SIDE / pw, MAX_SIDE / ph)
    image = _bitmap_image(pg.render(scale=scale, fill_color=(255, 255, 255, 255), may_draw_forms=False))
    left, top = min(int(x0 * image.width), image.width - 1), min(int(y0 * image.height), image.height - 1)
    return image.crop((left, top, max(left + 1, round(x1 * image.width)), max(top + 1, round(y1 * image.height))))


def _bitmap_image(bitmap):
    try:
        return bitmap.to_pil().convert("RGB")
    finally:
        bitmap.close()  # inside the PDFium lock

# endregion


# region Files

class Files:
    """Gets attachment files (PDFs, and the semantic search index's ZIP) from WebDAV or Zotero storage
    into a disk cache."""

    def __init__(self, library, cache_dir, webdav=None):
        self.library = library
        self.cache_dir = cache_dir
        self.webdav = webdav  # {"url", "username", "password"} or None
        self.locks = {}
        self.locks_lock = threading.Lock()
        self.cache_lock = threading.Lock()  # writing and pruning the cache

    @staticmethod
    def webdav_base(url):
        """The folder Zotero uses: <url>/zotero/, with https:// when no scheme is given."""
        url = url.strip()
        if not re.match(r"^https?://", url, re.I):
            url = "https://" + url
        return url.rstrip("/") + "/zotero/"

    def _lock(self, key):
        with self.locks_lock:
            return self.locks.setdefault(key, threading.Lock())

    def path(self, attachment, kind="pdf"):
        """→ (path of the cached file, filename); downloads it first if needed. Raises PdfError."""
        _, ext, label, cap = KINDS[kind]
        if attachment.get("linkMode") == "linked_file":
            raise PdfError("unavailable", f"This {label} is a linked file, which only the computer that has it can open.")
        key = attachment["key"]
        filename = attachment.get("filename") or "file" + ext
        with self._lock(key):
            try:
                if self.webdav:
                    version = self._webdav_hash(key)
                else:
                    version = attachment.get("md5")
                    if not version:
                        raise PdfError("unavailable", "This file hasn't been synced to Zotero storage.")
                path = os.path.join(self.cache_dir, key, re.sub(r"[^0-9a-f]", "", version.lower())[:64] or "file")
                if not os.path.exists(path):
                    with tempfile.TemporaryDirectory(dir=self._tmp_dir()) as tmp:
                        download = os.path.join(tmp, "download")
                        if self.webdav:
                            self._fetch(self._webdav_request(f"{key}.zip"), download, cap + cap // 100)  # a zip of it can be a little larger
                            self._unzip(download, filename, os.path.join(tmp, "file"), kind)
                            download = os.path.join(tmp, "file")
                        else:
                            self._fetch(self._storage_request(key), download, cap)
                        self._check(download, kind)
                        self._store(key, download, path)
                os.utime(path)  # recently used: kept by _prune
                return path, filename
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise PdfError("unavailable", "The file isn't on the " + ("WebDAV server." if self.webdav else "Zotero storage server."))
                raise PdfError("unavailable", f"Couldn't download the file (HTTP {e.code}).")
            except (urllib.error.URLError, http.client.HTTPException, zlib.error, RuntimeError, NotImplementedError,
                    zipfile.BadZipFile, ValueError, EOFError) as e:
                raise PdfError("unavailable", f"Couldn't download the file: {getattr(e, 'reason', None) or e.__class__.__name__}")
            except OSError:
                raise PdfError("unavailable", "Couldn't download or store the file on the server.")

    def _tmp_dir(self):
        os.makedirs(self.cache_dir, mode=0o700, exist_ok=True)
        return self.cache_dir

    @staticmethod
    def _check(path, kind):
        signature, _, label, _ = KINDS[kind]
        with open(path, "rb") as f:
            head = f.read(1024)
        if not (signature in head if kind == "pdf" else head.startswith(signature)):
            raise PdfError("unavailable", f"This file isn't a {label}.")

    # WebDAV: ITEMKEY.prop holds the file's hash, ITEMKEY.zip the file.

    def _webdav_request(self, name):
        req = urllib.request.Request(self.webdav_base(self.webdav["url"]) + urllib.parse.quote(name))
        auth = base64.b64encode(f"{self.webdav['username']}:{self.webdav['password']}".encode()).decode()
        req.add_unredirected_header("Authorization", "Basic " + auth)
        return req

    def _webdav_hash(self, key):
        with urllib.request.urlopen(self._webdav_request(f"{key}.prop"), timeout=60) as resp:
            prop = resp.read(64 * 1024).decode("utf-8", "replace")
        m = re.search(r"<hash>([0-9a-fA-F]{32})</hash>", prop)
        if not m:
            raise PdfError("unavailable", "The WebDAV server has no usable record of this file.")
        return m.group(1)

    @staticmethod
    def _unzip(zip_path, filename, out_path, kind="pdf"):
        _, ext, label, cap = KINDS[kind]
        with zipfile.ZipFile(zip_path) as archive:
            entries = [e for e in archive.infolist() if not e.is_dir()]
            entry = (next((e for e in entries if e.filename == filename), None)
                     or next((e for e in entries if e.filename.lower().endswith(ext)), None)
                     or (entries[0] if len(entries) == 1 else None))
            if not entry:
                raise PdfError("unavailable", f"The file's zip on the WebDAV server has no {label} in it.")
            if entry.file_size > cap:
                raise TooLarge(f"The {label} is too large.")
            with archive.open(entry) as src, open(out_path, "wb") as dst:
                Files._copy_capped(src, dst, cap)

    # Zotero storage: the Web API redirects to the file; the API key never follows the redirect.

    def _storage_request(self, key):
        req = urllib.request.Request(f"https://api.zotero.org{self.library.base}/items/{key}/file")
        req.add_unredirected_header("Zotero-API-Key", self.library.api_key)
        req.add_unredirected_header("Zotero-API-Version", "3")
        return req

    @staticmethod
    def _fetch(req, out_path, cap):
        with urllib.request.urlopen(req, timeout=120) as resp, open(out_path, "wb") as out:
            Files._copy_capped(resp, out, cap)

    @staticmethod
    def _copy_capped(src, dst, cap):
        total = 0
        while chunk := src.read(1024 * 1024):
            total += len(chunk)
            if total > cap:
                raise TooLarge()
            dst.write(chunk)

    def _store(self, key, src, path):
        with self.cache_lock:
            folder = os.path.dirname(path)
            os.makedirs(folder, mode=0o700, exist_ok=True)
            for old in os.listdir(folder):  # older versions of this file
                try:
                    os.remove(os.path.join(folder, old))
                except FileNotFoundError:
                    pass
            os.chmod(src, 0o600)
            os.replace(src, path)
            self._prune()

    def _prune(self):
        """Keeps the cache under CACHE_BYTES, oldest first, sparing files used in the last 10 minutes."""
        files = []
        for root, _, names in os.walk(self.cache_dir):
            for n in names:
                p = os.path.join(root, n)
                try:
                    st = os.stat(p)
                except FileNotFoundError:
                    continue
                files.append((st.st_mtime, st.st_size, p))
        total = sum(f[1] for f in files)
        recent = time.time() - 600
        for mtime, size, p in sorted(files):
            if total <= CACHE_BYTES or mtime > recent:
                break
            try:
                os.remove(p)
            except FileNotFoundError:
                pass
            total -= size

# endregion
