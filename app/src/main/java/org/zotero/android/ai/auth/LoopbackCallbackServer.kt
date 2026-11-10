package org.zotero.android.ai.auth

import kotlinx.coroutines.CompletableDeferred
import timber.log.Timber
import java.io.IOException
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.URLDecoder
import kotlin.concurrent.thread

/**
 * Minimal HTTP listener on 127.0.0.1 that receives the OAuth redirect. SIWC only accepts an HTTP
 * loopback redirect URI, so the browser hands the authorization code back to us through this
 * socket.
 *
 * Any app on the device can connect to a loopback port, so only a callback carrying the
 * [expectedState] of this sign-in attempt is accepted; everything else gets a 404.
 */
class LoopbackCallbackServer private constructor(
    private val socket: ServerSocket,
    private val expectedState: String,
    private val returnUrl: String,
) {
    val port: Int get() = socket.localPort
    val redirectUri: String
        get() = "http://${ChatGptAuthConfig.callbackHost}:$port${ChatGptAuthConfig.callbackPath}"

    private val callback = CompletableDeferred<Map<String, String>>()

    private fun start() {
        thread(name = "ChatGptLoopback", isDaemon = true) {
            while (!socket.isClosed && !callback.isCompleted) {
                val client = try {
                    socket.accept()
                } catch (e: IOException) {
                    if (!socket.isClosed) {
                        Timber.w(e, "LoopbackCallbackServer: accept failed")
                    }
                    continue
                }
                // One thread per connection, so an idle connection can't hold up the real callback.
                thread(name = "ChatGptLoopbackClient", isDaemon = true) {
                    try {
                        client.use { handle(it) }
                    } catch (e: Exception) {
                        Timber.w(e, "LoopbackCallbackServer: bad request")
                    }
                }
            }
        }
    }

    private fun handle(client: Socket) {
        client.soTimeout = 10_000
        val reader = client.getInputStream().bufferedReader()
        // "GET /auth/callback?code=...&state=... HTTP/1.1"
        val requestLine = reader.readLine() ?: return
        while (!reader.readLine().isNullOrEmpty()) {
            // Drain the headers.
        }
        val target = requestLine.split(" ").getOrNull(1) ?: return
        val params = if (target.substringBefore("?") == ChatGptAuthConfig.callbackPath) {
            parseQuery(target.substringAfter("?", missingDelimiterValue = ""))
        } else {
            null
        }
        if (params == null || params["state"] != expectedState) {
            respond(client, status = "404 Not Found", body = "")
            return
        }
        callback.complete(params)
        respond(client, status = "200 OK", body = resultPage(succeeded = params["error"] == null))
    }

    private fun parseQuery(query: String): Map<String, String>? = try {
        query.split("&")
            .filter { it.isNotEmpty() }
            .associate {
                val name = URLDecoder.decode(it.substringBefore("="), "UTF-8")
                val value = URLDecoder.decode(it.substringAfter("=", ""), "UTF-8")
                name to value
            }
    } catch (e: IllegalArgumentException) {
        null
    }

    private fun respond(client: Socket, status: String, body: String) {
        val bytes = body.toByteArray()
        val header = "HTTP/1.1 $status\r\n" +
                "Content-Type: text/html; charset=utf-8\r\n" +
                "Content-Length: ${bytes.size}\r\n" +
                "Cache-Control: no-store\r\n" +
                "Connection: close\r\n\r\n"
        client.getOutputStream().apply {
            write(header.toByteArray())
            write(bytes)
            flush()
        }
    }

    private fun resultPage(succeeded: Boolean): String {
        val title = if (succeeded) "Signed in to ChatGPT" else "Sign-in was not completed"
        return """
            <!doctype html><html><head><meta charset="utf-8">
            <meta name="viewport" content="width=device-width,initial-scale=1">
            <title>$title</title>
            <style>
              body{font-family:sans-serif;margin:48px 24px;text-align:center;color:#222}
              a{display:inline-block;margin-top:24px;padding:14px 28px;border-radius:24px;
                background:#3367d6;color:#fff;text-decoration:none;font-weight:600}
            </style></head>
            <body><h2>$title</h2><p>You can go back to Zotero now.</p>
            <a href="$returnUrl">Return to Zotero</a>
            <script>setTimeout(function(){location.href="$returnUrl"},300)</script>
            </body></html>
        """.trimIndent()
    }

    suspend fun awaitCallback(): Map<String, String> = callback.await()

    fun close() {
        try {
            socket.close()
        } catch (_: IOException) {
        }
    }

    companion object {
        fun start(expectedState: String, returnUrl: String): LoopbackCallbackServer {
            val address = InetAddress.getByName(ChatGptAuthConfig.callbackHost)
            val socket = try {
                ServerSocket(ChatGptAuthConfig.preferredCallbackPort, 8, address)
            } catch (e: IOException) {
                Timber.w(e, "LoopbackCallbackServer: preferred port busy, using a random one")
                ServerSocket(0, 8, address)
            }
            return LoopbackCallbackServer(socket, expectedState, returnUrl).also { it.start() }
        }
    }
}
