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
 * Writes run without the approval card, like the desktop plugin's test endpoint.
 */
class AiTestProvider : ContentProvider() {

    @EntryPoint
    @InstallIn(SingletonComponent::class)
    interface Deps {
        fun zoteroTools(): ZoteroTools
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

    override fun query(uri: Uri, p: Array<out String>?, s: String?, a: Array<out String>?, o: String?): Cursor? = null
    override fun getType(uri: Uri): String? = null
    override fun insert(uri: Uri, values: ContentValues?): Uri? = null
    override fun delete(uri: Uri, s: String?, a: Array<out String>?): Int = 0
    override fun update(uri: Uri, values: ContentValues?, s: String?, a: Array<out String>?): Int = 0
}
