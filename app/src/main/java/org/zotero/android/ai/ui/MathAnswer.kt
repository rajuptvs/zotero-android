package org.zotero.android.ai.ui

import android.annotation.SuppressLint
import android.content.Intent
import android.graphics.Color
import android.os.Handler
import android.os.Looper
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
import androidx.compose.ui.graphics.luminance
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import com.google.gson.Gson
import org.zotero.android.ai.chat.ChatMarkdown
import timber.log.Timber

/**
 * An answer with math, typeset by KaTeX (assets/ai-chat, MIT) as MathML in a small WebView. The
 * page loads once; the answer's blocks (ChatMarkdown.splitBlocks) are pushed into it as they
 * stream, and only blocks whose text changed are replaced, so finished formulas stay typeset. The
 * HTML is ChatMarkdown's note HTML (raw HTML escaped, images as text), the same markup the desktop
 * panel typesets. The WebView loads nothing from the network.
 *
 * [stateKey] is the turn's list key: it stays the same when a streaming answer is saved, so the
 * WebView and its measured height carry over.
 */
@SuppressLint("SetJavaScriptEnabled")
@Composable
internal fun MathAnswer(blocks: List<String>, stateKey: String, modifier: Modifier = Modifier) {
    val colors = MaterialTheme.colorScheme
    val textSize = MaterialTheme.typography.bodyMedium.fontSize.value
    val page = remember(colors, textSize) {
        mathPage(colors.onSurface, colors.primary, colors.surfaceVariant, colors.outlineVariant, textSize, dark = colors.surface.luminance() < 0.5f)
    }
    val htmlBlocks = remember(blocks) { blocks.map(ChatMarkdown::toHtml) }
    var heightDp by rememberSaveable(stateKey) { mutableIntStateOf(0) }
    val bridge = remember { MathBridge() }
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
                    override fun onPageFinished(view: WebView, url: String?) {
                        bridge.pageReady(view)
                    }

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
                bridge.pageLoading()
                webView.loadDataWithBaseURL("file:///android_asset/ai-chat/", page, "text/html", "utf-8", null)
            }
            bridge.push(webView, htmlBlocks)
        },
        onRelease = { it.destroy() },
    )
}

/**
 * Between Compose and the page: holds the latest blocks until the page has loaded, sends at most
 * one update every 50 ms, and receives the page's height (CSS pixels, which are dp here).
 */
private class MathBridge {
    private val main = Handler(Looper.getMainLooper())
    private val gson = Gson()
    private var ready = false
    private var scheduled = false
    private var latest: List<String> = emptyList()
    private var sent: List<String>? = null
    private var view: WebView? = null

    @Volatile
    var onHeight: (Int) -> Unit = {}

    fun pageLoading() {
        Timber.d("MathAnswer: page load")
        ready = false
        sent = null
    }

    fun pageReady(webView: WebView) {
        ready = true
        view = webView
        flush()
    }

    fun push(webView: WebView, blocks: List<String>) {
        view = webView
        latest = blocks
        if (!ready || scheduled) return
        scheduled = true
        main.postDelayed({
            scheduled = false
            flush()
        }, 50)
    }

    private fun flush() {
        val webView = view ?: return
        if (!ready || latest == sent) return
        sent = latest
        // JSON is a valid JavaScript expression, so the blocks arrive exactly as written.
        webView.evaluateJavascript("setBlocks(${gson.toJson(latest)})", null)
    }

    @JavascriptInterface
    fun height(cssPixels: Int) {
        if (cssPixels > 0) main.post { onHeight(cssPixels) }
    }
}

private fun mathPage(
    text: ComposeColor,
    link: ComposeColor,
    codeBackground: ComposeColor,
    border: ComposeColor,
    textSizeSp: Float,
    dark: Boolean,
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
${if (dark) CODE_THEME_DARK else CODE_THEME_LIGHT}
</style></head><body>
<div id="answer"></div>
<script src="katex.min.js"></script>
<script src="highlight.min.js"></script>
<script>
const shown = []; // [{html, el}] in order
function typeset(root) {
  for (const el of root.querySelectorAll(".math")) {
    const display = el.tagName == "PRE";
    const tex = el.textContent.replace(display ? /^\$\$|\$\$$/g : /^\$|\$$/g, "");
    try {
      const holder = document.createElement("span");
      katex.render(tex, holder, { output: "mathml", displayMode: display, throwOnError: true, trust: false, maxSize: 20, maxExpand: 1000 });
      el.replaceWith(holder);
    } catch (e) {
      // Unsupported TeX: keep the source.
    }
  }
}
// Syntax colors for code whose fence has closed (highlight.js, BSD-3-Clause).
function highlight(root) {
  for (const el of root.querySelectorAll("pre:not(.math) > code[class*='language-']")) {
    const lang = (el.className.match(/language-([\w+-]+)/) || [])[1];
    if (!lang || !hljs.getLanguage(lang) || el.textContent.length > 20000) continue;
    el.innerHTML = hljs.highlight(el.textContent, { language: lang, ignoreIllegals: true }).value;
  }
}
// Replaces only the blocks whose HTML changed; finished blocks keep their typeset math.
function setBlocks(blocks) {
  const answer = document.getElementById("answer");
  blocks.forEach((html, i) => {
    if (shown[i] && shown[i].html === html) return;
    const el = document.createElement("div");
    el.innerHTML = html;
    typeset(el);
    // The last block may still be streaming; colour its code once a later block exists.
    if (i < blocks.length - 1) highlight(el);
    if (shown[i]) shown[i].el.replaceWith(el);
    else answer.appendChild(el);
    shown[i] = { html, el };
  });
  for (const extra of shown.splice(blocks.length)) extra.el.remove();
  report();
}
const report = () => AiChat.height(Math.ceil(document.documentElement.getBoundingClientRect().height));
new ResizeObserver(report).observe(document.body);
</script></body></html>"""
}

// Code colors (GitHub-like) for highlight.js classes; same palette as the desktop panel.
private const val CODE_THEME_LIGHT = ".hljs-keyword,.hljs-selector-tag,.hljs-doctag{color:#cf222e}.hljs-string,.hljs-regexp,.hljs-meta .hljs-string{color:#0a3069}.hljs-number,.hljs-literal,.hljs-attr,.hljs-attribute,.hljs-variable,.hljs-template-variable,.hljs-property{color:#0550ae}.hljs-comment,.hljs-quote,.hljs-meta{color:#6e7781;font-style:italic}.hljs-title,.hljs-section,.hljs-function .hljs-title{color:#8250df}.hljs-built_in,.hljs-type,.hljs-symbol{color:#953800}"
private const val CODE_THEME_DARK = ".hljs-keyword,.hljs-selector-tag,.hljs-doctag{color:#ff7b72}.hljs-string,.hljs-regexp,.hljs-meta .hljs-string{color:#a5d6ff}.hljs-number,.hljs-literal,.hljs-attr,.hljs-attribute,.hljs-variable,.hljs-template-variable,.hljs-property{color:#79c0ff}.hljs-comment,.hljs-quote,.hljs-meta{color:#8b949e;font-style:italic}.hljs-title,.hljs-section,.hljs-function .hljs-title{color:#d2a8ff}.hljs-built_in,.hljs-type,.hljs-symbol{color:#ffa657}"
