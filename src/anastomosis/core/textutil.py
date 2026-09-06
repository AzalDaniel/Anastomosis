"""Text cleaning for source export cells and note HTML.

Four jobs: cell hygiene (:func:`clean_cell`/:func:`clean_numeric` normalize
null sentinels to ``None``); HTML notes to plain text (:func:`html_to_text`,
stdlib parser, never regex-over-HTML); semantic HTML repaired for rendering
(:func:`sanitize_soap_html`); and the one filesystem-name definition (17) —
:func:`safe_name`/:func:`budgeted_name`.
"""

from __future__ import annotations

import hashlib
import html as html_mod
import mimetypes
import re
from html.parser import HTMLParser
from pathlib import Path

__all__ = [
    "HASH_TAG_CHARS",
    "MAX_NAME_CHARS",
    "MAX_PATH_CHARS",
    "budgeted_name",
    "clean_cell",
    "clean_numeric",
    "format_phone",
    "html_to_text",
    "media_type_suffix",
    "safe_name",
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
    """Normalize a US phone number to ``(XXX) XXX-XXXX``. Ten digits (or
    eleven with a leading 1) get that format; anything else returns
    stripped-but-unchanged — a partial number is still information."""
    if raw is None:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return clean_cell(raw)


# ASCII letters/digits, `_`, `-`: the POSIX/Windows-safe intersection, so a
# name from this set never needs escaping or escapes its slot.
_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")

#: Longest component :func:`safe_name` returns; 200 stays under Windows'/
#: POSIX's ~255 component limit with room for suffixes writers append.
MAX_NAME_CHARS = 200
#: Longest full path :func:`budgeted_name` allows; 240 keeps a delivered tree
#: openable under Windows' 260-char ``MAX_PATH`` without long-path support.
MAX_PATH_CHARS = 240
#: Hex characters of sha256 appended when a value is cut (17); also the
#: shortest name :func:`budgeted_name` can return, which a caller reserves
#: room for. Chosen against the birthday bound, not for looks.
HASH_TAG_CHARS = 16


def _hash_tagged(cleaned: str, limit: int) -> str:
    """Contract: ``cleaned`` cut to ``limit`` chars, tagged with
    ``-<HASH_TAG_CHARS hex of sha256>`` of the ORIGINAL value when cut, so
    two ids differing only past the cut still land on different names (17).
    Raises :class:`ValueError` when ``limit`` cannot hold the tag."""
    if len(cleaned) <= limit:
        return cleaned
    if limit < HASH_TAG_CHARS:
        raise ValueError(
            f"cannot build a distinct filesystem name in {limit} character(s); "
            f"at least {HASH_TAG_CHARS} are needed"
        )
    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:HASH_TAG_CHARS]
    kept = cleaned[: limit - HASH_TAG_CHARS - 1].rstrip("_-")
    return f"{kept}-{digest}" if kept else digest


def safe_name(value: str | None, fallback: str) -> str:
    """Contract: a filesystem-safe name, ``fallback`` if nothing survives,
    never longer than :data:`MAX_NAME_CHARS` (cut and tagged past that, see
    :func:`_hash_tagged`). The one definition behind every delivered
    filename (17); changing it renames delivered output."""
    cleaned = _UNSAFE_NAME_RE.sub("_", (value or "").strip()).strip("_")
    return _hash_tagged(cleaned or fallback, MAX_NAME_CHARS)


def media_type_suffix(media_type: str | None) -> str:
    """A file extension for ``media_type``: names the bytes on disk, does
    not type them (``mime_type`` stays verbatim). An unmapped type gets no
    suffix, never a guessed one. Shared by the C-CDA reader and deliverer so
    a reference never names a differently-suffixed artifact (#373)."""
    if not media_type:
        return ""
    return mimetypes.guess_extension(media_type.split(";")[0].strip()) or ""


