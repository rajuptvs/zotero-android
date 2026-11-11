package org.zotero.android.ai.semantic

import com.google.gson.JsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.io.RandomAccessFile
import java.util.zip.CRC32
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

/** Reading index files (ai/spec/semantic.md, "Index file"), including hostile ones from synced storage. */
class SemanticIndexTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private sealed class Data {
        class Bytes(val bytes: ByteArray) : Data()

        /** [size] zero bytes, written a block at a time so the test never holds them. */
        class Zeros(val size: Long) : Data()
    }

    private class Entry(val name: String, val data: Data, val stored: Boolean = false)

    private fun manifest(dim: Any = 4, count: Any = 2, edit: JsonObject.() -> Unit = {}) = JsonObject().apply {
        addProperty("format", SemanticIndex.FORMAT)
        addProperty("version", SemanticIndex.VERSION)
        add("model", JsonObject().apply {
            addProperty("id", SemanticModel.MODEL_ID)
            addProperty("revision", SemanticModel.REVISION)
            add("sha256", JsonObject().apply { SemanticModel.FILES.forEach { addProperty(it.path, it.sha256) } })
        })
        if (dim is Number) addProperty("dim", dim) else addProperty("dim", dim.toString())
        if (count is Number) addProperty("count", count) else addProperty("count", count.toString())
        addProperty("items", 1)
        addProperty("builtAt", "2026-11-11T10:00:00Z")
        edit()
    }.toString()

    private fun row(itemKey: String = "ABCD2345", source: String = "metadata", key: String? = null, text: String = "hello") =
        JsonObject().apply {
            addProperty("itemKey", itemKey)
            addProperty("source", source)
            addProperty("key", key)
            addProperty("text", text)
        }.toString()

    private fun bytes(s: String) = Data.Bytes(s.toByteArray())

    private fun entries(
        manifest: String = manifest(),
        chunks: String = row() + "\n" + row(source = "note", key = "NOTE2345") + "\n",
        vectors: Data = Data.Bytes(byteArrayOf(1, 2, 3, 4, -5, 6, 7, 0)),
    ) = listOf(
        Entry("manifest.json", bytes(manifest), stored = true),
        Entry("chunks.jsonl", bytes(chunks)),
        Entry("vectors.i8", vectors, stored = true),
    )

    private fun zip(entries: List<Entry>): File {
        val file = tmp.newFile()
        ZipOutputStream(file.outputStream().buffered()).use { zip ->
            for (e in entries) {
                val entry = ZipEntry(e.name)
                if (e.stored) {
                    val data = (e.data as Data.Bytes).bytes
                    entry.method = ZipEntry.STORED
                    entry.size = data.size.toLong()
                    entry.crc = CRC32().apply { update(data) }.value
                }
                zip.putNextEntry(entry)
                when (val d = e.data) {
                    is Data.Bytes -> zip.write(d.bytes)
                    is Data.Zeros -> {
                        val block = ByteArray(1 shl 20)
                        var left = d.size
                        while (left > 0) {
                            val n = minOf(left, block.size.toLong()).toInt()
                            zip.write(block, 0, n)
                            left -= n
                        }
                    }
                }
                zip.closeEntry()
            }
        }
        return file
    }

    private fun assertUnusable(message: String, read: () -> Unit) {
        try {
            read()
            fail("expected Unusable")
        } catch (e: SemanticIndex.Unusable) {
            assertEquals(message, e.message)
        }
    }

    private fun assertDamaged(file: File) = assertUnusable(SemanticIndex.DAMAGED) { SemanticIndex.read(file) }

    @Test
    fun readsAValidIndex() {
        val index = SemanticIndex.read(zip(entries()))
        assertEquals(4, index.dim)
        assertEquals(2, index.count)
        assertEquals(listOf("metadata", "note"), index.chunks.map { it.source })
        assertEquals(listOf(null, "NOTE2345"), index.chunks.map { it.key })
        assertEquals("2026-11-11T10:00:00Z", index.builtAt)
    }

    @Test
    fun readsTheConformanceIndex() {
        val file = listOf(File("../ai/conformance/semantic/index.zip"), File("ai/conformance/semantic/index.zip")).first { it.exists() }
        val index = SemanticIndex.read(file)
        assertEquals(index.count, index.chunks.size)
        assertTrue(index.chunks.all { it.text.length <= SemanticIndex.MAX_TEXT })
    }

    @Test
    fun lastNewlineIsOptional() {
        assertEquals(2, SemanticIndex.read(zip(entries(chunks = row() + "\n" + row()))).count)
    }

    @Test
    fun manifestNeedNotComeFirst() {
        val (manifest, chunks, vectors) = entries()
        assertEquals(2, SemanticIndex.read(zip(listOf(vectors, chunks, manifest))).count)
        assertEquals(2, SemanticIndex.read(zip(listOf(chunks, manifest, vectors))).count)
    }

    @Test
    fun chunkLineAtMost64KiB() {
        val empty = row(text = "").length
        val fits = row(text = "y".repeat(SemanticIndex.MAX_LINE_BYTES - empty))
        SemanticIndex.read(zip(entries(chunks = fits + "\n" + row() + "\n")))
        val over = row(text = "y".repeat(SemanticIndex.MAX_LINE_BYTES - empty + 1))
        assertDamaged(zip(entries(chunks = over + "\n" + row() + "\n")))
    }

    @Test
    fun keepsTheFirst800Characters() {
        val long = "x".repeat(799) + "yz" + "w".repeat(2000)
        val index = SemanticIndex.read(zip(entries(chunks = row(text = long) + "\n" + row() + "\n")))
        assertEquals("x".repeat(799) + "y", index.chunks[0].text)
    }

    @Test
    fun differentModel() {
        val other = manifest { getAsJsonObject("model").addProperty("revision", "0".repeat(40)) }
        assertUnusable(SemanticIndex.DIFFERENT_MODEL) { SemanticIndex.read(zip(entries(manifest = other))) }
    }

    @Test
    fun onlyTheThreeEntriesEachOnce() {
        val (manifest, chunks, vectors) = entries()
        assertDamaged(zip(listOf(manifest, chunks)))
        assertDamaged(zip(entries() + Entry("extra.txt", bytes("x"))))
        assertDamaged(zip(listOf(manifest, Entry("Chunks.jsonl", (chunks.data)), vectors)))
        // ZipOutputStream refuses repeated names, so write a placeholder name and rename it in place.
        val file = zip(entries() + Entry("vectors.iX", Data.Bytes(ByteArray(8))))
        val bytes = file.readBytes()
        val from = "vectors.iX".toByteArray()
        var i = 0
        while (i <= bytes.size - from.size) {
            if (from.indices.all { bytes[i + it] == from[it] }) bytes[i + from.size - 1] = '8'.code.toByte()
            i++
        }
        file.writeBytes(bytes)
        assertDamaged(file)
    }

    @Test
    fun dimAndCountLimits() {
        // 769 values pass the size check but overrun the model's 768-value embedding.
        assertDamaged(zip(entries(manifest = manifest(dim = 769, count = 1), chunks = row(), vectors = Data.Bytes(ByteArray(769)))))
        assertDamaged(zip(entries(manifest = manifest(dim = 0, count = 2), vectors = Data.Bytes(ByteArray(0)))))
        assertDamaged(zip(entries(manifest = manifest(dim = 4, count = 0), chunks = "", vectors = Data.Bytes(ByteArray(0)))))
        assertDamaged(zip(entries(manifest = manifest(dim = 4.5))))
        assertDamaged(zip(entries(manifest = manifest(dim = "4"))))
        assertDamaged(zip(entries(manifest = manifest(dim = 4, count = -2))))
        // Refused from the manifest alone: the reader never allocates count × dim bytes.
        assertDamaged(zip(entries(manifest = manifest(dim = 768, count = 200_001))))
        assertDamaged(zip(entries(manifest = manifest(dim = 768, count = Int.MAX_VALUE))))
        val maxDim = SemanticIndex.read(zip(entries(manifest = manifest(dim = 768, count = 1), chunks = row(), vectors = Data.Bytes(ByteArray(768)))))
        assertEquals(768, maxDim.dim)
    }

    @Test
    fun vectorsExactSize() {
        assertDamaged(zip(entries(vectors = Data.Bytes(ByteArray(7)))))
        assertDamaged(zip(entries(vectors = Data.Bytes(ByteArray(9)))))
    }

    @Test
    fun manifestAtMost64KiB() {
        val padded = manifest { addProperty("pad", "x".repeat(64 * 1024)) }
        assertDamaged(zip(entries(manifest = padded)))
        // A deflated manifest of 100 MiB of zeros stops at 64 KiB.
        assertDamaged(zip(listOf(Entry("manifest.json", Data.Zeros(100L shl 20))) + entries().drop(1)))
    }

    @Test
    fun deflateBombsStopAtTheirLimit() {
        val (manifest, chunks, _) = entries()
        // vectors.i8 must be count × dim = 8 bytes; 256 MiB of zeros stops after 9.
        assertDamaged(zip(listOf(manifest, chunks, Entry("vectors.i8", Data.Zeros(256L shl 20)))))
        // chunks.jsonl limited to 1 MiB here (512 MiB in use); 64 MiB of newline-free zeros.
        val bomb = zip(listOf(manifest, Entry("chunks.jsonl", Data.Zeros(64L shl 20)), entries()[2]))
        assertUnusable(SemanticIndex.DAMAGED) { SemanticIndex.read(bomb, maxChunksBytes = 1L shl 20) }
        // Valid rows (each under the 64 KiB line cap) that pass the limit only by their total size.
        val big = zip(entries(chunks = row(text = "x".repeat(40_000)) + "\n" + row(text = "x".repeat(40_000)) + "\n"))
        assertEquals(2, SemanticIndex.read(big).count)
        assertUnusable(SemanticIndex.DAMAGED) { SemanticIndex.read(big, maxChunksBytes = 64L shl 10) }
    }

    @Test
    fun fileAtMost512MiB() {
        val file = zip(entries())
        RandomAccessFile(file, "rw").use { it.setLength(SemanticIndex.MAX_FILE_BYTES + 1) } // sparse
        assertDamaged(file)
    }

    @Test
    fun chunkLines() {
        val good = row()
        listOf(
            good, // one line short
            "$good\n$good\n$good\n", // one too many
            "$good\n\n$good\n", // blank line
            "$good\n" + row(itemKey = "ABCD234") + "\n", // 7-character itemKey
            "$good\n" + row(source = "web") + "\n",
            "$good\n" + """{"itemKey":"ABCD2345","source":"note","text":"t"}""" + "\n", // no key
            "$good\n" + """{"itemKey":"ABCD2345","source":"note","key":5,"text":"t"}""" + "\n",
            "$good\n" + """{"itemKey":"ABCD2345","source":"note","key":null,"text":null}""" + "\n",
            "$good\n" + """{'itemKey':'ABCD2345','source':'note','key':null,'text':'t'}""" + "\n", // not strict JSON
            "$good\n" + """{"itemKey":"ABCD2345","source":"note","key":null,"text":"t"} x""" + "\n",
            "$good\n[]\n",
        ).forEach { chunks -> assertDamaged(zip(entries(chunks = chunks))) }
    }

    @Test
    fun notAZip() {
        assertDamaged(tmp.newFile().apply { writeText("not a zip") })
    }
}
