package org.zotero.android.ai.semantic

import android.content.Context
import android.os.Build
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkInfo
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import dagger.hilt.EntryPoint
import dagger.hilt.InstallIn
import dagger.hilt.android.EntryPointAccessors
import dagger.hilt.android.qualifiers.ApplicationContext
import dagger.hilt.components.SingletonComponent
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import timber.log.Timber
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.security.MessageDigest
import java.util.concurrent.TimeUnit
import javax.inject.Inject
import javax.inject.Singleton
import kotlin.coroutines.coroutineContext

/**
 * The pinned embedding model (ai/spec/semantic.md, "Model"): whether semantic search is on for this
 * device, and the download that turning it on starts. Files go to
 * noBackupFilesDir/models/<revision>/ and are kept only once their sha256 matches.
 */
@Singleton
class SemanticModel @Inject constructor(@ApplicationContext private val context: Context) {

    class ModelFile(val path: String, val sha256: String, val size: Long)

    /** For the settings switch; `downloaded`/`total` in bytes. */
    data class State(
        val supported: Boolean,
        val enabled: Boolean,
        val model: String,
        val error: String?,
        val downloaded: Long,
        val total: Long,
        val waiting: Boolean,
    )

    private val prefs = context.getSharedPreferences("ai_semantic", Context.MODE_PRIVATE)
    private val workManager get() = WorkManager.getInstance(context)
    private val enabledFlow = MutableStateFlow(enabled)
    private val http = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .build()

    val dir: File get() = File(context.noBackupFilesDir, "models/$REVISION")

    /** ONNX Runtime needs API 24; below that semantic search stays off. */
    val supported: Boolean get() = Build.VERSION.SDK_INT >= 24

    val enabled: Boolean get() = supported && prefs.getBoolean(KEY_ENABLED, false)

    val ready: Boolean get() = FILES.all { File(dir, it.path).isFile }

    /** The files on disk have been checked against their sha256 in this process. */
    @Volatile
    private var verified = false

    fun file(path: String) = File(dir, path)

    /**
     * Before the first load in a process: whether the downloaded files still match their sha256.
     * Ones that don't are deleted and downloaded again; until then the model isn't [ready].
     */
    @Synchronized
    fun verify(): Boolean {
        if (verified) return true
        if (!ready) return false
        val bad = FILES.filter { sha256(File(dir, it.path)) != it.sha256 }
        if (bad.isEmpty()) {
            verified = true
            return true
        }
        Timber.w("SemanticModel: ${bad.joinToString { it.path }} didn't match the checksum; downloading again")
        bad.forEach { File(dir, it.path).delete() }
        if (enabled) enqueueDownload()
        return false
    }

    fun setEnabled(on: Boolean) {
        if (!supported) return
        prefs.edit().putBoolean(KEY_ENABLED, on).remove(KEY_ERROR).apply()
        enabledFlow.value = on
        if (on) {
            enqueueDownload()
        } else {
            workManager.cancelUniqueWork(WORK_NAME)
            // The model is large; turning the feature off gives the space back.
            File(context.noBackupFilesDir, "models").deleteRecursively()
            verified = false
        }
    }

    private fun enqueueDownload() {
        val request = OneTimeWorkRequestBuilder<SemanticModelWorker>()
            .setConstraints(
                Constraints.Builder()
                    .setRequiredNetworkType(NetworkType.UNMETERED)
                    .setRequiresStorageNotLow(true)
                    .build()
            )
            .build()
        workManager.enqueueUniqueWork(WORK_NAME, ExistingWorkPolicy.KEEP, request)
    }

    /** Model status as the test hook reports it: missing, downloading, ready or error. */
    fun status(): Pair<String, String?> {
        if (ready) return "ready" to null
        if (!enabled) return "missing" to null
        val info = runCatching { workManager.getWorkInfosForUniqueWork(WORK_NAME).get() }.getOrNull()?.lastOrNull()
        return statusOf(info)
    }

    private fun statusOf(info: WorkInfo?): Pair<String, String?> = when {
        info == null -> "missing" to null
        info.state == WorkInfo.State.FAILED -> "error" to (prefs.getString(KEY_ERROR, null) ?: "The download failed.")
        info.state.isFinished -> "missing" to null
        else -> "downloading" to prefs.getString(KEY_ERROR, null)
    }

    val state: Flow<State> = combine(enabledFlow, workManager.getWorkInfosForUniqueWorkFlow(WORK_NAME)) { on, infos ->
        val info = infos.lastOrNull()
        val (model, error) = if (ready) "ready" to null else if (!on) "missing" to null else statusOf(info)
        val partial = FILES.sumOf { f -> File(dir, f.path).takeIf { it.isFile }?.length() ?: File(dir, f.path + ".part").length() }
        State(
            supported = supported,
            enabled = on,
            model = model,
            error = error,
            downloaded = info?.progress?.getLong(PROGRESS_DONE, -1L)?.takeIf { it >= 0 } ?: partial,
            total = TOTAL_SIZE,
            waiting = model == "downloading" && info?.state != WorkInfo.State.RUNNING,
        )
    }

