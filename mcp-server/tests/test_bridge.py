"""mcp-bridge/bridge.mjs (stdio → Streamable HTTP) against a running server with a fake library, plus
small stand-in servers for SSE replies and timeouts. Skipped without Node."""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from support import IMAGE, Running, free_port

BRIDGE = os.path.join(os.path.dirname(__file__), "..", "..", "mcp-bridge", "bridge.mjs")
NODE = shutil.which("node")


def run_bridge(env, lines, wait_for, args=(), timeout=30, later=()):
    """Feeds lines to the bridge (`later` ones after the first reply), closes stdin once `wait_for`
    replies arrived, → (replies, stderr, exit code)."""
    proc = subprocess.Popen([NODE, BRIDGE, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env={"PATH": os.environ.get("PATH", ""), **env}, text=True)
    for line in lines:
        proc.stdin.write(line + "\n")
    proc.stdin.flush()
    replies, deadline = [], time.time() + timeout
    while len(replies) < wait_for and time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        replies.append(json.loads(line))
        if later and len(replies) == 1:
            proc.stdin.write("".join(x + "\n" for x in later))
            proc.stdin.flush()
    proc.stdin.close()
    rest = proc.stdout.read()
    replies += [json.loads(x) for x in rest.splitlines() if x.strip()]
    err = proc.stderr.read()
    return replies, err, proc.wait(10)


def msg(id, method, params=None):
    m = {"jsonrpc": "2.0", "method": method}
    if id is not None:
        m["id"] = id
    if params is not None:
        m["params"] = params
    return json.dumps(m)


@unittest.skipUnless(NODE, "needs node")
class Bridge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = Running(oauth=False)
        cls.srv.login()
        cls.token = cls.srv.admin("/admin/api/clients", {"name": "Bridge", "scope": "write"})[2]["token"]
        cls.tmp = tempfile.mkdtemp(prefix="zai-bridge-")
        cls.token_file = os.path.join(cls.tmp, "token")
        with open(cls.token_file, "w") as f:
            f.write(cls.token + "\n")

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_forwards_unchanged(self):
        page = {"name": "zotero_get_page_image", "arguments": {"item_key": "ABCD2345", "page": 1, "region": None, "max_width": None}}
        replies, err, code = run_bridge({"ZOTERO_AI_MCP_URL": self.srv.base + "/mcp", "ZOTERO_AI_MCP_TOKEN_FILE": self.token_file}, [
            msg(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}),
            msg(None, "notifications/initialized"),
            msg(2, "tools/list"), msg(3, "tools/call", page), msg("four", "ping"), "this is not json",
        ], wait_for=5)
        by_id = {r["id"]: r for r in replies}
        self.assertEqual(code, 0, err)
        self.assertEqual(len(replies), 5, replies)  # no reply to the notification
        self.assertEqual(by_id[1]["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("zotero_create_note", {t["name"] for t in by_id[2]["result"]["tools"]})
        self.assertEqual(by_id[3]["result"]["content"][1], IMAGE)
        self.assertEqual(by_id["four"], {"jsonrpc": "2.0", "id": "four", "result": {}})
        self.assertEqual(by_id[None]["error"]["code"], -32700)
        # Same bytes the server sends directly.
        direct = self.srv.mcp(self.token, "tools/call", page, id=3)[2]
        self.assertEqual(by_id[3], direct)

    def test_bad_token(self):
        replies, err, _ = run_bridge({"ZOTERO_AI_MCP_URL": self.srv.base + "/mcp", "ZOTERO_AI_MCP_TOKEN": "zai_wrong"}, [msg(1, "ping")], 1)
        self.assertIn("rejected the token", replies[0]["error"]["message"])
        self.assertIn("HTTP 401", err)

    def test_token_never_from_arguments(self):
        _, err, code = run_bridge({}, [], 0, args=[self.srv.base + "/mcp", self.token])
        self.assertEqual(code, 2)
        self.assertIn("command line", err)
        self.assertNotIn(self.token, err)

    def test_malformed_token_not_echoed(self):
        _, err, code = run_bridge({"ZOTERO_AI_MCP_TOKEN": self.token + "\n" + self.token}, [msg(1, "ping")], 0)
        self.assertEqual(code, 2)
        self.assertNotIn(self.token, err)

    def test_missing_token(self):
        _, err, code = run_bridge({"ZOTERO_AI_MCP_URL": self.srv.base + "/mcp"}, [], 0)
        self.assertEqual(code, 2)
        self.assertIn("No token", err)

    def test_unreachable(self):
        replies, err, _ = run_bridge({"ZOTERO_AI_MCP_URL": f"http://127.0.0.1:{free_port()}/mcp", "ZOTERO_AI_MCP_TOKEN": "zai_x"}, [msg(1, "ping")], 1)
        self.assertIn("Can't reach", replies[0]["error"]["message"])
        self.assertIn("ECONNREFUSED", err)

    def test_sse_reply_and_timeout(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append((dict(self.headers), body))
                if body.get("method") == "slow":
                    time.sleep(3)
                if "id" not in body:
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                events = (": comment\r\n"
                          'event: message\r\ndata: {"jsonrpc":"2.0","method":"notifications/progress",\r\ndata: "params":{"progress":1}}\r\n\r\n'
                          f'data: {json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-11-25", "echo": "é"}})}\n\n')
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Mcp-Session-Id", "s-1")
                self.end_headers()
                try:
                    for i in range(0, len(events), 7):  # split across chunks, \r\n included
                        self.wfile.write(events[i:i + 7].encode())
                        self.wfile.flush()
                except BrokenPipeError:  # the bridge gave up on "slow"
                    pass

        port = free_port()
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            replies, err, _ = run_bridge({"ZOTERO_AI_MCP_URL": f"http://127.0.0.1:{port}/mcp", "ZOTERO_AI_MCP_TOKEN": "zai_t", "ZOTERO_AI_MCP_TIMEOUT": "1"},
                                         [msg(1, "initialize", {"protocolVersion": "2025-11-25"})], 2)
            self.assertEqual(replies[0], {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1}})
            self.assertEqual(replies[1]["result"]["echo"], "é")
            replies, err, _ = run_bridge({"ZOTERO_AI_MCP_URL": f"http://127.0.0.1:{port}/mcp", "ZOTERO_AI_MCP_TOKEN": "zai_t", "ZOTERO_AI_MCP_TIMEOUT": "1"},
                                         [msg(1, "initialize", {})], 3, later=[msg(2, "slow")])
            self.assertIn("No reply within 1 s", [r for r in replies if r.get("id") == 2][0]["error"]["message"])
            headers = seen[-1][0]
            self.assertEqual(headers["Authorization"], "Bearer zai_t")
            self.assertEqual(headers["Mcp-Session-Id"], "s-1")
            self.assertEqual(headers["MCP-Protocol-Version"], "2025-11-25")
            self.assertNotIn("Origin", headers)
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
