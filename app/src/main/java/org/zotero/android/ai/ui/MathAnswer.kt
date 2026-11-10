package org.zotero.android.ai.ui

import android.annotation.SuppressLint
import android.content.Intent
import android.graphics.Color
import android.net.Uri
import android.webkit.JavascriptInterface
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color as ComposeColor
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import org.zotero.android.ai.chat.ChatMarkdown

/**
 * An answer with math, typeset by KaTeX (assets/ai-chat, MIT) as MathML in a small WebView. The
 * HTML is ChatMarkdown's note HTML (raw HTML escaped, images as text), the same markup the
 * desktop panel typesets, so both show the same thing. The WebView loads nothing from the network.
 */
@SuppressLint("SetJavaScriptEnabled")
@Composable
internal fun MathAnswer(markdown: String, modifier: Modifier = Modifier) {
    val colors = MaterialTheme.colorScheme
    val textSize = MaterialTheme.typography.bodyMedium.fontSize.value
    val page = remember(markdown, colors, textSize) {
        mathPage(ChatMarkdown.toHtml(markdown), colors.onSurface, colors.primary, colors.surfaceVariant, colors.outlineVariant, textSize)
    }
    // Remembered per answer so the list doesn't jump while the page measures itself again.
    var heightDp by rememberSaveable(markdown) { mutableIntStateOf(0) }
    val bridge = remember { HeightBridge() }
    bridge.onHeight = { heightDp = it }
    AndroidView(
        modifier = modifier
            .fillMaxWidth()
            .height(if (heightDp > 0) heightDp.dp else 48.dp),
        factory = { context ->
            WebView(context).apply {
                setBackgroundColor(Color.TRANSPARENT)
                isVerticalScrollBarEnabled = false
                settings.javaScriptEnabled = true
                settings.allowFileAccess = false
                settings.allowContentAccess = false
                settings.blockNetworkLoads = true
                addJavascriptInterface(bridge, "AiChat")
                webViewClient = object : WebViewClient() {
                    override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                        val url = request.url
                        if (url.scheme == "http" || url.scheme == "https" || url.scheme == "mailto") {
                            runCatching { context.startActivity(Intent(Intent.ACTION_VIEW, url)) }
                        }
                        return true
                    }
                }
            }
        },
        update = { webView ->
            if (webView.tag != page) {
                webView.tag = page
                webView.loadDataWithBaseURL("file:///android_asset/ai-chat/", page, "text/html", "utf-8", null)
            }
        },
    )
}

/** Lets the page report its height (in CSS pixels, which are dp here) back to Compose. */
private class HeightBridge {
    @Volatile
    var onHeight: (Int) -> Unit = {}

    @JavascriptInterface
    fun height(cssPixels: Int) {
        if (cssPixels > 0) android.os.Handler(android.os.Looper.getMainLooper()).post { onHeight(cssPixels) }
    }
}

private fun mathPage(
    body: String,
    text: ComposeColor,
    link: ComposeColor,
    codeBackground: ComposeColor,
    border: ComposeColor,
    textSizeSp: Float,
): String {
    fun css(color: ComposeColor) = String.format("#%06X", color.toArgb() and 0xFFFFFF)
    return """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
html, body { margin: 0; padding: 0; background: transparent; }
body { color: ${css(text)}; font: ${textSizeSp}px/1.45 sans-serif; overflow-wrap: anywhere; }
p { margin: 0 0 8px; }
a { color: ${css(link)}; }
code { font-family: monospace; background: ${css(codeBackground)}; padding: 0 3px; border-radius: 3px; }
pre:not(.math) { background: ${css(codeBackground)}; padding: 8px; border-radius: 6px; overflow-x: auto; }
pre:not(.math) code { background: none; padding: 0; }
table { border-collapse: collapse; display: block; overflow-x: auto; margin: 0 0 8px; }
td, th { border: 1px solid ${css(border)}; padding: 3px 6px; }
blockquote { margin: 0 0 8px; padding-left: 10px; border-left: 3px solid ${css(border)}; }
ul, ol { margin: 0 0 8px; padding-left: 22px; }
math { font-size: 1.1em; }
math[display="block"] { display: block; overflow-x: auto; margin: 6px 0; }
</style></head><body>
$body
<script src="katex.min.js"></script>
<script>
for (const el of document.querySelectorAll(".math")) {
  const display = el.tagName == "PRE";
  const tex = el.textContent.replace(display ? /^\$\$|\$\$$/g : /^\$|\$$/g, "");
  try {
    const holder = document.createElement("span");
    katex.render(tex, holder, { output: "mathml", displayMode: display, throwOnError: true });
    el.replaceWith(holder);
  } catch (e) {
    // Unsupported TeX: keep the source.
  }
}
const report = () => AiChat.height(Math.ceil(document.documentElement.getBoundingClientRect().height));
new ResizeObserver(report).observe(document.body);
report();
</script></body></html>"""
}
