package org.zotero.android.ai.chat

import org.commonmark.ext.gfm.strikethrough.StrikethroughExtension
import org.commonmark.ext.gfm.tables.TablesExtension
import org.commonmark.parser.Parser
import org.commonmark.renderer.html.HtmlRenderer

/** Turns an assistant answer (Markdown) into HTML the Zotero note editor keeps. */
object ChatMarkdown {
    private val extensions = listOf(TablesExtension.create(), StrikethroughExtension.create())
    private val parser = Parser.builder().extensions(extensions).build()
    private val renderer = HtmlRenderer.builder()
        .extensions(extensions)
        .escapeHtml(true)
        .sanitizeUrls(true)
        .build()

    fun toHtml(markdown: String): String = renderer.render(parser.parse(markdown))

    /**
     * A standalone note for one answer the user wants to keep: a heading from their question,
     * the passage it was about, the answer, and where it came from.
     */
    fun answerNote(
        question: String,
        passage: String?,
        answerMarkdown: String,
        documentTitle: String,
        location: String?,
        modelName: String?,
    ): String {
        val heading = question.lineSequence().firstOrNull { it.isNotBlank() }?.trim()
            ?.let { if (it.length > 90) it.take(87).trimEnd() + "…" else it }
            ?: "AI explanation"
        return buildString {
            // No line break before the heading: Zotero titles a note with its first line of text.
            append("<div data-schema-version=\"9\">")
            append("<h2>").append(escape(heading)).append("</h2>\n")
            if (!passage.isNullOrBlank()) {
                append("<blockquote><p>").append(escape(passage.trim())).append("</p></blockquote>\n")
            }
            append(toHtml(answerMarkdown))
            append("<p><em>")
            append(escape("From an AI chat about “$documentTitle”"))
            location?.let { append(escape(" ($it)")) }
            modelName?.let { append(escape(" · $it")) }
            append("</em></p>\n")
            append("</div>")
        }
    }

    private fun escape(text: String): String = text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\"", "&quot;")
}
