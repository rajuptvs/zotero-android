package org.zotero.android.ai.tools

import android.content.Context
import com.google.gson.JsonArray
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import javax.inject.Inject
import javax.inject.Singleton

/**
 * The tool definitions and prompts shared with the other Zotero AI clients (`ai/spec` in the repo,
 * bundled as assets). Keeping them in one place is what keeps the clients consistent.
 */
@Singleton
class ZoteroToolSpec @Inject constructor(context: Context) {
    private val tools: JsonArray
    private val prompts: JsonObject

    init {
        val assets = context.assets
        val spec = assets.open("ai/spec/tools.json").bufferedReader().use { it.readText() }
        tools = JsonParser.parseString(spec).asJsonObject.getAsJsonArray("tools")
        prompts = JsonParser.parseString(
            assets.open("ai/spec/prompts.json").bufferedReader().use { it.readText() }
        ).asJsonObject
    }

    val readerInstructions: String get() = prompts.get("reader").asString
    val libraryInstructions: String get() = prompts.get("library").asString

    /** Tools the chat offers the model (MCP-only tools such as zotero_await_approval left out). */
    private val chatTools: List<JsonObject> by lazy {
        tools.map { it.asJsonObject }.filter { it.get("x-mcp-only")?.asBoolean != true }
    }

    /** Tool definitions as sent to the Responses API (the `x-` fields stripped). */
    val apiTools: JsonArray by lazy {
        JsonArray().apply {
            chatTools.forEach { element ->
                val tool = element.asJsonObject.deepCopy()
                tool.keySet().filter { it.startsWith("x-") }.forEach { tool.remove(it) }
                add(tool)
            }
        }
    }

    private fun tool(name: String): JsonObject? =
        tools.map { it.asJsonObject }.firstOrNull { it.get("name").asString == name }

    val toolNames: List<String> get() = tools.map { it.asJsonObject.get("name").asString }

    fun isWrite(name: String): Boolean = tool(name)?.get("x-write")?.asBoolean == true

    /** A tool the chat may call. */
    fun isKnown(name: String): Boolean = chatTools.any { it.get("name").asString == name }

    /** Whether a call needs the user's decision (ai/spec/tools.md, "Write tools"). */
    fun shouldAsk(name: String, autoApprove: Boolean): Boolean {
        val tool = tool(name) ?: return false
        if (tool.get("x-write")?.asBoolean != true) return false
        return tool.get("x-always-ask")?.asBoolean == true || !autoApprove
    }
}
