package org.zotero.android.ai.semantic

import ai.onnxruntime.OnnxTensor
import ai.onnxruntime.OrtEnvironment
import ai.onnxruntime.OrtSession
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import timber.log.Timber
import java.io.File
import java.nio.FloatBuffer
import java.nio.LongBuffer
import java.text.Normalizer
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Query embedding and index loading for zotero_semantic_search (ai/spec/semantic.md). The model
 * session, tokenizer and parsed index are kept for a few minutes after the last use, then freed.
 */
@Singleton
class SemanticSearch @Inject constructor(val model: SemanticModel) {

    private val mutex = Mutex()
    private val scope = CoroutineScope(SupervisorJob() + kotlinx.coroutines.Dispatchers.Default)
    private var release: Job? = null
    private var embedder: Embedder? = null
    private var index: Pair<String, SemanticIndex>? = null

    /** The index file the conformance hook asked for instead of the library's (null = normal). */
    @Volatile
    var overrideIndex: File? = null

    /** On, supported and the model downloaded: the tool's "off on this device" check. */
    val usable: Boolean get() = model.enabled && model.ready

    /** [usable], with the model files checked against their sha256 the first time in this process. */
    suspend fun usableVerified(): Boolean = usable && withContext(kotlinx.coroutines.Dispatchers.IO) { model.verify() }

    suspend fun tokenize(text: String): IntArray = use { embedder().tokenizer.encode(text) }

    /** The model's 768-value `sentence_embedding` for [text] (as given, no prefix added). */
    suspend fun embed(text: String): FloatArray = use { embedder().embed(text) }

    /** The parsed index in [file], kept until the file changes. */
    suspend fun index(file: File): SemanticIndex = use {
        val stamp = "${file.absolutePath}|${file.length()}|${file.lastModified()}"
        index?.takeIf { it.first == stamp }?.second
            ?: SemanticIndex.read(file).also { index = stamp to it }
    }

    fun release() {
        scope.launch { mutex.withLock { close() } }
    }

    private fun close() {
        embedder?.close()
        embedder = null
        index = null
    }

    private suspend fun <T> use(block: suspend () -> T): T = withContext(kotlinx.coroutines.Dispatchers.IO) {
        mutex.withLock {
            release?.cancel()
            try {
                block()
            } finally {
                release = scope.launch {
                    delay(IDLE_MILLIS)
                    mutex.withLock { close() }
                }
            }
        }
    }

    private fun embedder(): Embedder {
        embedder?.let { return it }
        check(model.supported && model.ready && model.verify()) { "The model isn't downloaded." }
        val started = System.currentTimeMillis()
        return Embedder(model).also {
            embedder = it
            Timber.i("SemanticSearch: model loaded in ${System.currentTimeMillis() - started} ms")
        }
    }

    /** ONNX Runtime is only touched from here, and only on API 24+. */
    private class Embedder(model: SemanticModel) {
        val tokenizer = GemmaTokenizer.load(model.file(SemanticModel.TOKENIZER))
        private val env = OrtEnvironment.getEnvironment()
        private val session: OrtSession = env.createSession(
            model.file(SemanticModel.ONNX).absolutePath,
            OrtSession.SessionOptions().apply {
                setIntraOpNumThreads(Runtime.getRuntime().availableProcessors().coerceIn(1, 4))
            },
        )

        fun embed(text: String): FloatArray {
            val ids = tokenizer.encode(text).map { it.toLong() }.toLongArray()
            val shape = longArrayOf(1, ids.size.toLong())
            val tensors = mutableListOf<OnnxTensor>()
            try {
                val inputs = mapOf(
                    "input_ids" to OnnxTensor.createTensor(env, LongBuffer.wrap(ids), shape),
                    "attention_mask" to OnnxTensor.createTensor(env, LongBuffer.wrap(LongArray(ids.size) { 1 }), shape),
                    "image_features" to OnnxTensor.createTensor(env, FloatBuffer.allocate(0), longArrayOf(0, 512)),
                    "video_features" to OnnxTensor.createTensor(env, FloatBuffer.allocate(0), longArrayOf(0, 512)),
                    "audio_features" to OnnxTensor.createTensor(env, FloatBuffer.allocate(0), longArrayOf(0, 512)),
                )
                tensors.addAll(inputs.values)
                session.run(inputs, setOf("sentence_embedding")).use { result ->
                    @Suppress("UNCHECKED_CAST")
                    return (result.get(0).value as Array<FloatArray>)[0]
                }
            } finally {
                tensors.forEach { it.close() }
            }
        }

        fun close() = session.close()
    }

    companion object {
        private const val IDLE_MILLIS = 3 * 60_000L

        /** Unicode NFC, runs of whitespace (Python's `\s`) → one space, trimmed. */
        fun normalize(text: String): String =
            Normalizer.normalize(text, Normalizer.Form.NFC)
                .replace(WHITESPACE, " ")
                .trim(' ')

        private val WHITESPACE = Regex("[\\t\\n\\x0B\\f\\r\\x1C-\\x20\\x85\\xA0\\u1680\\u2000-\\u200A\\u2028\\u2029\\u202F\\u205F\\u3000]+")

        fun queryText(query: String) = "task: search result | query: ${normalize(query)}"
    }
}
