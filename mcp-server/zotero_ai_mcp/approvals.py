"""Writes requested over MCP become operations the user decides on the approval page (ai/spec/mcp.md,
"Writes and approvals"). Same rules as desktop-plugin/src/content/approvals.js.
"""
import json
import secrets
import threading
import time

DECLINED = {"error": "declined", "message": "The user declined this change."}
EXPIRED = {"error": "declined", "message": "Nobody answered the approval request in Zotero."}
TOO_MANY = {"error": "unavailable", "message": "Too many changes are waiting for approval in Zotero."}
MAX_OPEN_PER_CLIENT = 5
MAX_OPEN = 10


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class Approvals:
    def __init__(self, spec, library, clients, is_enabled):
        self.spec, self.library, self.clients, self.is_enabled = spec, library, clients, is_enabled
        self.timing = {"expireMs": 5 * 60 * 1000, "keepMs": 10 * 60 * 1000, "replayMs": 2 * 60 * 1000}
        self.ops = {}
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)

    @staticmethod
    def _open(op):
        return op["state"] in ("awaiting_approval", "running")

    def _finish(self, op, state, result):
        if not self._open(op):
            return
        op.update(state=state, result=result, completedAt=time.time())
        self.changed.notify_all()

    def _prune(self):
        now = time.time()
        for key in [k for k, op in self.ops.items() if not self._open(op) and now - op["completedAt"] > self.timing["keepMs"] / 1000]:
            del self.ops[key]
        for op in self.ops.values():
            if op["state"] == "awaiting_approval" and now >= op["expiresAt"]:
                self._finish(op, "expired", EXPIRED)

    def _start(self, op):
        """awaiting_approval → running, once; False if the operation was already decided, expired or
        its client lost the right to write. Call with the lock held."""
        if op["state"] != "awaiting_approval":
            return False
        client = self.clients.get(op["clientId"])
        if not client or client["scope"] != "write" or not self.is_enabled():
            self._finish(op, "declined", DECLINED)
            return False
        op["state"] = "running"
        self.changed.notify_all()
        return True

    def _run(self, op):
        result = self.library.execute(op["tool"], json.loads(op["argsJSON"]))
        with self.lock:
            self._finish(op, "done", result)

    def request(self, client, name, args):
        checked = self.spec.validate_args(name, args)
        if "error" in checked:
            return None, checked
        args_json = canonical_json(checked["args"])
        # The description may need a sync; work it out first, then check and insert in one step.
        description = self.spec.describe_write(self.library, name, json.loads(args_json))
        with self.lock:
            self._prune()
            now = time.time()
            for op in self.ops.values():
                if op["clientId"] != client["id"] or op["tool"] != name or op["argsJSON"] != args_json:
                    continue
                if self._open(op):
                    return op, None
                if op["state"] == "done" and "error" not in (op["result"] or {}) and now - op["completedAt"] < self.timing["replayMs"] / 1000:
                    return op, None
            open_ops = [op for op in self.ops.values() if self._open(op)]
            if len(open_ops) >= MAX_OPEN or sum(1 for op in open_ops if op["clientId"] == client["id"]) >= MAX_OPEN_PER_CLIENT:
                return None, TOO_MANY
            op = {"id": "op_" + secrets.token_urlsafe(16), "clientId": client["id"], "clientName": client["name"], "tool": name,
                  "argsJSON": args_json, "description": description, "state": "awaiting_approval", "createdAt": now,
                  "expiresAt": now + self.timing["expireMs"] / 1000, "completedAt": None, "result": None}
            self.ops[op["id"]] = op
            self.changed.notify_all()
            run = not self.spec.should_ask(name, client.get("autoApprove", False)) and self._start(op)
        if run:
            threading.Thread(target=self._run, args=(op,), daemon=True).start()
        return op, None

    def decide(self, op_id, approved):
        with self.lock:
            self._prune()
            op = self.ops.get(op_id)
            if not op or op["state"] != "awaiting_approval":
                return False
            if not approved:
                self._finish(op, "declined", DECLINED)
                return True
            if not self._start(op):
                return True
        threading.Thread(target=self._run, args=(op,), daemon=True).start()
        return True

    def get(self, op_id, client_id):
        with self.lock:
            self._prune()
            op = self.ops.get(op_id)
            return op if op and op["clientId"] == client_id else None

    def wait(self, op, seconds):
        deadline = time.time() + seconds
        with self.lock:
            while self._open(op):
                self._prune()
                left = min(deadline, op["expiresAt"] + 0.05 if op["state"] == "awaiting_approval" else deadline) - time.time()
                if left <= 0 or time.time() >= deadline:
                    break
                self.changed.wait(min(left, 1.0))

    def envelope(self, op):
        if not self._open(op):
            return op["result"]
        return {"status": "running" if op["state"] == "running" else "pending_approval", "approval_id": op["id"],
                "description": op["description"],
                "expires_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(op["expiresAt"])),
                "message": "Waiting for the user to approve this in Zotero. Call zotero_await_approval with this approval_id; don't repeat the original call."}

    def pending(self):
        with self.lock:
            self._prune()
            return sorted((op for op in self.ops.values() if op["state"] == "awaiting_approval"), key=lambda op: op["createdAt"])

    def client_revoked(self, client_id):
        with self.lock:
            for op in self.ops.values():
                if op["clientId"] == client_id and op["state"] == "awaiting_approval":
                    self._finish(op, "declined", DECLINED)

    def decline_all(self):
        with self.lock:
            for op in self.ops.values():
                if op["state"] == "awaiting_approval":
                    self._finish(op, "declined", DECLINED)
