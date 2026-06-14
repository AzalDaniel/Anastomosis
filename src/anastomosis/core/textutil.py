"""Text cleaning for source export cells and note HTML.

Three jobs:

* **Cell hygiene** — TSV/CSV dumps encode "no value" several ways
  (``\\N`` MySQL null escapes, literal ``NULL``, ``-1`` in numeric columns).
  :func:`clean_cell` / :func:`clean_numeric` normalize all of them to ``None``
  so sentinels can never masquerade as clinical values downstream.
* **Note HTML → text** — source note bodies arrive as HTML fragments.
  :func:`html_to_text` extracts readable text with paragraph structure
  preserved, using the stdlib parser (never regex-over-HTML), dropping
  script/style content outright. This feeds plain-text consumers (search, QA,
  addendum bodies).
* **Note HTML → rich HTML** — :func:`sanitize_soap_html` is the rendering path:
  it *preserves* the source's semantic HTML and only repairs it (TSV-exported
  ``\\n`` → ``<br>``, empty-block strip, ``pf-rich-text`` wrap) so a chart
  renders the way the source authored it.
"""

from __future__ import annotations

import html as html_mod
import re
from html.parser import HTMLParser

__all__ = [
    "clean_cell",
    "clean_numeric",
    "format_phone",
    "html_to_text",
    "sanitize_soap_html",
]

# Literal cell values that mean "no value" in source dumps.
_NULL_TOKENS = frozenset({r"\N", "NULL", "null"})
# Additional sentinels seen only in numeric columns.
_NUMERIC_SENTINELS = frozenset({"-1", "-1.0"})


def clean_cell(value: str | None) -> str | None:
    """Strip a raw export cell; null-sentinels and blanks become ``None``."""
    if value is None:
        return None
    text = value.strip()
    if not text or text in _NULL_TOKENS:
        return None
    return text


def clean_numeric(value: str | None) -> str | None:
    """:func:`clean_cell`, plus the ``-1`` not-set sentinel numeric columns use."""
    text = clean_cell(value)
    if text is None or text in _NUMERIC_SENTINELS:
        return None
    return text


def format_phone(raw: str | None) -> str | None:
    """Normalize a US phone number to ``(XXX) XXX-XXXX`` where possible.

    Ten digits (or eleven with a leading 1) get the standard chart format;
    anything else is returned stripped-but-unchanged — a partial number is
    still information, and losing it would violate the lossless guarantee.
    """
    if raw is None:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return clean_cell(raw)


# Paragraph-level tags separate with a blank line; remaining block tags
# (list items, divs, table rows...) get a single line break. Table cells get
# a space so adjacent cells never fuse ("height64in" hides values from
# boundary-anchored QA matching).
_PARA_TAGS = frozenset({"p", "blockquote", "table", "h1", "h2", "h3", "h4", "h5", "h6"})
_BLOCK_TAGS = _PARA_TAGS | frozenset({"div", "br", "hr", "li", "ul", "ol", "tr", "section"})
_CELL_TAGS = frozenset({"td", "th"})


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")
        elif tag in _CELL_TAGS:
            self._parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _PARA_TAGS:
            self._parts.append("\n")
        elif tag in _CELL_TAGS:
            self._parts.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # Self-closing tags (<br/>) are one boundary, not an open+close pair.
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            # Raw whitespace in HTML source carries no meaning; structure
            # comes only from the tag boundaries above.
            self._parts.append(re.sub(r"\s+", " ", data))

    def text(self) -> str:
        return "".join(self._parts)


def html_to_text(html: str | None) -> str | None:
    """Extract readable text from an HTML note fragment.

    Block-level tags become line breaks, runs of blank lines collapse to one
    blank line, entities are decoded, and script/style bodies are dropped.
    Plain text input passes through unharmed. Returns ``None`` when nothing
    readable remains.
    """
    if html is None:
        return None
    extractor = _TextExtractor()
    extractor.feed(html)
    extractor.close()
    lines = (re.sub(r"[ \t]{2,}", " ", line.strip()) for line in extractor.text().split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text or None


# Empty filler blocks PF leaves behind — stripped so a blank <p></p> never
# renders as a stray gap (generate_pdfs.py:151-154 empty_block_patterns).
_EMPTY_BLOCK_PATTERNS = (
    r"<p[^>]*>\s*(?:&nbsp;|&#160;|<br\s*/?>)?\s*</p>",
    r"<div[^>]*>\s*(?:&nbsp;|&#160;|<br\s*/?>)?\s*</div>",
    r"<h([1-6])[^>]*>\s*(?:&nbsp;|&#160;|<br\s*/?>)?\s*</h\1>",
)
# A stray \n that is NOT immediately adjacent to a block tag boundary becomes a
# <br> (generate_pdfs.py:150) — inline line breaks (e.g. numbered injection
# sites) must survive into the rendered chart.
_STRAY_NEWLINE_RE = re.compile(r"\n(?!</(p|div|ul|ol|li|h[1-6])>)(?!<(p|div|ul|ol|li|h[1-6])[ >])")


def sanitize_soap_html(raw_html: str | None) -> str:
    """Keep PF semantic HTML, removing only empty filler blocks.

    Ported from the predecessor's ``sanitize_soap_html`` (generate_pdfs.py:137).
    The EHI TSV export converts ``<br>`` to ``\\n`` — we restore them so line
    breaks within inline content render correctly in the browser. Output is HTML
    intended for ``autoescape=False`` rendering, wrapped in ``pf-rich-text``.

    This is the *rendering* path; :func:`html_to_text` remains the plain-text
    path for search/QA/addendum bodies.
    """
    if not raw_html:
        return ""
    text = str(raw_html).strip()
    # Unescape TSV-exported newlines back to real newlines first (gpdfs:144).
    text = text.replace("\\\\n", "\n").replace("\\n", "\n")
    if "<" not in text:
        # Plain text: escape, then turn newlines into <br> (gpdfs:146).
        return html_mod.escape(text).replace("\n", "<br>").strip()
    # Convert remaining \n inside HTML content to <br> so inline line breaks
    # render correctly; only convert \n NOT between two block tags (gpdfs:150).
    text = _STRAY_NEWLINE_RE.sub("<br>\n", text)
    for pattern in _EMPTY_BLOCK_PATTERNS:  # gpdfs:156 — strip empty blocks
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    if "pf-rich-text" not in text:  # gpdfs:158 — wrap once
        text = f'<div class="pf-rich-text">{text}</div>'
    return text.strip()
