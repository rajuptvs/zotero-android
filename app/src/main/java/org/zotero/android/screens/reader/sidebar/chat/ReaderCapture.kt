package org.zotero.android.screens.reader.sidebar.chat

import android.app.Activity
import android.content.Context
import android.content.ContextWrapper
import android.graphics.Bitmap
import android.graphics.Rect
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.util.Base64
import android.view.PixelCopy
import android.view.View
import androidx.core.view.drawToBitmap
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import org.zotero.android.ai.api.ChatImage
import timber.log.Timber
import java.io.ByteArrayOutputStream
import kotlin.coroutines.resume
import kotlin.math.max

/** Copies pixels of the reader so a page or a figure can be sent to the model as an image. */
object ReaderCapture {
    private const val maxImageSide = 1600

    /**
     * Captures [view] as shown on screen, or only the part inside [areaInWindow] (window pixels).
     * PixelCopy reads the composited window, which also works for the hardware-rendered WebView.
     */
    suspend fun capture(view: View, areaInWindow: Rect? = null): Bitmap? {
        val location = IntArray(2)
        view.getLocationInWindow(location)
        val viewRect = Rect(location[0], location[1], location[0] + view.width, location[1] + view.height)
        val rect = if (areaInWindow != null) {
            Rect(areaInWindow).takeIf { it.intersect(viewRect) } ?: return null
        } else {
            viewRect
        }
        if (rect.width() < 8 || rect.height() < 8) return null

        val window = view.context.findActivity()?.window
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && window != null) {
            val bitmap = Bitmap.createBitmap(rect.width(), rect.height(), Bitmap.Config.ARGB_8888)
            return suspendCancellableCoroutine { cont ->
                PixelCopy.request(window, rect, bitmap, { result ->
                    if (result == PixelCopy.SUCCESS) {
                        cont.resume(bitmap)
                    } else {
                        Timber.e("ReaderCapture: PixelCopy failed with $result")
                        cont.resume(null)
                    }
                }, Handler(Looper.getMainLooper()))
            }
        }
        return try {
            val full = view.drawToBitmap()
            Bitmap.createBitmap(
                full,
                rect.left - viewRect.left,
                rect.top - viewRect.top,
                rect.width(),
                rect.height()
            )
        } catch (e: Exception) {
            Timber.e(e, "ReaderCapture: drawing the view failed")
            null
        }
    }

    suspend fun toChatImage(bitmap: Bitmap, label: String): ChatImage = withContext(Dispatchers.Default) {
        val scale = maxImageSide.toFloat() / max(bitmap.width, bitmap.height)
        val scaled = if (scale < 1f) {
            Bitmap.createScaledBitmap(
                bitmap,
                (bitmap.width * scale).toInt(),
                (bitmap.height * scale).toInt(),
                true
            )
        } else {
            bitmap
        }
        val bytes = ByteArrayOutputStream().use { stream ->
            scaled.compress(Bitmap.CompressFormat.JPEG, 85, stream)
            stream.toByteArray()
        }
        ChatImage(
            dataUrl = "data:image/jpeg;base64," + Base64.encodeToString(bytes, Base64.NO_WRAP),
            label = label,
        )
    }

    private tailrec fun Context.findActivity(): Activity? = when (this) {
        is Activity -> this
        is ContextWrapper -> baseContext.findActivity()
        else -> null
    }
}
