"""A standalone server on a free port with a fake library (no network), for the tests."""
import http.client
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from zotero_ai_mcp.server import Server, State  # noqa: E402
from zotero_ai_mcp.spec import Spec  # noqa: E402

PASSWORD = "correct horse battery"
IMAGE = {"type": "image", "data": "/9j/AAAA", "mimeType": "image/jpeg"}


class FakeLibrary:
    """Answers every tool with a fixed result (a page image comes with an image part) and records calls."""

    def __init__(self):
        self.items, self.collections, self.lock, self.calls = {}, {}, threading.RLock(), []

    def sync(self, force=False):
        pass

    def title(self, item):
        return item.get("title", "")

    def execute(self, name, args):
        self.calls.append((name, args))
        if name == "zotero_get_page_image":
            return {"page": args.get("page"), "_content": [dict(IMAGE)]}
        return {"ok": True, "tool": name}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Running:
    def __init__(self, oauth=True, allowed_hosts=()):
        self.dir = tempfile.mkdtemp(prefix="zai-test-")
        self.state = State(self.dir)
        self.state.set_admin_password(PASSWORD)
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.library = FakeLibrary()
        self.server = Server(self.library, Spec(), self.state, "127.0.0.1", self.port, allowed_hosts, public_url=self.base if oauth else None, oauth=oauth)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.cookie, self.csrf = None, ""

    def stop(self):
        self.server.httpd.shutdown()
        self.server.httpd.server_close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def request(self, method, path, body=None, headers=None):
        """→ (status, headers with lowercase names, JSON body or text)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=40)
        headers = dict(headers or {})
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
            headers.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        ctype = resp.getheader("Content-Type") or ""
        parsed = json.loads(data) if data and ctype.startswith("application/json") else data.decode()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, parsed

    def form(self, path, fields, headers=None):
        return self.request("POST", path, urllib.parse.urlencode(fields), {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})})

    def mcp(self, token, method, params=None, id=1):
        msg = {"jsonrpc": "2.0", "id": id, "method": method}
        if params is not None:
            msg["params"] = params
        return self.request("POST", "/mcp", msg, {"Authorization": f"Bearer {token}"} if token else {})

    def login(self):
        status, headers, body = self.request("POST", "/admin/api/login", {"password": PASSWORD})
        assert status == 200, status
        self.cookie, self.csrf = headers["set-cookie"].split(";")[0], body["csrf"]

    def admin(self, path, body=None, csrf=True, cookie=True, headers=None):
        h = dict(headers or {})
        if cookie and self.cookie:
            h["Cookie"] = self.cookie
        if csrf:
            h["X-CSRF-Token"] = self.csrf
        return self.request("POST" if body is not None else "GET", path, body, h)
