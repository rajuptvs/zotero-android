package org.zotero.android.ai.auth

import android.content.Context
import android.net.Uri
import android.util.Base64
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import okhttp3.FormBody
import okhttp3.OkHttpClient
import okhttp3.Request
import timber.log.Timber
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.concurrent.TimeUnit
import javax.inject.Inject
import javax.inject.Singleton

sealed interface ChatGptAuthState {
    object SignedOut : ChatGptAuthState
    object SigningIn : ChatGptAuthState
    data class SignedIn(val email: String?, val planUsageEnabled: Boolean) : ChatGptAuthState
}

class ChatGptAuthException(message: String) : Exception(message)

/**
 * Sign in with ChatGPT (open-source plan-usage flow): dynamic client registration, PKCE, loopback
 * redirect, token refresh and revocation.
 * https://developers.openai.com/siwc/token-sharing-open-source/sign-in
 */
@Singleton
class ChatGptAuthManager @Inject constructor(
    private val context: Context,
    private val store: ChatGptCredentialStore,
) {
    private val http = OkHttpClient.Builder()
        .callTimeout(60, TimeUnit.SECONDS)
        .build()
    private val random = SecureRandom()
    private val tokenMutex = Mutex()
    private val signInMutex = Mutex()

    private val _state = MutableStateFlow(initialState())
    val state: StateFlow<ChatGptAuthState> = _state.asStateFlow()

    private fun initialState(): ChatGptAuthState {
        val credentials = store.load() ?: return ChatGptAuthState.SignedOut
        return ChatGptAuthState.SignedIn(credentials.email, credentials.isPlanUsageEnabled)
    }

    private val returnUrl: String
        get() = "${context.packageName}.chatgpt://signed-in"

    /**
     * Runs the whole browser sign-in. [openBrowser] is called with the authorization URL; the
     * function returns once the browser has redirected back and the code has been exchanged.
     *
     * @param newAccount register a new client, for a different ChatGPT account or workspace.
     * @param requestConsent show the consent screen again, to turn on plan usage after it was
     * declined (a returning sign-in otherwise skips consent).
     */
    suspend fun signIn(
        openBrowser: (Uri) -> Unit,
        newAccount: Boolean = false,
        requestConsent: Boolean = false,
    ) = signInMutex.withLock {
        val previousState = _state.value
        _state.value = ChatGptAuthState.SigningIn
        if (newAccount) {
            store.clearTokens()
            store.clearRegistration()
        }
        val registration = store.registration
        val verifier = randomToken(64)
        val state = randomToken(32)
        val nonce = randomToken(32)
        var server: LoopbackCallbackServer? = null
        try {
            server = withContext(Dispatchers.IO) { LoopbackCallbackServer.start(state, returnUrl) }
            ChatGptSignInService.start(context)
            val redirectUri = server.redirectUri

            val url = Uri.parse(ChatGptAuthConfig.authorizeUrl).buildUpon().apply {
                if (registration == null) {
                    appendQueryParameter("client_id", ChatGptAuthConfig.dynamicRegistrationClientId)
                    appendQueryParameter("agent_name_hint", ChatGptAuthConfig.agentNameHint)
                } else {
                    appendQueryParameter("client_id", registration.clientId)
                    store.idTokenHint?.let { appendQueryParameter("id_token_hint", it) }
                    registration.email?.let { appendQueryParameter("login_hint", it) }
                }
                appendQueryParameter("ext_agent_host_id", store.hostId())
                appendQueryParameter("response_type", "code")
                appendQueryParameter("redirect_uri", redirectUri)
                appendQueryParameter("scope", ChatGptAuthConfig.scopes)
                appendQueryParameter("resource", ChatGptAuthConfig.resource)
                appendQueryParameter("state", state)
                appendQueryParameter("nonce", nonce)
                appendQueryParameter("code_challenge_method", "S256")
                appendQueryParameter("code_challenge", codeChallenge(verifier))
                if (requestConsent) {
                    appendQueryParameter("prompt", "consent")
                }
            }.build()

            openBrowser(url)
            val params = withTimeoutOrNull(ChatGptSignInService.maxDurationMillis) {
                server.awaitCallback()
            } ?: throw ChatGptAuthException("Sign-in took too long. Please try again.")

            params["error"]?.let { error ->
                if (error == "access_denied") {
                    throw ChatGptAuthException("Sign-in was cancelled.")
                }
                throw ChatGptAuthException(params["error_description"] ?: error)
            }
            val code = params["code"]
                ?: throw ChatGptAuthException("Sign-in response had no authorization code.")
            val callbackClientId = params["client_id"]?.takeIf { it.isNotBlank() }
            val clientId = if (registration == null) {
                if (callbackClientId == null ||
                    callbackClientId == ChatGptAuthConfig.dynamicRegistrationClientId
                ) {
                    throw ChatGptAuthException("Registration did not return a client ID.")
                }
                // Keep the registration even if the rest fails, so we never register twice.
                store.saveRegistration(callbackClientId)
                callbackClientId
            } else {
                if (callbackClientId != null && callbackClientId != registration.clientId) {
                    throw ChatGptAuthException("Sign-in returned a different client ID.")
                }
                registration.clientId
            }

            val tokens = postForm(
                FormBody.Builder()
                    .add("grant_type", "authorization_code")
                    .add("client_id", clientId)
                    .add("code", code)
                    .add("code_verifier", verifier)
                    .add("redirect_uri", redirectUri)
                    .add("resource", ChatGptAuthConfig.resource)
                    .build()
            )
            val credentials = credentialsFrom(tokens, clientId, refreshTokenFallback = null)
            validateIdToken(credentials.idToken, clientId = clientId, nonce = nonce)
            val expectedSubject = registration?.subject
            if (expectedSubject != null && expectedSubject != credentials.subject) {
                throw ChatGptAuthException(
                    "You signed in with a different ChatGPT account than before. " +
                            "Use \"Sign in with a different account\" to switch."
                )
            }
            store.save(credentials)
            _state.value =
                ChatGptAuthState.SignedIn(credentials.email, credentials.isPlanUsageEnabled)
        } catch (e: Throwable) {
            _state.value = previousState.takeIf { it !is ChatGptAuthState.SigningIn && !newAccount }
                ?: ChatGptAuthState.SignedOut
            throw e
        } finally {
            server?.close()
            ChatGptSignInService.stop(context)
        }
    }

    /** A usable access token, refreshed when it's about to expire. */
    suspend fun accessToken(): String = tokenMutex.withLock {
        val credentials = store.load() ?: throw ChatGptAuthException("Not signed in to ChatGPT.")
        if (System.currentTimeMillis() < credentials.expiresAtMillis - 5 * 60 * 1000L) {
            return credentials.accessToken
        }
        // A rotated refresh token must be stored once it's issued, so don't let cancellation
        // interrupt the exchange halfway.
        withContext(NonCancellable) {
            val tokens = try {
                postForm(
                    FormBody.Builder()
                        .add("grant_type", "refresh_token")
                        .add("client_id", credentials.clientId)
                        .add("refresh_token", credentials.refreshToken)
                        .add("resource", ChatGptAuthConfig.resource)
                        .build()
                )
            } catch (e: TokenEndpointException) {
                if (e.isTerminalRefreshError) {
                    store.clearTokens()
                    _state.value = ChatGptAuthState.SignedOut
                    throw ChatGptAuthException("Your ChatGPT session expired. Please sign in again.")
                }
                throw e
            }
            val refreshed = credentialsFrom(
                tokens,
                credentials.clientId,
                refreshTokenFallback = credentials.refreshToken,
                previous = credentials
            )
            if (refreshed.subject != credentials.subject) {
                throw ChatGptAuthException("ChatGPT returned a session for a different account.")
            }
            store.save(refreshed)
            refreshed.accessToken
        }
    }

    /** The API rejected our token (e.g. the app was disconnected in ChatGPT settings). */
    fun onUnauthorized() {
        store.clearTokens()
        _state.value = ChatGptAuthState.SignedOut
    }

    /**
     * Revokes the renewable session, then forgets the tokens (but keeps the client and host IDs).
     * Returns false if revocation couldn't be confirmed.
     */
    suspend fun signOut(): Boolean = tokenMutex.withLock {
        val credentials = store.load()
        var revoked = credentials == null
        if (credentials != null) {
            revoked = withContext(Dispatchers.IO) { revoke(credentials) }
        }
        store.clearTokens()
        _state.value = ChatGptAuthState.SignedOut
        revoked
    }

    private suspend fun revoke(credentials: ChatGptCredentials): Boolean {
        repeat(3) { attempt ->
            try {
                val revocationUrl = http.newCall(
                    Request.Builder().url(ChatGptAuthConfig.openIdConfigurationUrl).build()
                ).execute().use { response ->
                    JsonParser.parseString(response.body.string()).asJsonObject
                        .get("revocation_endpoint")?.asString
                } ?: return false
                val status = http.newCall(
                    Request.Builder().url(revocationUrl).post(
                        FormBody.Builder()
                            .add("token", credentials.refreshToken)
                            .add("token_type_hint", "refresh_token")
                            .add("client_id", credentials.clientId)
                            .build()
                    ).build()
                ).execute().use { it.code }
                if (status == 200) return true
                if (status < 500) return false
            } catch (e: Exception) {
                Timber.w(e, "ChatGptAuthManager: revocation attempt failed")
            }
            kotlinx.coroutines.delay(1000L * (attempt + 1))
        }
        return false
    }

    private class TokenEndpointException(val code: String?, message: String) : Exception(message) {
        val isTerminalRefreshError: Boolean
            get() = code in setOf(
                "invalid_grant", "invalid_refresh_token", "token_expired",
                "refresh_token_expired", "refresh_token_invalidated", "refresh_token_reused"
            )
    }

    private suspend fun postForm(body: FormBody): JsonObject = withContext(Dispatchers.IO) {
        val request = Request.Builder().url(ChatGptAuthConfig.tokenUrl).post(body).build()
        http.newCall(request).execute().use { response ->
            val text = response.body.string()
            val json = runCatching { JsonParser.parseString(text).asJsonObject }.getOrNull()
            if (!response.isSuccessful || json == null) {
                val error = json?.get("error")?.takeIf { it.isJsonPrimitive }?.asString
                val description =
                    json?.get("error_description")?.takeIf { it.isJsonPrimitive }?.asString
                throw TokenEndpointException(
                    error,
                    "ChatGPT sign-in failed (${response.code}): ${description ?: error ?: text.take(200)}"
                )
            }
            json
        }
    }

    private fun credentialsFrom(
        tokens: JsonObject,
        clientId: String,
        refreshTokenFallback: String?,
        previous: ChatGptCredentials? = null,
    ): ChatGptCredentials {
        val idToken = tokens.get("id_token")?.asString ?: previous?.idToken
            ?: throw ChatGptAuthException("Token response had no ID token.")
        val claims = jwtPayload(idToken)
        val scopes = tokens.get("scope")?.asString?.split(" ")?.filter { it.isNotEmpty() }
            ?: previous?.scopes ?: emptyList()
        val expiresIn = tokens.get("expires_in")?.asLong ?: 3600L
        return ChatGptCredentials(
            email = claims.get("email")?.asString ?: previous?.email,
            subject = claims.get("sub")?.asString ?: previous?.subject
            ?: throw ChatGptAuthException("ID token had no subject."),
            clientId = clientId,
            idToken = idToken,
            accessToken = tokens.get("access_token")?.asString
                ?: throw ChatGptAuthException("Token response had no access token."),
            refreshToken = tokens.get("refresh_token")?.asString ?: refreshTokenFallback
            ?: throw ChatGptAuthException("Token response had no refresh token."),
            scopes = scopes,
            expiresAtMillis = System.currentTimeMillis() + expiresIn * 1000L,
        )
    }

    /**
     * The ID token comes straight from the token endpoint over TLS (pinned to system CAs in the
     * network security config), so per OpenID Connect Core §3.1.3.7(6) we rely on TLS for the
     * issuer and check the remaining claims here.
     */
    private fun validateIdToken(idToken: String, clientId: String, nonce: String) {
        val claims = jwtPayload(idToken)
        if (claims.get("iss")?.asString != ChatGptAuthConfig.issuer) {
            throw ChatGptAuthException("ID token has an unexpected issuer.")
        }
        val audience = claims.get("aud")
        val audienceMatches = when {
            audience == null -> false
            audience.isJsonArray -> audience.asJsonArray.any { it.asString == clientId }
            else -> audience.asString == clientId
        }
        if (!audienceMatches) {
            throw ChatGptAuthException("ID token was issued for a different client.")
        }
        if (claims.get("nonce")?.asString != nonce) {
            throw ChatGptAuthException("ID token doesn't belong to this sign-in.")
        }
        val expiry = claims.get("exp")?.asLong ?: 0L
        if (expiry * 1000L < System.currentTimeMillis()) {
            throw ChatGptAuthException("ID token has expired.")
        }
    }

    private fun jwtPayload(jwt: String): JsonObject {
        val payload = jwt.split(".").getOrNull(1)
            ?: throw ChatGptAuthException("Malformed ID token.")
        val decoded = Base64.decode(payload, Base64.URL_SAFE or Base64.NO_PADDING or Base64.NO_WRAP)
        return JsonParser.parseString(String(decoded)).asJsonObject
    }

    private fun randomToken(bytes: Int): String {
        val buffer = ByteArray(bytes)
        random.nextBytes(buffer)
        return Base64.encodeToString(buffer, Base64.URL_SAFE or Base64.NO_PADDING or Base64.NO_WRAP)
    }

    private fun codeChallenge(verifier: String): String {
        val digest = MessageDigest.getInstance("SHA-256").digest(verifier.toByteArray())
        return Base64.encodeToString(digest, Base64.URL_SAFE or Base64.NO_PADDING or Base64.NO_WRAP)
    }
}
