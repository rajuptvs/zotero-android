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

    /** Tool definitions as sent to the Responses API (the `x-` fields stripped). */
    val apiTools: JsonArray by lazy {
        JsonArray().apply {
            tools.forEach { element ->
                val tool = element.asJsonObject.deepCopy()
                tool.keySet().filter { it.startsWith("x-") }.forEach { tool.remove(it) }
                add(tool)
            }
        }
    }

    fun isWrite(name: String): Boolean = tools.any {
        val tool = it.asJsonObject
        tool.get("name").asString == name && tool.get("x-write")?.asBoolean == true
    }

    fun isKnown(name: String): Boolean = tools.any { it.asJsonObject.get("name").asString == name }
}
