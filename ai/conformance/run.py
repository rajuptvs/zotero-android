#!/usr/bin/env python3
"""Cross-device conformance for the Zotero AI tools (ai/spec).

Runs the same tool calls against two clients, diffs the JSON results, and checks that writes made
on one device show up on the other after a Zotero sync.

Drivers:
  android  - debug build's test provider over adb (content call ... --method tool / sync)
  desktop  - desktop plugin's dev endpoints on Zotero's local server (/zotero-ai/test/...)

  python3 ai/conformance/run.py --serial emulator-5554 --desktop http://127.0.0.1:23119
"""
import argparse
import base64
import http.client
import json
import os
import random
import string
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import zipfile


class Android:
    name = "android"

    def __init__(self, serial, package="org.zotero.android.debug"):
        self.serial = serial
        self.uri = f"content://{package}.aitest"

    def _call(self, method, arg=None, args=None, timeout=300):
        cmd = ["adb", "-s", self.serial, "shell", "content", "call", "--uri", self.uri, "--method", method]
        if arg:
            cmd += ["--arg", arg]
        if args is not None:
            encoded = base64.b64encode(json.dumps(args).encode()).decode()
            cmd += ["--extra", f"args64:s:{encoded}"]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
        prefix = "Result: Bundle[{result="
        if not out.startswith(prefix):
            raise RuntimeError(f"android {method} {arg}: {out[:300]}")
        result = json.loads(out[len(prefix):-2])
        if isinstance(result, dict) and list(result) == ["_file"]:  # too large for a binder reply
            package = self.uri.split("//")[1].removesuffix(".aitest")
            data = subprocess.run(["adb", "-s", self.serial, "exec-out", "run-as", package, "sh", "-c",
                                   f"cat {result['_file']} && rm {result['_file']}"], capture_output=True, timeout=timeout).stdout
            result = json.loads(data.decode("utf-8"))
        return result

    def batch(self, calls, timeout=600):
        """Many hook calls in one adb round trip: [(method, arg, args)] → their results, in order."""
        return self._call("batch", None, {"calls": [{"method": m, "arg": a, "args": x} for m, a, x in calls]}, timeout=timeout)["results"]

    def tools(self, calls):
        return self.batch([("tool", name, args) for name, args in calls])

    def tool(self, name, args):
        return self._call("tool", name, args)

    def sync(self):
        return self._call("sync")

    def describe(self, name, args):
        return self._call("describe", name, args)

    def notes(self, html):
        return self._call("notes", None, {"html": html})

    def markdown(self, text):
        return self._call("markdown", None, {"markdown": text})["html"]

    def policy(self):
        return self._call("policy")

    def mcpremote(self, payload):
        return self._call("mcpremote", None, payload)

    def pdfrender(self, payload):
        return self._call("pdfrender", None, payload)


class Desktop:
    name = "desktop"

    def __init__(self, base, token_file):
        self.base = base.rstrip("/")
        with open(os.path.expanduser(token_file)) as f:
            self.token = f.read().strip()

    def _post(self, path, body, timeout=300):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-Zotero-AI-Test-Token": self.token}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())

    def tool(self, name, args):
        return self._post("/zotero-ai/test/tool", {"name": name, "args": args})

    def tools(self, calls):
        return pmap(lambda c: self.tool(*c), calls)

    def sync(self):
        return self._post("/zotero-ai/test/sync", {})

    def describe(self, name, args):
        return self._post("/zotero-ai/test/describe", {"name": name, "args": args})

    def notes(self, html):
        return self._post("/zotero-ai/test/notes", {"html": html})

    def markdown(self, text):
        return self._post("/zotero-ai/test/markdown", {"markdown": text})["html"]

    def policy(self):
        return self._post("/zotero-ai/test/policy", {})

    def mcpremote(self, payload):
        return self._post("/zotero-ai/test/mcp-remote", payload)

    def pdfrender(self, payload):
        return self._post("/zotero-ai/test/pdf-render", payload)

    def eval(self, code):
        return self._post("/zotero-ai/test/eval", {"code": code})["result"]

    def chat_notes(self):
        """HTML of the real conversation notes in the library, to read back on both devices."""
        code = ("let items = await Zotero.Items.getAll(Zotero.Libraries.userLibraryID, false, false);"
                "return items.filter(i => i.isNote() && !i.deleted && ZoteroAI.Notes.isChatNote(i.getNoteTitle(), i.getNote()))"
                ".map(i => ({ name: 'real note ' + i.key, html: i.getNote() }));")
        return self._post("/zotero-ai/test/eval", {"code": code})["result"]


# --full also runs the slow, real-world checks: syncing to Android, the real 20 s approval hold, PDF
# downloads, and a semantic index built from real items. The default is the fast dev-loop tier.
FULL = False
FAST_HOLD_MS = 500  # approval hold in the fast tier (the test hooks shorten it, like expiry)


def settle(client, reply):
    """A write's final result: keeps calling zotero_await_approval while it's only running (with a
    short hold, an approved write can still be running when one call returns)."""
    deadline = time.time() + 30
    while isinstance(reply, dict) and reply.get("status") == "running" and time.time() < deadline:
        reply = client.tool("zotero_await_approval", {"approval_id": reply["approval_id"]})
    return reply


