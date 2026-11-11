package org.zotero.android.ai.tools

import android.text.Html
import com.google.gson.JsonArray
import com.google.gson.JsonElement
import com.google.gson.JsonNull
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import io.realm.Realm
import io.realm.kotlin.where
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.async
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import org.zotero.android.ai.chat.ChatMarkdown
import org.zotero.android.architecture.Defaults
import org.zotero.android.attachmentdownloader.AttachmentDownloader
import org.zotero.android.attachmentdownloader.AttachmentDownloaderEventStream
import org.zotero.android.database.objects.Attachment
import org.zotero.android.files.FileStore
import org.zotero.android.sync.AttachmentCreator
import org.zotero.android.architecture.coroutines.Dispatchers
import org.zotero.android.database.DbRequest
import org.zotero.android.database.DbResponseRequest
import org.zotero.android.database.DbWrapperMain
import org.zotero.android.database.objects.FieldKeys
import org.zotero.android.database.objects.ItemTypes
import org.zotero.android.database.objects.RCollection
import org.zotero.android.database.objects.RCustomLibraryType
import org.zotero.android.database.objects.RItem
import org.zotero.android.database.objects.RTypedTag
import org.zotero.android.database.requests.AssignItemsToCollectionsDbRequest
import org.zotero.android.database.requests.CreateCollectionDbRequest
import org.zotero.android.database.requests.CreateNoteDbRequest
import org.zotero.android.database.requests.DeleteItemsFromCollectionDbRequest
import org.zotero.android.database.requests.EditTagsForItemDbRequest
import org.zotero.android.database.requests.MarkItemsAsTrashedDbRequest
import org.zotero.android.database.requests.key
import org.zotero.android.database.requests.library
import org.zotero.android.screens.addbyidentifier.IdentifierLookupController
import org.zotero.android.screens.addbyidentifier.TranslatorLoadedEventStream
import org.zotero.android.screens.addbyidentifier.data.IdentifierLookupMode
import org.zotero.android.sync.KeyGenerator
import org.zotero.android.sync.LibraryIdentifier
import org.zotero.android.sync.Note
import org.zotero.android.sync.SchemaController
import org.zotero.android.sync.Tag
import timber.log.Timber
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import java.util.concurrent.TimeUnit
import javax.inject.Inject
import javax.inject.Singleton

class ToolError(val code: String, message: String) : Exception(message)

/**
 * The Zotero tools the AI can call, implemented against the local library. Semantics and result
 * shapes follow `ai/spec/tools.md` exactly, so every client answers the same way.
 * Writes go through the same database requests as the app's own UI, so they sync normally.
 */
