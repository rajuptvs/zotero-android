"""OAuth for remote MCP clients (ai/spec/mcp.md, "OAuth (optional)"), on only with --oauth: protected
resource metadata (RFC 9728), authorization server metadata (RFC 8414), dynamic client registration
(RFC 7591), authorization code + PKCE S256 with resource indicators (RFC 8707), rotating refresh
tokens and revocation (RFC 7009). It never fetches anything from elsewhere.

The owner approves each connection on a consent page that needs the admin password. An approved
connection becomes a client in state.json like a bearer-token one (same scopes, approvals and
revocation); its tokens are stored hashed in that client, so revoking it ends them.
"""
import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
import urllib.parse

from .throttle import Throttle, client_address

ACCESS_SECONDS = 3600
REFRESH_SECONDS = 30 * 86400  # since the last refresh
CODE_SECONDS = 120
REQUEST_SECONDS = 600  # an open consent page
MAX_APPS = 100  # registered apps kept in state.json
UNUSED_APP_SECONDS = 86400  # a registration nobody approved is dropped after this
MAX_REGISTRATIONS_PER_HOUR = 30  # per address
MAX_REQUESTS = 500  # open consent pages
MAX_REQUESTS_PER_ADDRESS = 10
SPENT_REFRESH_TOKENS = 1000  # rotated-out refresh tokens remembered per connection, each until it would have expired;
# a connection that would need more is ended (one hourly refresh for 30 days is ~720)
REFRESH_RACE_SECONDS = 30  # the token just rotated out may come back this long without revoking (parallel refreshes)
VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}")
CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}")
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ‪-‮⁦-⁩]")
CORS = {"Access-Control-Allow-Origin": "*"}


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def s256(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def is_loopback(host):
    return host in ("127.0.0.1", "localhost", "::1")


def check_redirect_uri(uri):
    """https, or http on a loopback address; absolute, no fragment."""
    try:
        u = urllib.parse.urlsplit(uri)
        u.port  # raises on a bad port
    except ValueError:
        return False
    if u.fragment or not u.hostname or u.username or u.password:
        return False
    return u.scheme == "https" or (u.scheme == "http" and is_loopback(u.hostname))


def normalize_public_url(url):
    """The origin clients reach the server at, e.g. https://zotero.example.com (no path)."""
    try:
        u = urllib.parse.urlsplit((url or "").strip())
        u.port
    except ValueError:
        raise ValueError("--public-url isn't a valid URL") from None
    if u.scheme not in ("https", "http") or not u.hostname or u.path not in ("", "/") or u.query or u.fragment or u.username:
        raise ValueError("--public-url must be an origin like https://zotero.example.com, without a path")
    if u.scheme == "http" and not is_loopback(u.hostname):
        raise ValueError("--public-url must be https (plain http only on 127.0.0.1 or localhost, for testing)")
    return f"{u.scheme}://{u.netloc.lower()}"


def same_resource(value, resource):
    """RFC 8707 resource match: scheme and host case-insensitive, a trailing slash ignored."""
    try:
        a, b = urllib.parse.urlsplit(value), urllib.parse.urlsplit(resource)
    except ValueError:
        return False
    norm = lambda u: (u.scheme.lower(), u.netloc.lower(), u.path.rstrip("/"), u.query, u.fragment)
    return norm(a) == norm(b)


def add_query(uri, **params):
    u = urllib.parse.urlsplit(uri)
    extra = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    return urllib.parse.urlunsplit(u._replace(query=f"{u.query}&{extra}" if u.query else extra))


def parse_form(raw):
    """application/x-www-form-urlencoded → dict; None if malformed or a parameter repeats."""
    try:
        pairs = urllib.parse.parse_qsl(raw.decode("utf-8") if isinstance(raw, bytes) else raw, keep_blank_values=True, strict_parsing=bool(raw))
    except (ValueError, UnicodeDecodeError):
        return None
    out = {}
    for k, v in pairs:
        if k in out:
            return None
        out[k] = v
    return out


def clean_name(value, fallback):
    name = CONTROL.sub("", value if isinstance(value, str) else "").strip()[:60]
    return name or fallback


class OAuth:
    def __init__(self, server, public_url):
        self.server, self.state = server, server.state
        self.issuer = public_url
        self.resource = public_url + "/mcp"
        self.metadata_url = public_url + "/.well-known/oauth-protected-resource/mcp"
        self.lock = threading.Lock()
        self.requests = {}  # consent request id → {params, address, expires}
        self.codes = {}  # code hash → the grant it stands for
        self.registrations = Throttle(MAX_REGISTRATIONS_PER_HOUR, 3600)  # per address

    # region Resource server

    def authenticate(self, header):
        """The client an OAuth access token belongs to, if it's valid for this server."""
        m = re.match(r"(?i)bearer[ \t]+(zat_[A-Za-z0-9_-]+)[ \t]*$", header or "")
        if not m:
            return None
        self.state.refresh()
        digest, now, found = token_hash(m.group(1)), time.time(), None
        for c in self.state.data["clients"]:
            o = c.get("oauth")
            if o and o["resource"] == self.resource:
                for t in o["access"]:
                    if hmac.compare_digest(t["hash"], digest) and t["expires"] > now:
                        found = c
        return found

    def challenge(self, presented):
        value = f'Bearer resource_metadata="{self.metadata_url}", scope="read write"'
        return value + ', error="invalid_token"' if presented else value

    # endregion

    # region Endpoints

    def handle(self, h, method, path):
        send = self.server.send
        if method == "OPTIONS":
            return send(h, 204, headers={**CORS, "Access-Control-Allow-Methods": "GET, POST", "Access-Control-Max-Age": "600",
                                         "Access-Control-Allow-Headers": "Authorization, Content-Type, MCP-Protocol-Version"})
        routes = {"/.well-known/oauth-protected-resource": ("GET", self.resource_metadata),
                  "/.well-known/oauth-protected-resource/mcp": ("GET", self.resource_metadata),
                  "/.well-known/oauth-authorization-server": ("GET", self.server_metadata),
                  "/oauth/register": ("POST", self.register), "/oauth/authorize": ("GET", self.authorize),
                  "/oauth/token": ("POST", self.token), "/oauth/revoke": ("POST", self.revoke)}
        if path not in routes:
            return send(h, 404, {"error": "not found"})
        allowed, fn = routes[path]
        if method != allowed:
            return send(h, 405, headers={"Allow": allowed})
        return fn(h)

    def resource_metadata(self, h):
        return self.server.send(h, 200, {"resource": self.resource, "authorization_servers": [self.issuer],
                                         "scopes_supported": ["read", "write"], "bearer_methods_supported": ["header"],
                                         "resource_name": "Zotero AI"}, headers=CORS)

    def server_metadata(self, h):
        return self.server.send(h, 200, {
            "issuer": self.issuer, "authorization_endpoint": self.issuer + "/oauth/authorize",
            "token_endpoint": self.issuer + "/oauth/token", "registration_endpoint": self.issuer + "/oauth/register",
            "revocation_endpoint": self.issuer + "/oauth/revoke", "scopes_supported": ["read", "write"],
            "response_types_supported": ["code"], "response_modes_supported": ["query"],
            "grant_types_supported": ["authorization_code", "refresh_token"], "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
            "revocation_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
            "authorization_response_iss_parameter_supported": True}, headers=CORS)

    def register(self, h):
        """RFC 7591. Anyone can register (that's the point); nothing is granted until the owner approves."""
        fail = lambda error, desc, status=400: self.server.send(h, status, {"error": error, "error_description": desc}, headers=CORS)
        now = time.time()
        if not self.registrations.take(client_address(h)):
            return fail("invalid_client_metadata", "Too many registrations. Try again later.", 429)
        try:
            body = self.server.read_json(h)
        except (ValueError, UnicodeEncodeError):
            body = None
        if not isinstance(body, dict):
            return fail("invalid_client_metadata", "Send the client metadata as a JSON object.")
        uris = body.get("redirect_uris")
        if not isinstance(uris, list) or not 0 < len(uris) <= 10 or not all(isinstance(u, str) and len(u) <= 2000 and check_redirect_uri(u) for u in uris):
            return fail("invalid_redirect_uri", "Redirect URIs must be https, or http on 127.0.0.1, localhost or [::1], without a fragment.")
        grants = body.get("grant_types", ["authorization_code"])
        if not isinstance(grants, list) or "authorization_code" not in grants or not set(grants) <= {"authorization_code", "refresh_token"}:
            return fail("invalid_client_metadata", "Only the authorization_code and refresh_token grants are supported.")
        if body.get("response_types", ["code"]) != ["code"]:
            return fail("invalid_client_metadata", "Only response type code is supported.")
        method = body.get("token_endpoint_auth_method", "client_secret_basic")  # the RFC 7591 default
        if method not in ("none", "client_secret_post", "client_secret_basic"):
            return fail("invalid_client_metadata", "token_endpoint_auth_method must be none, client_secret_post or client_secret_basic.")
        app = {"client_id": "zoc_" + secrets.token_urlsafe(16), "client_name": clean_name(body.get("client_name"), urllib.parse.urlsplit(uris[0]).hostname),
               "redirect_uris": uris, "auth": method, "createdAt": int(now)}
        secret = None
        if method != "none":
            secret = "zos_" + secrets.token_urlsafe(32)
            app["secretHash"] = token_hash(secret)
        self.state.refresh()
        with self.state.lock:
            apps = self._prune_apps(now)
            if len(apps) >= MAX_APPS:
                return fail("invalid_client_metadata", "Too many registered apps. Revoke some on the admin page.", 503)
            apps.append(app)
            self.state.save()
        reply = {"client_id": app["client_id"], "client_id_issued_at": app["createdAt"], "client_name": app["client_name"],
                 "redirect_uris": uris, "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
                 "token_endpoint_auth_method": method}
        if secret:
            reply.update(client_secret=secret, client_secret_expires_at=0)
        return self.server.send(h, 201, reply, headers=CORS)

    def _prune_apps(self, now):
        """Drops registrations no client uses that are over a day old, then the oldest unused ones
        while over the limit. Call with the state lock held."""
        used = {c["oauth"]["clientId"] for c in self.state.data["clients"] if c.get("oauth")}
        apps = [a for a in self.state.data.setdefault("oauthApps", []) if a["client_id"] in used or now - a["createdAt"] < UNUSED_APP_SECONDS]
        while len(apps) >= MAX_APPS and any(a["client_id"] not in used for a in apps):
            apps.remove(next(a for a in apps if a["client_id"] not in used))
        self.state.data["oauthApps"] = apps
        return apps

    def authorize(self, h):
        """Shows the consent page. The request is checked once the owner has signed in. Each address
        keeps at most MAX_REQUESTS_PER_ADDRESS open (its oldest goes first), so one address can't push
        out the owner's."""
        params = parse_form(urllib.parse.urlsplit(h.path).query)
        if not params or not params.get("client_id") or not params.get("redirect_uri"):
            return self.server.send(h, 400, ERROR_PAGE, "text/html; charset=utf-8", PAGE_HEADERS)
        rid, now, address = secrets.token_urlsafe(24), time.time(), client_address(h)
        with self.lock:
            self.requests = {k: r for k, r in self.requests.items() if r["expires"] > now}
            mine = [k for k, r in self.requests.items() if r["address"] == address]
            if len(mine) >= MAX_REQUESTS_PER_ADDRESS:
                del self.requests[mine[0]]
            while len(self.requests) >= MAX_REQUESTS:
                self.requests.pop(next(iter(self.requests)))
            self.requests[rid] = {"params": params, "address": address, "expires": now + REQUEST_SECONDS}
        return self.server.send(h, 200, CONSENT_PAGE.replace("{{REQUEST}}", rid), "text/html; charset=utf-8", PAGE_HEADERS)

    def token(self, h):
        fail = lambda error, desc, status=400: self.server.send(h, status, {"error": error, "error_description": desc}, headers=CORS)
        if (h.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/x-www-form-urlencoded":
            return fail("invalid_request", "Send application/x-www-form-urlencoded.")
        form = parse_form(h.body)
        if form is None:
            return fail("invalid_request", "Malformed or repeated parameters.")
        app = self.client_auth(h, form)
        if not app:
            return fail("invalid_client", "Unknown client or wrong client credentials.", 401)
        if "resource" in form and not same_resource(form["resource"], self.resource):
            return fail("invalid_target", f"This server only issues tokens for {self.resource}.")
        grant = form.get("grant_type")
        if grant == "authorization_code":
            return self.exchange_code(h, app, form, fail)
        if grant == "refresh_token":
            return self.refresh(h, app, form, fail)
        return fail("unsupported_grant_type", "Use authorization_code or refresh_token.")

    def exchange_code(self, h, app, form, fail):
        now = time.time()
        with self.lock:
            self.codes = {k: c for k, c in self.codes.items() if c["expires"] + 600 > now}
            grant = self.codes.get(token_hash(form.get("code") or ""))
            reused = grant and grant["used"] and grant["issued"]
            if not grant or grant["used"] or grant["expires"] < now:
                grant = None
            else:
                grant["used"] = True  # one attempt, right or wrong
        if reused:
            self.server.revoke_client(reused)  # RFC 6749 §4.1.2: a replayed code revokes what it issued
        if not grant:
            return fail("invalid_grant", "The authorization code is unknown, expired or already used.")
        if grant["clientId"] != app["client_id"]:
            return fail("invalid_grant", "The code was issued to another client.")
        if form.get("redirect_uri") != grant["redirectUri"]:  # required: one was sent at /authorize
            return fail("invalid_grant", "redirect_uri is missing or doesn't match the authorization request.")
        verifier = form.get("code_verifier") or ""
        if not VERIFIER.fullmatch(verifier) or not hmac.compare_digest(s256(verifier), grant["challenge"]):
            return fail("invalid_grant", "PKCE verification failed.")
        access, refresh = "zat_" + secrets.token_urlsafe(32), "zrt_" + secrets.token_urlsafe(32)
        client = {"id": "c_" + secrets.token_urlsafe(6), "name": grant["clientName"], "scope": grant["scope"], "autoApprove": False,
                  "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "oauth": {"clientId": app["client_id"], "redirectUri": grant["redirectUri"], "resource": self.resource,
                            "access": [{"hash": token_hash(access), "expires": now + ACCESS_SECONDS}],
                            "refresh": {"hash": token_hash(refresh), "expires": now + REFRESH_SECONDS}, "spent": []}}
        self.state.refresh()
        with self.state.lock:
            self.state.data["clients"].append(client)
            self.state.save()
        grant["issued"] = client["id"]
        return self.send_tokens(h, client, access, refresh)

    def refresh(self, h, app, form, fail):
        digest, now = token_hash(form.get("refresh_token") or ""), time.time()
        self.state.refresh()
        with self.state.lock:
            found = reused = racing = None
            for c in self.state.data["clients"]:
                o = c.get("oauth")
                if not o:
                    continue
                if hmac.compare_digest(o["refresh"]["hash"], digest):
                    found = c
                spent = o.get("spent", [])
                for i, t in enumerate(spent):
                    if hmac.compare_digest(t["hash"], digest) and t["expires"] > now:
                        reused = c
                        # The token rotated out just now: most likely a parallel refresh, not a stolen copy.
                        racing = i == len(spent) - 1 and now - t["rotatedAt"] <= REFRESH_RACE_SECONDS
            if found and (found["oauth"]["clientId"] != app["client_id"] or found["oauth"]["refresh"]["expires"] < now
                          or found["oauth"]["resource"] != self.resource):
                found = None
            if found:
                if "write" in (form.get("scope") or "").split() and found["scope"] != "write":
                    return fail("invalid_scope", "This connection was approved for reading only.")
                o = found["oauth"]
                spent = [t for t in o.get("spent", []) if t["expires"] > now]
                if len(spent) >= SPENT_REFRESH_TOKENS:
                    # Forgetting a live spent token would hide a later replay of it; end the connection instead.
                    self.server.revoke_client(found["id"])
                    return fail("invalid_grant", "This connection refreshed too many times, so it was ended. Connect again.")
                access, refresh = "zat_" + secrets.token_urlsafe(32), "zrt_" + secrets.token_urlsafe(32)
                o["access"] = [t for t in o["access"] if t["expires"] > now][-4:] + [{"hash": token_hash(access), "expires": now + ACCESS_SECONDS}]
                o["spent"] = spent + [{"hash": o["refresh"]["hash"], "expires": o["refresh"]["expires"], "rotatedAt": now}]
                o["refresh"] = {"hash": token_hash(refresh), "expires": now + REFRESH_SECONDS}
                self.state.save()
                return self.send_tokens(h, found, access, refresh)
        if reused and reused["oauth"]["clientId"] == app["client_id"]:
            if racing:
                return fail("invalid_grant", "This refresh token was just replaced by another request. Use the new one.")
            # A rotated-out refresh token came back: someone kept a copy. End the connection.
            self.server.revoke_client(reused["id"])
            return fail("invalid_grant", "This refresh token was already used, so the connection was revoked. Connect again.")
        return fail("invalid_grant", "The refresh token is unknown, expired or revoked.")

    def send_tokens(self, h, client, access, refresh):
        return self.server.send(h, 200, {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_SECONDS, "refresh_token": refresh,
                                         "scope": "read write" if client["scope"] == "write" else "read"}, headers={**CORS, "Pragma": "no-cache"})

    def revoke(self, h):
        """RFC 7009: a refresh token ends the connection; an access token ends just itself."""
        form = parse_form(h.body)
        app = self.client_auth(h, form) if form is not None else None
        if not app:
            return self.server.send(h, 401, {"error": "invalid_client"}, headers=CORS)
        digest = token_hash(form.get("token") or "")
        self.state.refresh()
        gone = None
        with self.state.lock:
            for c in self.state.data["clients"]:
                o = c.get("oauth")
                if not o or o["clientId"] != app["client_id"]:
                    continue
                if hmac.compare_digest(o["refresh"]["hash"], digest):
                    gone = c["id"]
                elif any(hmac.compare_digest(t["hash"], digest) for t in o["access"]):
                    o["access"] = [t for t in o["access"] if not hmac.compare_digest(t["hash"], digest)]
                    self.state.save()
        if gone:
            self.server.revoke_client(gone)
        return self.server.send(h, 200, headers=CORS)

    def client_auth(self, h, form):
        """The app making a token or revocation request, or None (unknown, or a wrong secret)."""
        client_id, secret = form.get("client_id"), form.get("client_secret")
        header = h.headers.get("Authorization") or ""
        if header[:6].lower() == "basic ":
            try:
                cid, _, sec = base64.b64decode(header[6:].strip(), validate=True).decode().partition(":")
            except (ValueError, UnicodeDecodeError):
                return None
            cid, sec = urllib.parse.unquote_plus(cid), urllib.parse.unquote_plus(sec)
            if client_id is not None and client_id != cid:
                return None
            client_id, secret = cid, sec
        if not client_id:
            return None
        app = self.registered(client_id)
        if app and app.get("secretHash") and not (secret and hmac.compare_digest(token_hash(secret), app["secretHash"])):
            return None
        return app

    # endregion

    # region Consent (the admin page's API; the owner is signed in)

    def registered(self, client_id):
        self.state.refresh()
        return next((a for a in self.state.data.get("oauthApps", []) if a["client_id"] == client_id), None)

    def check(self, params):
        """Validates an authorization request: {"app", …}, {"error"} (shown to the owner, not sent
        anywhere), or {"redirect"} (an error for the app, at its registered address)."""
        app = self.registered(params["client_id"])
        if not app:
            return {"error": "This app isn't registered here (or its registration expired). Connect again from the app."}
        redirect_uri = params["redirect_uri"]
        if redirect_uri not in app["redirect_uris"] or not check_redirect_uri(redirect_uri):
            return {"error": f"The app asked to send you to an address it didn't register: {redirect_uri}"}
        back = lambda error, desc: {"redirect": add_query(redirect_uri, error=error, error_description=desc, state=params.get("state"), iss=self.issuer)}
        if params.get("response_type") != "code":
            return back("unsupported_response_type", "Only response_type=code is supported.")
        if params.get("code_challenge_method") != "S256" or not CHALLENGE.fullmatch(params.get("code_challenge") or ""):
            return back("invalid_request", "PKCE with code_challenge_method=S256 is required.")
        if not same_resource(params.get("resource", self.resource), self.resource):
            return back("invalid_target", f"This server only issues tokens for {self.resource}.")
        return {"app": app, "requested": "write" if "write" in (params.get("scope") or "").split() else "read"}

    def admin_api(self, path, body):
        """(status, reply) for /admin/api/oauth/request and /admin/api/oauth/decide."""
        rid = str(body.get("id"))
        with self.lock:
            req = self.requests.get(rid)
        if not req or req["expires"] < time.time():
            return 200, {"error": "This request has expired or was already answered. Start connecting again from the app."}
        if path == "/admin/api/oauth/request":
            checked = self.check(req["params"])
            if "app" not in checked:
                return 200, checked
            app, redirect = checked["app"], urllib.parse.urlsplit(req["params"]["redirect_uri"])
            return 200, {"clientName": app["client_name"], "redirectUri": req["params"]["redirect_uri"], "redirectHost": redirect.hostname,
                         "loopback": is_loopback(redirect.hostname), "requested": checked["requested"]}
        if path == "/admin/api/oauth/decide":
            approve = body.get("approve") is True
            if approve and body.get("scope") not in ("read", "write"):
                return 400, {"error": "Choose read or write."}
            with self.lock:
                if self.requests.pop(rid, None) is None:  # one decision per request
                    return 200, {"error": "This request was already answered."}
            checked = self.check(req["params"])
            if "app" not in checked:
                return 200, checked
            params, app = req["params"], checked["app"]
            if not approve:
                return 200, {"redirect": add_query(params["redirect_uri"], error="access_denied", error_description="The owner declined.",
                                                   state=params.get("state"), iss=self.issuer)}
            code = secrets.token_urlsafe(32)
            with self.lock:
                self.codes[token_hash(code)] = {"clientId": app["client_id"], "clientName": app["client_name"], "redirectUri": params["redirect_uri"],
                                                "challenge": params["code_challenge"], "scope": body["scope"],
                                                "expires": time.time() + CODE_SECONDS, "used": False, "issued": None}
            return 200, {"redirect": add_query(params["redirect_uri"], code=code, state=params.get("state"), iss=self.issuer)}
        return 404, {"error": "not found"}

    # endregion


PAGE_HEADERS = {"Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
                "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"}

STYLE = """<style>
:root{color-scheme:light dark;--fg:#1d1d1f;--bg:#f6f6f8;--card:#fff;--line:#d9d9de;--accent:#2f6fdf;--warn:#e08a00}
@media (prefers-color-scheme:dark){:root{--fg:#ececf1;--bg:#151517;--card:#202024;--line:#3a3a40}}
body{margin:0;font:15px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--fg)}
main{max-width:560px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin:12px 0}
.warn{border:2px solid var(--warn)}
button{font:inherit;padding:6px 14px;border-radius:8px;border:1px solid var(--line);background:transparent;color:inherit}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.muted{opacity:.7;font-size:13px}
input{font:inherit;padding:6px;border-radius:6px;border:1px solid var(--line);background:transparent;color:inherit}
label{display:block;margin:6px 0}code{word-break:break-all}
</style>"""

ERROR_PAGE = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Zotero AI</title>{STYLE}</head><body><main><h1>Zotero AI</h1><div class="card"><p>This connection link is incomplete.
Start connecting again from the app.</p></div></main></body></html>"""

CONSENT_PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect to Zotero AI</title>""" + STYLE + r"""</head><body><main>
<h1>Zotero AI</h1>
<div id="login" class="card" hidden><p>An app wants to connect to your Zotero library. Sign in with the admin password to see which one.</p>
<div class="row"><input id="pw" type="password" autocomplete="current-password"><button class="primary" id="go">Sign in</button></div><p id="lerr" class="muted"></p></div>
<div id="problem" class="card" hidden></div>
<div id="consent" hidden>
<div class="card"><p><strong id="name"></strong> wants to use your Zotero library.</p>
<p>Access will be sent to <strong id="host"></strong><br><code id="uri" class="muted"></code></p>
<p class="muted">The app chose this name itself; check the address above.</p></div>
<div id="local" class="card warn" hidden>This sends access to a program on the computer where the connection was started (localhost).
Allow it only if you started connecting yourself just now.</div>
<div class="card"><label><input type="radio" name="scope" value="read" checked> Read only</label>
<label><input type="radio" name="scope" value="write"> Read and request changes <span class="muted">(changes still wait for your approval on the admin page)</span></label>
<p id="asked" class="muted"></p></div>
<div class="row"><button id="deny">Deny</button><button class="primary" id="allow">Allow</button></div>
<p class="muted">The connection appears under Agents on the admin page, where you can revoke it.</p>
</div></main><script>
const RID="{{REQUEST}}";let csrf="";
const $=id=>document.getElementById(id);
async function call(path,body){const r=await fetch(path,{method:body===undefined?"GET":"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":csrf},body:body===undefined?undefined:JSON.stringify(body),credentials:"same-origin"});if(r.status==401)throw new Error("signin");const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.error||("HTTP "+r.status));return j}
function show(id){for(const x of ["login","problem","consent"])$(x).hidden=x!=id}
function problem(text){$("problem").textContent=text;show("problem")}
// Allow works only after the page has been visible and focused for a second.
let armedAt=0;const rearm=()=>{armedAt=document.visibilityState=="visible"&&document.hasFocus()?Date.now():Infinity};
document.addEventListener("visibilitychange",rearm);window.addEventListener("focus",rearm);window.addEventListener("blur",()=>{armedAt=Infinity});
async function load(){let d;try{d=await call("/admin/api/oauth/request",{id:RID})}catch(e){return e.message=="signin"?show("login"):problem(e.message)}
 if(d.redirect)return location.replace(d.redirect);if(d.error)return problem(d.error);
 $("name").textContent="“"+d.clientName+"”";$("host").textContent=d.redirectHost;$("uri").textContent=d.redirectUri;
 $("local").hidden=!d.loopback;$("asked").textContent=d.requested=="write"?"The app asked to request changes.":"The app asked to read.";
 show("consent");rearm()}
async function decide(approve){const scope=document.querySelector("input[name=scope]:checked").value;let d;
 try{d=await call("/admin/api/oauth/decide",{id:RID,approve,scope})}catch(e){return problem(e.message)}
 if(d.redirect){problem(approve?"Allowed. Returning to the app…":"Denied. Returning to the app…");return location.replace(d.redirect)}problem(d.error||"Something went wrong.")}
$("allow").onclick=()=>{if(Date.now()-armedAt<1000||!document.hasFocus()){$("allow").textContent="Take a second to read it";return}decide(true)};
$("deny").onclick=()=>decide(false);
$("go").onclick=async()=>{const r=await fetch("/admin/api/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({password:$("pw").value}),credentials:"same-origin"});const j=await r.json().catch(()=>({}));if(r.ok){csrf=j.csrf;load()}else $("lerr").textContent=j.error||"Couldn't sign in"};
call("/admin/api/state").then(s=>{csrf=s.csrf;load()}).catch(e=>e.message=="signin"?show("login"):problem(e.message));
</script></body></html>"""