def budgeted_name(
    value: str | None,
    fallback: str,
    *,
    parent: Path,
    suffix: str = "",
    reserve: int = 0,
) -> str:
    """Contract: :func:`safe_name`, cut further so ``parent/<name><suffix>``
    stays inside :data:`MAX_PATH_CHARS`. ``suffix``/``reserve`` are budgeted
    but not appended — for a suffix the caller adds, for a directory the
    room its deepest child needs. ``parent`` must be the real output path.
    Raises when even a hash tag would not fit."""
    name = safe_name(value, fallback)
    # +1 for the separator the caller's ``parent / name`` will insert.
    room = MAX_PATH_CHARS - len(str(parent)) - 1 - len(suffix) - reserve
    if len(name) <= room:
        return name
    if room < HASH_TAG_CHARS:
        # PHI: lengths only — an output path can be named after a patient, so
        # it never enters a message or a log line (SECURITY.md).
        raise ValueError(
            f"output directory is {len(str(parent))} characters deep, leaving no room "
            f"for a distinct name within the {MAX_PATH_CHARS}-character path budget"
        )
    return _hash_tagged(name, room)


# Paragraph tags get a blank-line break, other block tags a single break;
# table cells get a space so adjacent cells never fuse ("height64in" would
# hide values from boundary-anchored matching, 6).
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
    """Extract readable text from an HTML note fragment: block tags become
    line breaks, entities decode, script/style bodies drop, plain text
    passes through unharmed. ``None`` when nothing readable remains."""
    if html is None:
        return None
    extractor = _TextExtractor()
    extractor.feed(html)
    extractor.close()
    lines = (re.sub(r"[ \t]{2,}", " ", line.strip()) for line in extractor.text().split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text or None


# Empty filler blocks PF leaves behind, stripped so a blank <p></p> never
# renders as a stray gap.
_EMPTY_BLOCK_PATTERNS = (
    r"<p[^>]*>\s*(?:&nbsp;|&#160;|<br\s*/?>)?\s*</p>",
    r"<div[^>]*>\s*(?:&nbsp;|&#160;|<br\s*/?>)?\s*</div>",
    r"<h([1-6])[^>]*>\s*(?:&nbsp;|&#160;|<br\s*/?>)?\s*</h\1>",
)
# A stray \n not adjacent to a block tag boundary becomes a <br>, so inline
# line breaks survive into the rendered chart.
_STRAY_NEWLINE_RE = re.compile(r"\n(?!</(p|div|ul|ol|li|h[1-6])>)(?!<(p|div|ul|ol|li|h[1-6])[ >])")


# Tags whose CONTENT is dropped too: a known XSS/info-disclosure carrier in
# the local-Chromium render context, so both tag and body are stripped.
_DROP_CONTENT_TAGS = frozenset(
    {
        "script",
        "style",
        "iframe",
        "object",
        "embed",
        "noscript",
        "head",
        "title",
        "link",
        "meta",
        "form",
        "button",
        "input",
        "textarea",
        "select",
        "option",
        "svg",
        "math",
        "applet",
    }
)
# Tags that pass through, chosen to cover legitimate clinical SOAP HTML;
# anything else is dropped but its TEXT children still flow through.
_ALLOWED_TAGS = frozenset(
    {
        "p",
        "br",
        "div",
        "span",
        "b",
        "i",
        "em",
        "strong",
        "u",
        "s",
        "small",
        "sub",
        "sup",
        "ul",
        "ol",
        "li",
        "dl",
        "dt",
        "dd",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
        "pre",
        "code",
        "hr",
        "table",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "td",
        "th",
        "caption",
        "colgroup",
        "col",
    }
)
# The only attribute let through: PF/generic SOAP templates style on class
# hooks only, and admitting style/href/src/event handlers is the XSS surface
# this sanitizer closes.
_ALLOWED_ATTRS = frozenset({"class"})
# Void elements among _ALLOWED_TAGS — no closing tag emitted.
_VOID_ALLOWED = frozenset({"br", "hr", "col"})
# Full HTML5 void-element set (html.spec.whatwg.org/#void-elements):
# drop-content tags here have no end-tag in source, so they must not enter
# drop mode or everything after them is silently swallowed.
_HTML5_VOID = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


class _SoapHtmlSanitizer(HTMLParser):
    """Contract: stdlib-only allowlist, defense-in-depth for the
    local-Chromium renderer, not a general web sanitizer. Drop-content tags
    lose their body too; non-allowlist tags lose the tag but keep their
    text; allowlist tags keep only :data:`_ALLOWED_ATTRS`.
    :func:`sanitize_soap_html` layers PF's repairs on top."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._parts: list[str] = []
        self._drop_depth = 0  # >0 while inside a drop-content tag

    def _format_attrs(self, attrs: list[tuple[str, str | None]]) -> str:
        out = ""
        for name, value in attrs:
            if not name:
                continue
            n = name.lower()
            if n not in _ALLOWED_ATTRS:
                continue
            if value is None:
                out += f" {n}"
            else:
                out += f' {n}="{html_mod.escape(value, quote=True)}"'
        return out

    def _emit_start(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
        self_closing: bool,
    ) -> None:
        if tag in _DROP_CONTENT_TAGS:
            # Void drop tags have no closing tag in source, so must not enter
            # drop mode (it would swallow everything after); no body anyway.
            if tag in _HTML5_VOID or self_closing:
                return
            self._drop_depth += 1
            return
        if self._drop_depth or tag not in _ALLOWED_TAGS:
            return
        # When every attribute is already allowed, emit the source tag
        # verbatim (byte-identical for the PF/Tebra e2e goldens) unless it
        # spans multiple lines, which would let the stray-newline regex
        # inject <br> inside the tag — reconstruct on one line instead.
        if all(name and name.lower() in _ALLOWED_ATTRS for name, _ in attrs):
            original = self.get_starttag_text()
            if original is not None and "\n" not in original:
                self._parts.append(original)
                return
        suffix = "/>" if self_closing and tag in _VOID_ALLOWED else ">"
        self._parts.append(f"<{tag}{self._format_attrs(attrs)}{suffix}")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._emit_start(tag.lower(), attrs, self_closing=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # Self-closing form (<br/>) — preserved verbatim when attrs are clean.
        self._emit_start(tag.lower(), attrs, self_closing=True)

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in _DROP_CONTENT_TAGS:
            self._drop_depth = max(0, self._drop_depth - 1)
            return
        if self._drop_depth or t not in _ALLOWED_TAGS or t in _VOID_ALLOWED:
            return
        self._parts.append(f"</{t}>")

    def handle_data(self, data: str) -> None:
        if self._drop_depth:
            return
        self._parts.append(data)

    def handle_entityref(self, name: str) -> None:
        if self._drop_depth:
            return
        self._parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._drop_depth:
            return
        self._parts.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        # Comments are dropped wholesale — ``<!--<script-->`` is a classic
        # parser-disagreement bypass vector and clinical notes have no need
        # for HTML comments.
        return

    def handle_decl(self, decl: str) -> None:
        # Drop doctype declarations; never meaningful inside a SOAP fragment.
        return

    def handle_pi(self, data: str) -> None:
        # Processing instructions can carry payloads in some renderers; drop.
        return

    def cleaned(self) -> str:
        return "".join(self._parts)


def sanitize_soap_html(raw_html: str | None) -> str:
    """Allowlist-clean PF semantic HTML via :class:`_SoapHtmlSanitizer`
    (needed because the section renders with Jinja's ``| safe``, autoescape
    off, by design), then apply the PF repairs (TSV ``\\n``→``<br>``,
    empty-block strip, ``pf-rich-text`` wrap). The rendering path;
    :func:`html_to_text` is the plain-text one."""
    if not raw_html:
        return ""
    text = str(raw_html).strip()
    # Unescape TSV-exported newlines back to real newlines first.
    text = text.replace("\\\\n", "\n").replace("\\n", "\n")
    if "<" not in text:
        # Plain text: escape, then turn newlines into <br>. No
        # angle brackets means no allowlist work to do — and html.escape on
        # plain text strictly defeats any HTML interpretation downstream.
        return html_mod.escape(text).replace("\n", "<br>").strip()
    # Allowlist-strip BEFORE the legacy repairs so the repairs operate on
    # already-safe content and any inserted markup (<br>) is trivially clean.
    sanitizer = _SoapHtmlSanitizer()
    sanitizer.feed(text)
    sanitizer.close()
    text = sanitizer.cleaned()
    # Convert remaining \n inside HTML content to <br> so inline line breaks
    # render correctly; only convert \n NOT between two block tags.
    text = _STRAY_NEWLINE_RE.sub("<br>\n", text)
    for pattern in _EMPTY_BLOCK_PATTERNS:  # strip empty filler blocks
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    if "pf-rich-text" not in text:  # wrap once (idempotent guard)
        text = f'<div class="pf-rich-text">{text}</div>'
    return text.strip()
