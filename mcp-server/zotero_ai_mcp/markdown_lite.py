"""Small Markdown → note HTML for notes the standalone server creates (zotero_create_note).

Covers what models write in notes: headings, paragraphs, bullet and numbered lists, block quotes,
fenced code, inline code, bold, italic, links, and math as Zotero note math (chat.md). Raw HTML is
escaped. The clients use full CommonMark renderers; notes from this server may format edge cases
more simply.
"""
import html
import re

MATH_OR_CODE = re.compile(r"(```[\s\S]*?```|`[^`\n]*`)|\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|\$(?!\s)([^$\n]+?)(?<!\s)\$(?!\d)")


def _esc(text):
    return html.escape(text, quote=True).replace("&#x27;", "'")


def _inline(text, math):
    def code(m):
        return f"<code>{_esc(m.group(1))}</code>"

    parts = re.split(r"(`[^`\n]*`)", text)
    out = []
    for part in parts:
        if part.startswith("`") and part.endswith("`") and len(part) >= 2:
            out.append(code(re.match(r"`(.*)`", part, re.S)))
            continue
        s = _esc(part)
        s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+|mailto:[^)\s]+)\)", r'<a href="\2">\1</a>', s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])", r"<em>\1</em>", s)
        s = re.sub(r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])", r"<em>\1</em>", s)
        out.append(s)
    html_text = "".join(out)
    for i, (tex, display) in enumerate(math):
        token = f"ZAIMATH{i}Z"
        if display and html_text.strip() == token:
            return f'<pre class="math">$${_esc(tex)}$$</pre>'
        html_text = html_text.replace(token, f'<span class="math">${_esc(tex)}$</span>')
    return html_text


def to_html(markdown):
    math = []

    def protect(m):
        if m.group(1) is not None:
            return m.group(0)
        display = m.group(2) if m.group(2) is not None else m.group(3)
        inline = m.group(4) if m.group(4) is not None else m.group(5)
        math.append(((display if display is not None else inline).strip(), display is not None))
        return f"ZAIMATH{len(math) - 1}Z"

    text = MATH_OR_CODE.sub(protect, markdown or "")
    lines = text.split("\n")
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            lang = line.strip()[3:].strip()
            body = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1
            cls = f' class="language-{_esc(lang)}"' if lang else ""
            out.append(f"<pre><code{cls}>{_esc(chr(10).join(body))}\n</code></pre>")
            continue
        if not line.strip():
            i += 1
            continue
        m = re.match(r"(#{1,6})\s+(.*)", line)
        if m:
            n = len(m.group(1))
            out.append(f"<h{n}>{_inline(m.group(2).strip(), math)}</h{n}>")
            i += 1
            continue
        if re.match(r"\s*([-*+]|\d+[.)])\s+", line):
            ordered = bool(re.match(r"\s*\d+[.)]\s+", line))
            items = []
            while i < len(lines) and re.match(r"\s*([-*+]|\d+[.)])\s+", lines[i]):
                items.append(re.sub(r"\s*([-*+]|\d+[.)])\s+", "", lines[i], count=1))
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{_inline(it, math)}</li>" for it in items) + f"</{tag}>")
            continue
        if line.startswith(">"):
            quote = []
            while i < len(lines) and lines[i].startswith(">"):
                quote.append(lines[i][1:].strip())
                i += 1
            out.append(f"<blockquote><p>{_inline(' '.join(quote), math)}</p></blockquote>")
            continue
        para = []
        while i < len(lines) and lines[i].strip() and not lines[i].strip().startswith("```") and not re.match(r"(#{1,6})\s|\s*([-*+]|\d+[.)])\s+|>", lines[i]):
            para.append(lines[i].strip())
            i += 1
        rendered = _inline(" ".join(para), math)
        out.append(rendered if rendered.startswith('<pre class="math">') else f"<p>{rendered}</p>")
    return "\n".join(out) + ("\n" if out else "")