def pmap(fn, items, workers=8):
    """fn over items in parallel threads, results in order."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fn, items))


def normalize(value):
    """Collapse whitespace in long free text (HTML→text conversion differs slightly by platform)."""
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize(v) for v in value]
    if isinstance(value, str):
        return " ".join(value.split())
    return value


def diff(a, b, path=""):
    out = []
    if type(a) != type(b):
        return [f"{path}: {json.dumps(a)[:120]} != {json.dumps(b)[:120]}"]
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                out.append(f"{path}.{k}: only on {'android' if k in a else 'desktop'}")
            else:
                out += diff(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append(f"{path}: length {len(a)} != {len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            out += diff(x, y, f"{path}[{i}]")
    elif a != b:
        out.append(f"{path}: {json.dumps(a)[:120]} != {json.dumps(b)[:120]}")
    return out


READ_CALLS = [
    ("zotero_search_items", {"query": "attention", "tag": None, "collection_key": None, "limit": None}),
    ("zotero_search_items", {"query": "reasoning 2025", "tag": None, "collection_key": None, "limit": 10}),
    ("zotero_search_items", {"query": "", "tag": "harness", "collection_key": None, "limit": 50}),
    ("zotero_get_item_metadata", {"item_key": "2MIWXEWJ"}),
    ("zotero_get_item_metadata", {"item_key": "4WS8XRLA"}),
    ("zotero_get_item_metadata", {"item_key": "NOPE0000"}),
    ("zotero_get_item_fulltext", {"item_key": "2MIWXEWJ", "max_chars": 2000}),
    ("zotero_get_collections", {}),
    ("zotero_get_tags", {"limit": 30}),
    ("zotero_get_recent", {"limit": 5}),
    ("zotero_get_notes", {"item_key": "4WS8XRLA", "query": None, "limit": None}),
    ("zotero_get_notes", {"item_key": None, "query": "transformer", "limit": 5}),
    ("zotero_get_annotations", {"item_key": "4WS8XRLA"}),
    # Paging, filters and mode "everything" (tools.md, zotero_search_items).
    ("zotero_search_items", {"query": "attention", "tag": None, "collection_key": None, "limit": 5, "offset": 3, "mode": None,
                             "item_type": None, "year_from": None, "year_to": None}),
    ("zotero_search_items", {"query": "", "tag": None, "collection_key": None, "limit": 100, "offset": None, "mode": None,
                             "item_type": "preprint", "year_from": 2024, "year_to": 2025}),
    ("zotero_search_items", {"query": "treebank", "tag": None, "collection_key": None, "limit": None, "offset": None,
                             "mode": "everything", "item_type": None, "year_from": None, "year_to": None}),
    ("zotero_search_items", {"query": "encoders", "tag": None, "collection_key": None, "limit": 100, "offset": None,
                             "mode": "everything", "item_type": None, "year_from": None, "year_to": None}),
    ("zotero_search_items", {"query": "grpo reward", "tag": None, "collection_key": None, "limit": 100, "offset": 2,
                             "mode": "everything", "item_type": None, "year_from": None, "year_to": None}),
    ("zotero_search_items", {"query": "x", "tag": None, "collection_key": None, "limit": None, "offset": None,
                             "mode": "all", "item_type": None, "year_from": None, "year_to": None}),
    ("zotero_get_item_fulltext", {"item_key": "2MIWXEWJ", "max_chars": 3000, "start": 37000}),
    ("zotero_get_item_fulltext", {"item_key": "2MIWXEWJ", "max_chars": 100, "start": 10 ** 9}),
    ("zotero_get_tags", {"limit": 10, "offset": 10}),
    ("zotero_get_notes", {"item_key": None, "query": None, "limit": 5, "offset": 5}),
    ("zotero_export_items", {"item_keys": ["4WS8XRLA", "2MIWXEWJ", "4WS8XRLA"], "format": "bibtex"}),
    ("zotero_export_items", {"item_keys": ["2MIWXEWJ", "4WS8XRLA"], "format": "csl_json"}),
    ("zotero_export_items", {"item_keys": ["2MIWXEWJ"], "format": "ris"}),
    ("zotero_export_items", {"item_keys": [], "format": "bibtex"}),
    ("zotero_export_items", {"item_keys": ["2MIWXEWJ", "NOPE0000"], "format": "bibtex"}),
]


class Mcp:
    """An MCP client of the Zotero AI server (ai/spec/mcp.md). Transport-agnostic apart from the URL and
    bearer token, so the same checks can run against another server implementing the contract."""
    name = "mcp"

    def __init__(self, url, token):
        self.url = urllib.parse.urlparse(url)
        self.token = token
        self._id = 0

    def raw(self, body=None, headers=None, method="POST", auth=True):
        """Returns (status, response headers, parsed JSON or None); status None if the connection was dropped."""
        conn = http.client.HTTPConnection(self.url.hostname, self.url.port, timeout=60)
        h = {"Content-Type": "application/json"}
        if auth and self.token:
            h["Authorization"] = f"Bearer {self.token}"
        h.update(headers or {})
        data = None if body is None else (body if isinstance(body, (bytes, str)) else json.dumps(body))
        try:
            conn.request(method, self.url.path, body=data, headers=h)
            resp = conn.getresponse()
            text = resp.read().decode("utf-8")
        except (http.client.RemoteDisconnected, ConnectionResetError, http.client.BadStatusLine):
            return None, {}, None
        finally:
            conn.close()
        try:
            parsed = json.loads(text) if text else None
        except ValueError:
            parsed = text
        return resp.status, dict(resp.getheaders()), parsed

    def rpc(self, method, params=None):
        self._id += 1
        status, _, reply = self.raw({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}})
        if status != 200 or "result" not in reply:
            raise RuntimeError(f"mcp {method}: {status} {reply}")
        return reply["result"]

    def call_full(self, name, args):
        """The tool result with its extra content parts under _content, as the desktop test hook returns it."""
        result = self.rpc("tools/call", {"name": name, "arguments": args})
        value = json.loads(result["content"][0]["text"])
        if result["content"][1:]:
            value["_content"] = result["content"][1:]
        return value

    def call(self, name, args):
        """The tool result as JSON, plus isError."""
        result = self.rpc("tools/call", {"name": name, "arguments": args})
        return json.loads(result["content"][0]["text"]), result["isError"]

    def tool(self, name, args):
        return self.call(name, args)[0]


class DesktopApprover:
    """Plays the human deciding approval requests, through the desktop dev hooks."""

    def __init__(self, desktop):
        self.desktop = desktop

    def client(self, name, scope, enable=True):
        return self.desktop._post("/zotero-ai/test/mcp-client", {"enable": enable, "create": {"name": name, "scope": scope}})

    def setup(self, data):
        return self.desktop._post("/zotero-ai/test/mcp-client", data)

    def pending(self):
        return self.desktop._post("/zotero-ai/test/approvals", {})["pending"]

    def decide(self, op_id, approved):
        return self.desktop._post("/zotero-ai/test/approvals", {"decide": {"id": op_id, "approved": approved}})["decided"]

    def timing(self, **t):
        return self.desktop._post("/zotero-ai/test/approvals", {"timing": t})

    def restart(self):
        return self.desktop._post("/zotero-ai/test/approvals", {"shutdown": True})

    def decline_all(self):
        for op in self.pending():
            self.decide(op["id"], False)

    def clients(self):
        return self.desktop._post("/zotero-ai/test/eval", {"code": "return ZoteroAI.McpClients.list();"})["result"]


class StandaloneApprover:
    """Plays the human for the standalone server, through its admin API (password-protected)."""

    def __init__(self, base, password):
        self.base = urllib.parse.urlparse(base)
        self.url = base.rstrip("/") + "/mcp"
        self.cookie, self.csrf = None, ""
        status, headers, body = self._req("/admin/api/login", {"password": password})
        if status != 200:
            raise RuntimeError(f"standalone login failed: {status}")
        self.cookie = headers.get("Set-Cookie", "").split(";")[0]
        self.csrf = body["csrf"]

    def _req(self, path, body=None):
        conn = http.client.HTTPConnection(self.base.hostname, self.base.port, timeout=60)
        h = {"Content-Type": "application/json", "X-CSRF-Token": self.csrf}
        if self.cookie:
            h["Cookie"] = self.cookie
        conn.request("POST" if body is not None else "GET", path, body=json.dumps(body) if body is not None else None, headers=h)
        resp = conn.getresponse()
        data = resp.read().decode()
        headers = dict(resp.getheaders())
        conn.close()
        return resp.status, headers, json.loads(data) if data else None

    def _post(self, path, body):
        return self._req(path, body)[2]

    def client(self, name, scope, enable=True):
        if enable:
            self._post("/admin/api/enabled", {"on": True})
        created = self._post("/admin/api/clients", {"name": name, "scope": scope})
        return {"url": self.url, "created": created}

    def setup(self, data):
        if "enable" in data:
            self._post("/admin/api/enabled", {"on": data["enable"]})
        if data.get("revoke"):
            self._post("/admin/api/revoke", {"id": data["revoke"]})
        if data.get("autoApprove"):
            self._post("/admin/api/auto-approve", data["autoApprove"])
        return {"url": self.url}

    def pending(self):
        return self._req("/admin/api/state")[2]["pending"]

    def decide(self, op_id, approved):
        return self._post("/admin/api/decide", {"id": op_id, "approved": approved})["decided"]

    def timing(self, **t):
        return self._post("/admin/api/timing", t)

    def restart(self):
        return self._post("/admin/api/restart", {})

    def pdfrender(self, payload):
        return self._post("/admin/api/render", payload)

    def decline_all(self):
        for op in self.pending():
            self.decide(op["id"], False)

    def clients(self):
        return self._req("/admin/api/state")[2]["clients"]


def spec_mcp_tools(spec_tools, scope):
    """ai/spec/mcp.md catalog derivation, computed independently of the server."""
    out = []
    for t in spec_tools:
        if scope != "write" and (t.get("x-write") or t.get("x-write-scope")):
            continue
        schema = json.loads(json.dumps(t["parameters"]))
        props = schema.get("properties", {})
        def nullable(k):
            ty = props.get(k, {}).get("type", [])
            return "null" in (ty if isinstance(ty, list) else [ty])
        schema["required"] = [k for k in schema.get("required", []) if not nullable(k)]
        out.append({"name": t["name"], "description": t["description"], "inputSchema": schema,
                    "annotations": {"readOnlyHint": not t.get("x-write", False), "destructiveHint": bool(t.get("x-always-ask"))}})
    return out


def mcp_checks(results, android, desktop, spec_tools, write_key, xpi_path, human=None, host="desktop"):
    """Checks one MCP host against ai/spec/mcp.md part A. `host` is "desktop" (the plugin) or
    "standalone" (the Web API server); `human` decides approvals on that host."""
    label = "mcp" if host == "desktop" else "standalone"
    print(f"== {label}: read-only endpoint")
    human = human or DesktopApprover(desktop)
    human.timing(expireMs=300000, keepMs=600000, replayMs=120000, holdMs=FAST_HOLD_MS)
    human.decline_all()
    setup = human.client("conformance writer", "write")
    url = setup["url"]
    writer = Mcp(url, setup["created"]["token"])
    reader = Mcp(url, human.client("conformance reader", "read")["created"]["token"])
    other = Mcp(url, human.client("conformance other", "write")["created"]["token"])
    clients = [setup["created"]["client"]["id"]]

    init = writer.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "conformance", "version": "1"}})
    check(results, f"{label} initialize", init["protocolVersion"] == "2025-06-18" and init["serverInfo"]["name"] == "zotero-ai", json.dumps(init)[:200])
    init = writer.rpc("initialize", {"protocolVersion": "1999-01-01", "capabilities": {}})
    check(results, f"{label} initialize answers an unknown version with a supported one", init["protocolVersion"] == "2025-11-25")
    for scope, client in (("write", writer), ("read", reader)):
        listed = client.rpc("tools/list")["tools"]
        check(results, f"{label} tools/list ({scope}) is derived from tools.json", listed == spec_mcp_tools(spec_tools, scope),
              "; ".join(diff(listed, spec_mcp_tools(spec_tools, scope))[:3]))

    collections = desktop.tool("zotero_get_collections", {})["collections"]
    calls = list(READ_CALLS) + [("zotero_get_collection_items", {"collection_key": c["key"], "limit": None}) for c in collections[:2]]
    got_m, got_d, got_a = pmap(lambda f: f(), (lambda: pmap(lambda c: writer.tool(*c), calls),
                                               lambda: desktop.tools(calls), lambda: android.tools(calls)), 3)
    for (name, args), m, d, a in zip(calls, got_m, got_d, got_a):
        problems = diff(m, d) + diff(normalize(m), normalize(a))
        check(results, f"{label} {name} {json.dumps(args)[:60]} = desktop = android", not problems, "; ".join(problems[:3]))
    full = writer.tool("zotero_search_items", {"query": "attention", "tag": None, "collection_key": None, "limit": None})
    omitted = writer.tool("zotero_search_items", {"query": "attention"})
    check(results, f"{label} omitted nullables = null", full == omitted)
    bad, is_error = writer.call("zotero_get_tags", {"limit": 2.5})
    check(results, f"{label} invalid arguments are a tool error", is_error and bad.get("error") == "invalid_argument", json.dumps(bad))

    status, _, _ = writer.raw({"jsonrpc": "2.0", "id": 1, "method": "ping"}, auth=False)
    check(results, f"{label} no token → 401", status == 401, str(status))
    status, _, _ = Mcp(url, "zai_wrong").raw({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    check(results, f"{label} wrong token → 401", status == 401, str(status))
    gone = human.client("conformance revoked", "read")
    human.setup({"revoke": gone["created"]["client"]["id"]})
    status, _, _ = Mcp(url, gone["created"]["token"]).raw({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    check(results, f"{label} revoked token → 401", status == 401, str(status))
    status, _, _ = writer.raw({"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"Origin": "https://evil.example"})
    check(results, f"{label} Origin refused", status is None if host == "desktop" else status == 403, str(status))
    status, _, _ = writer.raw({"jsonrpc": "2.0", "id": 1, "method": "ping"},
                              headers={"Origin": "https://evil.example", "Zotero-Allowed-Request": "1"})
    check(results, f"{label} Origin + Zotero-Allowed-Request → 403", status == 403, str(status))
    status, headers, _ = writer.raw(method="GET")
    check(results, f"{label} GET → 405", status == 405 and headers.get("Allow") == "POST", str(status))
    status, _, _ = writer.raw({"jsonrpc": "2.0", "method": "notifications/initialized"})
    check(results, f"{label} notification → 202", status == 202, str(status))
    status, _, reply = writer.raw({"jsonrpc": "2.0", "id": 9, "method": "resources/list"})
    check(results, f"{label} unknown method → -32601", status == 200 and reply["error"]["code"] == -32601, str(reply))
    status, _, reply = writer.raw([{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
    check(results, f"{label} batch → -32600", status == 400 and reply["error"]["code"] == -32600, str(reply))
    status, _, _ = writer.raw('{"jsonrpc":"2.0","id":1,"method":"ping"}', headers={"Content-Type": "text/plain"})
    check(results, f"{label} non-JSON content type → 415", status == 415, str(status))
    status, _, _ = writer.raw({"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"MCP-Protocol-Version": "1999-01-01"})
    check(results, f"{label} unsupported protocol header → 400", status == 400, str(status))

    if host == "desktop":
        with zipfile.ZipFile(xpi_path) as z:
            leaked = [n for n in z.namelist() if b"/zotero-ai/test/" in z.read(n)]
        check(results, "release xpi has no test endpoints", not leaked, str(leaked))
        desktop.eval("Zotero.Debug.setStore(true); Zotero.Prefs.set('debug.level', 5); return true;")
        writer.rpc("ping")
        # The token goes into the code only to compare; the logged copy of this request is redacted too.
        log = desktop.eval("let s = Zotero.Debug.get ? await Zotero.Debug.get() : ''; Zotero.Debug.setStore(false); "
                           f"return {{ redacted: s.includes('Bearer zai_[redacted]'), leaked: s.includes({json.dumps(writer.token)}) }};")
        check(results, f"{label} token is redacted in Zotero's debug log", log["redacted"] and not log["leaked"], json.dumps(log))

    # Library state as the host under test sees it.
    read = desktop.tool if host == "desktop" else writer.tool

    print(f"== {label}: writes with approval")
    tag = "zz-ai-mcp-" + "".join(random.choices(string.ascii_lowercase, k=5))
    add_args = {"item_keys": [write_key], "add_tags": [tag], "remove_tags": []}
    if FULL:  # the spec's real hold, once
        human.timing(holdMs=20000)
    t0 = time.time()
    pending = writer.tool("zotero_batch_update_tags", add_args)
    held = time.time() - t0
    human.timing(holdMs=FAST_HOLD_MS)
    check(results, f"{label} write returns pending_approval after holding ≤ 20 s",
          pending.get("status") == "pending_approval" and pending.get("approval_id", "").startswith("op_") and held < 25, f"{held:.1f}s {pending}")
    if FULL:
        check(results, f"{label} the hold is the spec's 20 s", 19 <= held < 25, f"{held:.1f}s")
    tags = read("zotero_get_item_metadata", {"item_key": write_key})["tags"]
    check(results, f"{label} pending write changed nothing", tag not in tags)
    check(results, f"{label} pending description = android = desktop describe",
          pending.get("description") == android.describe("zotero_batch_update_tags", add_args)["description"]
          == desktop.describe("zotero_batch_update_tags", add_args)["description"], pending.get("description"))
    again = writer.tool("zotero_batch_update_tags", add_args)
    check(results, f"{label} duplicate call while pending joins it",
          again.get("approval_id") == pending["approval_id"] and len([p for p in human.pending() if p["tool"] == "zotero_batch_update_tags"]) == 1)
    foreign, _ = other.call("zotero_await_approval", {"approval_id": pending["approval_id"]})
    check(results, f"{label} another client's approval_id → not_found", foreign.get("error") == "not_found", json.dumps(foreign))
    human.decide(pending["approval_id"], True)
    done = settle(writer, writer.tool("zotero_await_approval", {"approval_id": pending["approval_id"]}))
    check(results, f"{label} approved write = desktop result shape", done == {"updated": [write_key]}, json.dumps(done))
    check(results, f"{label} await returns the stored result again",
          writer.tool("zotero_await_approval", {"approval_id": pending["approval_id"]}) == done)
    replay = writer.tool("zotero_batch_update_tags", add_args)
    check(results, f"{label} retry right after success replays the result (no second write)",
          replay == done and not [p for p in human.pending() if p["tool"] == "zotero_batch_update_tags"], json.dumps(replay))
    if FULL:
        desktop.sync()
        android.sync()
        check(results, f"{label} approved change is on Android after sync",
              tag in android.tool("zotero_get_item_metadata", {"item_key": write_key})["tags"])

    remove_args = {"item_keys": [write_key], "add_tags": [], "remove_tags": [tag]}
    pending = writer.tool("zotero_batch_update_tags", remove_args)
    human.decide(pending["approval_id"], False)
    declined = writer.tool("zotero_await_approval", {"approval_id": pending["approval_id"]})
    check(results, f"{label} decline = the chat's decline", json.dumps(declined) == json.dumps({"error": "declined", "message": "The user declined this change."}), json.dumps(declined))
    retry = writer.tool("zotero_batch_update_tags", remove_args)
    check(results, f"{label} retry after a decline asks again", retry.get("status") == "pending_approval" and retry.get("approval_id") != pending["approval_id"])
    human.decide(retry["approval_id"], True)
    settle(writer, writer.tool("zotero_await_approval", {"approval_id": retry["approval_id"]}))

    a = writer.tool("zotero_create_collection", {"name": f"zz-ai-mcp A {tag}", "parent_collection_key": None})
    b = writer.tool("zotero_create_collection", {"name": f"zz-ai-mcp B {tag}", "parent_collection_key": None})
    human.decide(a["approval_id"], False)
    still = writer.tool("zotero_await_approval", {"approval_id": b["approval_id"]})
    check(results, f"{label} deciding A leaves B pending", still.get("status") == "pending_approval", json.dumps(still))
    human.decide(b["approval_id"], False)

    note_args = {"parent_item_key": write_key, "title": f"zz-ai-mcp note {tag}", "content": "From **MCP** — “quotes” too.", "tags": []}
    pending = writer.tool("zotero_create_note", note_args)
    human.decide(pending["approval_id"], True)
    first = settle(writer, writer.tool("zotero_await_approval", {"approval_id": pending["approval_id"]}))
    lost = writer.tool("zotero_create_note", note_args)  # the agent never saw `first` and calls again
    notes = read("zotero_get_notes", {"item_key": write_key, "query": None, "limit": None})["notes"]
    made = [n for n in notes if n["title"] == note_args["title"]]
    check(results, f"{label} lost response + retry creates one note", lost == first and len(made) == 1, f"{lost} {len(made)}")

    # Concurrent calls: admission must be atomic (ai/spec/mcp.md, "Writes and approvals").
    def at_once(calls):
        box = [None] * len(calls)
        threads = [threading.Thread(target=lambda i=i, c=c: box.__setitem__(i, writer.tool(*c))) for i, c in enumerate(calls)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return box
    same = at_once([("zotero_create_collection", {"name": f"zz-ai-mcp race {tag}", "parent_collection_key": None})] * 6)
    ids = {r.get("approval_id") for r in same}
    check(results, f"{label} 6 identical concurrent writes make one operation",
          len(ids) == 1 and None not in ids and len([p for p in human.pending() if p["tool"] == "zotero_create_collection"]) == 1, json.dumps(same)[:300])
    human.decide(ids.pop(), False)
    distinct = at_once([("zotero_create_collection", {"name": f"zz-ai-mcp race {tag} {i}", "parent_collection_key": None}) for i in range(6)])
    queued = [r for r in distinct if r.get("status") == "pending_approval"]
    refused = [r for r in distinct if r == {"error": "unavailable", "message": "Too many changes are waiting for approval in Zotero."}]
    check(results, f"{label} 6 distinct concurrent writes: 5 queued, 1 refused", len(queued) == 5 and len(refused) == 1, json.dumps(distinct)[:300])
    for r in queued:
        human.decide(r["approval_id"], False)
    status, _, reply = writer.raw({"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"Authorization": f"bearer \t{writer.token}"}, auth=False)
    check(results, f"{label} bearer scheme is case-insensitive", status == 200 and "result" in (reply or {}), f"{status} {reply}")

    blocked, is_error = reader.call("zotero_batch_update_tags", add_args)
    check(results, f"{label} read-only client can't request writes",
          is_error and blocked == {"error": "declined", "message": "This connection is read-only."} and not human.pending(), json.dumps(blocked))
    blocked, _ = reader.call("zotero_await_approval", {"approval_id": "op_x"})
    check(results, f"{label} read-only client can't await approvals", blocked.get("message") == "This connection is read-only.")

    writer_id = setup["created"]["client"]["id"]
    human.setup({"autoApprove": {"id": writer_id, "on": True}})
    auto = settle(writer, writer.tool("zotero_batch_update_tags", {"item_keys": [write_key], "add_tags": [tag + "-auto"], "remove_tags": []}))
    check(results, f"{label} auto-approve runs a tag change at once", auto == {"updated": [write_key]}, json.dumps(auto))
    trash = writer.tool("zotero_trash_items", {"item_keys": [write_key]})
    check(results, f"{label} trash still asks with auto-approve on", trash.get("status") == "pending_approval", json.dumps(trash))
    human.decide(trash["approval_id"], False)
    human.setup({"autoApprove": {"id": writer_id, "on": False}})
    undo = writer.tool("zotero_batch_update_tags", {"item_keys": [write_key], "add_tags": [], "remove_tags": [tag + "-auto"]})
    human.decide(undo["approval_id"], True)
    settle(writer, writer.tool("zotero_await_approval", {"approval_id": undo["approval_id"]}))

    human.timing(expireMs=1500, holdMs=3000)  # the call must still be held when it expires
    box = {}
    thread = threading.Thread(target=lambda: box.update(r=writer.tool("zotero_create_collection", {"name": f"zz-ai-mcp expire {tag}", "parent_collection_key": None})))
    thread.start()
    op_id = None
    # The host may sync before queueing it (describing the write comes first), so watch until the call returns.
    while op_id is None and thread.is_alive():
        waiting = [p for p in human.pending() if p["tool"] == "zotero_create_collection"]
        if waiting:
            op_id = waiting[0]["id"]
        time.sleep(0.05)
    thread.join()
    time.sleep(0.5)
    late = human.decide(op_id, True) if op_id else None
    expired_msg = {"error": "declined", "message": "Nobody answered the approval request in Zotero."}
    awaited = writer.tool("zotero_await_approval", {"approval_id": op_id}) if op_id else None
    check(results, f"{label} an expired request ignores a late decision",
          box.get("r") == expired_msg and late is False and awaited == expired_msg, f"{box.get('r')} {late} {awaited}")
    human.timing(expireMs=300000, holdMs=FAST_HOLD_MS)

    opened = pmap(lambda i: writer.tool("zotero_create_collection", {"name": f"zz-ai-mcp limit {i} {tag}", "parent_collection_key": None}), range(5))
    over = writer.tool("zotero_create_collection", {"name": f"zz-ai-mcp limit 5 {tag}", "parent_collection_key": None})
    check(results, f"{label} at most 5 open requests per client", over.get("error") == "unavailable" and all(o.get("status") == "pending_approval" for o in opened), json.dumps(over))
    human.decline_all()

    for how, action in (("revoke", lambda cid: human.setup({"revoke": cid})),
                          ("disable", lambda cid: human.setup({"enable": False})),
                          ("shutdown", lambda cid: human.restart())):
        temp = human.client(f"conformance {how}", "write")
        t = Mcp(url, temp["created"]["token"])
        op = t.tool("zotero_create_collection", {"name": f"zz-ai-mcp {how} {tag}", "parent_collection_key": None})
        action(temp["created"]["client"]["id"])
        human.setup({"enable": True})
        gone = not any(p["id"] == op.get("approval_id") for p in human.pending())
        check(results, f"{label} {how} declines open requests", op.get("status") == "pending_approval" and gone)
        human.setup({"revoke": temp["created"]["client"]["id"]})

    print(f"== {label}: cleanup")
    human.decline_all()
    note_keys = [n["key"] for n in made]
    for cid in [writer_id, *[c["id"] for c in human.clients() if c["name"].startswith("conformance")]]:
        human.setup({"revoke": cid})
    return note_keys


def mcp_function_name(server, tool):
    """ai/spec/mcp-client.md naming, computed independently."""
    import re
    s = re.sub(r"[^a-z0-9]+", "_", server.lower()).strip("_")[:16]
    t = re.sub(r"[^A-Za-z0-9_-]", "_", tool)
    return f"mcp_{s}_{t}"[:64]


def mcp_client_checks(results, android, desktop, serial):
    print("== chat panels as MCP clients")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from mcp_fixture import Fixture, TOOLS
    fixture = Fixture()
    port = fixture.port
    subprocess.run(["adb", "-s", serial, "reverse", f"tcp:{port}", f"tcp:{port}"], check=True, capture_output=True)
    servers = [
        {"id": "s1", "name": "Fixture JSON", "url": f"http://127.0.0.1:{port}/mcp", "enabled": True, "autoReadOnly": True, "authorization": "Bearer fixture-token"},
        {"id": "s2", "name": "Fixture SSE!", "url": f"http://127.0.0.1:{port}/mcp-sse", "enabled": True, "autoReadOnly": False, "authorization": "Bearer fixture-token"},
        {"id": "s3", "name": "Down", "url": "http://127.0.0.1:9/mcp", "enabled": True, "autoReadOnly": False, "authorization": None},
        {"id": "s4", "name": "Off", "url": f"http://127.0.0.1:{port}/mcp", "enabled": False, "autoReadOnly": False, "authorization": "Bearer fixture-token"},
    ]
    try:
        for client in (android, desktop):
            client.mcpremote({"action": "set", "servers": servers})
        a, d = pmap(lambda c: c.mcpremote({"action": "tools"}), (android, desktop), 2)
        problems = diff(a, d)
        check(results, f"mcp client: same tools, availability and approval rule on both", not problems, "; ".join(problems[:4]))
        names = [t["name"] for t in d["tools"]]
        expected = [mcp_function_name(s["name"], t["name"]) for s in servers[:2] for t in TOOLS]
        check(results, f"mcp client: tool names follow the spec", names == expected, f"{names} != {expected}")
        check(results, f"mcp client: unreachable server reported, disabled one skipped", d["unavailable"] == ["Down"], json.dumps(d["unavailable"]))
        weird = next(t for t in d["tools"] if t["name"].endswith("weird_name_v2"))
        check(results, f"mcp client: long description cut, missing schema replaced",
              len(weird["description"]) == 500 and weird["parameters"] == {"type": "object", "properties": {}} and weird["strict"] is False)
        echo1, echo2 = mcp_function_name("Fixture JSON", "echo"), mcp_function_name("Fixture SSE!", "echo")
        check(results, f"mcp client: read-only tools skip asking only where allowed",
              d["shouldAsk"][echo1] is False and d["shouldAsk"][echo2] is True
              and d["shouldAsk"][mcp_function_name("Fixture JSON", "save_note")] is True)
        long_args = {"text": "x" * 400, "b": "<tag> & \"quotes\" — ünïcode"}
        for name, args in ((echo1, {"text": "hi"}), (echo2, long_args)):
            da, dd = pmap(lambda c: c.mcpremote({"action": "describe", "name": name, "args": args}), (android, desktop), 2)
            check(results, f"mcp client: approval text equal ({name})", da == dd, f"{da} != {dd}")
        dd = desktop.mcpremote({"action": "describe", "name": echo2, "args": long_args})["description"]
        full = len(json.dumps(dict(sorted(long_args.items())), ensure_ascii=False, separators=(",", ":")))
        check(results, f"mcp client: approval text cut at 300 characters",
              dd.startswith("Run echo on Fixture SSE!: {\"b\":") and dd.endswith(f" … (+{full - 300} more characters)"), dd[-60:])
        calls = [(echo1, {"text": "<b>&'\" ünï"}), (echo2, {"text": "via SSE"}), (mcp_function_name("Fixture JSON", "picture"), {}),
                 (mcp_function_name("Fixture JSON", "gallery"), {}), (mcp_function_name("Fixture JSON", "broken"), {})]
        payloads = [{"action": "call", "name": n, "args": x} for n, x in calls]
        got_a, got_d = pmap(lambda f: f(), (lambda: android.batch([("mcpremote", None, p) for p in payloads]),
                                            lambda: pmap(desktop.mcpremote, payloads)), 2)
        for (name, _), oa, od in zip(calls, got_a, got_d):
            check(results, f"mcp client: call output and images equal ({name})", oa == od, f"{oa} != {od}")
        flood = mcp_function_name("Fixture JSON", "flood")
        oa, od = pmap(lambda c: c.mcpremote({"action": "call", "name": flood, "args": {}}), (android, desktop), 2)
        check(results, f"mcp client: a reply over 5 MiB without a length is refused on both",
              oa == od and json.loads(od["output"]).get("isError") is True, f"{str(oa)[:200]} != {str(od)[:200]}")
        call = lambda tool: desktop.mcpremote({"action": "call", "name": mcp_function_name("Fixture JSON", tool), "args": {}})
        picture, gallery = call("picture"), call("gallery")
        check(results, f"mcp client: output shapes, images passed on",
              json.loads(picture["output"]) == {"isError": False, "text": "[image 1]\n[image omitted]\n[image omitted]\na picture"}
              and picture["images"] == [{"mimeType": "image/png", "data": "iVBORw0KGgo="}]
              and json.loads(gallery["output"])["text"] == "[image 1]\n[image 2]\n[image 3]\n[image 4]\n[image omitted]"
              and [i["data"] for i in gallery["images"]] == [f"R0lGODlh{i}A==" for i in range(4)]
              and json.loads(call("broken")["output"]) == {"isError": True, "text": "it broke"} and call("broken")["images"] == [])
    finally:
        for client in (android, desktop):
            client.mcpremote({"action": "set", "servers": []})
        fixture.stop()


SEMANTIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "semantic")
# Real items with full text, for the end-to-end check: the desktop builds an index of them.
SEMANTIC_ITEMS = ["2MIWXEWJ", "4WS8XRLA", "IGMC66Y8", "PUWYDZJ4"]
SEMANTIC_QUERIES = ["replacing recurrence with attention for translation", "reasoning learned through self-play without human data",
                    "training language models on long documents", "a faster encoder-only model for retrieval"]


def same_rows(a, b, n=10):
    """Top-n rows equal, allowing neighbours to swap where their scores differ by < 1e-4 (semantic.md)."""
    a, b = a[:n], b[:n]
    if len(a) != len(b):
        return False
    i = 0
    while i < len(a):
        if a[i]["row"] == b[i]["row"]:
            i += 1
        elif i + 1 < len(a) and a[i]["row"] == b[i + 1]["row"] and a[i + 1]["row"] == b[i]["row"] \
                and abs(a[i]["score"] - a[i + 1]["score"]) < 1e-4:
            i += 2
        else:
            return False
    return True


def same_semantic_results(a, b):
    """Tool results equal, with scores within 0.002 (semantic.md, Conformance)."""
    if "error" in a or "error" in b:
        return a == b
    if a["index"] != b["index"] or len(a["items"]) != len(b["items"]):
        return False
    for x, y in zip(a["items"], b["items"]):
        if {k: v for k, v in x.items() if k != "score"} != {k: v for k, v in y.items() if k != "score"} or abs(x["score"] - y["score"]) > 0.002:
            return False
    return True


REAL_INDEX = os.path.expanduser("~/.cache/zotero-ai-conformance/semantic-real-index.zip")


def build_real_index(desktop):
    """The desktop builds an index of SEMANTIC_ITEMS into REAL_INDEX → (ok, hook reply)."""
    call = lambda p: desktop._post("/zotero-ai/test/semantic", p, timeout=3600)
    status = call({"action": "enable", "on": True})
    deadline = time.time() + 1800
    while status.get("model") not in ("ready", "error") and time.time() < deadline:
        time.sleep(3)
        status = call({"action": "status"})
    os.makedirs(os.path.dirname(REAL_INDEX), exist_ok=True)
    tmp = REAL_INDEX + ".new"
    out = call({"action": "build", "itemKeys": SEMANTIC_ITEMS, "out": tmp})
    ok = os.path.exists(tmp) and "error" not in out
    if ok:
        os.replace(tmp, REAL_INDEX)
    return ok, out


def semantic_checks(results, android, desktop, standalone=None, build=None):
    """Semantic search (ai/spec/semantic.md): golden tokenizer, embedding and ranking on every client,
    then the tool on the real library with an index the desktop builds."""
    print("== semantic search")
    package = android.uri.split("//")[1].removesuffix(".aitest")

    def android_path(local, name):
        subprocess.run(["adb", "-s", android.serial, "push", local, f"/data/local/tmp/{name}"], check=True, capture_output=True)
        subprocess.run(["adb", "-s", android.serial, "shell", "run-as", package, "cp", f"/data/local/tmp/{name}", f"files/{name}"],
                       check=True, capture_output=True)
        return f"/data/data/{package}/files/{name}"

    hosts = [("android", lambda p: android._call("semantic", None, p, timeout=900), android_path),
             ("desktop", lambda p: desktop._post("/zotero-ai/test/semantic", p, timeout=900), lambda local, name: local)]
    if standalone:
        hosts.append(("standalone", lambda p: standalone._post("/admin/api/semantic", p), lambda local, name: local))
    ready = {}
    for name, call, _ in hosts:
        status = call({"action": "status"})
        if not status.get("enabled") or status.get("model") != "ready":
            call({"action": "enable", "on": True})
        deadline = time.time() + 1800
        while status.get("model") != "ready" and status.get("model") != "error" and time.time() < deadline:
            time.sleep(5)
            status = call({"action": "status"})
        ready[name] = status.get("model") == "ready"
        check(results, f"semantic: {name} has the model ready", ready[name], json.dumps(status)[:300])
    hosts = [h for h in hosts if ready[h[0]]]

    with open(os.path.join(SEMANTIC_DIR, "golden.json")) as f:
        golden = json.load(f)
    golden_index = os.path.join(SEMANTIC_DIR, "index.zip")
    def golden_checks(host):
        name, call, place = host
        index_path = place(golden_index, "zai-golden-index.zip")
        payloads = []
        for q in golden["queries"]:
            payloads += [{"action": "tokenize", "text": q["text"]}, {"action": "embed", "text": q["text"]},
                         {"action": "rank", "indexPath": index_path, "text": q["text"], "top": 20}]
        if name == "android":  # one adb round trip
            replies = android.batch([("semantic", None, p) for p in payloads], timeout=900)
        else:
            replies = pmap(call, payloads, 6)
        got = []
        for n, q in enumerate(golden["queries"]):
            ids, emb, ranked = replies[3 * n:3 * n + 3]
            e, g = emb.get("embedding") or [0.0], q["embedding"]
            dot = sum(x * y for x, y in zip(e, g))
            cos = dot / ((sum(x * x for x in e) ** 0.5) * (sum(y * y for y in g) ** 0.5) or 1)
            got.append((ids.get("ids") == q["ids"], cos, same_rows(ranked.get("rows") or [], q["rows"])))
        return name, got

    for name, got in pmap(golden_checks, hosts, len(hosts)):
        check(results, f"semantic: {name} token ids = the reference", all(g[0] for g in got))
        cos_min = min(g[1] for g in got)
        check(results, f"semantic: {name} query embeddings match (cosine ≥ 0.9999)", cos_min >= 0.9999, f"min cosine {cos_min:.7f}")
        check(results, f"semantic: {name} ranks the golden index like the reference", all(g[2] for g in got))

    # End to end: the desktop builds an index of real items (the fast tier reuses the last one);
    # every client searches it with the tool.
    built = REAL_INDEX
    if build or not os.path.exists(built):
        ok, out = build.result() if build else build_real_index(desktop)
        check(results, "semantic: the desktop builds an index of real items", ok, json.dumps(out)[:300])
        if not ok:
            return
    for name, call, place in hosts:
        call({"action": "useIndex", "path": place(built, "zai-built-index.zip")})
    try:
        tools = {"android": android.tool, "desktop": desktop.tool}
        if standalone:
            token = standalone.client("conformance semantic", "read")["created"]
            tools["standalone"] = Mcp(standalone.url, token["token"]).tool
        queries = SEMANTIC_QUERIES + [""]
        calls = [("zotero_semantic_search", {"query": q, "limit": 3}) for q in queries]
        names = [name for name, _, _ in hosts]
        answers = dict(zip(names, pmap(lambda n: android.tools(calls) if n == "android" else pmap(lambda c: tools[n](*c), calls), names, len(names))))
        for n, query in enumerate(queries):
            args = calls[n][1]
            got = {name: answers[name][n] for name in names}
            first = got["desktop"] if "desktop" in got else next(iter(got.values()))
            same = all(same_semantic_results(first, r) for r in got.values())
            check(results, f"semantic: zotero_semantic_search {json.dumps(args)} same on {', '.join(got)}", same,
                  "; ".join(f"{n}: {json.dumps(r)[:200]}" for n, r in got.items()))
        top = desktop.tool("zotero_semantic_search", {"query": SEMANTIC_QUERIES[0], "limit": 1})
        check(results, "semantic: finds the right paper", (top.get("items") or [{}])[0].get("key") == "2MIWXEWJ", json.dumps(top)[:300])
        if standalone:
            standalone.setup({"revoke": token["client"]["id"]})
    finally:
        for name, call, _ in hosts:
            call({"action": "useIndex", "path": None})



PDF_ITEM = "XX9GTLGR"  # the PDF of "Attention Is All You Need", synced through file storage


def pdf_expected_size(page, region, max_width):
    """tools.md size rule, computed here from the test PDF's own page boxes."""
    import math
    from pdf_fixture import PAGES
    p = PAGES[page - 1]
    x0, y0, x1, y1 = p["crop"] or p["media"]
    w, h = x1 - x0, y1 - y0
    if p["rotate"] in (90, 270):
        w, h = h, w
    rx0, ry0, rx1, ry1 = 0, 0, 1, 1
    if region:
        rx0, ry0, rx1, ry1 = max(0, region[0]), max(0, region[1]), min(1, region[0] + region[2]), min(1, region[1] + region[3])
    W, H = (rx1 - rx0) * w, (ry1 - ry0) * h
    width = min(max(int(max_width), 200), 2000)
    height = math.floor(width * H / W + 0.5)
    if height > 2000:
        height, width = 2000, math.floor(2000 * W / H + 0.5)
    return width, height


