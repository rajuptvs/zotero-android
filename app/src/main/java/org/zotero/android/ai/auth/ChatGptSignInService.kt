package org.zotero.android.ai.auth

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import org.zotero.android.R
import timber.log.Timber

/**
 * Keeps the app process running (and unfrozen) while the user signs in to ChatGPT in the browser,
 * so the loopback listener is still there when the browser redirects back to 127.0.0.1.
 *
 * `shortService` is capped at ~3 minutes, which a first sign-in with 2FA can exceed, so this uses
 * `specialUse` and enforces its own limit ([maxDurationMillis]), which the sign-in also waits for.
 */
class ChatGptSignInService : Service() {

    private val handler = Handler(Looper.getMainLooper())

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val manager = getSystemService(NotificationManager::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            manager.createNotificationChannel(
                NotificationChannel(channelId, "ChatGPT sign-in", NotificationManager.IMPORTANCE_LOW)
            )
        }
        val notification = NotificationCompat.Builder(this, channelId)
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentTitle("Signing in to ChatGPT")
            .setContentText("Finish signing in in your browser.")
            .setOngoing(true)
            .build()
        try {
            val type = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
                ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE
            } else {
                0
            }
            ServiceCompat.startForeground(this, notificationId, notification, type)
        } catch (e: Exception) {
            // Not fatal: sign-in usually still completes while we're the most recent app.
            Timber.w(e, "ChatGptSignInService: can't start in foreground")
        }
        handler.removeCallbacksAndMessages(null)
        handler.postDelayed({ stopSelf() }, maxDurationMillis + 30_000L)
        return START_NOT_STICKY
    }

    override fun onTimeout(startId: Int) {
        stopSelf()
    }

    override fun onTimeout(startId: Int, fgsType: Int) {
        stopSelf()
    }

    override fun onDestroy() {
        handler.removeCallbacksAndMessages(null)
        super.onDestroy()
    }

    companion object {
        const val maxDurationMillis = 10 * 60 * 1000L
        private const val channelId = "chatgpt_sign_in"
        private const val notificationId = 4711

        fun start(context: Context) {
            try {
                context.startService(Intent(context, ChatGptSignInService::class.java))
            } catch (e: Exception) {
                Timber.w(e, "ChatGptSignInService: can't start")
            }
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, ChatGptSignInService::class.java))
        }
    }
}
