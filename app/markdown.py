from __future__ import annotations

import html
import re
from markupsafe import Markup


_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")


def _inline(text: str) -> str:
    escaped = html.escape(text, quote=True)
    escaped = _INLINE_CODE_RE.sub(r"<code>\1</code>", escaped)
    escaped = _LINK_RE.sub(
        r'<a href="\2" target="_blank" rel="noopener noreferrer">\1</a>',
        escaped,
    )
    escaped = _BOLD_RE.sub(r"<strong>\1</strong>", escaped)
    escaped = _ITALIC_RE.sub(r"<em>\1</em>", escaped)
    return escaped


def _flush_paragraph(parts: list[str], out: list[str]) -> None:
    if parts:
        out.append(f"<p>{_inline(' '.join(parts))}</p>")
        parts.clear()


def render_markdown(value: object) -> Markup:
    """Render a conservative Markdown subset for chat messages.

    The renderer escapes input before adding tags, so model/user content cannot
    inject raw HTML. It intentionally supports the chat basics we need here:
    fenced code, headings, unordered/ordered lists, blockquotes, paragraphs,
    links, bold, italic, and inline code.
    """
    text = "" if value is None else str(value)
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    paragraph: list[str] = []
    list_open: str | None = None
    in_code = False
    code_lang = ""
    code_lines: list[str] = []

    def close_list() -> None:
        nonlocal list_open
        if list_open:
            out.append(f"</{list_open}>")
            list_open = None

    for raw in lines:
        line = raw.rstrip()
        fence = re.match(r"^\s*```([\w.+-]*)\s*$", line)
        if fence:
            if in_code:
                lang_class = f' class="language-{html.escape(code_lang, quote=True)}"' if code_lang else ""
                out.append(f"<pre><code{lang_class}>{html.escape(chr(10).join(code_lines))}</code></pre>")
                in_code = False
                code_lang = ""
                code_lines = []
            else:
                _flush_paragraph(paragraph, out)
                close_list()
                in_code = True
                code_lang = fence.group(1)
            continue

        if in_code:
            code_lines.append(raw)
            continue

        if not line.strip():
            _flush_paragraph(paragraph, out)
            close_list()
            continue

        heading = re.match(r"^(#{1,4})\s+(.+)$", line)
        if heading:
            _flush_paragraph(paragraph, out)
            close_list()
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2).strip())}</h{level}>")
            continue

        quote = re.match(r"^>\s?(.*)$", line)
        if quote:
            _flush_paragraph(paragraph, out)
            close_list()
            out.append(f"<blockquote>{_inline(quote.group(1))}</blockquote>")
            continue

        unordered = re.match(r"^\s*[-*]\s+(.+)$", line)
        ordered = re.match(r"^\s*\d+[.)]\s+(.+)$", line)
        if unordered or ordered:
            _flush_paragraph(paragraph, out)
            tag = "ul" if unordered else "ol"
            if list_open != tag:
                close_list()
                out.append(f"<{tag}>")
                list_open = tag
            item = (unordered or ordered).group(1)
            out.append(f"<li>{_inline(item)}</li>")
            continue

        paragraph.append(line.strip())

    if in_code:
        lang_class = f' class="language-{html.escape(code_lang, quote=True)}"' if code_lang else ""
        out.append(f"<pre><code{lang_class}>{html.escape(chr(10).join(code_lines))}</code></pre>")
    _flush_paragraph(paragraph, out)
    close_list()
    return Markup("\n".join(out))
