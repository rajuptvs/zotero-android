package org.zotero.android.ai.chat

import android.text.Html
import org.zotero.android.ai.api.ChatTurn

/**
 * A chat is saved as an ordinary Zotero child note so it syncs to every device (and desktop).
 * The HTML only uses elements the note editor schema keeps (h1, p, strong), so the note stays
 * readable — and parseable — after being opened and edited in any Zotero client.
 *
 * Speaker labels are paragraphs containing only `<strong>You:</strong>` or
 * `<strong>ChatGPT:</strong>`; message text is never wrapped in `<strong>` on its own line, so a
 * message that happens to say "You:" can't be mistaken for a label.
 */
object ChatNoteFormat {
    private const val titleLabel = "AI chat:"
    private const val userLabel = "You:"
    private const val assistantLabel = "ChatGPT:"

    fun isChatNote(title: String, html: String): Boolean =
        title.startsWith(titleLabel) || attachmentAttribute.containsMatchIn(html) ||
                html.contains(libraryScopeAttribute)

    /**
     * The scope attribute is dropped when a note is opened in desktop Zotero's note editor, so the
     * title heading also marks a library chat (ai/spec/chat.md).
     */
    fun isLibraryChat(html: String): Boolean =
        html.contains(libraryScopeAttribute) || libraryTitle.containsMatchIn(html)

    const val libraryChatTag = "ai-chat"
    private const val libraryScopeAttribute = "data-zotero-ai-scope=\"library\""
    private val libraryTitle = Regex("""^\s*(<div[^>]*>)?\s*<h1>\s*AI chat: Library —""")

    /** A library-wide conversation: a standalone note (see ai/spec/chat.md). */
    fun encodeLibrary(turns: List<ChatTurn>): String {
        val firstQuestion = turns.firstOrNull { it.role == "user" }?.text
            ?.lineSequence()?.firstOrNull { it.isNotBlank() }?.trim().orEmpty()
        val shortQuestion = if (firstQuestion.length > 60) firstQuestion.take(57).trimEnd() + "…" else firstQuestion
        val html = StringBuilder()
        html.append("<div data-schema-version=\"9\" $libraryScopeAttribute>")
        html.append("<h1>").append(escape("$titleLabel Library — $shortQuestion".trim())).append("</h1>\n")
        appendTurns(html, turns)
        html.append("</div>")
        return html.toString()
    }

    /** The attachment this chat was about, if the note still carries it. */
    fun attachmentKey(noteHtml: String): String? =
        attachmentAttribute.find(noteHtml)?.groupValues?.get(1)

    fun encode(documentTitle: String, attachmentKey: String, turns: List<ChatTurn>): String {
        val html = StringBuilder()
        // Zotero titles a note with its first line of text, so the heading must be on line one.
        html.append("<div data-schema-version=\"9\" data-zotero-ai-attachment=\"")
            .append(escape(attachmentKey)).append("\">")
        html.append("<h1>").append(escape("$titleLabel $documentTitle".trim())).append("</h1>\n")
        appendTurns(html, turns)
        html.append("</div>")
        return html.toString()
    }

    private fun appendTurns(html: StringBuilder, turns: List<ChatTurn>) {
        for (turn in turns) {
            val label = if (turn.role == "user") userLabel else assistantLabel
            html.append("<p><strong>").append(label).append("</strong></p>\n")
            turn.text.trim().split("\n").filter { it.isNotBlank() }.forEach { line ->
                html.append("<p>").append(escape(line)).append("</p>\n")
            }
            // Images aren't uploaded with the note; record that they were part of the message.
            turn.images.forEach { image ->
                html.append("<p>").append(escape("[Image: ${image.label}]")).append("</p>\n")
            }
        }
    }

    fun decode(noteHtml: String): List<ChatTurn> {
        val turns = mutableListOf<ChatTurn>()
        var role: String? = null
        val lines = mutableListOf<String>()
        fun flush() {
            val currentRole = role ?: return
            if (lines.isNotEmpty()) {
                val text = if (currentRole == "assistant") joinMarkdownLines(lines) else lines.joinToString("\n")
                turns.add(ChatTurn(currentRole, text))
            }
            lines.clear()
        }
        paragraph.findAll(noteHtml).forEach { match ->
            val inner = match.groupValues[1].trim()
            when (labelParagraph.matchEntire(inner)?.groupValues?.get(1)) {
                userLabel -> {
                    flush(); role = "user"
                }

                assistantLabel -> {
                    flush(); role = "assistant"
                }

                else -> {
                    val text = unescape(inner).trim()
                    if (role != null && text.isNotEmpty()) lines.add(text)
                }
            }
        }
        flush()
        return turns
    }

    /**
     * Notes keep one paragraph per line and lose blank lines, so put them back between Markdown
     * blocks: keep table rows and fenced code together, separate everything else (ai/spec/chat.md).
     */
    fun joinMarkdownLines(lines: List<String>): String {
        val out = StringBuilder()
        var inFence = false
        lines.forEachIndexed { index, line ->
            if (index > 0) {
                val previous = lines[index - 1]
                val together = inFence || (previous.startsWith("|") && line.startsWith("|"))
                out.append(if (together) "\n" else "\n\n")
            }
            out.append(line)
            if (line.startsWith("```")) inFence = !inFence
        }
        return out.toString()
    }

    private val paragraph = Regex("<p(?:\\s[^>]*)?>(.*?)</p>", RegexOption.DOT_MATCHES_ALL)
    private val labelParagraph = Regex("<strong>\\s*(You:|ChatGPT:)\\s*</strong>")
    private val attachmentAttribute = Regex("data-zotero-ai-attachment=\"([^\"]+)\"")

    private fun escape(text: String): String = text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\"", "&quot;")

    private fun unescape(html: String): String =
        Html.fromHtml(html, Html.FROM_HTML_MODE_LEGACY).toString()
}
