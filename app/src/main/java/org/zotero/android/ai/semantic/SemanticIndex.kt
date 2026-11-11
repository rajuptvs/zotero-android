package org.zotero.android.ai.semantic

import com.google.gson.Gson
import com.google.gson.JsonElement
import com.google.gson.JsonObject
import com.google.gson.Strictness
import com.google.gson.stream.JsonReader
import com.google.gson.stream.JsonToken
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.FilterInputStream
import java.io.InputStream
import java.io.StringReader
import java.math.BigDecimal
import java.util.zip.ZipInputStream

/** A semantic search index file (ai/spec/semantic.md, "Index file"), read into memory. */
class SemanticIndex private constructor(
    val builtAt: String,
    val items: Int,
    val count: Int,
    val dim: Int,
    val chunks: List<Chunk>,
    private val vectors: ByteArray,
    private val norms: DoubleArray,
) {
    class Chunk(val itemKey: String, val source: String, val key: String?, val text: String)

    /** Thrown for an index this client can't use; the message is the tool's `unavailable` message. */
    class Unusable(message: String) : Exception(message)

    /**
     * Rows by score, highest first, ties by row number (step 3): `score = (q · r) / |r|`, with q
     * the query embedding cut to [dim] values and renormalised.
     */
    fun rank(embedding: FloatArray, top: Int): List<Pair<Int, Double>> {
        val q = DoubleArray(dim) { embedding[it].toDouble() }
        val qNorm = Math.sqrt(q.sumOf { it * it })
        for (i in q.indices) q[i] /= qNorm
        val scores = DoubleArray(count) { row ->
            var dot = 0.0
            val base = row * dim
            for (i in 0 until dim) dot += q[i] * vectors[base + i]
            if (norms[row] == 0.0) 0.0 else dot / norms[row]
        }
        return (0 until count).sortedWith { a, b -> if (scores[a] != scores[b]) scores[b].compareTo(scores[a]) else a.compareTo(b) }
            .take(top)
            .map { it to scores[it] }
    }

    companion object {
        const val FORMAT = "zotero-ai-index"
        const val VERSION = 1
        const val DIFFERENT_MODEL = "The search index was built with a different model; rebuild it."
        const val DAMAGED = "The search index is damaged or too large; rebuild it."

        const val MAX_FILE_BYTES = 512L * 1024 * 1024
        const val MAX_MANIFEST_BYTES = 64L * 1024
        const val MAX_CHUNKS_BYTES = 512L * 1024 * 1024
        const val MAX_LINE_BYTES = 64 * 1024 // one chunks.jsonl row, without its newline
        const val MAX_DIM = 768
        const val MAX_COUNT = 200_000
        private const val MAX_VECTORS_BYTES = MAX_COUNT.toLong() * MAX_DIM

        /** All a passage shows, so the rest of each chunk's text isn't kept. */
        const val MAX_TEXT = 800

        private const val MANIFEST = "manifest.json"
        private const val CHUNKS = "chunks.jsonl"
        private const val VECTORS = "vectors.i8"
        private val SOURCES = setOf("metadata", "fulltext", "note")
        private val elements = Gson().getAdapter(JsonElement::class.java)

        /**
         * Reads [file], checking it while unzipping (the file comes from synced storage): only the
         * three entries, each once, and every unzipped byte counted against its limit as it's
         * produced, so nothing sized from the file is allocated before the manifest is checked.
         */
        fun read(file: File): SemanticIndex = read(file, MAX_CHUNKS_BYTES)

        /** [read], with a smaller chunks.jsonl limit for tests. */
        internal fun read(file: File, maxChunksBytes: Long): SemanticIndex = try {
            if (file.length() > MAX_FILE_BYTES) throw Unusable(DAMAGED)
            var manifest: JsonObject? = null
            var vectors: ByteArray? = null
            var chunks: List<Chunk>? = null
            fun pass() = entries(file) { name, input ->
                val m = manifest
                when {
                    name == MANIFEST -> if (m == null) manifest = readManifest(input) else drain(input, MAX_MANIFEST_BYTES)
                    m == null || (name == VECTORS && vectors != null) || (name == CHUNKS && chunks != null) ->
                        drain(input, if (name == VECTORS) MAX_VECTORS_BYTES else maxChunksBytes)
                    name == VECTORS -> vectors = readVectors(input, m.int("count") * m.int("dim"))
                    else -> chunks = readChunks(input, m.int("count"), maxChunksBytes)
                }
            }
            pass()
            // The builders write the manifest first; if it came later, a second pass reads what came before it.
            if (manifest != null && (vectors == null || chunks == null)) pass()
            val m = manifest ?: throw Unusable(DAMAGED)
            build(m, vectors ?: throw Unusable(DAMAGED), chunks ?: throw Unusable(DAMAGED))
        } catch (e: Unusable) {
            throw e
        } catch (e: Exception) {
            throw Unusable(DAMAGED)
        } catch (e: OutOfMemoryError) {
            // A single huge chunks.jsonl line (within the 512 MiB limit) can still be too much for the heap.
            throw Unusable(DAMAGED)
        }

        /** Calls [block] for each entry, which must read it to its end; other or repeated names are damage. */
        private fun entries(file: File, block: (String, InputStream) -> Unit) {
            val seen = HashSet<String>()
            ZipInputStream(file.inputStream().buffered()).use { zip ->
                while (true) {
                    val entry = zip.nextEntry ?: break
                    if (entry.name !in setOf(MANIFEST, CHUNKS, VECTORS) || !seen.add(entry.name)) throw Unusable(DAMAGED)
                    block(entry.name, zip)
                }
            }
            if (seen.size != 3) throw Unusable(DAMAGED)
        }

        private fun readManifest(input: InputStream): JsonObject {
            val bytes = ByteArrayOutputStream()
            Limited(input, MAX_MANIFEST_BYTES).copyTo(bytes)
            val manifest = parse(bytes.toString(Charsets.UTF_8.name())) as? JsonObject ?: throw Unusable(DAMAGED)
            checkModel(manifest)
            val dim = manifest.int("dim")
            val count = manifest.int("count")
            if (dim !in 1..MAX_DIM || count !in 1..MAX_COUNT) throw Unusable(DAMAGED)
            return manifest
        }

        private fun readVectors(input: InputStream, size: Int): ByteArray {
            val limited = Limited(input, size.toLong())
            val vectors = ByteArray(size)
            var read = 0
            while (read < size) {
                val n = limited.read(vectors, read, size - read)
                if (n < 0) throw Unusable(DAMAGED)
                read += n
            }
            if (limited.read() >= 0) throw Unusable(DAMAGED)
            return vectors
        }

        /** Exactly [count] lines, the last newline optional; each line's text cut to [MAX_TEXT]. */
        private fun readChunks(input: InputStream, count: Int, limit: Long): List<Chunk> {
            val chunks = ArrayList<Chunk>(count)
            val line = ByteArrayOutputStream()
            fun row() {
                if (chunks.size == count) throw Unusable(DAMAGED)
                chunks.add(chunk(line.toString(Charsets.UTF_8.name())))
                line.reset()
            }
            val limited = Limited(input, limit)
            val buffer = ByteArray(1 shl 16)
            var pending = false
            while (true) {
                val n = limited.read(buffer)
                if (n < 0) break
                var start = 0
                for (i in 0 until n) {
                    if (buffer[i] == '\n'.code.toByte()) {
                        line.write(buffer, start, i - start)
                        if (line.size() > MAX_LINE_BYTES) throw Unusable(DAMAGED)
                        row()
                        start = i + 1
                    }
                }
                line.write(buffer, start, n - start)
                if (line.size() > MAX_LINE_BYTES) throw Unusable(DAMAGED)
                pending = line.size() > 0
            }
            if (pending) row()
            if (chunks.size != count) throw Unusable(DAMAGED)
            return chunks
        }

        private fun chunk(line: String): Chunk {
            val o = parse(line) as? JsonObject ?: throw Unusable(DAMAGED)
            val itemKey = o.string("itemKey")?.takeIf { it.length == 8 }
            val source = o.string("source")?.takeIf { it in SOURCES }
            val key = o.string("key")
            val text = o.string("text")
            if (itemKey == null || source == null || text == null || (key == null && o.get("key")?.isJsonNull != true)) {
                throw Unusable(DAMAGED)
            }
            return Chunk(itemKey, source, key, text.take(MAX_TEXT))
        }

        private fun drain(input: InputStream, limit: Long) {
            val limited = Limited(input, limit)
            val buffer = ByteArray(1 shl 16)
            while (limited.read(buffer) >= 0) Unit
        }

        private fun build(manifest: JsonObject, vectors: ByteArray, chunks: List<Chunk>): SemanticIndex {
            val dim = manifest.int("dim")
            val count = manifest.int("count")
            val norms = DoubleArray(count) { row ->
                var sum = 0.0
                for (i in row * dim until (row + 1) * dim) sum += vectors[i].toDouble() * vectors[i]
                Math.sqrt(sum)
            }
            return SemanticIndex(
                builtAt = manifest.get("builtAt").asString,
                items = manifest.get("items").asInt,
                count = count,
                dim = dim,
                chunks = chunks,
                vectors = vectors,
                norms = norms,
            )
        }

        /** One JSON value, strictly (RFC 8259), with nothing after it. */
        private fun parse(text: String): JsonElement {
            val reader = JsonReader(StringReader(text)).apply { strictness = Strictness.STRICT }
            val value = elements.read(reader)
            if (reader.peek() != JsonToken.END_DOCUMENT) throw Unusable(DAMAGED)
            return value
        }

        private fun JsonObject.string(name: String) =
            get(name)?.takeIf { it.isJsonPrimitive && it.asJsonPrimitive.isString }?.asString

        /** An integral JSON number (`512` or `512.0`); anything else is damage. */
        private fun JsonObject.int(name: String): Int {
            val value = get(name)?.takeIf { it.isJsonPrimitive && it.asJsonPrimitive.isNumber }?.asBigDecimal
                ?: throw Unusable(DAMAGED)
            return try {
                value.intValueExact()
            } catch (e: ArithmeticException) {
                throw Unusable(DAMAGED)
            }
        }

        private fun checkModel(manifest: JsonObject) {
            val model = manifest.get("model") as? JsonObject
            val sha = model?.get("sha256") as? JsonObject
            val sameModel = manifest.string("format") == FORMAT &&
                    manifest.get("version")?.let { it.isJsonPrimitive && it.asJsonPrimitive.isNumber && it.asBigDecimal.compareTo(BigDecimal.ONE) == 0 } == true &&
                    model?.string("revision") == SemanticModel.REVISION &&
                    sha != null && sha.keySet() == SemanticModel.FILES.map { it.path }.toSet() &&
                    SemanticModel.FILES.all { sha.string(it.path) == it.sha256 }
            if (!sameModel) throw Unusable(DIFFERENT_MODEL)
        }
    }

    /** Stops with [DAMAGED] as soon as more than [limit] bytes have come out of [input]. */
    private class Limited(input: InputStream, private val limit: Long) : FilterInputStream(input) {
        private var count = 0L

        override fun read(): Int {
            if (count >= limit) {
                if (super.read() >= 0) throw Unusable(DAMAGED)
                return -1
            }
            return super.read().also { if (it >= 0) count++ }
        }

        override fun read(b: ByteArray, off: Int, len: Int): Int {
            if (len == 0) return 0
            // Asking for at most one byte past the limit, so an overflow is seen without inflating more.
            val n = super.read(b, off, minOf(len.toLong(), limit - count + 1).toInt())
            if (n > 0) {
                count += n
                if (count > limit) throw Unusable(DAMAGED)
            }
            return n
        }

        override fun skip(n: Long): Long = throw UnsupportedOperationException()
    }
}
