package org.zotero.android.ai.chat

import org.commonmark.ext.gfm.strikethrough.StrikethroughExtension
import org.commonmark.ext.gfm.tables.TablesExtension
import org.commonmark.node.Image
import org.commonmark.node.Node
import org.commonmark.parser.Parser
import org.commonmark.renderer.NodeRenderer
import org.commonmark.renderer.html.HtmlRenderer

/** Turns an assistant answer (Markdown) into HTML the Zotero note editor keeps. */
object ChatMarkdown {
    private val extensions = listOf(TablesExtension.create(), StrikethroughExtension.create())
    private val parser = Parser.builder().extensions(extensions).build()
    private val renderer = HtmlRenderer.builder()
        .extensions(extensions)
        .escapeHtml(true)
        .sanitizeUrls(true)
        // Images become their alt text: a note shouldn't load a URL chosen by the model.
        .nodeRendererFactory { context ->
            object : NodeRenderer {
                override fun getNodeTypes(): Set<Class<out Node>> = setOf(Image::class.java)
                override fun render(node: Node) {
                    var child = node.firstChild
                    while (child != null) {
                        context.render(child)
                        child = child.next
                    }
                }
            }
        }
        .build()

    /**
     * Math becomes Zotero's own math markup (typeset by the note editor on every platform). It's
     * set aside before Markdown parsing so `_`, `*` and `\` inside it survive (ai/spec/chat.md).
     */
    fun toHtml(markdown: String): String {
        val math = mutableListOf<Pair<String, Boolean>>()
        val protected = mathOrCode.replace(markdown) { m ->
            val display = m.groups[2] ?: m.groups[3]
            val inline = m.groups[4] ?: m.groups[5]
            when {
                display != null -> placeholder(math, display.value.trim(), true)
                inline != null -> placeholder(math, inline.value.trim(), false)
                else -> m.value // code stays as it is
            }
        }
        var html = renderer.render(parser.parse(protected))
        math.forEachIndexed { index, (tex, display) ->
            val token = "ZAIMATH${index}Z"
            html = if (display && html.contains("<p>$token</p>")) {
                html.replace("<p>$token</p>", "<pre class=\"math\">$$${escape(tex)}$$</pre>")
            } else {
                html.replace(token, "<span class=\"math\">$${escape(tex)}$</span>")
            }
        }
        return html
    }

    /** Whether the answer has any math (outside code), so the panel typesets it. */
    fun containsMath(markdown: String): Boolean =
        mathOrCode.findAll(markdown).any { it.groups[1] == null }

    private fun placeholder(math: MutableList<Pair<String, Boolean>>, tex: String, display: Boolean): String {
        math.add(tex to display)
        return "ZAIMATH${math.size - 1}Z"
    }

    // Code (left alone) | $$…$$ | \[…\] | \(…\) | $…$ (not "$5 and $10").
    private val mathOrCode = Regex(
        """(```[\s\S]*?```|`[^`\n]*`)|\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|\$(?!\s)([^$\n]+?)(?<!\s)\$(?!\d)"""
    )

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
