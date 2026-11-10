/*
 * The one place a write can run. Tools.execute refuses writes unless it gets the capability object
 * kept in this closure, so every write goes through a decision here: the user's, or a setting the
 * user turned on (ai/spec/tools.md, "Write tools"). The arguments that run are the ones that were
 * shown: they're stored as canonical JSON and parsed again for both the description and the run.
 */

ZoteroAI.Approvals = (() => {
	const capability = Object.freeze({});
	const DECLINED = Object.freeze({ error: "declined", message: "The user declined this change." });

	return {
		DECLINED,

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
			let autoApprove = origin?.kind == "chat" && ZoteroAI.pref("autoApproveChanges") === true;
			let approved = !ZoteroAI.Spec.shouldAsk(name, autoApprove) || await present(description);
			if (!approved) return { result: DECLINED, description, approved: false };
			let result = await ZoteroAI.Tools.execute(name, JSON.parse(argsJSON), capability);
			return { result, description, approved: true };
		},
	};
})();
