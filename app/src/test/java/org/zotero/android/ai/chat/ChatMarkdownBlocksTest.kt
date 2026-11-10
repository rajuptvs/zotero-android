package org.zotero.android.ai.chat

import com.google.gson.JsonParser
import org.junit.Assert.assertEquals
import org.junit.Test
import java.io.File

/** Streaming block splits (ai/spec/blocks.json), the same fixtures the desktop plugin is tested with. */
class ChatMarkdownBlocksTest {
    private data class Case(val name: String, val text: String, val blocks: List<String>?)

    private val cases: List<Case> by lazy {
        // Unit tests run in the app module; the spec is at the repository root.
        val file = listOf(File("../ai/spec/blocks.json"), File("ai/spec/blocks.json")).first { it.exists() }
        JsonParser.parseString(file.readText()).asJsonObject.getAsJsonArray("cases").map { element ->
            val o = element.asJsonObject
            Case(
                name = o.get("name").asString,
                text = o.get("text").asString,
                blocks = o.get("blocks").takeUnless { it.isJsonNull }?.asJsonArray?.map { it.asString },
            )
        }
    }

    @Test
    fun splitsAsSpecified() {
        for (case in cases) {
            assertEquals(case.name, case.blocks, ChatMarkdown.splitBlocks(case.text))
        }
    }

    @Test
    fun everyPrefixJoinsBackAndFinishedBlocksNeverChange() {
        for (case in cases) {
            var previous: List<String>? = null
            for (end in 0..case.text.length) {
                val prefix = case.text.substring(0, end)
                val blocks = ChatMarkdown.splitBlocks(prefix)
                if (blocks == null) {
                    previous = null
                    continue
                }
                assertEquals("${case.name} @ $end", prefix, blocks.joinToString(""))
                previous?.let { before ->
                    val finished = before.dropLast(1)
                    assertEquals("${case.name} @ $end", finished, blocks.take(finished.size))
                }
                previous = blocks
            }
        }
    }
}
