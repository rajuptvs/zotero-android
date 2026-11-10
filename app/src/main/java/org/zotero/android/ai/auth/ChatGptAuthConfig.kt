package org.zotero.android.ai.auth

/**
 * Sign in with ChatGPT, open-source "ChatGPT plan usage" flow.
 * https://developers.openai.com/siwc/token-sharing-open-source
 */
object ChatGptAuthConfig {
    const val issuer = "https://auth.openai.com"
    const val authorizeUrl = "https://auth.openai.com/api/accounts/authorize"
    const val tokenUrl = "https://auth.openai.com/api/accounts/oauth/token"
    const val openIdConfigurationUrl = "https://auth.openai.com/.well-known/openid-configuration"
    const val resource = "https://api.openai.com/v1"
    const val apiBaseUrl = "https://api.openai.com/v1"

    // First-time registration entrypoint. Never saved or used for token exchange.
    const val dynamicRegistrationClientId = "dynamic_agent_client"
    const val agentNameHint = "Zotero for Android"

    const val scopes =
        "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
    const val planUsageScope = "chatgpt.tokens.use.direct"

    // Only the port may vary between sign-ins; scheme, host and path must stay the same.
    const val callbackHost = "127.0.0.1"
    const val callbackPath = "/auth/callback"
    const val preferredCallbackPort = 1455

    const val usageSettingsUrl = "https://chatgpt.com/settings/usage"
}