    /** Downloads the missing files (resuming partial ones). Run by [SemanticModelWorker]. */
    internal suspend fun download(worker: CoroutineWorker): androidx.work.ListenableWorker.Result {
        if (!enabled) return androidx.work.ListenableWorker.Result.success()
        // Other revisions are no longer used.
        File(context.noBackupFilesDir, "models").listFiles()?.filter { it.name != REVISION }?.forEach { it.deleteRecursively() }
        dir.mkdirs()
        val needed = FILES.filter { !File(dir, it.path).isFile }.sumOf { it.size }
        if (dir.usableSpace < needed + 50L * 1024 * 1024) {
            return fail("There isn't enough free space for the model (${megabytes(needed)} MB).")
        }
        var done = FILES.filter { File(dir, it.path).isFile }.sumOf { it.size }
        for (f in FILES) {
            val target = File(dir, f.path)
            if (target.isFile) continue
            target.parentFile?.mkdirs()
            val part = File(dir, f.path + ".part")
            try {
                fetch(f, part) { bytes -> worker.setProgress(workDataOf(PROGRESS_DONE to done + bytes, PROGRESS_TOTAL to TOTAL_SIZE)) }
            } catch (e: IOException) {
                Timber.w(e, "SemanticModel: download of ${f.path} interrupted")
                if (!enabled) return androidx.work.ListenableWorker.Result.success()
                prefs.edit().putString(KEY_ERROR, "Download interrupted: ${e.message ?: e.javaClass.simpleName}").apply()
                return androidx.work.ListenableWorker.Result.retry()
            }
            if (!enabled) {
                part.delete()
                return androidx.work.ListenableWorker.Result.success()
            }
            if (sha256(part) != f.sha256) {
                part.delete()
                return if (worker.runAttemptCount < 2) androidx.work.ListenableWorker.Result.retry()
                else fail("The downloaded ${f.path} didn't match its checksum.")
            }
            if (!part.renameTo(target)) return fail("Couldn't save ${f.path}.")
            done += f.size
        }
        prefs.edit().remove(KEY_ERROR).apply()
        return androidx.work.ListenableWorker.Result.success()
    }

    private fun fail(message: String): androidx.work.ListenableWorker.Result {
        prefs.edit().putString(KEY_ERROR, message).apply()
        return androidx.work.ListenableWorker.Result.failure()
    }

    /** Appends to [part] from where it stopped (HTTP Range), restarting if the server ignores it. */
    private suspend fun fetch(f: ModelFile, part: File, progress: suspend (Long) -> Unit) {
        val have = if (part.length() > f.size) {
            part.delete(); 0L
        } else part.length()
        if (have == f.size) return
        val request = Request.Builder().url("$BASE_URL/${f.path}")
            .apply { if (have > 0) header("Range", "bytes=$have-") }
            .build()
        http.newCall(request).execute().use { response ->
            val append = response.code == 206
            if (have > 0) Timber.i("SemanticModel: ${f.path}: resuming at $have bytes (HTTP ${response.code})")
            if (!response.isSuccessful) throw IOException("HTTP ${response.code}")
            var written = if (append) have else 0L
            FileOutputStream(part, append).use { out ->
                val input = response.body.byteStream()
                val buffer = ByteArray(1 shl 16)
                var lastReport = 0L
                while (true) {
                    coroutineContext.ensureActive()
                    if (!enabled) throw IOException("Turned off")
                    val n = input.read(buffer)
                    if (n < 0) break
                    out.write(buffer, 0, n)
                    written += n
                    if (written - lastReport >= 1 shl 20) {
                        lastReport = written
                        progress(written)
                    }
                }
            }
            if (written != f.size) throw IOException("Incomplete download (${written} of ${f.size} bytes)")
        }
    }

    private fun sha256(file: File): String {
        val digest = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { input ->
            val buffer = ByteArray(1 shl 16)
            while (true) {
                val n = input.read(buffer)
                if (n < 0) break
                digest.update(buffer, 0, n)
            }
        }
        return digest.digest().joinToString("") { "%02x".format(it) }
    }

    companion object {
        const val MODEL_ID = "onnx-community/embeddinggemma-2-ONNX"
        const val REVISION = "daa72c51243991dfcaf9f9137d2c573d8f7790c0"
        private const val BASE_URL = "https://huggingface.co/$MODEL_ID/resolve/$REVISION"
        const val ONNX = "onnx/model_quantized.onnx"
        const val TOKENIZER = "tokenizer.json"
        val FILES = listOf(
            ModelFile(ONNX, "d06edd601f851c633a2519304cbeb8dc6170d7ceb61b436625c17fb9b6e74953", 495_165),
            ModelFile("onnx/model_quantized.onnx_data", "278a7ff1248c3618e4bd11a607fc54f7bdc7778854230f3956d3f86bd9db4f3b", 313_724_928),
            ModelFile(TOKENIZER, "4d777ef5bdc1aa36227abdfb77c3e49e7b9c892d16e1b6bda41c393504828be4", 32_170_510),
        )
        val TOTAL_SIZE = FILES.sumOf { it.size }

        fun megabytes(bytes: Long) = (bytes + 500_000) / 1_000_000

        private const val KEY_ENABLED = "enabled"
        private const val KEY_ERROR = "error"
        private const val WORK_NAME = "ai-semantic-model"
        private const val PROGRESS_DONE = "done"
        private const val PROGRESS_TOTAL = "total"
    }
}

class SemanticModelWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {

    @EntryPoint
    @InstallIn(SingletonComponent::class)
    interface Deps {
        fun semanticModel(): SemanticModel
    }

    override suspend fun doWork(): Result = withContext(kotlinx.coroutines.Dispatchers.IO) {
        EntryPointAccessors.fromApplication(applicationContext, Deps::class.java).semanticModel().download(this@SemanticModelWorker)
    }
}
