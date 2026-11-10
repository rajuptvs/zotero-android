/*
 * Responses API calls billed to the user's ChatGPT plan: store=false, stream=true, the whole
 * conversation in `input` every time (ai/spec/chat.md). Mirrors ChatGptResponsesClient on Android.
 */

ZoteroAI.API = {
	BASE: "https://api.openai.com/v1",

	async models() {
		let response = await fetch(`${this.BASE}/models`, {
			headers: { Authorization: `Bearer ${await ZoteroAI.Auth.accessToken()}` },
		});
		let text = await response.text();
		if (!response.ok) throw await this._error(response.status, text);
		let models = JSON.parse(text).models || [];
		return models.filter((m) => m.visibility == "list" && m.slug).map((m) => ({
			slug: m.slug,
			displayName: m.display_name || m.slug,
			reasoningEfforts: (m.supported_reasoning_levels || m.supported_reasoning_efforts || [])
				.map((l) => (typeof l == "string" ? l : l?.effort || l?.id)).filter(Boolean),
			defaultReasoningEffort: m.default_reasoning_level || m.default_reasoning_effort || null,
			supportsImages: Array.isArray(m.input_modalities) ? m.input_modalities.includes("image") : true,
		}));
	},

	/** Input item for a conversation turn (text, or text plus images for a user turn). */
	inputItem(turn) {
		if (turn.role != "user" || !(turn.images || []).length) return { role: turn.role, content: turn.text };
		return {
			role: "user",
			content: [
				{ type: "input_text", text: turn.text },
				...turn.images.map((image) => ({ type: "input_image", image_url: image.dataUrl })),
			],
		};
	},

	/**
	 * One streamed request. `onEvent` gets {type:"phase", phase:"thinking"|"writing"},
	 * {type:"text", delta} and {type:"item", item} (each finished output item). Resolves after
	 * `response.completed`. `abort.cancel` is set while streaming; calling it stops the request.
	 */
	async stream({ model, instructions, input, tools, reasoningEffort, abort, onEvent }) {
		let body = { model, instructions, input, store: false, stream: true };
		if (tools && tools.length) {
			body.tools = tools;
			body.tool_choice = "auto";
			body.parallel_tool_calls = true;
		}
		if (reasoningEffort) body.reasoning = { effort: reasoningEffort };
		// Stop must also cancel a request that hasn't started streaming yet.
		let controller = new AbortController();
		if (abort) abort.cancel = () => controller.abort();
		let response;
		try {
			response = await fetch(`${this.BASE}/responses`, {
				method: "POST",
				signal: controller.signal,
			headers: {
				Authorization: `Bearer ${await ZoteroAI.Auth.accessToken()}`,
				"Content-Type": "application/json",
				Accept: "text/event-stream",
			},
				body: JSON.stringify(body),
			});
		}
		catch (e) {
			if (abort?.cancelled) throw new Error("Stopped.");
			throw e;
		}
		if (abort?.cancelled) {
			response.body?.cancel().catch(() => {});
			throw new Error("Stopped.");
		}
		if (!response.ok) throw await this._error(response.status, await response.text());
		onEvent({ type: "phase", phase: "thinking" });
		let reader = response.body.getReader();
		let decoder = new TextDecoder();
		let buffer = "";
		let writing = false;
		let completed = false;
		let handle = (event) => {
			switch (event.type) {
				case "response.output_text.delta":
					if (!writing) {
						writing = true;
						onEvent({ type: "phase", phase: "writing" });
					}
					onEvent({ type: "text", delta: event.delta || "" });
					break;
				case "response.output_item.done":
					if (event.item) onEvent({ type: "item", item: event.item });
					break;
				case "response.completed":
					completed = true;
					break;
				case "response.incomplete":
					throw new Error(`The answer was cut off (${event.response?.incomplete_details?.reason || "incomplete"}).`);
				case "response.failed":
				case "error": {
					let error = event.response?.error || event.error || event;
					throw this._apiError(error.code, error.message);
				}
			}
		};
		try {
			while (!completed) {
				let { value, done } = await reader.read();
				if (done) break;
				buffer += decoder.decode(value, { stream: true });
				let lines = buffer.split("\n");
				buffer = lines.pop();
				for (let line of lines) {
					if (!line.startsWith("data:")) continue;
					let data = line.slice(5).trim();
					if (!data || data == "[DONE]") continue;
					handle(JSON.parse(data));
					if (completed) break;
				}
			}
		}
		catch (e) {
			if (abort?.cancelled) throw new Error("Stopped.");
			throw e;
		}
		finally {
			reader.cancel().catch(() => {});
		}
		if (abort?.cancelled) throw new Error("Stopped.");
		if (!completed) throw new Error("The connection closed before the answer finished.");
	},

	async _error(status, text) {
		if (status == 401) await ZoteroAI.Auth.onUnauthorized();
		let json = null;
		try { json = JSON.parse(text); }
		catch (e) {}
		if (json?.error && typeof json.error == "object") return this._apiError(json.error.code, json.error.message);
		return new Error(`ChatGPT request failed (${status}): ${json?.detail || text.slice(0, 200)}`);
	},

	_apiError(code, message) {
		let friendly = {
			subscription_sharing_usage_limit_exceeded: `You've reached the usage limit for this app. Check ${ZoteroAI.Auth.USAGE_URL}`,
			subscription_sharing_user_not_eligible: "Your ChatGPT plan can't be used in this app.",
			subscription_sharing_usage_unavailable: "ChatGPT couldn't check your plan usage right now. Try again shortly.",
			subscription_sharing_user_unavailable: "ChatGPT couldn't check your plan usage right now. Try again shortly.",
			subscription_sharing_unsupported_capability: `This model doesn't support that request${message ? ": " + message : "."}`,
		}[code];
		let error = new Error(friendly || message || code || "ChatGPT request failed.");
		error.code = code;
		return error;
	},
};
