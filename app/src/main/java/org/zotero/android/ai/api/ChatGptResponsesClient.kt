package org.zotero.android.ai.api

import com.google.gson.Gson
import com.google.gson.JsonArray
import com.google.gson.JsonElement
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.flowOn
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.zotero.android.ai.auth.ChatGptAuthConfig
import org.zotero.android.ai.auth.ChatGptAuthManager
import java.util.concurrent.TimeUnit
import javax.inject.Inject
import javax.inject.Singleton

data class ChatGptModel(
    val slug: String,
    val displayName: String,
    /** Reasoning efforts the model accepts, in the server's order; empty if it has none. */
    val reasoningEfforts: List<String> = emptyList(),
    val defaultReasoningEffort: String? = null,
    val supportsImages: Boolean = true,
)

/** An image sent with a message, as a `data:` URL. [label] says where it came from. */
data class ChatImage(val dataUrl: String, val label: String)

data class ChatTurn(
    val role: String,
    val text: String,
    val images: List<ChatImage> = emptyList(),
)

class ChatGptApiException(message: String, val code: String? = null) : Exception(message)

/** What the stream is doing, for the status line. */
enum class ChatStreamPhase { Thinking, Writing }

sealed interface ChatStreamEvent {
    data class Phase(val phase: ChatStreamPhase) : ChatStreamEvent
    data class Text(val delta: String) : ChatStreamEvent
}

/**
 * Responses API calls billed to the user's ChatGPT plan. In this flow every request must use
 * store=false and stream=true, and history is resent each turn (no previous_response_id).
 * https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference
 */
