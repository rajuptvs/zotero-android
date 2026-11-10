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

    // region Streaming blocks
    // Adapted from T3 Code's splitMarkdownBlocks (apps/web/src/components/chat/markdownStreamBlocks.ts),
    // MIT License, Copyright (c) 2026 T3 Tools Inc. Same rule on desktop (Notes.splitBlocks);
    // fixtures in ai/spec/blocks.json.

    // Definitions, footnotes, raw HTML and container directives can tie blocks together.
    private val documentWideSyntax = Regex("""^ {0,3}(?:\[[^\]\n]+\]:|<[A-Za-z!?/]|:::)|\[\^""", RegexOption.MULTILINE)
    private val fencePattern = Regex("""^ {0,3}(`{3,}|~{3,})""")
    // After a blank line these still continue the previous block: a list item or indented content.
    private val continuationPattern = Regex("""^(?:[ \t]|(?:[-*+]|\d{1,9}[.)])(?:[ \t]|$))""")
    // A number alone on the last line may be the start of the next list item ("2" before "2.").
    private val pendingListNumber = Regex("""^ {0,3}\d{1,9}$""")
    private val mathOpener = Regex("""\$\$|\\\[|\\\(""")

    /**
     * Cuts an answer at blank lines that close every open block, so each piece renders exactly as it
     * would inside the whole text, and pieces before the last never change as more text streams in.
     * Never cuts inside math (a [mathOrCode] match) or after a math opener that isn't closed yet.
     * Returns null when the text uses syntax that spans blocks; render it whole then.
     */
    fun splitBlocks(text: String): List<String>? {
        if (text.contains('\r') || documentWideSyntax.containsMatchIn(text)) return null
        // Spans no cut may fall inside: math, and everything after an unclosed math opener.
        val protectedSpans = mutableListOf<IntRange>()
        var gapStart = 0
        var unclosedFrom = -1
        fun scanGap(from: Int, to: Int) {
            if (unclosedFrom >= 0) return
            mathOpener.find(text.substring(from, to))?.let { unclosedFrom = from + it.range.first }
        }
        mathOrCode.findAll(text).forEach { m ->
            scanGap(gapStart, m.range.first)
            if (m.groups[1] == null) protectedSpans += m.range.first until m.range.last + 1
            gapStart = m.range.last + 1
        }
        scanGap(gapStart, text.length)
        if (unclosedFrom >= 0) protectedSpans += unclosedFrom until text.length
        fun canCut(at: Int) = protectedSpans.none { it.first < at && at < it.last + 1 }

        val blocks = mutableListOf<String>()
        var blockStart = 0
        var lineStart = 0
        var fence: String? = null
        var fenceIndent = 0
        var afterBlankLine = false
        val lines = text.split("\n")
        for ((i, line) in lines.withIndex()) {
            val opener = fencePattern.find(line)?.groupValues?.get(1)
            if (fence != null) {
                // A line left of an indented fence may end the list item holding it, which isn't tracked.
                if (line.take(fenceIndent).any { !it.isWhitespace() }) break
                if (opener != null && opener[0] == fence[0] && opener.length >= fence.length && line.trim() == opener) fence = null
            } else if (line.isBlank()) {
                afterBlankLine = true
            } else {
                val continues = continuationPattern.containsMatchIn(line) ||
                        (i == lines.lastIndex && pendingListNumber.containsMatchIn(line))
                if (afterBlankLine && lineStart > blockStart && !continues && canCut(lineStart)) {
                    blocks += text.substring(blockStart, lineStart)
                    blockStart = lineStart
                }
                afterBlankLine = false
                fence = opener
                if (opener != null) fenceIndent = line.indexOf(opener)
            }
            lineStart += line.length + 1
        }
        blocks += text.substring(blockStart)
        return blocks
    }

    // endregion

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