def image_distance(a, b):
    """Mean difference (0-255) of two base64 JPEGs, shrunk to 32 × 32 greyscale; None without Pillow."""
    try:
        from PIL import Image
    except ImportError:
        return None
    import io
    load = lambda data: Image.open(io.BytesIO(base64.b64decode(data))).convert("L").resize((32, 32))
    pa, pb = load(a).getdata(), load(b).getdata()
    return sum(abs(x - y) for x, y in zip(pa, pb)) / len(pa)


def close_sizes(a, b):
    """Results equal, except width/height may differ by a pixel or two (Android uses whole points)."""
    if set(a) != set(b) or any(a[k] != b[k] for k in a if k not in ("width", "height")):
        return False
    return all(abs(a.get(k, 0) - b.get(k, 0)) <= 2 for k in ("width", "height"))


def pdf_checks(results, android, desktop, standalone=None):
    """Page images and PDF files (ai/spec/tools.md, "PDF pages and files") on every host."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import hashlib
    import pdf_fixture
    fixture = base64.b64encode(pdf_fixture.make_pdf()).decode()
    renderers = [("android", android), ("desktop", desktop)] + ([("standalone", standalone)] if standalone else [])
    print("== pdf: page images of the test PDF")
    def render_all(specs):
        """Each spec on every renderer, all in parallel → [{name: reply}] in spec order."""
        jobs = [(n, name, r, spec) for n, spec in enumerate(specs) for name, r in renderers]
        replies = pmap(lambda j: j[2].pdfrender({"pdf": fixture, "page": j[3][0], "region": j[3][1], "max_width": j[3][2]}), jobs)
        out = [{} for _ in specs]
        for (n, name, _, _), reply in zip(jobs, replies):
            out[n][name] = reply
        return out

    rendered = render_all(pdf_fixture.RENDERS + pdf_fixture.ERRORS)
    for (page, region, width), out in zip(pdf_fixture.RENDERS, rendered):
        expected = pdf_expected_size(page, region, width)
        ref = out["desktop"]
        problems = []
        if (ref["result"].get("width"), ref["result"].get("height")) != expected:
            problems.append(f"desktop size {ref['result']} != {expected}")
        for name, o in out.items():
            if not close_sizes(o["result"], ref["result"]):
                problems.append(f"{name} {o['result']} != desktop {ref['result']}")
            elif name != "desktop":
                d = image_distance(o["image"], ref["image"])
                if d is not None and d > 12:
                    problems.append(f"{name} image differs from desktop by {d:.1f}")
        check(results, f"pdf: page {page} region {region} width {width} on {', '.join(out)}", not problems, "; ".join(problems))
    for (page, region, width), replies in zip(pdf_fixture.ERRORS, rendered[len(pdf_fixture.RENDERS):]):
        out = {name: reply["result"] for name, reply in replies.items()}
        same = all(o == out["desktop"] for o in out.values())
        check(results, f"pdf: page {page} region {region} refused the same way", same and out["desktop"].get("error") == "invalid_argument",
              json.dumps(out)[:300])

    print("== pdf: the same paper on every device")
    tools = [("android", android.tool), ("desktop", desktop.tool)]
    if standalone:
        token = standalone.client("conformance pdf", "read")["created"]
        tools.append(("standalone", Mcp(standalone.url, token["token"]).call_full))
    calls = [
        ("zotero_get_pdf_info", {"item_key": PDF_ITEM}),
        ("zotero_get_page_image", {"item_key": PDF_ITEM, "page": 3, "region": [0.3, 0.05, 0.45, 0.45], "max_width": 800}),
        ("zotero_get_page_image", {"item_key": PDF_ITEM, "page": 1, "region": None, "max_width": None}),
        ("zotero_get_page_image", {"item_key": PDF_ITEM, "page": 99, "region": None, "max_width": None}),
        ("zotero_get_pdf_info", {"item_key": "NOPE0000"}),
    ]
    some = desktop.tool("zotero_search_items", {"query": "", "tag": None, "collection_key": None, "limit": 100})["items"]
    metas = desktop.tools([("zotero_get_item_metadata", {"item_key": i["key"]}) for i in some])
    no_pdf = next((m["key"] for m in metas if not any(a["contentType"] == "application/pdf" for a in m["attachments"])), None)
    if no_pdf:
        calls.append(("zotero_get_pdf_info", {"item_key": no_pdf}))
    answers = pmap(lambda c: dict(zip([h for h, _ in tools], pmap(lambda t: t[1](*c), tools, len(tools)))), calls, 3)
    for (name, args), out in zip(calls, answers):
        images = {host: o.pop("_content", None) for host, o in out.items()}
        ref = out["desktop"]
        problems = [f"{host} {json.dumps(o)[:160]} != desktop {json.dumps(ref)[:160]}" for host, o in out.items() if not close_sizes(o, ref)]
        if name == "zotero_get_page_image" and "error" not in ref:
            for host, parts in images.items():
                if not parts or parts[0].get("type") != "image" or parts[0].get("mimeType") != "image/jpeg":
                    problems.append(f"{host} sent no JPEG")
                elif host != "desktop":
                    d = image_distance(parts[0]["data"], images["desktop"][0]["data"])
                    if d is not None and d > 12:
                        problems.append(f"{host} image differs from desktop by {d:.1f}")
        check(results, f"pdf: {name} {json.dumps(args)[:70]} same on {', '.join(out)}", not problems, "; ".join(problems))

    # The PDF itself, over MCP only: the same file from the desktop and the standalone server.
    desktop_human = DesktopApprover(desktop)
    created = desktop_human.client("conformance pdf", "read")
    files = {"desktop": Mcp(created["url"], created["created"]["token"]).call_full("zotero_get_pdf_file", {"item_key": PDF_ITEM})}
    if standalone:
        files["standalone"] = Mcp(standalone.url, token["token"]).call_full("zotero_get_pdf_file", {"item_key": PDF_ITEM})
    digests = {}
    for host, f in files.items():
        part = (f.get("_content") or [{}])[0]
        blob = base64.b64decode(part.get("resource", {}).get("blob", ""))
        ok = (part.get("type") == "resource" and part["resource"].get("mimeType") == "application/pdf"
              and part["resource"].get("uri") == f"zotero://attachment/{PDF_ITEM}" and len(blob) == f.get("size") and blob[:5] == b"%PDF-")
        check(results, f"pdf: {host} zotero_get_pdf_file sends the file as a resource", ok, json.dumps({k: v for k, v in f.items() if k != "_content"}))
        digests[host] = (f.get("filename"), f.get("size"), hashlib.sha256(blob).hexdigest())
    if standalone:
        check(results, "pdf: desktop and standalone send the same file", digests["desktop"] == digests["standalone"], json.dumps(digests))
        standalone.setup({"revoke": token["client"]["id"]})
    desktop_human.setup({"revoke": created["created"]["client"]["id"]})


def describe_calls(collection_key):
    """Write calls whose approval text must read the same on both devices."""
    five = ["2MIWXEWJ", "7KLQ74ZI", "4GX3L9K7", "GITEQ5QS", "W74L85JR"]
    return [
        ("zotero_batch_update_tags", {"item_keys": five[:1], "add_tags": ["to-read"], "remove_tags": []}),
        ("zotero_batch_update_tags", {"item_keys": five[:2], "add_tags": [], "remove_tags": ["to-read"]}),
        ("zotero_batch_update_tags", {"item_keys": five, "add_tags": ["a", "b"], "remove_tags": ["c"]}),
        ("zotero_set_item_collections", {"item_keys": five[:1], "add_to": [collection_key], "remove_from": []}),
        ("zotero_set_item_collections", {"item_keys": five[:1], "add_to": [], "remove_from": [collection_key]}),
        ("zotero_create_collection", {"name": "Transformers", "parent_collection_key": None}),
        ("zotero_create_collection", {"name": "Transformers", "parent_collection_key": collection_key}),
        ("zotero_create_note", {"parent_item_key": None, "title": "Reading list", "content": "x", "tags": []}),
        ("zotero_create_note", {"parent_item_key": "2MIWXEWJ", "title": "Summary", "content": "x", "tags": []}),
        ("zotero_add_item_by_identifier", {"identifier": "10.48550/arXiv.1706.03762", "collection_key": None, "tags": []}),
        ("zotero_add_item_by_identifier", {"identifier": "arXiv:1810.04805", "collection_key": collection_key, "tags": []}),
        ("zotero_trash_items", {"item_keys": five[:4]}),
        ("zotero_trash_items", {"item_keys": ["NOPE0000"]}),
    ]


MATH_SAMPLES = [
    ("inline and money", "Scale by $1/\\sqrt{d_k}$ and $x_i * y_i$, costs $5 and $10."),
    ("display block", "Before.\n\n$$\\mathrm{softmax}\\left(\\frac{QK^\\top}{\\sqrt{d_k}}\\right)V$$\n\nAfter."),
    ("bracket forms", "Inline \\(a<b\\) and\n\n\\[ E = mc^2 \\]"),
    ("code is left alone", "`$not math$` and\n\n```\n$also_not$\n```\n\nbut $y$ is."),
    ("math in a table", "| a | b |\n|---|---|\n| $[0.73,\\,0.27]$ | x |"),
]


STARTED = time.time()


def check(results, label, ok, detail=""):
    results.append((label, ok, detail))
    stamp = f"[{time.time() - STARTED:6.1f}s] " if os.environ.get("CONFORMANCE_TIMES") else ""
    print(stamp + ("PASS " if ok else "FAIL ") + label + (f"  — {detail}" if detail and not ok else ""))


SECTIONS = ["sync", "read", "policy", "describe", "notes", "math", "mcpclient", "pdf", "semantic", "mcp", "standalone", "writes"]
FULL_ONLY = {"writes"}  # changes made on one device and checked on the other after syncing


def main():
    global FULL
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default="emulator-5554")
    ap.add_argument("--desktop", default="http://127.0.0.1:23119")
    ap.add_argument("--desktop-token-file", default="~/.zotero-ai-test/test-token",
                    help="per-launch secret written by desktop-plugin/scripts/dev-zotero.sh")
    ap.add_argument("--full", action="store_true",
                    help="also the slow real-world checks: cross-device writes after sync, the real 20 s approval hold, "
                         "a semantic index built from real items")
    ap.add_argument("--only", help="comma-separated sections: " + ",".join(SECTIONS))
    ap.add_argument("--skip-writes", action="store_true")
    ap.add_argument("--no-mcp", dest="mcp", action="store_false", help="skip the MCP server checks")
    ap.add_argument("--no-pdf", dest="pdf", action="store_false", help="skip the page image and PDF checks")
    ap.add_argument("--no-semantic", dest="semantic", action="store_false", help="skip the semantic search checks (they download the model)")
    ap.add_argument("--standalone", help="also check the standalone MCP server at this base URL, e.g. http://127.0.0.1:8770")
    ap.add_argument("--standalone-password", default=os.environ.get("ZOTERO_AI_MCP_ADMIN_PASSWORD"))
    opts = ap.parse_args()
    FULL = opts.full
    selected = set(opts.only.split(",")) if opts.only else set(SECTIONS) - (set() if FULL else FULL_ONLY)
    for flag, names in ((opts.mcp, {"mcp", "standalone"}), (opts.pdf, {"pdf"}), (opts.semantic, {"semantic"}),
                        (not opts.skip_writes, {"writes"}), (bool(opts.standalone), {"standalone"})):
        if not flag:
            selected -= names
    android, desktop = Android(opts.serial), Desktop(opts.desktop, opts.desktop_token_file)
    standalone = StandaloneApprover(opts.standalone, opts.standalone_password) if opts.standalone else None
    results, timings = [], []
    started = time.time()
    # --full rebuilds the real semantic index; it takes minutes, so it runs while other sections do.
    semantic_build = None
    if FULL and "semantic" in selected:
        from concurrent.futures import ThreadPoolExecutor
        semantic_build = ThreadPoolExecutor(max_workers=1).submit(build_real_index, desktop)

    def section(name, fn):
        if name not in selected:
            return
        t0 = time.time()
        print(f"== [{name}]")
        fn()
        timings.append((name, time.time() - t0))

    def sync_both():
        a, d = pmap(lambda f: f(), (android.sync, desktop.sync), 2)
        return a, d

    def sync_section():
        a, d = sync_both()
        check(results, "android sync", a.get("synced") is True)
        check(results, "desktop sync", "error" not in d)

    def read_section():
        collections = android.tool("zotero_get_collections", {})["collections"]
        calls = list(READ_CALLS)
        for c in collections[:3]:
            calls.append(("zotero_get_collection_items", {"collection_key": c["key"], "limit": None}))
            calls.append(("zotero_get_collection_items", {"collection_key": c["key"], "limit": 2, "offset": 1}))
        got_a, got_d = pmap(lambda host: host.tools(calls), (android, desktop), 2)
        for (name, args), a, d in zip(calls, got_a, got_d):
            problems = diff(normalize(a), normalize(d))
            check(results, f"{name} {json.dumps(args)}", not problems, "; ".join(problems[:5]))

    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "spec", "tools.json")) as f:
        spec_tools = json.load(f)["tools"]

    def policy_section():
        a, d = pmap(lambda f: f(), (android.policy, desktop.policy), 2)
        check(results, "shouldAsk is the same for every tool", a == d, "; ".join(diff(a, d)[:5]))
        writes = [t["name"] for t in spec_tools if t.get("x-write")]
        check(results, "every write asks with auto-approve off", all(d[n]["ask"] for n in writes))
        check(results, "trash asks even with auto-approve on", d["zotero_trash_items"]["askWithAutoApprove"] is True)
        check(results, "reads never ask", not any(d[t["name"]]["ask"] for t in spec_tools if not t.get("x-write")))

    def describe_section():
        collections = android.tool("zotero_get_collections", {})["collections"]
        calls = describe_calls(collections[0]["key"])
        got_a, got_d = pmap(lambda f: f(), (lambda: android.batch([("describe", n, x) for n, x in calls]),
                                            lambda: pmap(lambda c: desktop.describe(*c), calls)), 2)
        for (name, args), a, d in zip(calls, got_a, got_d):
            check(results, f"describe {name} {json.dumps(args)[:80]}", a == d, f"{a} != {d}")

    def notes_section():
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "notes.json")) as f:
            fixtures = json.load(f)
        every = fixtures + desktop.chat_notes()
        got_a, got_d = pmap(lambda f: f(), (lambda: android.batch([("notes", None, {"html": fx["html"]}) for fx in every]),
                                            lambda: pmap(lambda fx: desktop.notes(fx["html"]), every)), 2)
        for fixture, a, d in zip(every, got_a, got_d):
            problems = diff(a, d)
            check(results, f"notes: {fixture['name']}", not problems, "; ".join(problems[:3]))
        expected = {f["name"]: f for f in fixtures}
        a = android.notes(expected["library note, attribute stripped by note editor"]["html"])
        check(results, "stripped library note still a library chat", a["isLibraryChat"] is True)
        a = android.notes(expected["ordinary note"]["html"])
        check(results, "ordinary note isn't a chat", a["isLibraryChat"] is False and a["turns"] == [], json.dumps(a))
        a = android.notes(expected["reader note"]["html"])
        table = [t for t in a["turns"] if t["role"] == "assistant"][0]["text"]
        check(results, "table rows stay together, paragraph after it doesn't",
              "| With scaling | $[2,1]$ | $[0.73, 0.27]$ |\n\nThe output" in table and "|---|---|---|\n| Without" in table, table[:300])
        check(results, "code fence lines stay together", "```python\nscores = q @ k.T" in table, table[-300:])

    def math_section():
        # The two Markdown renderers differ in whitespace, so compare the math and the code they keep.
        import re
        marks = lambda html: re.findall(r'<(?:span|pre) class="math">.*?</(?:span|pre)>|<code>.*?</code>', html, re.S)
        got_a = [marks(r["html"]) for r in android.batch([("markdown", None, {"markdown": t}) for _, t in MATH_SAMPLES])]
        got_d = pmap(lambda sample: marks(desktop.markdown(sample[1])), MATH_SAMPLES)
        for (name, _), a, d in zip(MATH_SAMPLES, got_a, got_d):
            check(results, f"math: {name}", a == d and len(a) > 0, f"{a} != {d}")

    def write_item():
        found = desktop.tool("zotero_search_items", {"query": "", "tag": "zz-ai-test", "collection_key": None, "limit": 5})["items"]
        return found[0]["key"] if found else None

    xpi = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "desktop-plugin", "dist", "zotero-ai.xpi")

    def mcp_section():
        key = write_item()
        if not key:
            print("no zz-ai-test item to write to; skipping MCP checks")
            return
        made = mcp_checks(results, android, desktop, spec_tools, key, xpi)
        desktop.tool("zotero_trash_items", {"item_keys": made})

    def standalone_section():
        key = write_item()
        if not key:
            print("no zz-ai-test item to write to; skipping standalone MCP checks")
            return
        # The standalone server reads zotero.org: start from what has synced.
        sync_both()
        made = mcp_checks(results, android, desktop, spec_tools, key, xpi, human=standalone, host="standalone")
        desktop.sync()
        desktop.tool("zotero_trash_items", {"item_keys": made})
        desktop.sync()

    def semantic_section():
        try:
            semantic_checks(results, android, desktop, standalone, semantic_build)
        finally:
            # Don't leave the desktop building an index of the real library in the background.
            desktop._post("/zotero-ai/test/semantic", {"action": "enable", "on": False})

    section("sync", sync_section)
    section("read", read_section)
    section("policy", policy_section)
    section("describe", describe_section)
    section("notes", notes_section)
    section("math", math_section)
    section("mcpclient", lambda: mcp_client_checks(results, android, desktop, opts.serial))
    section("pdf", lambda: pdf_checks(results, android, desktop, standalone))
    section("semantic", semantic_section)
    section("mcp", mcp_section)
    section("standalone", standalone_section)
    section("writes", lambda: write_checks(results, android, desktop, sync_both))

    failed = [r for r in results if not r[1]]
    print("\n" + "  ".join(f"{name} {t:.0f}s" for name, t in timings) + f"  · total {time.time() - started:.0f}s")
    print(f"{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)


def write_checks(results, android, desktop, sync_both):
    """Changes made on one device show up on the other after syncing."""
    suffix = "".join(random.choices(string.ascii_lowercase, k=5))
    tag_a, tag_d = f"zz-ai-conf-{suffix}-android", f"zz-ai-conf-{suffix}-desktop"
    # A throwaway item created for these tests (BERT, tagged zz-ai-test).
    item = android.tool("zotero_search_items", {"query": "", "tag": "zz-ai-test", "collection_key": None, "limit": 5})["items"]
    if not item:
        print("no zz-ai-test item to write to; skipping write checks")
    else:
        key = item[0]["key"]
        print(f"== writes on {key}")
        # Earlier sections (MCP hosts) changed this item; start from the same version everywhere.
        sync_both()
        coll = android.tool("zotero_create_collection", {"name": f"zz-ai-conf {suffix}", "parent_collection_key": None})
        check(results, "android create_collection", "key" in coll, json.dumps(coll))
        r = android.tool("zotero_batch_update_tags", {"item_keys": [key], "add_tags": [tag_a], "remove_tags": []})
        check(results, "android add tag", r == {"updated": [key]}, json.dumps(r))
        r = android.tool("zotero_set_item_collections", {"item_keys": [key], "add_to": [coll["key"]], "remove_from": []})
        check(results, "android add to collection", r == {"updated": [key]}, json.dumps(r))
        note = android.tool("zotero_create_note", {"parent_item_key": key, "title": f"zz-ai-conf note {suffix}",
                                                   "content": "Made on **Android**.\n\n- point one\n- point two", "tags": [tag_a]})
        check(results, "android create_note", "key" in note, json.dumps(note))
        android.sync(); desktop.sync()
        meta = desktop.tool("zotero_get_item_metadata", {"item_key": key})
        check(results, "desktop sees android tag", tag_a in meta.get("tags", []), json.dumps(meta.get("tags")))
        check(results, "desktop sees android collection",
              any(c["key"] == coll.get("key") for c in meta.get("collections", [])), json.dumps(meta.get("collections")))
        notes = desktop.tool("zotero_get_notes", {"item_key": key, "query": None, "limit": None})["notes"]
        mine = [n for n in notes if n["key"] == note.get("key")]
        check(results, "desktop sees android note title",
              bool(mine) and mine[0]["title"] == f"zz-ai-conf note {suffix}", json.dumps(mine)[:200])

        r = desktop.tool("zotero_batch_update_tags", {"item_keys": [key], "add_tags": [tag_d], "remove_tags": [tag_a]})
        check(results, "desktop swap tags", r == {"updated": [key]}, json.dumps(r))
        r = desktop.tool("zotero_set_item_collections", {"item_keys": [key], "add_to": [], "remove_from": [coll["key"]]})
        check(results, "desktop remove from collection", r == {"updated": [key]}, json.dumps(r))
        dnote = desktop.tool("zotero_create_note", {"parent_item_key": key, "title": f"zz-ai-conf desktop note {suffix}",
                                                    "content": "Made on *desktop*.", "tags": []})
        check(results, "desktop create_note", "key" in dnote, json.dumps(dnote))
        desktop.sync(); android.sync()
        meta = android.tool("zotero_get_item_metadata", {"item_key": key})
        check(results, "android sees desktop tag swap",
              tag_d in meta.get("tags", []) and tag_a not in meta.get("tags", []), json.dumps(meta.get("tags")))
        check(results, "android sees desktop collection removal",
              all(c["key"] != coll.get("key") for c in meta.get("collections", [])), json.dumps(meta.get("collections")))
        notes = android.tool("zotero_get_notes", {"item_key": key, "query": None, "limit": None})["notes"]
        check(results, "android sees desktop note", any(n["key"] == dnote.get("key") for n in notes))

        a = normalize(android.tool("zotero_get_item_metadata", {"item_key": key}))
        d = normalize(desktop.tool("zotero_get_item_metadata", {"item_key": key}))
        problems = diff(a, d)
        check(results, "item identical on both after writes", not problems, "; ".join(problems[:5]))

        print("== cleanup")
        desktop.tool("zotero_batch_update_tags", {"item_keys": [key], "add_tags": [], "remove_tags": [tag_d]})
        android.tool("zotero_trash_items", {"item_keys": [note["key"], dnote["key"]]})
        android.sync(); desktop.sync()
        print(f"(leave collection {coll.get('key')} for manual deletion)")



if __name__ == "__main__":
    main()
