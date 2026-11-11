/*
 * The one place a write can run. Tools.execute refuses writes unless it gets the capability object
 * kept in this closure, so every write goes through a decision here: the user's, or a setting the
 * user turned on (ai/spec/tools.md, "Write tools"). The arguments that run are the ones that were
 * shown: they're stored as canonical JSON and parsed again for both the description and the run.
 *
 * Chat writes ask in the chat (run). Writes from MCP clients become operations (request) that the
 * user decides on the approval sheet (decide); see ai/spec/mcp.md.
 */

ZoteroAI.Approvals = (() => {
	const capability = Object.freeze({});
	const DECLINED = Object.freeze({ error: "declined", message: "The user declined this change." });
	const EXPIRED = Object.freeze({ error: "declined", message: "Nobody answered the approval request in Zotero." });
	const TOO_MANY = Object.freeze({ error: "unavailable", message: "Too many changes are waiting for approval in Zotero." });
	// Changed only by the dev test endpoints, to test expiry without waiting minutes.
	const timing = {
		expireMs: 5 * 60 * 1000, // unanswered requests expire
		keepMs: 10 * 60 * 1000, // finished operations stay readable by approval_id
		replayMs: 2 * 60 * 1000, // an identical call this soon after success gets the same result
	};
	const MAX_OPEN_PER_CLIENT = 5;
	const MAX_OPEN = 10;

	let ops = new Map(); // id → operation
	let waiters = new Map(); // id → Set of resolve functions
	let listeners = new Set();

	let isOpen = (op) => op.state == "awaiting_approval" || op.state == "running";
	let notify = () => {
		for (let l of listeners) {
			try { l(); }
			catch (e) { Zotero.logError(e); }
		}
	};

	function finish(op, state, result) {
		if (!isOpen(op)) return;
		op.state = state;
		op.result = result;
		op.completedAt = Date.now();
		clearTimeout(op._timer);
		for (let resolve of waiters.get(op.id) || []) resolve();
		waiters.delete(op.id);
		notify();
	}

	async function execute(op) {
		// The client may have been revoked, or MCP turned off, while the card was up.
		let client = ZoteroAI.McpClients.get(op.clientId);
		if (!client || client.scope != "write" || !ZoteroAI.McpServer.isEnabled()) {
			finish(op, "declined", DECLINED);
			return;
		}
		clearTimeout(op._timer);
		op.state = "running";
		notify();
		let result = await ZoteroAI.Tools.execute(op.tool, JSON.parse(op.argsJSON), capability);
		finish(op, "done", result);
	}

	function prune() {
		let now = Date.now();
		for (let [id, op] of ops) {
			if (!isOpen(op) && now - op.completedAt > timing.keepMs) ops.delete(id);
		}
	}

	return {
		DECLINED,
		timing,

		/** True only for the capability held here (Tools.execute checks it). */
		authorizes(token) {
			return token === capability;
		},

		/**
		 * A write requested by the chat. `present(description)` shows the chat's own approval card and
		 * resolves to the user's answer; it isn't called when the setting allows the change.
		 * Resolves to {result, description, approved}.
		 */
		async run({ origin, name, args, present }) {
			let checked = ZoteroAI.Spec.validateArgs(name, args);
			if (checked.error) return { result: checked, description: null, approved: false };
			let argsJSON = ZoteroAI.canonicalJSON(checked.args);
			let description = await ZoteroAI.Tools.describeWrite(name, JSON.parse(argsJSON));
			// Not after another service's result in the same turn (ai/spec/mcp-client.md).
			let autoApprove = origin?.kind == "chat" && !origin.tainted && ZoteroAI.pref("autoApproveChanges") === true;
			let approved = !ZoteroAI.Spec.shouldAsk(name, autoApprove) || await present(description);
			if (!approved) return { result: DECLINED, description, approved: false };
			let result = await ZoteroAI.Tools.execute(name, JSON.parse(argsJSON), capability);
			return { result, description, approved: true };
		},

		// region MCP operations

		/**
		 * A write requested by an MCP client. Resolves to {op} (a new operation, an open one with the
		 * same client, tool and arguments, or one that succeeded within timing.replayMs), or to {result} when
		 * the request is refused outright (invalid arguments, too many open requests).
		 */
		async request({ client, name, args }) {
			let checked = ZoteroAI.Spec.validateArgs(name, args);
			if (checked.error) return { result: checked };
			let argsJSON = ZoteroAI.canonicalJSON(checked.args);
			prune();
			let now = Date.now();
			for (let op of ops.values()) {
				if (op.clientId != client.id || op.tool != name || op.argsJSON != argsJSON) continue;
				if (isOpen(op)) return { op };
				if (op.state == "done" && !op.result?.error && now - op.completedAt < timing.replayMs) return { op };
			}
			let open = [...ops.values()].filter(isOpen);
			if (open.length >= MAX_OPEN || open.filter((op) => op.clientId == client.id).length >= MAX_OPEN_PER_CLIENT) {
				return { result: TOO_MANY };
			}
			let op = {
				id: "op_" + ZoteroAI.randomToken(16),
				clientId: client.id,
				clientName: client.name,
				tool: name,
				argsJSON,
				description: await ZoteroAI.Tools.describeWrite(name, JSON.parse(argsJSON)),
				state: "awaiting_approval",
				createdAt: now,
				expiresAt: now + timing.expireMs,
				completedAt: null,
				result: null,
			};
			ops.set(op.id, op);
			if (ZoteroAI.Spec.shouldAsk(name, client.autoApprove)) {
				op._timer = setTimeout(() => finish(op, "expired", EXPIRED), timing.expireMs);
				notify();
			}
			else {
				execute(op).catch((e) => Zotero.logError(e));
			}
			return { op };
		},

		/** The user's decision on the approval sheet. Only an open request can be decided, once. */
		decide(id, approved) {
			let op = ops.get(id);
			if (!op || op.state != "awaiting_approval") return false;
			if (approved) execute(op).catch((e) => Zotero.logError(e));
			else finish(op, "declined", DECLINED);
			return true;
		},

		/** An operation for zotero_await_approval: only the client that asked can read it. */
		get(id, clientId) {
			prune();
			let op = ops.get(id);
			return op && op.clientId == clientId ? op : null;
		},

		/** Waits up to `ms` for the operation to finish. */
		wait(op, ms) {
			if (!isOpen(op)) return Promise.resolve();
			return new Promise((resolve) => {
				let timer = setTimeout(done, ms);
				function done() {
					clearTimeout(timer);
					waiters.get(op.id)?.delete(done);
					resolve();
				}
				if (!waiters.has(op.id)) waiters.set(op.id, new Set());
				waiters.get(op.id).add(done);
			});
		},

		/** What a tool call returns for an operation (ai/spec/mcp.md). */
		envelope(op) {
			if (!isOpen(op)) return op.result;
			return {
				status: op.state == "running" ? "running" : "pending_approval",
				approval_id: op.id,
				description: op.description,
				expires_at: new Date(op.expiresAt).toISOString(),
				message: "Waiting for the user to approve this in Zotero. Call zotero_await_approval with this approval_id; don't repeat the original call.",
			};
		},

		/** Requests waiting for the user, oldest first (for the approval sheet). */
		pending() {
			return [...ops.values()].filter((op) => op.state == "awaiting_approval").sort((a, b) => a.createdAt - b.createdAt);
		},

		onChange(listener) {
			listeners.add(listener);
			return () => listeners.delete(listener);
		},

		/** A revoked client's waiting requests are declined. */
		clientRevoked(clientId) {
			for (let op of ops.values()) {
				if (op.clientId == clientId && op.state == "awaiting_approval") finish(op, "declined", DECLINED);
			}
		},

		/** MCP turned off or the plugin shutting down: every waiting request is declined. */
		declineAll() {
			for (let op of ops.values()) {
				if (op.state == "awaiting_approval") finish(op, "declined", DECLINED);
			}
		},

		// endregion
	};
})();
