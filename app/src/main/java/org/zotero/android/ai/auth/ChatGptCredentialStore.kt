package org.zotero.android.ai.auth

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import androidx.core.content.edit
import com.google.gson.Gson
import timber.log.Timber
import java.security.KeyStore
import java.util.UUID
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec
import javax.inject.Inject
import javax.inject.Singleton

data class ChatGptCredentials(
    val email: String?,
    val subject: String,
    val clientId: String,
    val idToken: String,
    val accessToken: String,
    val refreshToken: String,
    val scopes: List<String>,
    val expiresAtMillis: Long,
) {
    val isPlanUsageEnabled: Boolean
        get() = scopes.contains(ChatGptAuthConfig.planUsageScope)
}

/**
 * Keeps ChatGPT tokens out of plain SharedPreferences: the credential record is encrypted with an
 * AES-GCM key that lives in the Android Keystore and never leaves it.
 *
 * The host ID and issued client ID are not secrets and are kept in the clear so they survive a
 * sign-out, as the SIWC docs require.
 */
@Singleton
class ChatGptCredentialStore @Inject constructor(
    context: Context,
    private val gson: Gson,
) {
    private val prefs = context.getSharedPreferences("ChatGptAuth", Context.MODE_PRIVATE)

    fun hostId(): String {
        prefs.getString(keyHostId, null)?.let { return it }
        val hostId = "urn:uuid:${UUID.randomUUID()}"
        prefs.edit(commit = true) { putString(keyHostId, hostId) }
        return hostId
    }

    /**
     * The client registered for one ChatGPT account and workspace. It outlives sign-out so a later
     * sign-in reuses it, and its subject lets us refuse a sign-in that comes back as someone else.
     */
    data class Registration(val clientId: String, val subject: String?, val email: String?)

    val registration: Registration?
        get() {
            val clientId = prefs.getString(keyClientId, null) ?: return null
            return Registration(
                clientId = clientId,
                subject = prefs.getString(keySubject, null),
                email = prefs.getString(keyEmail, null),
            )
        }

    fun saveRegistration(clientId: String) {
        prefs.edit(commit = true) { putString(keyClientId, clientId) }
    }

    /** Forget the registration, so the next sign-in registers a new client (another account). */
    fun clearRegistration() {
        prefs.edit(commit = true) {
            remove(keyClientId)
            remove(keySubject)
            remove(keyEmail)
            remove(keyIdTokenHint)
        }
    }

    /** ID token from the last successful sign-in, sent back as `id_token_hint` on reauthorization. */
    val idTokenHint: String?
        get() = prefs.getString(keyIdTokenHint, null)?.let {
            try {
                decrypt(it)
            } catch (e: Exception) {
                null
            }
        }

    var selectedModel: String?
        get() = prefs.getString(keyModel, null)
        set(value) = prefs.edit(commit = true) { putString(keyModel, value) }

    /** Attach text selected in the reader to the next message while the AI panel is open. */
    var autoAttachSelection: Boolean
        get() = prefs.getBoolean(keyAutoAttach, true)
        set(value) = prefs.edit { putBoolean(keyAutoAttach, value) }

    fun reasoningEffort(model: String): String? = prefs.getString("$keyEffortPrefix$model", null)

    fun setReasoningEffort(model: String, effort: String?) =
        prefs.edit { putString("$keyEffortPrefix$model", effort) }

    fun load(): ChatGptCredentials? {
        val encrypted = prefs.getString(keyCredentials, null) ?: return null
        return try {
            gson.fromJson(decrypt(encrypted), ChatGptCredentials::class.java)
        } catch (e: javax.crypto.AEADBadTagException) {
            // The key no longer matches (e.g. restored from a backup): these can never be read.
            Timber.e(e, "ChatGptCredentialStore: credentials can't be decrypted, clearing them")
            clearTokens()
            null
        } catch (e: Exception) {
            Timber.e(e, "ChatGptCredentialStore: can't read credentials")
            null
        }
    }

    fun save(credentials: ChatGptCredentials) {
        prefs.edit(commit = true) {
            putString(keyCredentials, encrypt(gson.toJson(credentials)))
            putString(keyClientId, credentials.clientId)
            putString(keySubject, credentials.subject)
            putString(keyEmail, credentials.email)
            putString(keyIdTokenHint, encrypt(credentials.idToken))
        }
    }

    fun clearTokens() {
        prefs.edit(commit = true) { remove(keyCredentials) }
    }

    private fun secretKey(): SecretKey {
        val keyStore = KeyStore.getInstance(androidKeyStore).apply { load(null) }
        (keyStore.getKey(keyAlias, null) as? SecretKey)?.let { return it }
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, androidKeyStore)
        generator.init(
            KeyGenParameterSpec.Builder(
                keyAlias,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT
            )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build()
        )
        return generator.generateKey()
    }

    private fun encrypt(plain: String): String {
        val cipher = Cipher.getInstance(transformation)
        cipher.init(Cipher.ENCRYPT_MODE, secretKey())
        val iv = Base64.encodeToString(cipher.iv, Base64.NO_WRAP)
        val body = Base64.encodeToString(cipher.doFinal(plain.toByteArray()), Base64.NO_WRAP)
        return "$iv:$body"
    }

    private fun decrypt(encrypted: String): String {
        val (iv, body) = encrypted.split(":", limit = 2)
        val cipher = Cipher.getInstance(transformation)
        cipher.init(
            Cipher.DECRYPT_MODE,
            secretKey(),
            GCMParameterSpec(128, Base64.decode(iv, Base64.NO_WRAP))
        )
        return String(cipher.doFinal(Base64.decode(body, Base64.NO_WRAP)))
    }

    private companion object {
        const val androidKeyStore = "AndroidKeyStore"
        const val keyAlias = "zotero_chatgpt_credentials"
        const val transformation = "AES/GCM/NoPadding"
        const val keyHostId = "hostId"
        const val keyClientId = "clientId"
        const val keyEmail = "email"
        const val keySubject = "subject"
        const val keyIdTokenHint = "idTokenHint"
        const val keyModel = "model"
        const val keyAutoAttach = "autoAttachSelection"
        const val keyEffortPrefix = "effort."
        const val keyCredentials = "credentials"
    }
}
