package org.zotero.android.ai.test

import android.content.ContentProvider
import android.content.ContentValues
import android.database.Cursor
import android.net.Uri
import android.os.Bundle
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import dagger.hilt.EntryPoint
import dagger.hilt.InstallIn
import dagger.hilt.android.EntryPointAccessors
import dagger.hilt.components.SingletonComponent
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeoutOrNull
import com.google.gson.JsonArray
import org.zotero.android.ai.chat.ChatMarkdown
import org.zotero.android.ai.chat.ChatNoteFormat
import org.zotero.android.ai.tools.ZoteroTools
import org.zotero.android.sync.Libraries
import org.zotero.android.sync.SyncKind
import org.zotero.android.sync.SyncScheduler

/**
 * Debug-only test hook (guarded by the DUMP permission, which only the shell has):
 *   adb shell content call --uri content://org.zotero.android.debug.aitest --method tool \
 *       --arg zotero_get_collections --extra args64:s:<base64 JSON>
 *   adb shell content call --uri content://org.zotero.android.debug.aitest --method sync
 *   ... --method describe --arg <write tool> --extra args64:s:<base64 JSON>  (approval card text)
 *   ... --method notes --extra args64:s:<base64 {"html"}>  (how a conversation note is read back)
 *   ... --method markdown --extra args64:s:<base64 {"markdown"}>  (answer → note HTML)
 *   ... --method policy  (shouldAsk for every tool)
 *   ... --method mcpremote --extra args64:s:<base64 {"action": "set"|"tools"|"describe"|"call", ...}>
 * Writes run without the approval card, like the desktop plugin's test endpoint.
 */
class AiTestProvider : ContentProvider() {

    @EntryPoint
    @InstallIn(SingletonComponent::class)
    interface Deps {
        fun zoteroTools(): ZoteroTools
        fun toolSpec(): org.zotero.android.ai.tools.ZoteroToolSpec
        fun mcpRemote(): org.zotero.android.ai.mcp.McpRemote
        fun syncScheduler(): SyncScheduler
    }

    private val deps: Deps
        get() = EntryPointAccessors.fromApplication(context!!.applicationContext, Deps::class.java)

    override fun onCreate(): Boolean = true

    override fun call(method: String, arg: String?, extras: Bundle?): Bundle {
        // JSON is passed base64-encoded: `content call` can't parse ':' inside a binding.
        val args = extras?.getString("args64")
            ?.let { String(android.util.Base64.decode(it, android.util.Base64.DEFAULT)) }
            ?.let { JsonParser.parseString(it).asJsonObject }
            ?: JsonObject()
        val result: JsonObject = when (method) {
            "tool" -> runBlocking { deps.zoteroTools().execute(arg.orEmpty(), args) }

            "describe" -> runBlocking {
                JsonObject().apply { addProperty("description", deps.zoteroTools().describeWrite(arg.orEmpty(), args)) }
            }

            // The chat's MCP client (ai/spec/mcp-client.md), compared with desktop by run.py.
            "mcpremote" -> runBlocking { mcpRemoteTest(args) }

            // The approval rule for every tool, with auto-approve off and on (compared with desktop).
            "policy" -> JsonObject().apply {
                val spec = deps.toolSpec()
                spec.toolNames.forEach { name ->
                    add(name, JsonObject().apply {
                        addProperty("ask", spec.shouldAsk(name, false))
                        addProperty("askWithAutoApprove", spec.shouldAsk(name, true))
                    })
                }
            }

            "markdown" -> JsonObject().apply {
                addProperty("html", ChatMarkdown.toHtml(args.get("markdown")?.asString.orEmpty()))
            }

            "notes" -> {
                val html = args.get("html")?.asString.orEmpty()
                JsonObject().apply {
                    add("turns", JsonArray().apply {
                        ChatNoteFormat.decode(html).forEach { turn ->
                            add(JsonObject().apply {
                                addProperty("role", turn.role)
                                addProperty("text", turn.text)
                            })
                        }
                    })
                    addProperty("isLibraryChat", ChatNoteFormat.isLibraryChat(html))
                    addProperty("attachmentKey", ChatNoteFormat.attachmentKey(html))
                }
            }

            "sync" -> runBlocking {
                val scheduler = deps.syncScheduler()
                // A cold process (started just for this call) hasn't set up syncing yet.
                if (!scheduler.isInitialized) {
                    return@runBlocking JsonObject().apply {
                        addProperty("error", "Zotero isn't running; open the app first.")
                    }
                }
                scheduler.request(SyncKind.normal, Libraries.all)
                // Wait for the sync to start (if it does) and then to finish.
                withTimeoutOrNull(5_000) { scheduler.inProgress.first { it } }
                val finished = withTimeoutOrNull(120_000) { scheduler.inProgress.first { !it } } != null
                JsonObject().apply { addProperty("synced", finished) }
            }

            else -> JsonObject().apply { addProperty("error", "unknown method $method") }
        }
        return Bundle().apply { putString("result", result.toString()) }
    }

    private suspend fun mcpRemoteTest(args: JsonObject): JsonObject {
        val remote = deps.mcpRemote()
        return when (args.get("action")?.asString) {
            "set" -> {
                remote.servers().forEach { remote.remove(it.id) }
                args.getAsJsonArray("servers")?.forEach { e ->
                    val o = e.asJsonObject
                    remote.save(
                        org.zotero.android.ai.mcp.McpServer(
                            id = o.get("id").asString, name = o.get("name").asString, url = o.get("url").asString,
                            enabled = o.get("enabled")?.asBoolean ?: true, autoReadOnly = o.get("autoReadOnly")?.asBoolean ?: false,
                        ),
                        o.get("authorization")?.takeIf { !it.isJsonNull }?.asString,
                    )
                }
                JsonObject().apply { addProperty("servers", remote.servers().size) }
            }
            "tools" -> remote.toolsForTurn().let { turn ->
                JsonObject().apply {
                    add("tools", turn.tools)
                    add("unavailable", com.google.gson.JsonArray().apply { turn.unavailable.forEach { add(it) } })
                    add("shouldAsk", JsonObject().apply { turn.index.forEach { (name, entry) -> addProperty(name, remote.shouldAsk(entry)) } })
                }
            }
            "describe", "call" -> {
                val turn = remote.toolsForTurn()
                val entry = turn.index[args.get("name").asString]
                    ?: return JsonObject().apply { addProperty("error", "unknown tool") }
                val callArgs = args.getAsJsonObject("args") ?: JsonObject()
                JsonObject().apply {
                    if (args.get("action").asString == "describe") addProperty("description", remote.describe(entry, callArgs))
                    else addProperty("output", remote.call(entry, callArgs))
                }
            }
            else -> JsonObject().apply { addProperty("error", "unknown action") }
        }
    }

    override fun query(uri: Uri, p: Array<out String>?, s: String?, a: Array<out String>?, o: String?): Cursor? = null
    override fun getType(uri: Uri): String? = null
    override fun insert(uri: Uri, values: ContentValues?): Uri? = null
    override fun delete(uri: Uri, s: String?, a: Array<out String>?): Int = 0
    override fun update(uri: Uri, values: ContentValues?, s: String?, a: Array<out String>?): Int = 0
}