@Singleton
class ChatGptResponsesClient @Inject constructor(
    private val authManager: ChatGptAuthManager,
    private val gson: Gson,
) {
    private val http = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(5, TimeUnit.MINUTES)
        .build()

    suspend fun models(): List<ChatGptModel> = withContext(Dispatchers.IO) {
        val request = Request.Builder()
            .url("${ChatGptAuthConfig.apiBaseUrl}/models")
            .header("Authorization", "Bearer ${authManager.accessToken()}")
            .build()
        http.newCall(request).execute().use { response ->
            val text = response.body.string()
            if (!response.isSuccessful) {
                throw errorFrom(response.code, text)
            }
            val models = JsonParser.parseString(text).asJsonObject
                .get("models")?.takeIf { it.isJsonArray }?.asJsonArray
                ?: JsonArray()
            models.mapNotNull { element ->
                val model = element.takeIf { it.isJsonObject }?.asJsonObject
                    ?: return@mapNotNull null
                if (model.string("visibility") != "list") return@mapNotNull null
                val slug = model.string("slug") ?: return@mapNotNull null
                ChatGptModel(
                    slug = slug,
                    displayName = model.string("display_name") ?: slug,
                    reasoningEfforts = reasoningEfforts(model),
                    defaultReasoningEffort = model.string("default_reasoning_level")
                        ?: model.string("default_reasoning_effort"),
                    supportsImages = model.get("input_modalities")
                        ?.takeIf { it.isJsonArray }?.asJsonArray
                        ?.any { it.isJsonPrimitive && it.asString == "image" }
                        ?: true,
                )
            }
        }
    }

    /**
     * The catalog lists supported reasoning levels per model; accept plain strings or
     * `{effort: ...}` objects under either name.
     */
    private fun reasoningEfforts(model: JsonObject): List<String> {
        val levels = model.get("supported_reasoning_levels")
            ?: model.get("supported_reasoning_efforts")
            ?: return emptyList()
        if (!levels.isJsonArray) return emptyList()
        return levels.asJsonArray.mapNotNull { level ->
            when {
                level.isJsonPrimitive -> level.asString
                level.isJsonObject -> level.asJsonObject.string("effort")
                    ?: level.asJsonObject.string("id")

                else -> null
            }
        }
    }

    /**
     * Streams the answer. Emits [ChatStreamEvent.Phase] when the model starts reasoning or
     * writing, then text deltas. Completes only after `response.completed`.
     */
    fun stream(
        model: String,
        instructions: String,
        context: String?,
        turns: List<ChatTurn>,
        reasoningEffort: String? = null,
    ): Flow<ChatStreamEvent> = flow {
        val input = JsonArray()
        if (context != null) {
            input.add(textMessage(role = "developer", text = context))
        }
        turns.forEach { input.add(message(it)) }
        val body = JsonObject().apply {
            addProperty("model", model)
            addProperty("instructions", instructions)
            add("input", input)
            addProperty("store", false)
            addProperty("stream", true)
            if (reasoningEffort != null) {
                add("reasoning", JsonObject().apply { addProperty("effort", reasoningEffort) })
            }
        }
        val request = Request.Builder()
            .url("${ChatGptAuthConfig.apiBaseUrl}/responses")
            .header("Authorization", "Bearer ${authManager.accessToken()}")
            .header("Accept", "text/event-stream")
            .post(gson.toJson(body).toRequestBody("application/json".toMediaType()))
            .build()

        val call = http.newCall(request)
        // readUtf8Line() blocks, so cancellation has to close the connection to stop the stream.
        val cancelHandle = currentCoroutineContext()[Job]?.invokeOnCompletion { call.cancel() }
        try {
            call.execute().use { response ->
                if (!response.isSuccessful) {
                    throw errorFrom(response.code, response.body.string())
                }
                val source = response.body.source()
                var completed = false
                var writing = false
                emit(ChatStreamEvent.Phase(ChatStreamPhase.Thinking))
                while (!completed) {
                    currentCoroutineContext().ensureActive()
                    val line = source.readUtf8Line() ?: break
                    if (!line.startsWith("data:")) continue
                    val data = line.removePrefix("data:").trim()
                    if (data.isEmpty() || data == "[DONE]") continue
                    val event = JsonParser.parseString(data).asJsonObject
                    when (event.string("type")) {
                        "response.output_text.delta" -> {
                            if (!writing) {
                                writing = true
                                emit(ChatStreamEvent.Phase(ChatStreamPhase.Writing))
                            }
                            emit(ChatStreamEvent.Text(event.string("delta") ?: ""))
                        }

                        "response.completed" -> completed = true
                        "response.incomplete" -> throw ChatGptApiException(
                            "The answer was cut off (${
                                event.obj("response")?.obj("incomplete_details")
                                    ?.string("reason") ?: "incomplete"
                            })."
                        )

                        "response.failed", "error" -> {
                            val error = event.obj("response")?.obj("error")
                                ?: event.obj("error")
                                ?: event
                            throw apiError(code = error.string("code"), message = error.string("message"))
                        }
                    }
                }
                if (!completed) {
                    throw ChatGptApiException("The connection closed before the answer finished.")
                }
            }
        } finally {
            cancelHandle?.dispose()
        }
    }.flowOn(Dispatchers.IO)

    private fun textMessage(role: String, text: String): JsonObject = JsonObject().apply {
        addProperty("role", role)
        addProperty("content", text)
    }

    private fun message(turn: ChatTurn): JsonObject {
        if (turn.images.isEmpty() || turn.role != "user") {
            return textMessage(turn.role, turn.text)
        }
        val content = JsonArray()
        content.add(JsonObject().apply {
            addProperty("type", "input_text")
            addProperty("text", turn.text)
        })
        turn.images.forEach { image ->
            content.add(JsonObject().apply {
                addProperty("type", "input_image")
                addProperty("image_url", image.dataUrl)
            })
        }
        return JsonObject().apply {
            addProperty("role", turn.role)
            add("content", content)
        }
    }

    private fun JsonObject.obj(name: String): JsonObject? =
        get(name)?.takeIf { it.isJsonObject }?.asJsonObject

    private fun JsonObject.string(name: String): String? =
        get(name)?.takeIf(JsonElement::isJsonPrimitive)?.asString

    private fun errorFrom(status: Int, body: String): ChatGptApiException {
        if (status == 401) {
            // Token revoked or the app was disconnected in ChatGPT settings.
            authManager.onUnauthorized()
        }
        val json = runCatching { JsonParser.parseString(body).asJsonObject }.getOrNull()
        val error = json?.obj("error")
        if (error != null) {
            return apiError(code = error.string("code"), message = error.string("message"))
        }
        val detail = json?.string("detail")
        return ChatGptApiException("ChatGPT request failed ($status): ${detail ?: body.take(200)}")
    }

    private fun apiError(code: String?, message: String?): ChatGptApiException {
        val friendly = when (code) {
            "subscription_sharing_usage_limit_exceeded" ->
                "You've reached the usage limit for this app. Check ${ChatGptAuthConfig.usageSettingsUrl}"

            "subscription_sharing_user_not_eligible" ->
                "Your ChatGPT plan can't be used in this app."

            "subscription_sharing_usage_unavailable", "subscription_sharing_user_unavailable" ->
                "ChatGPT couldn't check your plan usage right now. Try again shortly."

            "subscription_sharing_unsupported_capability" ->
                "This model doesn't support that request${message?.let { ": $it" } ?: "."}"

            else -> message ?: code ?: "ChatGPT request failed."
        }
        return ChatGptApiException(friendly, code)
    }
}
