package org.zotero.android.ai.tools

import android.graphics.Bitmap
import android.graphics.Color
import android.graphics.Matrix
import android.graphics.pdf.PdfRenderer
import android.os.ParcelFileDescriptor
import com.google.gson.JsonElement
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.IOException
import kotlin.math.floor
import kotlin.math.max
import kotlin.math.min

/**
 * Page images for zotero_get_page_image (ai/spec/tools.md, "PDF pages and files"), drawn with
 * Android's PdfRenderer. The desktop plugin (pdf.js) and the standalone server (PDFium) follow
 * the same geometry.
 */
object PdfPages {
    const val MAX_SIDE = 2000
    private const val MAX_FILE_BYTES = 200L * 1024 * 1024
    private const val JPEG_QUALITY = 85

    data class Plan(val x0: Double, val y0: Double, val x1: Double, val y1: Double, val width: Int, val height: Int)

    class Rendered(val pageCount: Int, val width: Int, val height: Int, val jpeg: ByteArray)

    /** The clipped region (fractions) and image size for a page of pageWidth × pageHeight points. */
    fun plan(pageWidth: Double, pageHeight: Double, region: JsonElement?, maxWidth: JsonElement?): Plan {
        var x0 = 0.0
        var y0 = 0.0
        var x1 = 1.0
        var y1 = 1.0
        if (region != null && !region.isJsonNull) {
            val values = region.takeIf { it.isJsonArray }?.asJsonArray
                ?.mapNotNull { v -> v.takeIf { it.isJsonPrimitive && it.asJsonPrimitive.isNumber }?.asDouble }
            if (values == null || values.size != 4 || region.asJsonArray.size() != 4 || !(values[2] > 0) || !(values[3] > 0)) {
                throw ToolError("invalid_argument", "region must be [x, y, width, height] with width and height above 0.")
            }
            val (x, y, w, h) = values
            x0 = max(0.0, x)
            y0 = max(0.0, y)
            x1 = min(1.0, x + w)
            y1 = min(1.0, y + h)
            if (x1 <= x0 || y1 <= y0) throw ToolError("invalid_argument", "region is outside the page.")
        }
        var width = maxWidth?.takeIf { it.isJsonPrimitive && it.asJsonPrimitive.isNumber }?.asDouble
            ?.let { min(max(it.toLong(), 200L), MAX_SIDE.toLong()).toInt() } ?: 1200
        val w = (x1 - x0) * pageWidth
        val h = (y1 - y0) * pageHeight
        var height = roundHalfUp(width * h / w)
        if (height > MAX_SIDE) {
            height = MAX_SIDE
            width = roundHalfUp(MAX_SIDE * w / h)
        }
        return Plan(x0, y0, x1, y1, max(1, width), max(1, height))
    }

    private fun roundHalfUp(v: Double): Int = floor(v + 0.5).toInt()

    fun pageCount(file: File): Int = withRenderer(file) { it.pageCount }

    /** A 1-based page (or part of it) as a JPEG. */
    fun render(file: File, page: Int, region: JsonElement?, maxWidth: JsonElement?): Rendered = withRenderer(file) { renderer ->
        val count = renderer.pageCount
        if (page !in 1..count) throw ToolError("invalid_argument", "page must be between 1 and $count.")
        renderer.openPage(page - 1).use { p ->
            // Sizes in points, crop box with the page's rotation applied.
            val pw = p.width.toDouble()
            val ph = p.height.toDouble()
            val plan = plan(pw, ph, region, maxWidth)
            val bitmap = Bitmap.createBitmap(plan.width, plan.height, Bitmap.Config.ARGB_8888)
            try {
                bitmap.eraseColor(Color.WHITE)
                val matrix = Matrix().apply {
                    postTranslate((-plan.x0 * pw).toFloat(), (-plan.y0 * ph).toFloat())
                    postScale((plan.width / ((plan.x1 - plan.x0) * pw)).toFloat(), (plan.height / ((plan.y1 - plan.y0) * ph)).toFloat())
                }
                p.render(bitmap, null, matrix, PdfRenderer.Page.RENDER_MODE_FOR_DISPLAY)
                val out = ByteArrayOutputStream()
                bitmap.compress(Bitmap.CompressFormat.JPEG, JPEG_QUALITY, out)
                Rendered(count, plan.width, plan.height, out.toByteArray())
            } finally {
                bitmap.recycle()
            }
        }
    }

    // PdfRenderer allows one page open at a time per document; keep one document at a time too.
    private val lock = Any()

    private fun <T> withRenderer(file: File, block: (PdfRenderer) -> T): T = synchronized(lock) {
        if (file.length() > MAX_FILE_BYTES) throw ToolError("unavailable", "The PDF is too large.")
        val descriptor = ParcelFileDescriptor.open(file, ParcelFileDescriptor.MODE_READ_ONLY)
        val renderer = try {
            PdfRenderer(descriptor)
        } catch (e: Exception) {
            descriptor.close()
            if (e !is IOException && e !is SecurityException) throw e
            throw ToolError("unavailable", "This PDF can't be opened (it may be damaged or password-protected).")
        }
        renderer.use(block)
    }
}
