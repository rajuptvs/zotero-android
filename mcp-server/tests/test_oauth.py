"""OAuth for remote clients (oauth.py), end to end against a server on a free port with a fake library.

    cd mcp-server && python3 -m unittest discover -s tests -v
"""
import base64
import hashlib
import re
import secrets
import threading
import time
import unittest
import urllib.parse

from support import PASSWORD, Running

from zotero_ai_mcp import oauth, throttle

REDIRECT = "http://127.0.0.1:9999/callback"
WRITE_CALL = {"name": "zotero_create_collection", "arguments": {"name": "From OAuth", "parent_collection_key": None}}


def pkce():
    verifier = secrets.token_urlsafe(48)
    return verifier, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def query(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


class OAuthFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = Running(oauth=True)
        cls.srv.login()
        cls.resource = cls.srv.base + "/mcp"

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    # region Steps

    def register(self, redirect=REDIRECT, **extra):
        status, _, body = self.srv.request("POST", "/oauth/register", {"redirect_uris": [redirect], "client_name": "Test agent",
                                                                       "token_endpoint_auth_method": "none", **extra})
        self.assertEqual(status, 201, body)
        return body

    def start(self, client_id, challenge, redirect=REDIRECT, **over):
        params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect, "code_challenge": challenge,
                  "code_challenge_method": "S256", "state": "st4te", "scope": "read write", "resource": self.resource, **over}
        status, headers, page = self.srv.request("GET", "/oauth/authorize?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}))
        self.assertEqual(status, 200)
        self.assertEqual(headers["x-frame-options"], "DENY")
        return re.search(r'const RID="([^"]+)"', page).group(1)

    def decide(self, rid, approve=True, scope="read"):
        status, _, body = self.srv.admin("/admin/api/oauth/decide", {"id": rid, "approve": approve, "scope": scope})
        self.assertEqual(status, 200, body)
        return body

    def code(self, scope="read", client=None):
        """Registers (unless given a client), consents and returns (client_id, verifier, code)."""
        client = client or self.register()
        verifier, challenge = pkce()
        rid = self.start(client["client_id"], challenge)
        status, _, details = self.srv.admin("/admin/api/oauth/request", {"id": rid})
        self.assertEqual(details["clientName"], client["client_name"])
        self.assertEqual(details["redirectHost"], "127.0.0.1")
        self.assertTrue(details["loopback"])
        q = query(self.decide(rid, scope=scope)["redirect"])
        self.assertEqual(q["state"], "st4te")
        self.assertEqual(q["iss"], self.srv.base)
        return client["client_id"], verifier, q["code"]

    def exchange(self, client_id, verifier, code, **over):
        fields = {"grant_type": "authorization_code", "client_id": client_id, "code": code, "code_verifier": verifier,
                  "redirect_uri": REDIRECT, "resource": self.resource, **over}
        status, headers, body = self.srv.form("/oauth/token", {k: v for k, v in fields.items() if v is not None})
        self.assertEqual(headers.get("cache-control"), "no-store")
        return status, body

    def connect(self, scope="read"):
        client_id, verifier, code = self.code(scope)
        status, tokens = self.exchange(client_id, verifier, code)
        self.assertEqual(status, 200, tokens)
        self.assertEqual(tokens["token_type"], "Bearer")
        self.assertEqual(tokens["scope"], "read write" if scope == "write" else "read")
        tokens["client_id"] = client_id
        return tokens

    def refresh(self, tokens, **over):
        return self.srv.form("/oauth/token", {"grant_type": "refresh_token", "client_id": tokens["client_id"],
                                              "refresh_token": tokens["refresh_token"], "resource": self.resource, **over})

    # endregion

    def test_discovery(self):
        status, headers, _ = self.srv.mcp(None, "ping")
        self.assertEqual(status, 401)
        self.assertEqual(headers["www-authenticate"],
                         f'Bearer resource_metadata="{self.srv.base}/.well-known/oauth-protected-resource/mcp", scope="read write"')
        for path in ("/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-protected-resource"):
            status, _, meta = self.srv.request("GET", path)
            self.assertEqual(status, 200)
            self.assertEqual(meta["resource"], self.resource)
            self.assertEqual(meta["authorization_servers"], [self.srv.base])
        status, _, meta = self.srv.request("GET", "/.well-known/oauth-authorization-server")
        self.assertEqual(meta["issuer"], self.srv.base)
        self.assertEqual(meta["code_challenge_methods_supported"], ["S256"])
        self.assertNotIn("client_id_metadata_document_supported", meta)  # dynamic registration only
        self.assertEqual(meta["registration_endpoint"], self.srv.base + "/oauth/register")

    def test_read_connection(self):
        tokens = self.connect("read")
        status, _, reply = self.srv.mcp(tokens["access_token"], "initialize", {"protocolVersion": "2025-11-25"})
        self.assertEqual(status, 200)
        self.assertEqual(reply["result"]["protocolVersion"], "2025-11-25")
        names = {t["name"] for t in self.srv.mcp(tokens["access_token"], "tools/list")[2]["result"]["tools"]}
        self.assertIn("zotero_search_items", names)
        self.assertNotIn("zotero_create_collection", names)
        reply = self.srv.mcp(tokens["access_token"], "tools/call", WRITE_CALL)[2]["result"]
        self.assertIn("This connection is read-only.", reply["content"][0]["text"])
        self.assertEqual(self.srv.server.approvals.pending(), [])
        # Listed like any agent, without hashes.
        listed = [c for c in self.srv.admin("/admin/api/state")[2]["clients"] if c.get("oauth")]
        self.assertTrue(listed)
        self.assertEqual(listed[-1]["oauth"], {"redirectHost": "127.0.0.1"})
        self.assertNotIn("tokenHash", str(listed))

    def test_bearer_tokens_still_work(self):
        made = self.srv.admin("/admin/api/clients", {"name": "Pasted", "scope": "read"})[2]
        status, _, reply = self.srv.mcp(made["token"], "ping")
        self.assertEqual((status, reply["result"]), (200, {}))

    def test_write_waits_for_the_owner(self):
        tokens = self.connect("write")
        out = {}
        t = threading.Thread(target=lambda: out.update(reply=self.srv.mcp(tokens["access_token"], "tools/call", WRITE_CALL)[2]))
        t.start()
        for _ in range(100):
            pending = self.srv.admin("/admin/api/state")[2]["pending"]
            if pending:
                break
            time.sleep(0.05)
        self.assertEqual(pending[0]["clientName"], "Test agent")
        self.assertEqual(pending[0]["tool"], "zotero_create_collection")
        self.assertTrue(self.srv.admin("/admin/api/decide", {"id": pending[0]["id"], "approved": True})[2]["decided"])
        t.join(10)
        self.assertIn('"ok":true', out["reply"]["result"]["content"][0]["text"])
        self.assertIn(("zotero_create_collection", {"name": "From OAuth", "parent_collection_key": None}), self.srv.library.calls)

    def test_bad_pkce_burns_the_code(self):
        client_id, verifier, code = self.code()
        status, body = self.exchange(client_id, pkce()[0], code)
        self.assertEqual((status, body["error"]), (400, "invalid_grant"))
        status, body = self.exchange(client_id, verifier, code)
        self.assertEqual((status, body["error"]), (400, "invalid_grant"))

    def test_missing_or_plain_pkce(self):
        client = self.register()
        for over in ({"code_challenge": None, "code_challenge_method": None}, {"code_challenge_method": "plain"}):
            rid = self.start(client["client_id"], pkce()[1], **over)
            q = query(self.srv.admin("/admin/api/oauth/request", {"id": rid})[2]["redirect"])
            self.assertEqual((q["error"], q["state"]), ("invalid_request", "st4te"))

    def test_wrong_redirect(self):
        client = self.register()
        rid = self.start(client["client_id"], pkce()[1], redirect="https://evil.example/cb")
        body = self.srv.admin("/admin/api/oauth/request", {"id": rid})[2]
        self.assertNotIn("redirect", body)  # never sent to an unregistered address
        self.assertIn("didn't register", body["error"])
        self.assertNotIn("redirect", self.decide(rid))
        client_id, verifier, code = self.code(client=client)
        status, body = self.exchange(client_id, verifier, code, redirect_uri="http://127.0.0.1:9999/other")
        self.assertEqual((status, body["error"]), (400, "invalid_grant"))

    def test_token_request_needs_redirect_uri(self):
        client_id, verifier, code = self.code()
        status, body = self.exchange(client_id, verifier, code, redirect_uri=None)
        self.assertEqual((status, body["error"]), (400, "invalid_grant"))
        self.assertIn("redirect_uri", body["error_description"])

    def test_wrong_resource(self):
        client = self.register()
        rid = self.start(client["client_id"], pkce()[1], resource="https://other.example/mcp")
        q = query(self.srv.admin("/admin/api/oauth/request", {"id": rid})[2]["redirect"])
        self.assertEqual(q["error"], "invalid_target")
        client_id, verifier, code = self.code(client=client)
        status, body = self.exchange(client_id, verifier, code, resource="https://other.example/mcp")
        self.assertEqual((status, body["error"]), (400, "invalid_target"))
        # The canonical form may differ in case and a trailing slash.
        status, body = self.exchange(client_id, verifier, code, resource=self.resource.upper().replace("/MCP", "/mcp/"))
        self.assertEqual(status, 200, body)

    def test_code_from_another_client(self):
        _, verifier, code = self.code()
        other = self.register()
        status, body = self.exchange(other["client_id"], verifier, code)
        self.assertEqual((status, body["error"]), (400, "invalid_grant"))

    def test_reused_code_revokes(self):
        client_id, verifier, code = self.code()
        status, tokens = self.exchange(client_id, verifier, code)
        self.assertEqual(status, 200)
        status, body = self.exchange(client_id, verifier, code)
        self.assertEqual((status, body["error"]), (400, "invalid_grant"))
        self.assertEqual(self.srv.mcp(tokens["access_token"], "ping")[0], 401)

    def test_csrf_and_sign_in(self):
        rid = self.start(self.register()["client_id"], pkce()[1])
        self.assertEqual(self.srv.admin("/admin/api/oauth/decide", {"id": rid, "approve": True, "scope": "write"}, csrf=False)[0], 403)
        self.assertEqual(self.srv.admin("/admin/api/oauth/request", {"id": rid}, csrf=False)[0], 403)
        self.assertEqual(self.srv.admin("/admin/api/oauth/decide", {"id": rid, "approve": True, "scope": "write"},
                                        headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.srv.admin("/admin/api/oauth/decide", {"id": rid, "approve": True, "scope": "write"}, cookie=False)[0], 401)
        self.assertIn("redirect", self.decide(rid))  # still answerable by the owner
        self.assertIn("already answered", self.decide(rid)["error"])  # once only

    def test_owner_must_choose_scope(self):
        rid = self.start(self.register()["client_id"], pkce()[1])
        status, _, body = self.srv.admin("/admin/api/oauth/decide", {"id": rid, "approve": True})
        self.assertEqual(status, 400)

    def test_deny(self):
        rid = self.start(self.register()["client_id"], pkce()[1])
        q = query(self.decide(rid, approve=False)["redirect"])
        self.assertEqual((q["error"], q["state"], q["iss"]), ("access_denied", "st4te", self.srv.base))

    def test_revoked_client(self):
        tokens = self.connect("write")
        mine = self.srv.server.oauth.authenticate("Bearer " + tokens["access_token"])["id"]
        self.srv.admin("/admin/api/revoke", {"id": mine})
        self.assertEqual(self.srv.mcp(tokens["access_token"], "ping")[0], 401)
        status, _, body = self.refresh(tokens)
        self.assertEqual((status, body["error"]), (400, "invalid_grant"))

    def backdate_rotation(self, tokens, seconds=oauth.REFRESH_RACE_SECONDS + 1):
        """Makes the connection's refresh tokens look rotated out `seconds` earlier."""
        client = self.srv.server.oauth.authenticate("Bearer " + tokens["access_token"])
        with self.srv.state.lock:
            for t in client["oauth"]["spent"]:
                t["rotatedAt"] -= seconds
            self.srv.state.save()

    def test_refresh_rotates(self):
        tokens = self.connect("read")
        status, _, fresh = self.refresh(tokens)
        self.assertEqual(status, 200, fresh)
        self.assertNotEqual(fresh["refresh_token"], tokens["refresh_token"])
        self.assertEqual(self.srv.mcp(fresh["access_token"], "ping")[0], 200)
        # The rotated-out refresh token coming back ends the connection.
        self.backdate_rotation(fresh)
        status, _, body = self.refresh(tokens)
        self.assertEqual((status, body["error"]), (400, "invalid_grant"))
        self.assertIn("revoked", body["error_description"])
        self.assertEqual(self.srv.mcp(fresh["access_token"], "ping")[0], 401)

    def test_parallel_refresh_doesnt_revoke(self):
        tokens = self.connect("read")
        status, _, fresh = self.refresh(tokens)
        self.assertEqual(status, 200, fresh)
        # Right after its rotation, the previous token (a second request racing the first) is refused…
        status, _, body = self.refresh(tokens)
        self.assertEqual((status, body["error"]), (400, "invalid_grant"))
        self.assertNotIn("revoked", body["error_description"])
        # …but the connection lives on.
        self.assertEqual(self.srv.mcp(fresh["access_token"], "ping")[0], 200)
        status, _, newer = self.refresh({**fresh, "client_id": tokens["client_id"]})
        self.assertEqual(status, 200, newer)
        # Only the latest rotated-out token gets that grace: the one before it revokes even now.
        status, _, body = self.refresh(tokens)
        self.assertIn("revoked", body["error_description"])
        self.assertEqual(self.srv.mcp(newer["access_token"], "ping")[0], 401)

    def test_replay_of_any_spent_generation_revokes(self):
        for reload in (False, True):
            with self.subTest(reload=reload):
                r0 = self.connect("read")
                r1 = {**self.refresh(r0)[2], "client_id": r0["client_id"]}
                r2 = {**self.refresh(r1)[2], "client_id": r0["client_id"]}
                self.assertEqual(self.srv.mcp(r2["access_token"], "ping")[0], 200)
                self.backdate_rotation(r2)
                if reload:  # remembered in state.json, not just in memory
                    self.srv.state.mtime = None
                    self.srv.state.refresh()
                status, _, body = self.refresh(r0)
                self.assertEqual((status, body["error"]), (400, "invalid_grant"))
                self.assertIn("revoked", body["error_description"])
                self.assertEqual(self.srv.mcp(r2["access_token"], "ping")[0], 401)
                status, _, body = self.refresh(r2)
                self.assertEqual((status, body["error"]), (400, "invalid_grant"))

    def test_full_spent_history_ends_the_connection(self):
        limit = oauth.SPENT_REFRESH_TOKENS
        oauth.SPENT_REFRESH_TOKENS = 3
        try:
            r0 = self.connect("read")
            tokens = r0
            for _ in range(3):
                tokens = {**self.refresh(tokens)[2], "client_id": r0["client_id"]}
                self.backdate_rotation(tokens)
            self.srv.state.mtime = None  # the history is read back from state.json
            self.srv.state.refresh()
            # A fourth rotation would have to forget r0, which is still live: the connection ends.
            status, _, body = self.refresh(tokens)
            self.assertEqual((status, body["error"]), (400, "invalid_grant"))
            self.assertEqual(self.srv.mcp(tokens["access_token"], "ping")[0], 401)
            self.assertEqual(self.refresh(r0)[0], 400)
        finally:
            oauth.SPENT_REFRESH_TOKENS = limit

    def test_read_scope_cant_grow_on_refresh(self):
        tokens = self.connect("read")
        status, _, body = self.refresh(tokens, scope="read write")
        self.assertEqual((status, body["error"]), (400, "invalid_scope"))

    def test_expired_access_token(self):
        tokens = self.connect("read")
        client = self.srv.server.oauth.authenticate("Bearer " + tokens["access_token"])
        with self.srv.state.lock:
            client["oauth"]["access"][0]["expires"] = time.time() - 1
            self.srv.state.save()
        status, headers, _ = self.srv.mcp(tokens["access_token"], "ping")
        self.assertEqual(status, 401)
        self.assertIn('error="invalid_token"', headers["www-authenticate"])

    def test_token_revocation_endpoint(self):
        tokens = self.connect("read")
        status, _, _ = self.srv.form("/oauth/revoke", {"token": tokens["refresh_token"], "client_id": tokens["client_id"]})
        self.assertEqual(status, 200)
        self.assertEqual(self.srv.mcp(tokens["access_token"], "ping")[0], 401)

    def test_registration_rules(self):
        for uri in ("http://example.com/cb", "myapp://cb", "https://example.com/cb#frag", "not a url"):
            status, _, body = self.srv.request("POST", "/oauth/register", {"redirect_uris": [uri]})
            self.assertEqual((status, body["error"]), (400, "invalid_redirect_uri"), uri)
        status, _, body = self.srv.request("POST", "/oauth/register", {"redirect_uris": [REDIRECT], "grant_types": ["client_credentials"]})
        self.assertEqual(status, 400)
        self.register("http://[::1]:8000/cb")
        self.register("https://claude.ai/api/mcp/auth_callback")

    def test_confidential_client(self):
        client = self.register(token_endpoint_auth_method="client_secret_basic")
        self.assertTrue(client["client_secret"].startswith("zos_"))
        _, verifier, code = self.code(client=client)
        status, body = self.exchange(client["client_id"], verifier, code, client_secret="wrong")
        self.assertEqual((status, body["error"]), (401, "invalid_client"))
        _, verifier, code = self.code(client=client)
        basic = base64.b64encode(f"{client['client_id']}:{client['client_secret']}".encode()).decode()
        status, _, body = self.srv.form("/oauth/token", {"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
                                                        "redirect_uri": REDIRECT}, {"Authorization": "Basic " + basic})
        self.assertEqual(status, 200, body)


class Throttling(unittest.TestCase):
    """Sign-in, registration and consent limits count per address (support's server sees every request
    from 127.0.0.1, so X-Forwarded-For stands in for a proxy's clients)."""

    def setUp(self):
        self.srv = Running(oauth=True, allowed_hosts=("zotero.example.com",))
        self.srv.server.login_delay = 0

    def tearDown(self):
        self.srv.stop()

    def login(self, password, host=None, address=None):
        headers = {"Host": host or f"127.0.0.1:{self.srv.port}"}
        if address:
            headers["X-Forwarded-For"] = address
        return self.srv.request("POST", "/admin/api/login", {"password": password}, headers)[0]

    def test_sign_in_per_address(self):
        public = "zotero.example.com"
        for _ in range(5):
            self.assertEqual(self.login("guess", public, "203.0.113.5"), 403)
        self.assertEqual(self.login(PASSWORD, public, "203.0.113.5"), 429)
        self.assertEqual(self.login(PASSWORD, public, "198.51.100.7, 203.0.113.5"), 429)  # only the proxy's entry counts
        self.assertEqual(self.login(PASSWORD, public, "203.0.113.5, 198.51.100.7"), 200)
        # A proxy that doesn't say who the client is: all its requests share one address…
        for _ in range(5):
            self.login("guess", public)
        self.assertEqual(self.login(PASSWORD, public), 429)
        # …but the owner signing in on the loopback address isn't held up.
        self.assertEqual(self.login(PASSWORD), 200)

    def test_parallel_guesses_count_before_the_password_check(self):
        public = "zotero.example.com"
        checked = []
        check = self.srv.server.state.check_admin_password
        self.srv.server.state.check_admin_password = lambda pw: checked.append(pw) or (time.sleep(0.2), check(pw))[1]
        statuses = []
        threads = [threading.Thread(target=lambda: statuses.append(self.login("guess", public, "203.0.113.9"))) for _ in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(checked), 5)
        self.assertEqual(sorted(statuses), [403] * 5 + [429] * 35)
        # A right password doesn't use up the allowance.
        self.srv.server.state.check_admin_password = check
        for _ in range(8):
            self.assertEqual(self.login(PASSWORD, public, "198.51.100.9"), 200)

    def test_registration_per_address(self):
        self.srv.server.oauth.registrations.limit = 2
        register = lambda address: self.srv.request("POST", "/oauth/register", {"redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none"},
                                                    {"X-Forwarded-For": address})[0]
        self.assertEqual([register("203.0.113.5") for _ in range(3)], [201, 201, 429])
        self.assertEqual(register("2001:db8:1:2::5"), 201)
        self.assertEqual(register("2001:db8:1:2::6"), 201)
        self.assertEqual(register("2001:db8:1:2:ffff::1"), 429)  # the same /64
        self.assertEqual(register("198.51.100.7"), 201)

    def test_open_consent_requests_per_address(self):
        start = lambda address: re.search(r'const RID="([^"]+)"', self.srv.request(
            "GET", "/oauth/authorize?client_id=x&redirect_uri=" + urllib.parse.quote(REDIRECT), headers={"X-Forwarded-For": address})[2]).group(1)
        owner = start("198.51.100.7")
        flood = [start("203.0.113.5") for _ in range(oauth.MAX_REQUESTS_PER_ADDRESS + 5)]
        requests = self.srv.server.oauth.requests
        self.assertIn(owner, requests)
        self.assertEqual(sum(r in requests for r in flood), oauth.MAX_REQUESTS_PER_ADDRESS)
        self.assertNotIn(flood[0], requests)  # the flooder's oldest went

    def test_client_address(self):
        class H:
            def __init__(self, peer, forwarded=None):
                self.client_address = (peer, 1234)
                self.headers = {"X-Forwarded-For": forwarded} if forwarded else {}
        self.assertEqual(throttle.client_address(H("203.0.113.5", "1.2.3.4")), "203.0.113.5")  # not from the proxy: ignored
        self.assertEqual(throttle.client_address(H("127.0.0.1", "1.2.3.4, 203.0.113.5")), "203.0.113.5")
        self.assertEqual(throttle.client_address(H("::1", "[2001:db8::1]")), "2001:db8::/64")
        self.assertEqual(throttle.client_address(H("::ffff:203.0.113.5")), "203.0.113.5")
        self.assertEqual(throttle.client_address(H("127.0.0.1", "garbage")), "127.0.0.1")


class OAuthOff(unittest.TestCase):
    def setUp(self):
        self.srv = Running(oauth=False)

    def tearDown(self):
        self.srv.stop()

    def test_off_by_default(self):
        self.assertEqual(self.srv.request("GET", "/.well-known/oauth-protected-resource")[0], 404)
        self.assertEqual(self.srv.request("POST", "/oauth/register", {"redirect_uris": [REDIRECT]})[0], 404)
        status, headers, _ = self.srv.mcp("zat_whatever", "ping")
        self.assertEqual(status, 401)
        self.assertNotIn("www-authenticate", headers)


class PublicUrl(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(oauth.normalize_public_url("https://Zotero.Example.com/"), "https://zotero.example.com")
        self.assertEqual(oauth.normalize_public_url("http://127.0.0.1:8790"), "http://127.0.0.1:8790")
        for bad in ("http://zotero.example.com", "https://example.com/zotero", "zotero.example.com", "https://u@example.com"):
            with self.assertRaises(ValueError, msg=bad):
                oauth.normalize_public_url(bad)


if __name__ == "__main__":
    unittest.main()
