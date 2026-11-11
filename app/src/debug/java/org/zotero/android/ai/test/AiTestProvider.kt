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
 *   ... --method pdfrender --extra args64:s:<base64 {"pdf", "page", "region", "max_width"}>
 *   ... --method batch --extra args64:s:<base64 {"calls": [{"method", "arg", "args"}, ...]}>  (one round trip)
 *   ... --method semantic --extra args64:s:<base64 {"action": "status"|"enable"|"tokenize"|"embed"|"rank"|"useIndex", ...}>
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
        fun semanticSearch(): org.zotero.android.ai.semantic.SemanticSearch
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
        // "batch" runs many calls in one adb round trip: {"calls": [{"method", "arg", "args"}]} → {"results": [...]}.
        val result: JsonObject = if (method == "batch") {
            JsonObject().apply {
                add("results", JsonArray().apply {
                    args.getAsJsonArray("calls").forEach { c ->
                        val o = c.asJsonObject
                        add(dispatch(o.get("method").asString, o.get("arg")?.takeUnless { it.isJsonNull }?.asString,
                            o.get("args")?.takeIf { it.isJsonObject }?.asJsonObject ?: JsonObject()))
                    }
                })
            }
        } else {
            dispatch(method, arg, args)
        }
        // Binder replies are small: a large result (a page image) goes to a file run.py reads.
        val text = result.toString()
        if (text.length > 200_000) {
            val file = java.io.File(context!!.cacheDir, "aitest-result-${java.util.UUID.randomUUID()}.json")
            file.writeText(text)
            return Bundle().apply { putString("result", JsonObject().apply { addProperty("_file", file.absolutePath) }.toString()) }
        }
        return Bundle().apply { putString("result", text) }
    }

    private fun dispatch(method: String, arg: String?, args: JsonObject): JsonObject = when (method) {
            "tool" -> runBlocking { deps.zoteroTools().execute(arg.orEmpty(), args) }

            "describe" -> runBlocking {
                JsonObject().apply { addProperty("description", deps.zoteroTools().describeWrite(arg.orEmpty(), args)) }
            }

            // The chat's MCP client (ai/spec/mcp-client.md), compared with desktop by run.py.
            "mcpremote" -> runBlocking { mcpRemoteTest(args) }

            // Renders a PDF sent by run.py (same code as zotero_get_page_image).
            "pdfrender" -> {
                val file = java.io.File(context!!.cacheDir, "aitest-${java.util.UUID.randomUUID()}.pdf")
                file.writeBytes(android.util.Base64.decode(args.get("pdf").asString, android.util.Base64.DEFAULT))
                try {
                    val image = org.zotero.android.ai.tools.PdfPages.render(file, args.get("page").asInt, args.get("region"), args.get("max_width"))
                    JsonObject().apply {
                        add("result", JsonObject().apply {
                            addProperty("pageCount", image.pageCount)
                            addProperty("width", image.width)
                            addProperty("height", image.height)
                        })
                        addProperty("image", android.util.Base64.encodeToString(image.jpeg, android.util.Base64.NO_WRAP))
                    }
                } catch (e: org.zotero.android.ai.tools.ToolError) {
                    JsonObject().apply {
                        add("result", JsonObject().apply {
                            addProperty("error", e.code)
                            addProperty("message", e.message)
                        })
                    }
                } finally {
                    file.delete()
                }
            }

            // Semantic search checks (ai/spec/semantic.md, Conformance), run by run.py.
            "semantic" -> runBlocking { semanticTest(args) }

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

    private suspend fun semanticTest(args: JsonObject): JsonObject {
        val search = deps.semanticSearch()
        val model = search.model
        suspend fun status(): JsonObject {
            val (state, error) = model.status()
            val index = deps.zoteroTools().semanticIndexStatus()
            return JsonObject().apply {
                addProperty("enabled", model.enabled)
                addProperty("model", state)
                addProperty("modelError", error)
                add("index", index ?: com.google.gson.JsonNull.INSTANCE)
                add("build", com.google.gson.JsonNull.INSTANCE)
            }
        }
        fun text() = args.get("text")?.asString.orEmpty()
        return try {
            when (args.get("action")?.asString) {
                "status" -> status()
                "enable" -> {
                    model.setEnabled(args.get("on")?.asBoolean == true)
                    if (!model.enabled) search.release()
                    status()
                }
                "tokenize" -> JsonObject().apply {
                    add("ids", JsonArray().apply { search.tokenize(text()).forEach { add(it) } })
                }
                "embed" -> JsonObject().apply {
                    add("embedding", JsonArray().apply { search.embed(text()).forEach { add(it) } })
                }
                "rank" -> {
                    val index = search.index(java.io.File(args.get("indexPath").asString))
                    val rows = index.rank(search.embed(text()), args.get("top")?.asInt ?: 20)
                    JsonObject().apply {
                        add("rows", JsonArray().apply {
                            rows.forEach { (row, score) ->
                                add(JsonObject().apply {
                                    addProperty("row", row)
                                    addProperty("itemKey", index.chunks[row].itemKey)
                                    addProperty("score", score)
                                })
                            }
                        })
                    }
                }
                "useIndex" -> {
                    search.overrideIndex = args.get("path")?.takeUnless { it.isJsonNull }?.asString?.let { java.io.File(it) }
                    status()
                }
                else -> JsonObject().apply {
                    addProperty("error", "invalid_argument")
                    addProperty("message", "Unknown action.")
                }
            }
        } catch (e: kotlinx.coroutines.CancellationException) {
            throw e
        } catch (e: Exception) {
            // An unusable index or the model not downloaded is "unavailable"; anything else "failed".
            val unavailable = e is org.zotero.android.ai.semantic.SemanticIndex.Unusable || e is IllegalStateException
            JsonObject().apply {
                addProperty("error", if (unavailable) "unavailable" else "failed")
                addProperty("message", e.message ?: e.javaClass.simpleName)
            }
        }
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
                    else remote.call(entry, callArgs).let { called ->
                        addProperty("output", called.output)
                        add("images", JsonArray().apply {
                            called.images.forEach { image ->
                                add(JsonObject().apply {
                                    addProperty("mimeType", image.mimeType)
                                    addProperty("data", image.data)
                                })
                            }
                        })
                    }
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
