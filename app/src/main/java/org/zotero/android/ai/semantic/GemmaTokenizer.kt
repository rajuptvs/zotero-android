package org.zotero.android.ai.semantic

import com.google.gson.stream.JsonReader
import com.google.gson.stream.JsonToken
import java.io.File
import java.util.PriorityQueue

/**
 * The pinned EmbeddingGemma `tokenizer.json`, applied exactly as the Hugging Face `tokenizers`
 * library applies it (ai/spec/semantic.md), so every client sends the model the same ids:
 * - added tokens (`<bos>`, `<eos>`, …; none normalised, no strip flags) are found in the raw text,
 *   leftmost-longest, and every other piece is encoded on its own;
 * - normaliser: `" "` → `"▁"`. The pre-tokeniser splits on `" "`, which no longer occurs after
 *   that, so each piece is a single BPE word;
 * - BPE: one symbol per code point, else its UTF-8 bytes as `<0xXX>` tokens (every byte has
 *   one); merges applied lowest rank first, then leftmost, as `tokenizers`' `Word::merge_all` does;
 * - truncation to [MAX_TOKENS] and the template `<bos> A <eos>`.
 * Only what this file uses is implemented; [load] refuses any other kind of tokenizer.
 */
class GemmaTokenizer private constructor(
    private val codePointIds: HashMap<Int, Int>,
    private val byteIds: IntArray,
    private val merges: MergeTable,
    private val added: List<Pair<String, Int>>,
    private val bosId: Int,
    private val eosId: Int,
) {

    fun encode(text: String): IntArray {
        val ids = ArrayList<Int>()
        var start = 0
        var i = 0
        while (i < text.length) {
            val match = addedTokenAt(text, i)
            if (match == null) {
                i++
                continue
            }
            if (i > start) encodePiece(text.substring(start, i), ids)
            ids.add(match.second)
            i += match.first.length
            start = i
        }
        if (start < text.length) encodePiece(text.substring(start), ids)
        val kept = minOf(ids.size, MAX_TOKENS - 2)
        return IntArray(kept + 2).also { out ->
            out[0] = bosId
            for (n in 0 until kept) out[n + 1] = ids[n]
            out[kept + 1] = eosId
        }
    }

    private val addedFirstChars = added.map { it.first[0] }.toSet()

    private fun addedTokenAt(text: String, i: Int): Pair<String, Int>? =
        if (text[i] !in addedFirstChars) null
        else added.filter { text.startsWith(it.first, i) }.maxByOrNull { it.first.length }

    private fun encodePiece(piece: String, out: MutableList<Int>) {
        val normalized = piece.replace(' ', '▁')
        // Symbols as a doubly linked list over arrays (Word in tokenizers).
        val ids = ArrayList<Int>(normalized.length)
        var i = 0
        while (i < normalized.length) {
            val cp = normalized.codePointAt(i)
            val id = codePointIds[cp]
            if (id != null) {
                ids.add(id)
            } else {
                String(Character.toChars(cp)).toByteArray(Charsets.UTF_8).forEach { ids.add(byteIds[it.toInt() and 0xff]) }
            }
            i += Character.charCount(cp)
        }
        val n = ids.size
        if (n == 0) return
        val c = ids.toIntArray()
        val prev = IntArray(n) { it - 1 }
        val next = IntArray(n) { if (it + 1 < n) it + 1 else -1 }
        val removed = BooleanArray(n)
        // Entries are (rank, pos, newId) packed so the smallest is the lowest rank, then the leftmost.
        val queue = PriorityQueue<Long>()
        fun push(pos: Int, left: Int, right: Int) {
            val merge = merges.get(left, right)
            if (merge != MergeTable.NONE) queue.add(pack(MergeTable.rank(merge), pos, MergeTable.newId(merge)))
        }
        for (pos in 0 until n - 1) push(pos, c[pos], c[pos + 1])
        while (queue.isNotEmpty()) {
            val top = queue.poll()!!
            val pos = ((top ushr 18) and POS_MASK).toInt()
            val newId = (top and ID_MASK).toInt()
            if (removed[pos] || next[pos] == -1) continue
            val right = next[pos]
            // Skip entries that no longer describe the pair at pos.
            val merge = merges.get(c[pos], c[right])
            if (merge == MergeTable.NONE || MergeTable.newId(merge) != newId) continue
            c[pos] = newId
            removed[right] = true
            next[pos] = next[right]
            if (next[right] != -1) prev[next[right]] = pos
            if (prev[pos] >= 0) push(prev[pos], c[prev[pos]], c[pos])
            if (next[pos] != -1) push(pos, c[pos], c[next[pos]])
        }
        for (pos in 0 until n) if (!removed[pos]) out.add(c[pos])
    }

    /** Merge pairs (left id, right id) → rank and new id, in open addressing over primitive arrays. */
    private class MergeTable(expected: Int) {
        private val capacity = Integer.highestOneBit(maxOf(expected, 1) * 2) * 2
        private val keys = LongArray(capacity) { EMPTY }
        private val values = LongArray(capacity)

        fun put(left: Int, right: Int, rank: Int, newId: Int) {
            val key = key(left, right)
            var slot = slot(key)
            while (keys[slot] != EMPTY && keys[slot] != key) slot = (slot + 1) and (capacity - 1)
            // tokenizers builds a HashMap from the merges in order, so a repeated pair keeps the last.
            keys[slot] = key
            values[slot] = (rank.toLong() shl 32) or newId.toLong()
        }

        fun get(left: Int, right: Int): Long {
            val key = key(left, right)
            var slot = slot(key)
            while (true) {
                val k = keys[slot]
                if (k == EMPTY) return NONE
                if (k == key) return values[slot]
                slot = (slot + 1) and (capacity - 1)
            }
        }

        private fun key(left: Int, right: Int) = (left.toLong() shl 32) or right.toLong()

        private fun slot(key: Long): Int {
            var h = key * -0x61c8864680b583ebL
            h = h xor (h ushr 29)
            return (h and (capacity - 1).toLong()).toInt()
        }

        companion object {
            const val EMPTY = -1L
            const val NONE = -1L
            fun rank(value: Long) = (value ushr 32).toInt()
            fun newId(value: Long) = (value and 0xffffffffL).toInt()
        }
    }

    companion object {
        const val MAX_TOKENS = 2048

        private const val POS_MASK = (1L shl 24) - 1
        private const val ID_MASK = (1L shl 18) - 1

        private fun pack(rank: Int, pos: Int, newId: Int): Long {
            require(rank < (1 shl 21) && pos <= POS_MASK && newId <= ID_MASK) { "Input too long to tokenize." }
            return (rank.toLong() shl 42) or (pos.toLong() shl 18) or newId.toLong()
        }

        fun load(file: File): GemmaTokenizer {
            var vocab: HashMap<String, Int>? = null
            var merges: IntArray? = null
            val added = ArrayList<Pair<String, Int>>()
            val checks = HashMap<String, String>()
            JsonReader(file.bufferedReader(Charsets.UTF_8, 1 shl 16)).use { r ->
                r.beginObject()
                while (r.hasNext()) {
                    when (val name = r.nextName()) {
                        "added_tokens" -> {
                            r.beginArray()
                            while (r.hasNext()) {
                                var id = -1
                                var content = ""
                                var plain = true
                                r.beginObject()
                                while (r.hasNext()) {
                                    when (r.nextName()) {
                                        "id" -> id = r.nextInt()
                                        "content" -> content = r.nextString()
                                        "single_word", "lstrip", "rstrip", "normalized" -> if (r.nextBoolean()) plain = false
                                        else -> r.skipValue()
                                    }
                                }
                                r.endObject()
                                require(plain) { "Unsupported added token $content" }
                                added.add(content to id)
                            }
                            r.endArray()
                        }
                        "normalizer", "pre_tokenizer", "post_processor" -> checks[name] = com.google.gson.JsonParser.parseReader(r).toString()
                        "model" -> {
                            r.beginObject()
                            while (r.hasNext()) {
                                when (val key = r.nextName()) {
                                    "vocab" -> {
                                        val map = HashMap<String, Int>(400_000)
                                        r.beginObject()
                                        while (r.hasNext()) map[r.nextName()] = r.nextInt()
                                        r.endObject()
                                        vocab = map
                                    }
                                    "merges" -> {
                                        // Resolved to ids as they're read (the vocab comes first in this file).
                                        val ids = vocab ?: error("Merges before vocab")
                                        var triples = IntArray(3 * 600_000)
                                        var n = 0
                                        r.beginArray()
                                        while (r.hasNext()) {
                                            r.beginArray()
                                            val left = r.nextString()
                                            val right = r.nextString()
                                            r.endArray()
                                            if (n + 3 > triples.size) triples = triples.copyOf(triples.size * 2)
                                            triples[n++] = ids.getValue(left)
                                            triples[n++] = ids.getValue(right)
                                            triples[n++] = ids.getValue(left + right)
                                        }
                                        r.endArray()
                                        merges = triples.copyOf(n)
                                    }
                                    else -> checks["model.$key"] = if (r.peek() == JsonToken.NULL) {
                                        r.nextNull(); "null"
                                    } else com.google.gson.JsonParser.parseReader(r).toString()
                                }
                            }
                            r.endObject()
                        }
                        else -> r.skipValue()
                    }
                }
                r.endObject()
            }
            // The pipeline above assumes exactly this configuration.
            require(checks["normalizer"] == """{"type":"Replace","pattern":{"String":" "},"content":"▁"}""") { "Unsupported normalizer" }
            require(checks["pre_tokenizer"] == """{"type":"Split","pattern":{"String":" "},"behavior":"MergedWithPrevious","invert":false}""") { "Unsupported pre-tokenizer" }
            require(checks["model.type"] == "\"BPE\"" && checks["model.byte_fallback"] == "true" && checks["model.dropout"] == "null" &&
                    checks["model.continuing_subword_prefix"] == "null" && checks["model.end_of_word_suffix"] == "null" &&
                    checks["model.ignore_merges"] == "false") { "Unsupported model" }
            require(checks["post_processor"]?.contains(""""single":[{"SpecialToken":{"id":"<bos>","type_id":0}},{"Sequence":{"id":"A","type_id":0}},{"SpecialToken":{"id":"<eos>","type_id":0}}]""") == true) { "Unsupported post-processor" }

            val v = vocab ?: error("No vocab")
            val triples = merges ?: error("No merges")
            val table = MergeTable(triples.size / 3)
            for (rank in 0 until triples.size / 3) table.put(triples[3 * rank], triples[3 * rank + 1], rank, triples[3 * rank + 2])
            val codePointIds = HashMap<Int, Int>()
            v.forEach { (token, id) ->
                if (token.isNotEmpty() && token.codePointCount(0, token.length) == 1) codePointIds[token.codePointAt(0)] = id
            }
            val byteIds = IntArray(256) { b -> v.getValue("<0x%02X>".format(b)) }
            return GemmaTokenizer(codePointIds, byteIds, table, added, v.getValue("<bos>"), v.getValue("<eos>"))
        }
    }
}
