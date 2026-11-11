"""A small MCP server for testing the chat panels as MCP clients (ai/spec/mcp-client.md).

Streamable HTTP on 127.0.0.1: /mcp answers with JSON, /mcp-sse with an SSE stream and a session id.
Both need `Authorization: Bearer fixture-token`. GET /calls lists the tool calls received.
"""
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "Bearer fixture-token"
TOOLS = [
    {"name": "echo", "description": "Repeat the text.", "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "add", "description": "Add two numbers.", "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}}},
    {"name": "save_note", "description": "Save a note somewhere else.",
     "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}, "body": {"type": "string"}}}},
    {"name": "picture", "description": "Returns an image and a caption.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "broken", "description": "Always fails.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "weird.name/v2", "description": "x" * 800},
]


class Fixture:
    def __init__(self, port=0):
        self.calls = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path == "/calls":
                    return self._send(200, json.dumps(fixture.calls), "application/json")
                self._send(405, "", "text/plain")

            def do_POST(self):
                if self.headers.get("Authorization") != TOKEN:
                    return self._send(401, "{}", "application/json")
                sse = self.path == "/mcp-sse"
                if self.path not in ("/mcp", "/mcp-sse"):
                    return self._send(404, "", "text/plain")
                msg = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if "id" not in msg:
                    return self._send(202, "", "text/plain")
                headers = {}
                method = msg.get("method")
                if method == "initialize":
                    result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fixture", "version": "1"}}
                    if sse:
                        headers["Mcp-Session-Id"] = str(uuid.uuid4())
                elif method == "tools/list":
                    result = {"tools": TOOLS}
                elif method == "tools/call":
                    name, args = msg["params"]["name"], msg["params"].get("arguments", {})
                    fixture.calls.append({"name": name, "arguments": args, "path": self.path})
                    result = fixture.call(name, args)
                else:
                    return self._send(200, json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "nope"}}), "application/json")
                reply = json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result})
                if sse:
                    body = 'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/message","params":{}}\n\n' + f"event: message\ndata: {reply}\n\n"
                    return self._send(200, body, "text/event-stream", headers)
                self._send(200, reply, "application/json", headers)

            def _send(self, status, body, ctype, headers=None):
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def call(self, name, args):
        if name == "echo":
            return {"content": [{"type": "text", "text": args.get("text", "")}]}
        if name == "add":
            return {"content": [{"type": "text", "text": f"{args.get('a', 0) + args.get('b', 0)}"}]}
        if name == "save_note":
            return {"content": [{"type": "text", "text": f"saved {args.get('title')}"}]}
        if name == "picture":
            return {"content": [{"type": "image", "data": "iVBORw0KGgo=", "mimeType": "image/png"}, {"type": "text", "text": "a picture"}]}
        if name == "broken":
            return {"content": [{"type": "text", "text": "it broke"}], "isError": True}
        return {"content": [{"type": "text", "text": "ok"}]}

    def stop(self):
        self.server.shutdown()


if __name__ == "__main__":
    import sys
    f = Fixture(int(sys.argv[1]) if len(sys.argv) > 1 else 8765)
    print(f"fixture MCP server on http://127.0.0.1:{f.port}/mcp and /mcp-sse")
    threading.Event().wait()
