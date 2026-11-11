"""The standalone Zotero AI MCP server (ai/spec/mcp.md, part A): the ai/spec tools over MCP against the
Zotero Web API, so agents can use the library with Zotero closed. Changes wait for approval on the
server's own page (/admin), protected by an admin password that agents never have. Remote clients
can also connect with OAuth (oauth.py) when the server runs with --oauth.
"""
import base64
import hashlib
import re
import hmac
import json
import os
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class HTTPServer(ThreadingHTTPServer):
    request_queue_size = 128  # the default backlog of 5 resets bursts of new connections

from .approvals import Approvals
from .library import Library
from .oauth import OAuth, is_loopback
from .spec import Spec
from .throttle import Throttle, client_address

PROTOCOLS = ["2025-11-25", "2025-06-18"]
LONE_SURROGATE = re.compile("[\ud800-\udfff]")
READ_ONLY = {"error": "declined", "message": "This connection is read-only."}
MAX_BODY = 1024 * 1024
SESSION_SECONDS = 12 * 3600


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class State:
    """Clients, the admin password and the on/off switch, in <data dir>/state.json (mode 600)."""

    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "state.json")
        os.makedirs(data_dir, mode=0o700, exist_ok=True)
        self.lock = threading.RLock()
        self.mtime = None
        self.on_revoked = lambda client_id: None
        self.data = {"clients": [], "admin": None, "enabled": True}
        self.refresh()

    def refresh(self):
        """Picks up changes made by the CLI (add-client, revoke, set-admin-password) while serving."""
        with self.lock:
            try:
                mtime = os.stat(self.path).st_mtime_ns
            except FileNotFoundError:
                return
            if mtime == self.mtime:
                return
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            before = {c["id"] for c in self.data["clients"]}
            self.data, self.mtime = data, mtime
            for gone in before - {c["id"] for c in data["clients"]}:
                self.on_revoked(gone)

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=1)
            os.replace(tmp, self.path)
            self.mtime = os.stat(self.path).st_mtime_ns

    # Clients

    def clients(self):
        out = []
        for c in self.data["clients"]:
            view = {k: v for k, v in c.items() if k not in ("tokenHash", "oauth")}
            if c.get("oauth"):  # connected with OAuth: show where its tokens go, never the hashes
                view["oauth"] = {"redirectHost": urllib.parse.urlsplit(c["oauth"]["redirectUri"]).hostname}
            out.append(view)
        return out

    def get(self, client_id):
        return next((c for c in self.data["clients"] if c["id"] == client_id), None)

    def create_client(self, name, scope):
        if scope not in ("read", "write"):
            raise ValueError("scope must be read or write")
        self.refresh()
        token = "zai_" + secrets.token_urlsafe(32)
        client = {"id": "c_" + secrets.token_urlsafe(6), "name": (name or "").strip()[:60] or "Agent", "tokenHash": token_hash(token),
                  "scope": scope, "autoApprove": False, "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        with self.lock:
            self.data["clients"].append(client)
            self.save()
        return {k: v for k, v in client.items() if k != "tokenHash"}, token

    def revoke(self, client_id):
        self.refresh()
        with self.lock:
            self.data["clients"] = [c for c in self.data["clients"] if c["id"] != client_id]
            self.save()

    def set_auto_approve(self, client_id, on):
        self.refresh()
        with self.lock:
            c = self.get(client_id)
            if c:
                c["autoApprove"] = bool(on) and c["scope"] == "write"
                self.save()

    def authenticate(self, header):
        # The scheme is case-insensitive (RFC 9110).
        m = re.match(r"(?i)bearer[ \t]+(zai_[A-Za-z0-9_-]+)[ \t]*$", header or "")
        if not m:
            return None
        self.refresh()
        digest = token_hash(m.group(1))
        found = None
        for c in self.data["clients"]:
            if c.get("tokenHash") and hmac.compare_digest(c["tokenHash"], digest):
                found = c
        return found

    # Admin password

    def set_admin_password(self, password):
        self.refresh()
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
        with self.lock:
            self.data["admin"] = {"salt": salt.hex(), "hash": digest.hex()}
            self.save()

    def check_admin_password(self, password):
        self.refresh()
        admin = self.data.get("admin")
        if not admin:
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(admin["salt"]), 200_000)
        return hmac.compare_digest(digest.hex(), admin["hash"])