@Singleton
class ZoteroTools @Inject constructor(
    private val dbWrapperMain: DbWrapperMain,
    private val defaults: Defaults,
    private val schemaController: SchemaController,
    private val identifierLookupController: IdentifierLookupController,
    private val translatorLoadedEventStream: TranslatorLoadedEventStream,
    private val dispatchers: Dispatchers,
    private val fileStore: FileStore,
    private val attachmentDownloader: AttachmentDownloader,
    private val attachmentDownloaderEventStream: AttachmentDownloaderEventStream,
) {
    private val libraryId: LibraryIdentifier = LibraryIdentifier.custom(RCustomLibraryType.myLibrary)
    private val http = OkHttpClient.Builder().callTimeout(30, TimeUnit.SECONDS).build()

    /** Runs a tool. Never throws (except cancellation): failures come back as error objects. */
    suspend fun execute(name: String, args: JsonObject): JsonObject = try {
        when (name) {
            "zotero_search_items" -> searchItems(args)
            "zotero_get_item_metadata" -> read { getItemMetadata(it, args.str("item_key")) }
            "zotero_get_item_fulltext" -> getItemFulltext(args)
            "zotero_get_collections" -> read { getCollections(it) }
            "zotero_get_collection_items" -> read { getCollectionItems(it, args) }
            "zotero_get_tags" -> read { getTags(it, args) }
            "zotero_get_recent" -> read { getRecent(it, args) }
            "zotero_get_notes" -> read { getNotes(it, args) }
            "zotero_get_annotations" -> read { getAnnotations(it, args.str("item_key")) }
            "zotero_get_pdf_info" -> getPdfInfo(args)
            "zotero_get_page_image" -> getPageImage(args)
            "zotero_batch_update_tags" -> batchUpdateTags(args)
            "zotero_set_item_collections" -> setItemCollections(args)
            "zotero_create_collection" -> createCollection(args)
            "zotero_create_note" -> createNote(args)
            "zotero_add_item_by_identifier" -> addItemByIdentifier(args)
            "zotero_trash_items" -> trashItems(args)
            else -> throw ToolError("invalid_argument", "Unknown tool $name")
        }
    } catch (e: CancellationException) {
        throw e
    } catch (e: ToolError) {
        error(e.code, e.message ?: e.code)
    } catch (e: Throwable) {
        Timber.e(e, "ZoteroTools: $name failed")
        error("failed", e.message ?: e.javaClass.simpleName)
    }

    /** One line describing what a write would change, for the approval card. */
    suspend fun describeWrite(name: String, args: JsonObject): String = try {
        read { realm ->
            fun titles(keys: List<String>): String {
                val names = keys.map { key ->
                    realm.where<RItem>().key(key, libraryId).findFirst()
                        ?.let { "“${it.baseTitle.ifBlank { it.displayTitle }}”" } ?: key
                }
                return if (names.size <= 3) names.joinToString(", ")
                else names.take(3).joinToString(", ") + " and ${names.size - 3} more"
            }

            fun collectionNames(keys: List<String>) = keys.joinToString(", ") { key ->
                realm.where<RCollection>().key(key, libraryId).findFirst()?.name?.let { "“$it”" } ?: key
            }
            when (name) {
                "zotero_batch_update_tags" -> buildList {
                    val add = args.strList("add_tags")
                    val remove = args.strList("remove_tags")
                    if (add.isNotEmpty()) add("Add tag${if (add.size > 1) "s" else ""} ${add.joinToString(", ")}")
                    if (remove.isNotEmpty()) add("remove ${remove.joinToString(", ")}")
                }.joinToString(" and ").replaceFirstChar { it.uppercase() } + " on ${titles(args.strList("item_keys"))}"

                "zotero_set_item_collections" -> buildList {
                    val add = args.strList("add_to")
                    val remove = args.strList("remove_from")
                    if (add.isNotEmpty()) add("Add to ${collectionNames(add)}")
                    if (remove.isNotEmpty()) add("remove from ${collectionNames(remove)}")
                }.joinToString(" and ").replaceFirstChar { it.uppercase() } + ": ${titles(args.strList("item_keys"))}"

                "zotero_create_collection" -> "Create collection “${args.str("name")}”" +
                        (args.strOrNull("parent_collection_key")
                            ?.let { " inside ${collectionNames(listOf(it))}" } ?: "")

                "zotero_create_note" -> "Create note “${args.str("title")}”" +
                        (args.strOrNull("parent_item_key")?.let { " on ${titles(listOf(it))}" }
                            ?: " (standalone)")

                "zotero_add_item_by_identifier" -> "Add ${args.str("identifier")} to the library" +
                        (args.strOrNull("collection_key")
                            ?.let { " in ${collectionNames(listOf(it))}" } ?: "")

                "zotero_trash_items" -> "Move to trash: ${titles(args.strList("item_keys"))}"
                else -> name
            }
        }
    } catch (e: Throwable) {
        if (e is CancellationException) throw e
        name
    }

    // region Reads

    private suspend fun searchItems(args: JsonObject): JsonObject = read { realm ->
        val words = args.strOrNull("query").orEmpty().lowercase().split(Regex("\\s+"))
            .filter { it.isNotEmpty() }
        val tag = args.strOrNull("tag")
        val collectionKey = args.strOrNull("collection_key")
        val limit = args.limit("limit", default = 20, max = 100)
        var items = regularItems(realm).filter { item ->
            words.all { word -> searchableTexts(item).any { it.contains(word) } }
        }
        if (tag != null) items = items.filter { item -> tagNames(item).contains(tag) }
        if (collectionKey != null) {
            items = items.filter { item ->
                item.collections?.any { it.key == collectionKey && !it.deleted && !it.trash } == true
            }
        }
        val sorted = items.sortedWith(titleOrder)
        JsonObject().apply {
            addProperty("total", sorted.size)
            add("items", JsonArray().apply { sorted.take(limit).forEach { add(summary(it)) } })
        }
    }

    private fun getItemMetadata(realm: Realm, key: String): JsonObject {
        val item = findItem(realm, key)
        val creators = item.creators.sortedBy { it.orderId }
        val children = item.children?.filter { !it.trash && !it.deleted }.orEmpty()
        return JsonObject().apply {
            addProperty("key", item.key)
            addProperty("itemType", item.rawType)
            addProperty("title", title(item))
            add("creators", JsonArray().apply {
                creators.forEach { creator ->
                    add(JsonObject().apply {
                        addProperty("type", creator.rawType)
                        addProperty("name", creatorFullName(creator.firstName, creator.lastName, creator.name))
                    })
                }
            })
            addProperty("date", field(item, FieldKeys.Item.date))
            addProperty("year", year(item))
            addProperty("publication", listOf("publicationTitle", "proceedingsTitle", "bookTitle", "websiteTitle")
                .map { field(item, it) }.firstOrNull { it.isNotEmpty() } ?: "")
            addProperty("doi", field(item, FieldKeys.Item.doi))
            addProperty("url", field(item, FieldKeys.Item.url))
            addProperty("abstract", field(item, FieldKeys.Item.abstractN))
            add("tags", tagsArray(item))
            add("collections", JsonArray().apply {
                item.collections?.filter { !it.deleted && !it.trash }
                    ?.sortedWith(compareBy({ it.name.lowercase() }, { it.key }))
                    ?.forEach { collection ->
                        add(JsonObject().apply {
                            addProperty("key", collection.key)
                            addProperty("name", collection.name)
                        })
                    }
            })
            add("attachments", JsonArray().apply {
                children.filter { it.rawType == ItemTypes.attachment }.sortedWith(titleOrder).forEach {
                    add(JsonObject().apply {
                        addProperty("key", it.key)
                        addProperty("title", title(it))
                        addProperty("contentType", field(it, FieldKeys.Item.Attachment.contentType))
                    })
                }
            })
            add("notes", JsonArray().apply {
                children.filter { it.rawType == ItemTypes.note }.sortedWith(titleOrder).forEach {
                    add(JsonObject().apply {
                        addProperty("key", it.key)
                        addProperty("title", title(it))
                    })
                }
            })
        }
    }

    private suspend fun getItemFulltext(args: JsonObject): JsonObject {
        val maxChars = args.limit("max_chars", default = 20000, max = 100000)
        val attachmentKey = read { realm ->
            resolveTextAttachment(realm, findItem(realm, args.str("item_key")))?.key.orEmpty()
        }.ifEmpty { throw ToolError("not_found", "This item has no PDF, EPUB or snapshot.") }
        val apiKey = defaults.getApiToken() ?: throw ToolError("unavailable", "Not signed in to Zotero.")
        val url = "https://api.zotero.org/users/${defaults.getUserId()}/items/$attachmentKey/fulltext"
        val text = withContext(dispatchers.io) {
            http.newCall(
                Request.Builder().url(url)
                    .header("Zotero-API-Key", apiKey)
                    .header("Zotero-API-Version", "3")
                    .build()
            ).execute().use { response ->
                if (response.code == 404) {
                    throw ToolError("unavailable", "This attachment's text hasn't been indexed yet.")
                }
                if (!response.isSuccessful) {
                    throw ToolError("failed", "Zotero returned ${response.code}.")
                }
                JsonParser.parseString(response.body.string()).asJsonObject.get("content")?.asString.orEmpty()
            }
        }
        return JsonObject().apply {
            addProperty("attachmentKey", attachmentKey)
            addProperty("totalChars", text.length)
            addProperty("truncated", text.length > maxChars)
            addProperty("text", text.take(maxChars))
        }
    }

    private suspend fun getPdfInfo(args: JsonObject): JsonObject {
        val (attachmentKey, file) = pdfFile(args.str("item_key"))
        val count = withContext(dispatchers.io) { PdfPages.pageCount(file) }
        return JsonObject().apply {
            addProperty("attachmentKey", attachmentKey)
            addProperty("pageCount", count)
        }
    }

    /** The image travels beside the result, as MCP content parts under _content (mcp.md, chat.md). */
    private suspend fun getPageImage(args: JsonObject): JsonObject {
        val page = args.get("page")?.takeIf { it.isJsonPrimitive && it.asJsonPrimitive.isNumber }?.asDouble
            ?.takeIf { it == kotlin.math.floor(it) && it in Int.MIN_VALUE.toDouble()..Int.MAX_VALUE.toDouble() }?.toInt()
            ?: throw ToolError("invalid_argument", "page must be integer.")
        val (attachmentKey, file) = pdfFile(args.str("item_key"))
        val image = withContext(dispatchers.io) { PdfPages.render(file, page, args.get("region"), args.get("max_width")) }
        return JsonObject().apply {
            addProperty("attachmentKey", attachmentKey)
            addProperty("page", page)
            addProperty("pageCount", image.pageCount)
            addProperty("width", image.width)
            addProperty("height", image.height)
            addProperty("mimeType", "image/jpeg")
            add("_content", JsonArray().apply {
                add(JsonObject().apply {
                    addProperty("type", "image")
                    addProperty("data", android.util.Base64.encodeToString(image.jpeg, android.util.Base64.NO_WRAP))
                    addProperty("mimeType", "image/jpeg")
                })
            })
        }
    }

    /**
     * The item's PDF (the key itself, else its first PDF attachment in title order) as a local
     * file, downloaded first the way the app opens attachments (Zotero storage or WebDAV).
     */
    private suspend fun pdfFile(itemKey: String): Pair<String, java.io.File> {
        val (attachment, parentKey) = read { realm ->
            val item = findItem(realm, itemKey)
            val candidates = if (item.rawType == ItemTypes.attachment) listOf(item) else {
                item.children?.filter { it.rawType == ItemTypes.attachment && !it.trash && !it.deleted }.orEmpty()
            }
            val pdf = candidates.filter { field(it, FieldKeys.Item.Attachment.contentType) == "application/pdf" }
                .sortedWith(titleOrder).firstOrNull()
                ?: throw ToolError("not_found", "This item has no PDF.")
            val attachment = AttachmentCreator.attachment(pdf, fileStorage = fileStore, isForceRemote = false, urlDetector = null, defaults = defaults)
                ?: throw ToolError("unavailable", "This PDF can't be opened on this device.")
            attachment to pdf.parent?.key
        }
        val kind = attachment.type as? Attachment.Kind.file
            ?: throw ToolError("unavailable", "This PDF can't be opened on this device.")
        if (kind.linkType == Attachment.FileLinkType.linkedFile || kind.linkType == Attachment.FileLinkType.embeddedImage) {
            throw ToolError("unavailable", "This PDF is a linked file, which this device can't open.")
        }
        val file = fileStore.attachmentFile(libraryId = attachment.libraryId, key = attachment.key, filename = kind.filename)
        if (kind.location != Attachment.FileLocation.local || !file.exists()) {
            val update = coroutineScope {
                // Subscribe before asking, so the result can't be missed.
                val waiter = async(start = kotlinx.coroutines.CoroutineStart.UNDISPATCHED) {
                    withTimeoutOrNull(TimeUnit.MINUTES.toMillis(3)) {
                        attachmentDownloaderEventStream.flow().first { update ->
                            update.key == attachment.key && update.libraryId == attachment.libraryId &&
                                    update.kind !is AttachmentDownloader.Update.Kind.progress
                        }
                    }
                }
                attachmentDownloader.downloadIfNeeded(attachment, parentKey)
                waiter.await()
            }
            // A failure or cancellation from another download of the same file still counts if the file arrived.
            if (!file.exists()) {
                throw ToolError("unavailable", if (update == null) "Downloading this PDF took too long." else "Couldn't download this PDF.")
            }
        }
        return attachment.key to file
    }

    private fun getCollections(realm: Realm): JsonObject {
        val collections = realm.where<RCollection>().library(libraryId).equalTo("deleted", false).equalTo("trash", false).findAll()
            .sortedWith(compareBy({ it.name.lowercase() }, { it.key }))
        return JsonObject().apply {
            add("collections", JsonArray().apply {
                collections.forEach { collection ->
                    add(JsonObject().apply {
                        addProperty("key", collection.key)
                        addProperty("name", collection.name)
                        add("parentKey", collection.parentKey?.let { com.google.gson.JsonPrimitive(it) } ?: JsonNull.INSTANCE)
                        addProperty("itemCount", collection.items.count { isRegular(it) })
                    })
                }
            })
        }
    }

    private fun getCollectionItems(realm: Realm, args: JsonObject): JsonObject {
        val collection = findCollection(realm, args.str("collection_key"))
        val limit = args.limit("limit", default = 50, max = 100)
        val items = collection.items.filter { isRegular(it) }.sortedWith(titleOrder)
        return JsonObject().apply {
            add("collection", JsonObject().apply {
                addProperty("key", collection.key)
                addProperty("name", collection.name)
            })
            addProperty("total", items.size)
            add("items", JsonArray().apply { items.take(limit).forEach { add(summary(it)) } })
        }
    }

    private fun getTags(realm: Realm, args: JsonObject): JsonObject {
        val limit = args.limit("limit", default = 200, max = 500)
        val counts = mutableMapOf<String, Int>()
        regularItems(realm).forEach { item ->
            tagNames(item).forEach { counts[it] = (counts[it] ?: 0) + 1 }
        }
        val sorted = counts.entries.sortedWith(
            compareByDescending<Map.Entry<String, Int>> { it.value }
                .thenBy { it.key.lowercase() }
                .thenBy { it.key }
        )
        return JsonObject().apply {
            addProperty("total", sorted.size)
            add("tags", JsonArray().apply {
                sorted.take(limit).forEach {
                    add(JsonObject().apply {
                        addProperty("tag", it.key)
                        addProperty("count", it.value)
                    })
                }
            })
        }
    }

    private fun getRecent(realm: Realm, args: JsonObject): JsonObject {
        val limit = args.limit("limit", default = 10, max = 100)
        val items = regularItems(realm)
            .sortedWith(compareByDescending<RItem> { it.dateAdded }.thenBy { it.key })
            .take(limit)
        return JsonObject().apply {
            add("items", JsonArray().apply {
                items.forEach { add(summary(it).apply { addProperty("dateAdded", iso(it.dateAdded)) }) }
            })
        }
    }

    private fun getNotes(realm: Realm, args: JsonObject): JsonObject {
        val parentKey = args.strOrNull("item_key")
        val query = args.strOrNull("query")?.lowercase()
        val limit = args.limit("limit", default = 20, max = 50)
        if (parentKey != null) findItem(realm, parentKey)
        val notes = realm.where<RItem>().library(libraryId)
            .equalTo("rawType", ItemTypes.note)
            .equalTo("trash", false)
            .equalTo("deleted", false)
            .findAll()
            .map { it to plainText(field(it, FieldKeys.Item.note)) }
            .filter { (note, text) ->
                (parentKey == null || note.parent?.key == parentKey) &&
                        (query == null || text.lowercase().contains(query))
            }
            .sortedWith(compareByDescending<Pair<RItem, String>> { it.first.dateModified }.thenBy { it.first.key })
        return JsonObject().apply {
            addProperty("total", notes.size)
            add("notes", JsonArray().apply {
                notes.take(limit).forEach { (note, text) ->
                    add(JsonObject().apply {
                        addProperty("key", note.key)
                        add("parentKey", note.parent?.key?.let { com.google.gson.JsonPrimitive(it) } ?: JsonNull.INSTANCE)
                        addProperty("title", title(note))
                        addProperty("text", text.take(2000))
                    })
                }
            })
        }
    }

    private fun getAnnotations(realm: Realm, key: String): JsonObject {
        val item = findItem(realm, key)
        val attachments = if (item.rawType == ItemTypes.attachment) {
            listOf(item)
        } else {
            item.children?.filter { it.rawType == ItemTypes.attachment && !it.trash && !it.deleted }.orEmpty()
        }
        return JsonObject().apply {
            add("annotations", JsonArray().apply {
                attachments.sortedBy { it.key }.forEach { attachment ->
                    attachment.children
                        ?.filter { it.rawType == ItemTypes.annotation && !it.trash && !it.deleted }
                        ?.sortedWith(compareBy({ it.annotationSortIndex }, { it.key }))
                        ?.forEach { annotation ->
                            add(JsonObject().apply {
                                addProperty("key", annotation.key)
                                addProperty("attachmentKey", attachment.key)
                                addProperty("type", field(annotation, FieldKeys.Item.Annotation.type))
                                addProperty("text", field(annotation, FieldKeys.Item.Annotation.text))
                                addProperty("comment", field(annotation, FieldKeys.Item.Annotation.comment))
                                addProperty("pageLabel", field(annotation, FieldKeys.Item.Annotation.pageLabel))
                                addProperty("color", field(annotation, FieldKeys.Item.Annotation.color))
                                add("tags", tagsArray(annotation))
                            })
                        }
                }
            })
        }
    }

    // endregion

    // region Writes

    private suspend fun batchUpdateTags(args: JsonObject): JsonObject {
        val keys = args.strList("item_keys").distinct()
        val add = args.strList("add_tags").map { it.trim() }.filter { it.isNotEmpty() }
        val remove = args.strList("remove_tags").toSet()
        val edits = read { realm ->
            keys.map { findItem(realm, it) }.mapNotNull { item ->
                val existing = item.tags?.map { Tag(it) }.orEmpty()
                val names = existing.map { it.name }.toSet()
                val kept = existing.filter { it.name !in remove }
                val added = add.filter { it !in names }.distinct()
                    .map { Tag(name = it, color = "", type = RTypedTag.Kind.manual) }
                if (added.isEmpty() && kept.size == existing.size) null
                else item.key to (kept + added)
            }
        }
        write(edits.map { (key, tags) -> EditTagsForItemDbRequest(key, libraryId, tags) })
        return JsonObject().apply { add("updated", stringArray(edits.map { it.first }.sorted())) }
    }

    private suspend fun setItemCollections(args: JsonObject): JsonObject {
        val keys = args.strList("item_keys").distinct()
        val addTo = args.strList("add_to")
        val removeFrom = args.strList("remove_from")
        val (requests, updated) = read { realm ->
            keys.forEach { findItem(realm, it) }
            val requests = mutableListOf<DbRequest>()
            val updated = mutableSetOf<String>()
            addTo.forEach { collectionKey ->
                val collection = findCollection(realm, collectionKey)
                val missing = keys.filter { key -> collection.items.none { it.key == key } }.toSet()
                if (missing.isNotEmpty()) {
                    requests += AssignItemsToCollectionsDbRequest(setOf(collectionKey), missing, libraryId)
                    updated += missing
                }
            }
            removeFrom.forEach { collectionKey ->
                val collection = findCollection(realm, collectionKey)
                val present = keys.filter { key -> collection.items.any { it.key == key } }.toSet()
                if (present.isNotEmpty()) {
                    requests += DeleteItemsFromCollectionDbRequest(collectionKey, present, libraryId)
                    updated += present
                }
            }
            requests to updated
        }
        write(requests)
        return JsonObject().apply { add("updated", stringArray(updated.sorted())) }
    }

    private suspend fun createCollection(args: JsonObject): JsonObject {
        val name = args.str("name").trim()
        if (name.isEmpty()) throw ToolError("invalid_argument", "The collection needs a name.")
        val parentKey = args.strOrNull("parent_collection_key")
        if (parentKey != null) read { findCollection(it, parentKey) }
        val key = KeyGenerator.newKey()
        write(listOf(CreateCollectionDbRequest(libraryId, key, name, parentKey)))
        return JsonObject().apply {
            addProperty("key", key)
            addProperty("name", name)
        }
    }

    private suspend fun createNote(args: JsonObject): JsonObject {
        val parentKey = args.strOrNull("parent_item_key")
        if (parentKey != null) read { findItem(it, parentKey) }
        // The line break keeps the heading on its own line: Zotero titles a note with its first line.
        val html = "<div data-schema-version=\"9\"><h1>${escape(args.str("title"))}</h1>\n" +
                // Empty content is allowed: a note can be just its title (ai/spec/tools.md).
                ChatMarkdown.toHtml(args.get("content")?.takeIf { it.isJsonPrimitive }?.asString.orEmpty()) + "</div>"
        val key = KeyGenerator.newKey()
        val note = Note(
            key = key,
            text = html,
            tags = args.strList("tags").map { Tag(name = it, color = "", type = RTypedTag.Kind.manual) }
        )
        withContext(dispatchers.io) {
            dbWrapperMain.realmDbStorage.perform(
                request = CreateNoteDbRequest(
                    note = note,
                    localizedType = schemaController.localizedItemType(ItemTypes.note) ?: "",
                    libraryId = libraryId,
                    collectionKey = null,
                    parentKey = parentKey,
                ),
                invalidateRealm = true
            )
        }
        return JsonObject().apply { addProperty("key", key) }
    }

    private suspend fun addItemByIdentifier(args: JsonObject): JsonObject {
        val identifier = args.str("identifier").trim()
        if (identifier.isEmpty()) throw ToolError("invalid_argument", "No identifier given.")
        val collectionKey = args.strOrNull("collection_key")
        if (collectionKey != null) read { findCollection(it, collectionKey) }
        val collectionKeys = setOfNotNull(collectionKey)
        val stored = withContext(dispatchers.main) {
            identifierLookupController.initialize(
                lookupMode = IdentifierLookupMode.normal,
                libraryId = libraryId,
                collectionKeys = collectionKeys,
            ) {}
            coroutineScope {
                val outcome = async {
                    identifierLookupController.observable.flow().first { update ->
                        when (update.kind) {
                            is IdentifierLookupController.Update.Kind.itemStored,
                            is IdentifierLookupController.Update.Kind.lookupFailed,
                            is IdentifierLookupController.Update.Kind.parseFailed,
                            is IdentifierLookupController.Update.Kind.itemCreationFailed,
                            is IdentifierLookupController.Update.Kind.lookupError -> true

                            else -> false
                        }
                    }
                }
                // A lookup sent while the translators are still loading is dropped, so wait.
                withTimeoutOrNull(240_000) { translatorLoadedEventStream.flow().first { it == true } }
                    ?: throw ToolError("unavailable", "Zotero's translators didn't finish loading.")
                identifierLookupController.lookUp(libraryId, collectionKeys, identifier)
                withTimeoutOrNull(120_000) { outcome.await() }
                    ?: run {
                        outcome.cancel()
                        throw ToolError("failed", "Looking up $identifier timed out.")
                    }
            }
        }
        val kind = stored.kind as? IdentifierLookupController.Update.Kind.itemStored
            ?: throw ToolError("not_found", "Couldn't find metadata for $identifier.")
        val key = kind.response.key
        val tags = args.strList("tags")
        if (tags.isNotEmpty()) {
            batchUpdateTags(JsonObject().apply {
                add("item_keys", stringArray(listOf(key)))
                add("add_tags", stringArray(tags))
                add("remove_tags", JsonArray())
            })
        }
        return read { realm ->
            val item = findItem(realm, key)
            JsonObject().apply {
                addProperty("key", key)
                addProperty("title", title(item))
                addProperty("itemType", item.rawType)
            }
        }
    }

    private suspend fun trashItems(args: JsonObject): JsonObject {
        val keys = args.strList("item_keys").distinct()
        read { realm -> keys.forEach { findItem(realm, it) } }
        write(listOf(MarkItemsAsTrashedDbRequest(keys, libraryId, trashed = true)))
        return JsonObject().apply { add("trashed", stringArray(keys.sorted())) }
    }

    // endregion

    // region Helpers

    private val titleOrder = compareBy<RItem>({ title(it).lowercase() }, { it.key })

    private suspend inline fun <reified T : Any> read(crossinline block: (Realm) -> T): T = withContext(dispatchers.io) {
        dbWrapperMain.realmDbStorage.perform(
            request = object : DbResponseRequest<T> {
                override val needsWrite: Boolean get() = false
                override fun process(database: Realm): T = block(database)
            },
            invalidateRealm = true,
            // See writes made elsewhere in the app (e.g. an item just saved by a lookup).
            refreshRealm = true,
        )
    }

    private suspend fun write(requests: List<DbRequest>) {
        if (requests.isEmpty()) return
        withContext(dispatchers.io) { dbWrapperMain.realmDbStorage.perform(requests) }
    }

    private fun regularItems(realm: Realm): List<RItem> =
        realm.where<RItem>().library(libraryId)
            .equalTo("trash", false)
            .equalTo("deleted", false)
            .findAll()
            .filter { isRegular(it) }

    private fun isRegular(item: RItem): Boolean =
        !item.trash && !item.deleted && item.rawType != ItemTypes.attachment &&
                item.rawType != ItemTypes.note && item.rawType != ItemTypes.annotation

    private fun findItem(realm: Realm, key: String): RItem =
        realm.where<RItem>().key(key, libraryId).equalTo("deleted", false).findFirst()
            ?: throw ToolError("not_found", "No item with key $key.")

    private fun findCollection(realm: Realm, key: String): RCollection =
        realm.where<RCollection>().key(key, libraryId).equalTo("deleted", false).equalTo("trash", false).findFirst()
            ?: throw ToolError("not_found", "No collection with key $key.")

    private fun resolveTextAttachment(realm: Realm, item: RItem): RItem? {
        val types = listOf("application/pdf", "application/epub+zip", "text/html")
        val candidates = if (item.rawType == ItemTypes.attachment) listOf(item) else {
            item.children?.filter { it.rawType == ItemTypes.attachment && !it.trash && !it.deleted }.orEmpty()
        }
        return candidates
            .filter { field(it, FieldKeys.Item.Attachment.contentType) in types }
            .sortedWith(
                compareBy<RItem> { types.indexOf(field(it, FieldKeys.Item.Attachment.contentType)) }
                    .thenBy { title(it).lowercase() }
                    .thenBy { it.key }
            )
            .firstOrNull()
    }

    private fun field(item: RItem, key: String): String =
        (item.fields.firstOrNull { it.key == key } ?: item.fields.firstOrNull { it.baseKey == key })
            ?.value?.trim().orEmpty()

    private fun title(item: RItem): String = when (item.rawType) {
        ItemTypes.note -> noteTitle(field(item, FieldKeys.Item.note))
        else -> item.baseTitle.trim().ifEmpty { item.displayTitle.trim() }
    }

    /** Zotero's own note title rule (Zotero.Utilities.Item.noteToTitle), so every client agrees. */
    private fun noteTitle(html: String): String {
        val trimmed = html.trim()
        var text = trimmed
            .replace(Regex("(</(h\\d|p|div)+>)"), "$1\n")
            .replace(Regex("<br\\s*/?>"), " ")
            .replace(Regex("<[^>]*>"), "")
        text = org.apache.commons.text.StringEscapeUtils.unescapeHtml4(text)
        if (Regex("^<[^>\n]+[^/]>\n").containsMatchIn(trimmed)) text = text.trim()
        var title = text.take(120)
        val lineBreak = title.indexOf('\n')
        if (lineBreak > -1) title = title.substring(0, lineBreak)
        return title
    }

    private fun year(item: RItem): String =
        Regex("(?<!\\d)\\d{4}(?!\\d)").find(field(item, FieldKeys.Item.date))?.value ?: ""

    private fun tagNames(item: RItem): List<String> =
        item.tags?.mapNotNull { it.tag?.name }?.distinct().orEmpty()

    private fun tagsArray(item: RItem): JsonArray =
        stringArray(tagNames(item).sortedWith(compareBy({ it.lowercase() }, { it })))

    private fun searchableTexts(item: RItem): List<String> = buildList {
        add(title(item).lowercase())
        item.creators.forEach {
            add(it.firstName.lowercase()); add(it.lastName.lowercase()); add(it.name.lowercase())
        }
        add(year(item))
        tagNames(item).forEach { add(it.lowercase()) }
    }

    private fun summary(item: RItem): JsonObject = JsonObject().apply {
        addProperty("key", item.key)
        addProperty("itemType", item.rawType)
        addProperty("title", title(item))
        addProperty("creators", creatorSummary(item))
        addProperty("year", year(item))
        add("tags", tagsArray(item))
    }

    private fun creatorSummary(item: RItem): String {
        val creators = item.creators.sortedBy { it.orderId }
        val primary = creators.filter { it.primary }.ifEmpty { creators }
        val names = primary.map { it.lastName.trim().ifEmpty { it.name.trim() } }
        return when (names.size) {
            0 -> ""
            1 -> names[0]
            2 -> "${names[0]} and ${names[1]}"
            else -> "${names[0]} et al."
        }
    }

    private fun creatorFullName(first: String, last: String, name: String): String =
        if (name.isNotBlank()) name.trim() else "${first.trim()} ${last.trim()}".trim()

    private fun plainText(html: String): String =
        Html.fromHtml(html, Html.FROM_HTML_MODE_LEGACY).toString()
            .replace(Regex("\\s+"), " ").trim()

    private fun iso(date: Date): String =
        SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", Locale.US)
            .apply { timeZone = TimeZone.getTimeZone("UTC") }
            .format(date)

    private fun escape(text: String): String =
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\"", "&quot;")

    private fun stringArray(values: List<String>) = JsonArray().apply { values.forEach { add(it) } }

    private fun error(code: String, message: String) = JsonObject().apply {
        addProperty("error", code)
        addProperty("message", message)
    }

    private fun JsonObject.strOrNull(name: String): String? =
        get(name)?.takeIf { it.isJsonPrimitive }?.asString?.trim()?.takeIf { it.isNotEmpty() }

    private fun JsonObject.str(name: String): String = strOrNull(name)
        ?: throw ToolError("invalid_argument", "Missing $name.")

    private fun JsonObject.strList(name: String): List<String> =
        get(name)?.takeIf { it.isJsonArray }?.asJsonArray
            ?.mapNotNull { it.takeIf(JsonElement::isJsonPrimitive)?.asString }
            .orEmpty()

    private fun JsonObject.limit(name: String, default: Int, max: Int): Int {
        val value = get(name)?.takeIf { it.isJsonPrimitive }?.asInt ?: return default
        return value.coerceIn(1, max)
    }

    // endregion
}
