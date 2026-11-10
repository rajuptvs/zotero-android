/*
 * Sign in with ChatGPT, open-source plan-usage flow (ai/spec/chat.md): dynamic client registration,
 * PKCE, a loopback redirect to http://127.0.0.1:<Zotero's port>/auth/callback (served by Zotero's
 * own local server), refresh with rotation, revocation on sign-out. Mirrors ChatGptAuthManager on
 * Android.
 */

ZoteroAI.Auth = {
	ISSUER: "https://auth.openai.com",
	AUTHORIZE_URL: "https://auth.openai.com/api/accounts/authorize",
	TOKEN_URL: "https://auth.openai.com/api/accounts/oauth/token",
	OPENID_CONFIGURATION_URL: "https://auth.openai.com/.well-known/openid-configuration",
	RESOURCE: "https://api.openai.com/v1",
	DYNAMIC_CLIENT: "dynamic_agent_client",
	AGENT_NAME: "Zotero Desktop AI",
	SCOPES: "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct",
	PLAN_SCOPE: "chatgpt.tokens.use.direct",
	CALLBACK_PATH: "/auth/callback",
	USAGE_URL: "https://chatgpt.com/settings/usage",
	LOGIN_ORIGIN: "chrome://zotero-ai",
	LOGIN_REALM: "ChatGPT credentials",
	MAX_WAIT_MS: 10 * 60 * 1000,

	_listeners: new Set(),
	_state: null, // { status: "signedOut" | "signingIn" | "signedIn", email, planUsageEnabled }
	_pending: null, // the sign-in waiting for its callback
	_tokenChain: Promise.resolve(),

	// region State

	async init() {
		this._registerCallbackEndpoint();
		let c = await this.credentials();
		this._setState(c ? { status: "signedIn", email: c.email, planUsageEnabled: c.scopes.includes(this.PLAN_SCOPE) }
			: { status: "signedOut" });
	},

	state() {
		return this._state || { status: "signedOut" };
	},

	onChange(listener) {
		this._listeners.add(listener);
		return () => this._listeners.delete(listener);
	},

	_setState(state) {
		this._state = state;
		for (let l of this._listeners) {
			try { l(state); }
			catch (e) { Zotero.logError(e); }
		}
	},

	// endregion

	// region Storage

	hostId() {
		let id = ZoteroAI.pref("hostId");
		if (!id) id = ZoteroAI.pref("hostId", "urn:uuid:" + crypto.randomUUID());
		return id;
	},

	/** The client registered for one ChatGPT account (survives sign-out). */
	registration() {
		let clientId = ZoteroAI.pref("clientId");
		return clientId ? { clientId, subject: ZoteroAI.pref("subject") || null, email: ZoteroAI.pref("email") || null } : null;
	},

	clearRegistration() {
		for (let p of ["clientId", "subject", "email"]) Zotero.Prefs.clear(ZoteroAI.PREF_BRANCH + p, true);
		this._writeSecret("idTokenHint", null);
	},

	async credentials() {
		let json = await this._readSecret("credentials");
		if (!json) return null;
		try {
			return JSON.parse(json);
		}
		catch (e) {
			return null;
		}
	},

	async _save(credentials) {
		await this._writeSecret("credentials", JSON.stringify(credentials));
		await this._writeSecret("idTokenHint", credentials.idToken);
		ZoteroAI.pref("clientId", credentials.clientId);
		ZoteroAI.pref("subject", credentials.subject);
		if (credentials.email) ZoteroAI.pref("email", credentials.email);
	},

	async _clearTokens() {
		await this._writeSecret("credentials", null);
	},

	_logins(name) {
		return Services.logins.findLogins(this.LOGIN_ORIGIN, null, this.LOGIN_REALM).filter((l) => l.username == name);
	},

	/** Secrets go in Zotero's login manager, encrypted with the OS key store when there is one. */
	async _readSecret(name) {
		let login = this._logins(name)[0];
		if (!login) return null;
		let value = login.password;
		if (value.startsWith("oskv1:")) {
			try {
				value = await Zotero.OSKeyStore.decrypt(value);
			}
			catch (e) {
				Zotero.logError(e);
				return null;
			}
		}
		return value;
	},

	async _writeSecret(name, value) {
		let old = this._logins(name);
		if (value != null) {
			let stored = value;
			// Zotero 10 has an OS key store (Keychain, Credential Manager, libsecret); Zotero 7 doesn't.
			if (Zotero.OSKeyStore?.available) {
				try {
					stored = await Zotero.OSKeyStore.encrypt(value); // returns "oskv1:…"
				}
				catch (e) {
					// Locked or missing keyring: the login manager still keeps it out of prefs.
					Zotero.logError(e);
				}
			}
			let login = Cc["@mozilla.org/login-manager/loginInfo;1"].createInstance(Ci.nsILoginInfo);
			login.init(this.LOGIN_ORIGIN, null, this.LOGIN_REALM, name, stored, "", "");
			// Replace in place: the login manager refuses a second login with the same name, and
			// modifyLogin keeps the old value if writing the new one fails.
			if (old.length) {
				Services.logins.modifyLogin(old.shift(), login);
			}
			else {
				await Services.logins.addLoginAsync(login);
			}
		}
		for (let login of old) Services.logins.removeLogin(login);
	},

	// endregion

	// region Sign-in

	redirectUri() {
		return `http://127.0.0.1:${Zotero.Prefs.get("httpServer.port")}${this.CALLBACK_PATH}`;
	},

	_registerCallbackEndpoint() {
		let auth = this;
		let Endpoint = function () {};
		Endpoint.prototype = {
			supportedMethods: ["GET"],
			// The browser delivers the redirect, so Zotero must accept a request from web content here.
			allowRequestsFromUnsafeWebContent: true,
			init(request) {
				let params = Object.fromEntries(request.searchParams.entries());
				let pending = auth._pending;
				if (!pending || params.state !== pending.state) {
					return [404, "text/plain", "No sign-in is waiting for this response."];
				}
				pending.resolve(params);
				let ok = !params.error;
				let title = ok ? "Signed in to ChatGPT" : "Sign-in was not completed";
				return [200, "text/html; charset=utf-8",
					`<!doctype html><meta charset="utf-8"><title>${title}</title>`
					+ `<body style="font-family:sans-serif;text-align:center;margin:48px"><h2>${title}</h2>`
					+ `<p>You can close this tab and go back to Zotero.</p></body>`];
			},
		};
		Zotero.Server.Endpoints[this.CALLBACK_PATH] = Endpoint;
	},

	unregister() {
		delete Zotero.Server.Endpoints[this.CALLBACK_PATH];
		this._pending?.reject(new Error("Zotero AI was shut down."));
	},

	cancelSignIn() {
		this._pending?.reject(new Error("Sign-in was cancelled."));
	},

	/**
	 * Runs the whole browser sign-in: `openURL` is called with the authorization URL; resolves once
	 * the browser has come back and the code has been exchanged.
	 */
	async signIn(openURL, { newAccount = false, requestConsent = false } = {}) {
		// One sign-in at a time: a newer one takes over (Android serializes them with a mutex).
		let mine = {};
		this._signInRun = mine;
		if (this._pending) this._pending.reject(new Error("Sign-in was restarted."));
		let previous = this._signInPrevious ?? this.state();
		this._signInPrevious = previous;
		this._setState({ status: "signingIn" });
		try {
			if (newAccount) {
				await this._clearTokens();
				this.clearRegistration();
			}
			let registration = this.registration();
			let verifier = ZoteroAI.randomToken(64);
			let state = ZoteroAI.randomToken(32);
			let nonce = ZoteroAI.randomToken(32);
			let redirectUri = this.redirectUri();
			let params = new URLSearchParams();
			if (registration) {
				params.set("client_id", registration.clientId);
				let hint = await this._readSecret("idTokenHint");
				if (hint) params.set("id_token_hint", hint);
				if (registration.email) params.set("login_hint", registration.email);
			}
			else {
				params.set("client_id", this.DYNAMIC_CLIENT);
				params.set("agent_name_hint", this.AGENT_NAME);
			}
			params.set("ext_agent_host_id", this.hostId());
			params.set("response_type", "code");
			params.set("redirect_uri", redirectUri);
			params.set("scope", this.SCOPES);
			params.set("resource", this.RESOURCE);
			params.set("state", state);
			params.set("nonce", nonce);
			params.set("code_challenge_method", "S256");
			params.set("code_challenge", await ZoteroAI.sha256base64url(verifier));
			if (requestConsent) params.set("prompt", "consent");

			let callback = new Promise((resolve, reject) => {
				let timer = setTimeout(() => reject(new Error("Sign-in took too long. Please try again.")), this.MAX_WAIT_MS);
				this._pending = {
					run: mine,
					state,
					resolve: (v) => { clearTimeout(timer); resolve(v); },
					reject: (e) => { clearTimeout(timer); reject(e); },
				};
			});
			openURL(`${this.AUTHORIZE_URL}?${params}`);
			let result = await callback;

			if (result.error) {
				throw new Error(result.error == "access_denied" ? "Sign-in was cancelled." : (result.error_description || result.error));
			}
			if (!result.code) throw new Error("Sign-in response had no authorization code.");
			let callbackClientId = result.client_id || null;
			let clientId;
			if (!registration) {
				if (!callbackClientId || callbackClientId == this.DYNAMIC_CLIENT) throw new Error("Registration did not return a client ID.");
				// Keep the registration even if the rest fails, so we never register twice.
				ZoteroAI.pref("clientId", callbackClientId);
				clientId = callbackClientId;
			}
			else {
				if (callbackClientId && callbackClientId != registration.clientId) throw new Error("Sign-in returned a different client ID.");
				clientId = registration.clientId;
			}

			let tokens = await this._post({
				grant_type: "authorization_code",
				client_id: clientId,
				code: result.code,
				code_verifier: verifier,
				redirect_uri: redirectUri,
				resource: this.RESOURCE,
			});
			let credentials = this._credentialsFrom(tokens, clientId, null);
			this._validateIdToken(credentials.idToken, clientId, nonce);
			if (registration?.subject && registration.subject != credentials.subject) {
				throw new Error("You signed in with a different ChatGPT account than before. Use \"Sign in with a different account\" to switch.");
			}
			await this._save(credentials);
			this._setState({ status: "signedIn", email: credentials.email, planUsageEnabled: credentials.scopes.includes(this.PLAN_SCOPE) });
		}
		catch (e) {
			// A sign-in that was taken over leaves the state to the newer one.
			if (this._signInRun === mine) {
				this._setState(previous.status == "signingIn" || newAccount ? { status: "signedOut" } : previous);
			}
			throw e;
		}
		finally {
			if (this._pending?.run === mine) this._pending = null;
			if (this._signInRun === mine) {
				this._signInRun = null;
				this._signInPrevious = null;
			}
		}
	},

	/** For the dev endpoint: start a sign-in and return the URL to open in any browser on this machine. */
	startSignInForTest() {
		return new Promise((resolve) => {
			this.signIn((url) => resolve(url)).catch((e) => Zotero.logError(e));
		});
	},

	// endregion

	// region Tokens

	/** A usable access token, refreshed when it's about to expire (refreshes run one at a time). */
	accessToken() {
		let run = this._tokenChain.then(() => this._accessToken());
		this._tokenChain = run.catch(() => {});
		return run;
	},

	async _accessToken() {
		let c = await this.credentials();
		if (!c) throw new Error("Not signed in to ChatGPT.");
		if (Date.now() < c.expiresAt - 5 * 60 * 1000) return c.accessToken;
		let tokens;
		try {
			tokens = await this._post({
				grant_type: "refresh_token",
				client_id: c.clientId,
				refresh_token: c.refreshToken,
				resource: this.RESOURCE,
			});
		}
		catch (e) {
			if (["invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
				"refresh_token_invalidated", "refresh_token_reused"].includes(e.code)) {
				await this._clearTokens();
				this._setState({ status: "signedOut" });
				throw new Error("Your ChatGPT session expired. Please sign in again.");
			}
			throw e;
		}
		let refreshed = this._credentialsFrom(tokens, c.clientId, c);
		if (refreshed.subject != c.subject) throw new Error("ChatGPT returned a session for a different account.");
		await this._save(refreshed);
		return refreshed.accessToken;
	},

	/** The API rejected our token (e.g. the app was disconnected in ChatGPT settings). */
	async onUnauthorized() {
		await this._clearTokens();
		this._setState({ status: "signedOut" });
	},

	/** Revokes the renewable session, then forgets the tokens. Returns false if revocation wasn't confirmed. */
	async signOut() {
		let run = this._tokenChain.then(async () => {
			let c = await this.credentials();
			let revoked = !c;
			if (c) {
				for (let attempt = 0; attempt < 3 && !revoked; attempt++) {
					try {
						let config = await (await fetch(this.OPENID_CONFIGURATION_URL)).json();
						if (!config.revocation_endpoint) break;
						let response = await fetch(config.revocation_endpoint, {
							method: "POST",
							headers: { "Content-Type": "application/x-www-form-urlencoded" },
							body: new URLSearchParams({ token: c.refreshToken, token_type_hint: "refresh_token", client_id: c.clientId }),
						});
						if (response.status == 200) revoked = true;
						else if (response.status < 500) break;
					}
					catch (e) {
						Zotero.debug(e);
					}
					if (!revoked) await Zotero.Promise.delay(1000 * (attempt + 1));
				}
			}
			await this._clearTokens();
			this._setState({ status: "signedOut" });
			return revoked;
		});
		this._tokenChain = run.catch(() => {});
		return run;
	},

	async _post(fields) {
		let response = await fetch(this.TOKEN_URL, {
			method: "POST",
			headers: { "Content-Type": "application/x-www-form-urlencoded" },
			body: new URLSearchParams(fields),
		});
		let text = await response.text();
		let json = null;
		try { json = JSON.parse(text); }
		catch (e) {}
		if (!response.ok || !json) {
			let error = new Error(`ChatGPT sign-in failed (${response.status}): ${json?.error_description || json?.error || text.slice(0, 200)}`);
			error.code = typeof json?.error == "string" ? json.error : null;
			throw error;
		}
		return json;
	},

	_jwt(token) {
		let part = (token || "").split(".")[1];
		if (!part) throw new Error("Malformed ID token.");
		return JSON.parse(ZoteroAI.base64urlDecode(part));
	},

	_credentialsFrom(tokens, clientId, previous) {
		let idToken = tokens.id_token || previous?.idToken;
		if (!idToken) throw new Error("Token response had no ID token.");
		let claims = this._jwt(idToken);
		let accessToken = tokens.access_token;
		if (!accessToken) throw new Error("Token response had no access token.");
		let refreshToken = tokens.refresh_token || previous?.refreshToken;
		if (!refreshToken) throw new Error("Token response had no refresh token.");
		return {
			email: claims.email || previous?.email || null,
			subject: claims.sub || previous?.subject || (() => { throw new Error("ID token has no subject."); })(),
			clientId,
			idToken,
			accessToken,
			refreshToken,
			scopes: tokens.scope ? tokens.scope.split(" ").filter(Boolean) : (previous?.scopes || []),
			expiresAt: Date.now() + (tokens.expires_in || 3600) * 1000,
		};
	},

	/**
	 * The ID token comes straight from the token endpoint over TLS, so per OpenID Connect Core
	 * §3.1.3.7(6) we rely on TLS for the issuer and check the remaining claims here.
	 */
	_validateIdToken(idToken, clientId, nonce) {
		let claims = this._jwt(idToken);
		if (claims.iss != this.ISSUER) throw new Error("ID token has an unexpected issuer.");
		let aud = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
		if (!aud.includes(clientId)) throw new Error("ID token was issued for a different client.");
		if (claims.nonce != nonce) throw new Error("ID token doesn't belong to this sign-in.");
		if ((claims.exp || 0) * 1000 < Date.now()) throw new Error("ID token has expired.");
	},

	// endregion
};