class Server:
    def __init__(self, library, spec, state, host, port, allowed_hosts=(), test_hooks=False, public_url=None, oauth=False):
        self.library, self.spec, self.state = library, spec, state
        self.test_hooks = test_hooks
        self.approvals = Approvals(spec, library, state, lambda: self.state.data.get("enabled", True))
        state.on_revoked = self.approvals.client_revoked
        self.login_failures = Throttle(5, 600)  # failed sign-ins per (address, loopback Host)
        self.login_delay = 1.0  # seconds a wrong password waits
        self.sessions = {}  # admin session → {csrf, expires}
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}", *allowed_hosts}
        self.public_url = public_url  # normalized by oauth.normalize_public_url
        if public_url:
            self.allowed_hosts.add(urllib.parse.urlsplit(public_url).netloc)
        self.oauth = OAuth(self, public_url) if oauth else None
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            timeout = 60  # seconds a connection may sit idle or mid-request

            def log_message(self, fmt, *args):  # request lines only; never headers
                pass

            def do_GET(self):
                server.dispatch(self, "GET")

            def do_POST(self):
                server.dispatch(self, "POST")

            def do_DELETE(self):
                server.dispatch(self, "DELETE")

            def do_OPTIONS(self):
                server.dispatch(self, "OPTIONS")

        self.httpd = HTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True

    # region HTTP plumbing

    def send(self, h, status, body=b"", content_type="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = LONE_SURROGATE.sub("\ufffd", body).encode("utf-8")
        h.send_response(status)
        if body:
            h.send_header("Content-Type", content_type)
        h.send_header("Content-Length", str(len(body)))
        h.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        h.end_headers()
        if body:
            h.wfile.write(body)

    def read_json(self, h):
        """The body read by dispatch, as JSON. Strings must be valid Unicode (no lone surrogates)."""
        raw = h.body
        if not raw:
            return None
        value = json.loads(raw)
        json.dumps(value, ensure_ascii=False).encode("utf-8")  # raises on lone surrogates
        return value

    def dispatch(self, h, method):
        try:
            # Read the whole body first, so no reply leaves bytes on a kept-alive connection.
            length = h.headers.get("Content-Length") or "0"
            if not length.isdigit() or int(length) > MAX_BODY:
                h.close_connection = True
                return self.send(h, 400, {"error": "Bad Content-Length"})
            h.body = h.rfile.read(int(length)) if int(length) else b""
            if h.headers.get("Host") not in self.allowed_hosts:
                return self.send(h, 400, {"error": "Invalid Host header"})
            path = h.path.split("?")[0]
            if path == "/mcp":
                return self.mcp(h, method)
            if path == "/admin" or path.startswith("/admin/"):
                return self.admin(h, method, path)
            if self.oauth and (path.startswith("/.well-known/") or path.startswith("/oauth/")):
                return self.oauth.handle(h, method, path)
            self.send(h, 404, {"error": "not found"})
        except Exception:  # noqa: BLE001 — one request, not the server
            import traceback
            traceback.print_exc()
            try:
                self.send(h, 500, {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": "Internal error"}})
            except Exception:
                pass

    # endregion

    # region MCP (same handling as desktop-plugin/src/content/mcpServer.js)

    def mcp(self, h, method):
        if method != "POST":
            return self.send(h, 405, headers={"Allow": "POST"})
        err = lambda code, message, id=None: {"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}
        if h.headers.get("Origin") is not None:
            return self.send(h, 403, err(-32000, "Requests from web pages aren't allowed."))
        if not self.state.data.get("enabled", True):
            return self.send(h, 503, err(-32000, "The server is turned off."))
        auth = h.headers.get("Authorization")
        client = self.state.authenticate(auth) or (self.oauth and self.oauth.authenticate(auth))
        if not client:
            # With OAuth on, the challenge points clients at the metadata (RFC 9728); otherwise none.
            challenge = {"WWW-Authenticate": self.oauth.challenge(bool(auth))} if self.oauth else None
            return self.send(h, 401, err(-32000, "Missing or invalid token. Add this agent on the server (python3 -m zotero_ai_mcp add-client)."), headers=challenge)
        if (h.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            return self.send(h, 415, err(-32000, "Send JSON (Content-Type: application/json)."))
        version = h.headers.get("MCP-Protocol-Version")
        if version and version not in PROTOCOLS:
            return self.send(h, 400, err(-32000, f"Unsupported MCP-Protocol-Version {version}."))
        try:
            msg = self.read_json(h)
        except (ValueError, UnicodeEncodeError):
            return self.send(h, 400, err(-32700, "Parse error"))
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return self.send(h, 400, err(-32600, "Invalid request."))
        if not isinstance(msg.get("method"), str):
            if "id" in msg and ("result" in msg or "error" in msg):
                return self.send(h, 202)
            return self.send(h, 400, err(-32600, "Invalid request.", msg.get("id")))
        if "id" not in msg:
            return self.send(h, 202)
        params = msg.get("params")
        if params is not None and not isinstance(params, dict):
            return self.send(h, 200, err(-32602, "params must be an object", msg["id"]))
        reply = self.rpc(client, msg["method"], params or {})
        if "error" in reply:
            return self.send(h, 200, {"jsonrpc": "2.0", "id": msg["id"], "error": reply["error"]})
        self.send(h, 200, {"jsonrpc": "2.0", "id": msg["id"], "result": reply["result"]})

    def rpc(self, client, method, params):
        if method == "initialize":
            asked = params.get("protocolVersion")
            return {"result": {"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0], "capabilities": {"tools": {"listChanged": False}},
                               "serverInfo": {"name": "zotero-ai", "version": "standalone"}, "instructions": self.spec.prompts["mcp"]}}
        if method == "ping":
            return {"result": {}}
        if method == "tools/list":
            return {"result": {"tools": self.spec.mcp_tools(client["scope"])}}
        if method == "tools/call":
            return self.call(client, params)
        return {"error": {"code": -32601, "message": f"Method not found: {method}"}}

    def call(self, client, params):
        name, args = params.get("name"), params.get("arguments")
        args = {} if args is None else args
        tool = self.spec.tool(name)
        if not tool:
            return {"error": {"code": -32602, "message": f"Unknown tool {name}"}}
        # Same bytes as JSON.stringify on the desktop (which writes a lone surrogate as \udXXX).
        stringify = lambda value: LONE_SURROGATE.sub(lambda m: "\\u%04x" % ord(m.group()), json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        def result(value):
            extra = value.pop("_content", []) if isinstance(value, dict) else []
            return {"result": {"content": [{"type": "text", "text": stringify(value)}, *extra],
                               "isError": isinstance(value, dict) and "error" in value}}
        if (tool.get("x-write") or tool.get("x-write-scope")) and client["scope"] != "write":
            return result(READ_ONLY)
        if name == "zotero_await_approval":
            checked = self.spec.validate_args(name, args)
            if "error" in checked:
                return result(checked)
            op = self.approvals.get(checked["args"]["approval_id"], client["id"])
            if not op:
                return result({"error": "not_found", "message": "No change with that approval_id."})
            self.approvals.wait(op, self.approvals.timing["holdMs"] / 1000)
            return result(self.approvals.envelope(op))
        if tool.get("x-write"):
            op, refused = self.approvals.request(client, name, args)
            if not op:
                return result(refused)
            self.approvals.wait(op, self.approvals.timing["holdMs"] / 1000)
            return result(self.approvals.envelope(op))
        checked = self.spec.validate_args(name, args)
        if "error" in checked:
            return result(checked)
        return result(self.library.execute(name, checked["args"]))

    # endregion

    # region Admin page and API

    def session(self, h):
        cookie = h.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "zai_admin":
                s = self.sessions.get(v)
                if s and s["expires"] > time.time():
                    return v, s
        return None, None

    def admin(self, h, method, path):
        # A browser page: only from its own origin.
        origin = h.headers.get("Origin")
        if origin is not None and origin not in (f"http://{h.headers.get('Host')}", f"https://{h.headers.get('Host')}"):
            return self.send(h, 403, {"error": "forbidden"})
        if path in ("/admin", "/admin/") and method == "GET":
            return self.send(h, 200, ADMIN_PAGE, "text/html; charset=utf-8",
                             {"Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'",
                              "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"})
        if method != "POST" and path != "/admin/api/state":
            return self.send(h, 405, headers={"Allow": "POST"})
        if path == "/admin/api/login":
            # After 5 failures in 10 minutes from one address, wait for them to age out. Sign-ins on a
            # loopback Host count apart, so guessing through the public address can't lock out the owner.
            host = urllib.parse.urlsplit("//" + (h.headers.get("Host") or "")).hostname
            bucket = (client_address(h), is_loopback(host))
            # Each attempt is counted before the password is checked (and given back if it's right), so
            # parallel requests can't all slip in while the slow hash runs.
            if not self.login_failures.take(bucket):
                return self.send(h, 429, {"error": "Too many attempts. Try again in a few minutes."})
            try:
                body = self.read_json(h) or {}
            except (ValueError, UnicodeEncodeError):
                body = {}
            if not self.state.check_admin_password(str(body.get("password", ""))):
                time.sleep(self.login_delay)
                return self.send(h, 403, {"error": "Wrong password."})
            self.login_failures.refund(bucket)
            sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
            self.sessions[sid] = {"csrf": csrf, "expires": time.time() + SESSION_SECONDS}
            behind_https = self.public_url and self.public_url.startswith("https://") and h.headers.get("Host") == urllib.parse.urlsplit(self.public_url).netloc
            secure = "; Secure" if h.headers.get("X-Forwarded-Proto") == "https" or behind_https else ""
            return self.send(h, 200, {"csrf": csrf}, headers={"Set-Cookie": f"zai_admin={sid}; HttpOnly; SameSite=Strict; Path=/admin{secure}"})
        sid, session = self.session(h)
        if not session:
            return self.send(h, 401, {"error": "Sign in first."})
        if method == "POST" and not hmac.compare_digest(h.headers.get("X-CSRF-Token") or "", session["csrf"]):
            return self.send(h, 403, {"error": "forbidden"})
        try:
            body = (self.read_json(h) if method == "POST" else None) or {}
        except (ValueError, UnicodeEncodeError):
            return self.send(h, 400, {"error": "Bad JSON"})
        if path == "/admin/api/state":
            pending = [{"id": op["id"], "clientName": op["clientName"], "tool": op["tool"], "description": op["description"],
                        "args": json.loads(op["argsJSON"]), "expiresAt": op["expiresAt"]} for op in self.approvals.pending()]
            return self.send(h, 200, {"pending": pending, "clients": self.state.clients(), "enabled": self.state.data.get("enabled", True), "csrf": session["csrf"]})
        if path == "/admin/api/decide":
            return self.send(h, 200, {"decided": self.approvals.decide(str(body.get("id")), body.get("approved") is True)})
        if path == "/admin/api/clients":
            client, token = self.state.create_client(body.get("name"), body.get("scope"))
            return self.send(h, 200, {"client": client, "token": token})
        if path == "/admin/api/revoke":
            self.revoke_client(str(body.get("id")))
            return self.send(h, 200, {"ok": True})
        if path.startswith("/admin/api/oauth/") and self.oauth:
            return self.send(h, *self.oauth.admin_api(path, body))
        if path == "/admin/api/auto-approve":
            self.state.set_auto_approve(str(body.get("id")), body.get("on") is True)
            return self.send(h, 200, {"ok": True})
        if path == "/admin/api/enabled":
            with self.state.lock:
                self.state.data["enabled"] = body.get("on") is True
                self.state.save()
            if not self.state.data["enabled"]:
                self.approvals.decline_all()
            return self.send(h, 200, {"ok": True})
        if path == "/admin/api/logout":
            self.sessions.pop(sid, None)
            return self.send(h, 200, {"ok": True})
        if self.test_hooks and path == "/admin/api/timing":
            self.approvals.timing.update({k: v for k, v in body.items() if k in self.approvals.timing})
            return self.send(h, 200, self.approvals.timing)
        if self.test_hooks and path == "/admin/api/render":
            # Renders a PDF sent by the conformance suite (same code as zotero_get_page_image).
            from . import pdf
            try:
                count, width, height, jpeg = pdf.render(base64.b64decode(body.get("pdf", "")), body.get("page"), body.get("region"), body.get("max_width"))
            except pdf.PdfError as e:
                return self.send(h, 200, {"result": {"error": e.code, "message": str(e)}})
            return self.send(h, 200, {"result": {"pageCount": count, "width": width, "height": height}, "image": base64.b64encode(jpeg).decode("ascii")})
        if self.test_hooks and path == "/admin/api/semantic":
            # Semantic search checks from the conformance suite (same code as zotero_semantic_search).
            semantic = getattr(self.library, "semantic", None)
            if not semantic:
                return self.send(h, 200, {"error": "unavailable", "message": "This server has no semantic search."})
            return self.send(h, 200, semantic.hook(body))
        if self.test_hooks and path == "/admin/api/restart":
            self.approvals.decline_all()
            return self.send(h, 200, {"ok": True})
        return self.send(h, 404, {"error": "not found"})

    # endregion

    def revoke_client(self, client_id):
        """Removes a client (its token or OAuth tokens stop working) and declines what it's waiting on."""
        self.state.revoke(client_id)
        self.approvals.client_revoked(client_id)

    def serve_forever(self):
        self.httpd.serve_forever()


ADMIN_PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Zotero AI approvals</title><style>
:root{color-scheme:light dark;--fg:#1d1d1f;--bg:#f6f6f8;--card:#fff;--line:#d9d9de;--accent:#2f6fdf;--warn:#e08a00}
@media (prefers-color-scheme:dark){:root{--fg:#ececf1;--bg:#151517;--card:#202024;--line:#3a3a40}}
body{margin:0;font:15px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--fg)}
main{max-width:640px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin:12px 0}
.req{border:2px solid var(--warn)}
pre{white-space:pre-wrap;word-break:break-word;font-size:12px;background:rgba(127,127,127,.12);padding:8px;border-radius:6px;max-height:200px;overflow:auto}
button{font:inherit;padding:6px 14px;border-radius:8px;border:1px solid var(--line);background:transparent;color:inherit}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.muted{opacity:.7;font-size:13px}
input,select{font:inherit;padding:6px;border-radius:6px;border:1px solid var(--line);background:transparent;color:inherit}
code{word-break:break-all}
</style></head><body><main>
<h1>Zotero AI</h1>
<div id="login" class="card" hidden><p>Admin password</p><div class="row"><input id="pw" type="password" autocomplete="current-password"><button class="primary" id="go">Sign in</button></div><p id="lerr" class="muted"></p></div>
<div id="app" hidden>
<div class="row"><label><input type="checkbox" id="enabled"> Let agents use the library</label></div>
<h2>Waiting for you</h2><div id="pending"></div>
<h2>Agents</h2><div id="clients"></div>
<div class="card"><div class="row"><input id="cname" placeholder="Agent name, e.g. Claude Code"><select id="cscope"><option value="write">Read and request changes</option><option value="read">Read only</option></select><button class="primary" id="add">Add agent</button></div><div id="newtoken"></div></div>
<p class="muted">Changes run only after you approve them here. Moving items to the trash always asks. Revoking an agent stops its token and declines what it's waiting on.</p>
</div></main><script>
let csrf=null, shownAt={};
const $=id=>document.getElementById(id);
const el=(tag,text,cls)=>{const e=document.createElement(tag);if(text!=null)e.textContent=text;if(cls)e.className=cls;return e};
async function api(path,body){const r=await fetch(path,{method:body===undefined?"GET":"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":csrf||""},body:body===undefined?undefined:JSON.stringify(body),credentials:"same-origin"});if(r.status==401){show(false);throw new Error("signed out")}if(!r.ok)throw new Error("HTTP "+r.status);return r.json()}
// Approve works only once the top request has been on screen for a second in a visible, focused page.
let topId=null,armedAt=0;const rearm=()=>{armedAt=document.visibilityState=="visible"&&document.hasFocus()?Date.now():Infinity};
document.addEventListener("visibilitychange",rearm);window.addEventListener("focus",rearm);window.addEventListener("blur",()=>{armedAt=Infinity});
function show(signedIn){$("login").hidden=signedIn;$("app").hidden=!signedIn}
$("go").onclick=async()=>{const r=await fetch("/admin/api/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({password:$("pw").value}),credentials:"same-origin"});const j=await r.json();if(r.ok){csrf=j.csrf;show(true);refresh()}else $("lerr").textContent=j.error||"Couldn't sign in"};
$("add").onclick=async()=>{const j=await api("/admin/api/clients",{name:$("cname").value,scope:$("cscope").value});const box=$("newtoken");box.replaceChildren(el("p","Token for "+j.client.name+" (shown once):"),el("code",j.token),el("p","Claude Code: claude mcp add --transport http zotero "+location.origin+"/mcp --header \"Authorization: Bearer "+j.token+"\"","muted"));refresh()};
$("enabled").onchange=()=>api("/admin/api/enabled",{on:$("enabled").checked}).then(refresh);
async function refresh(){let s;try{s=await api("/admin/api/state")}catch(e){return}if(s.csrf)csrf=s.csrf;$("enabled").checked=s.enabled;
 const top=s.pending[0]?s.pending[0].id:null;if(top!==topId){topId=top;rearm()}
 const p=$("pending");p.replaceChildren();if(!s.pending.length)p.append(el("p","Nothing right now.","muted"));
 for(const op of s.pending){if(!shownAt[op.id])shownAt[op.id]=Date.now();const c=el("div",null,"card req");c.append(el("strong",op.clientName+" wants to change your library"),el("p",op.description),el("div","Tool: "+op.tool,"muted"),el("pre",JSON.stringify(op.args,null,2).slice(0,2000)));
  const row=el("div",null,"row"),no=el("button","Decline"),yes=el("button","Approve","primary");
  // Approve works only after the card has been visible for a second in a focused page.
  yes.onclick=async()=>{if(op.id!==topId||Date.now()-armedAt<1000||!document.hasFocus()||document.visibilityState!="visible"){yes.textContent="Take a second to read it";return}await api("/admin/api/decide",{id:op.id,approved:true});refresh()};
  no.onclick=async()=>{await api("/admin/api/decide",{id:op.id,approved:false});refresh()};row.append(no,yes);c.append(row);p.append(c)}
 const cl=$("clients");cl.replaceChildren();if(!s.clients.length)cl.append(el("p","None yet.","muted"));
 for(const c of s.clients){const card=el("div",null,"card"),row=el("div",null,"row");row.append(el("strong",c.name),el("span",(c.scope=="write"?"Read and request changes":"Read only")+(c.oauth?" · OAuth, to "+c.oauth.redirectHost:""),"muted"));
  if(c.scope=="write"){const l=el("label"),b=el("input");b.type="checkbox";b.checked=c.autoApprove;b.onchange=()=>api("/admin/api/auto-approve",{id:c.id,on:b.checked});l.append(b,document.createTextNode(" Approve changes automatically (except trash)"));row.append(l)}
  const r=el("button","Revoke");r.onclick=()=>api("/admin/api/revoke",{id:c.id}).then(refresh);row.append(r);card.append(row);cl.append(card)}}
api("/admin/api/state").then(s=>{if(s.csrf)csrf=s.csrf;show(true);refresh()}).catch(()=>show(false));
setInterval(()=>{if(!$("app").hidden)refresh()},2000);
</script></body></html>"""
