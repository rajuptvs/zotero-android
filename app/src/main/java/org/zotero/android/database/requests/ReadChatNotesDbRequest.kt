package org.zotero.android.database.requests

import io.realm.Realm
import io.realm.Sort
import io.realm.kotlin.where
import org.zotero.android.ai.chat.ChatNoteFormat
import org.zotero.android.database.DbResponseRequest
import org.zotero.android.database.objects.FieldKeys
import org.zotero.android.database.objects.ItemTypes
import org.zotero.android.database.objects.RItem
import org.zotero.android.sync.LibraryIdentifier

data class ChatNoteSnapshot(val key: String, val html: String)

/**
 * AI chat notes under [parentKey] that belong to [attachmentKey] (or don't say which attachment
 * they're for), most recently modified first. Returns plain values so no Realm objects escape.
 */
class ReadChatNotesDbRequest(
    val parentKey: String,
    val attachmentKey: String,
    val libraryId: LibraryIdentifier,
) : DbResponseRequest<List<ChatNoteSnapshot>> {
    override val needsWrite: Boolean
        get() = false

    override fun process(database: Realm): List<ChatNoteSnapshot> {
        val parent = database.where<RItem>().key(parentKey, libraryId).findFirst()
            ?: return emptyList()
        val children = parent.children ?: return emptyList()
        return children.where()
            .equalTo("rawType", ItemTypes.note)
            .equalTo("trash", false)
            .equalTo("deleted", false)
            .sort("dateModified", Sort.DESCENDING)
            .findAll()
            .map { item ->
                Triple(
                    item.key,
                    item.displayTitle,
                    item.fields.where().key(FieldKeys.Item.note).findFirst()?.value ?: ""
                )
            }
            .filter { (_, title, html) -> ChatNoteFormat.isChatNote(title, html) }
            .map { (key, _, html) -> ChatNoteSnapshot(key = key, html = html) }
            .filter { note ->
                val noteAttachment = ChatNoteFormat.attachmentKey(note.html)
                noteAttachment == null || noteAttachment == attachmentKey
            }
    }
}

data class NoteHtml(val html: String?)

/** A note's current HTML; null if it's gone (deleted or in the trash). */
class ReadNoteHtmlDbRequest(
    val key: String,
    val libraryId: LibraryIdentifier,
) : DbResponseRequest<NoteHtml> {
    override val needsWrite: Boolean
        get() = false

    override fun process(database: Realm): NoteHtml {
        val note = database.where<RItem>().key(key, libraryId).findFirst()
        if (note == null || note.trash || note.deleted) return NoteHtml(null)
        return NoteHtml(note.fields.where().key(FieldKeys.Item.note).findFirst()?.value ?: "")
    }
}

/** Display title of an item, read inside the request so no live Realm object escapes. */
class ReadItemTitleDbRequest(
    val key: String,
    val libraryId: LibraryIdentifier,
) : DbResponseRequest<String> {
    override val needsWrite: Boolean
        get() = false

    override fun process(database: Realm): String =
        database.where<RItem>().key(key, libraryId).findFirst()?.displayTitle ?: ""
}

/**
 * The bibliographic facts the model should know about the paper being read, as plain text
 * lines ("Authors: …", "Abstract: …").
 */
class ReadChatItemContextDbRequest(
    val key: String,
    val libraryId: LibraryIdentifier,
) : DbResponseRequest<String> {
    override val needsWrite: Boolean
        get() = false

    override fun process(database: Realm): String {
        val item = database.where<RItem>().key(key, libraryId).findFirst() ?: return ""
        fun field(key: String): String? = item.fields
            .firstOrNull { it.key == key || it.baseKey == key }
            ?.value?.takeIf { it.isNotBlank() }
        val creators = item.creators.sortedBy { it.orderId }.joinToString(", ") { creator ->
            creator.name.ifBlank { "${creator.firstName} ${creator.lastName}".trim() }
        }
        val tags = item.tags?.mapNotNull { it.tag?.name }?.sorted().orEmpty()
        return buildString {
            appendLine("Title: ${item.displayTitle}")
            if (creators.isNotBlank()) appendLine("Authors: $creators")
            field(FieldKeys.Item.date)?.let { appendLine("Date: $it") }
            field(FieldKeys.Item.publicationTitle)?.let { appendLine("Published in: $it") }
            field(FieldKeys.Item.doi)?.let { appendLine("DOI: $it") }
            if (tags.isNotEmpty()) appendLine("Tags: ${tags.joinToString(", ")}")
            field(FieldKeys.Item.abstractN)?.let { appendLine("Abstract: $it") }
        }.trim()
    }
}

/** Library-wide AI conversations: standalone notes marked with the library chat scope. */
class ReadLibraryChatNotesDbRequest(
    val libraryId: LibraryIdentifier,
) : DbResponseRequest<List<ChatNoteSnapshot>> {
    override val needsWrite: Boolean
        get() = false

    override fun process(database: Realm): List<ChatNoteSnapshot> =
        database.where<RItem>().library(libraryId)
            .equalTo("rawType", ItemTypes.note)
            .equalTo("trash", false)
            .equalTo("deleted", false)
            .isNull("parent")
            .sort("dateModified", Sort.DESCENDING)
            .findAll()
            .map { item ->
                ChatNoteSnapshot(
                    key = item.key,
                    html = item.fields.where().key(FieldKeys.Item.note).findFirst()?.value ?: ""
                )
            }
            .filter { ChatNoteFormat.isLibraryChat(it.html) }
}
