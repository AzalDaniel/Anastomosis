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
* **Text → filesystem name** — :func:`safe_name` is the ONE definition of a
  filesystem-safe file/directory component (renderer filenames, archive and
  bundle patient directories, C-CDA export filenames), and
  :func:`budgeted_name` is the ONE definition of "…and it still fits inside
  the full path a writer is about to build". Delivered layouts depend on
  their exact output, so every writer shares them rather than re-deriving the
  same regex and the same length arithmetic.
"""

from __future__ import annotations

import hashlib
import html as html_mod
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


# Everything outside the safe set collapses to a single underscore. ASCII
# letters/digits plus ``_`` and ``-`` are the intersection of what POSIX and
# Windows accept unquoted, so a name built from this set never needs escaping
# and never escapes its slot (no ``/``, no ``..``, no drive letters).
_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")

#: Longest component :func:`safe_name` will hand back. Windows' per-component
#: limit is 255 and POSIX ``NAME_MAX`` is usually 255 too; 200 leaves room for
#: the suffixes writers append (``.html``, ``.xml``, a collision tag) while
#: staying far enough under both that an unbounded source id cannot produce a
#: component the filesystem refuses (``ENAMETOOLONG`` / ``FileNotFoundError``).
MAX_NAME_CHARS = 200
#: Longest FULL path :func:`budgeted_name` will let a writer build. Windows'
#: classic ``MAX_PATH`` is 260 including the drive and the NUL terminator, and
#: long-path support is opt-in per machine; 240 keeps a delivered tree openable
#: by Explorer and by tools that still use the ANSI APIs.
MAX_PATH_CHARS = 240
#: Hex characters of sha256 appended when — and only when — a value is cut.
#: Public because it is also the SHORTEST distinct name :func:`budgeted_name`
#: can return, which is exactly what a writer must reserve room for when it
#: budgets a directory against the children it will write underneath.
#: 16 hex = 64 bits. The tag is the ONLY thing keeping two cut ids apart, and a
#: collision files one patient's chart on top of another's, so the width is
#: chosen against the birthday bound rather than for looks: at 8 hex (32 bits)
#: a delivery of 10k same-prefix ids collides with probability ~1.2% and 100k
#: with ~69%; at 16 hex the same 100k sits below 3e-10.
HASH_TAG_CHARS = 16


def _hash_tagged(cleaned: str, limit: int) -> str:
    """``cleaned`` cut to ``limit`` characters, the cut tagged with its hash.

    A value already within ``limit`` is returned **byte-identical** — the whole
    point, since every delivered filename and directory in the archive, the
    bundle, and the C-CDA export is built from these components and must not
    move. Only a value that has to be cut gets ``-<16 hex of sha256>`` appended,
    hashing the ORIGINAL (uncut) value so two ids differing only past the cut
    still land on different names: a silent collision here would file one
    patient's chart on top of another's.

    Raises :class:`ValueError` when ``limit`` cannot even hold the hash tag —
    there is no safe name to return, and inventing a colliding one is the
    failure mode this whole function exists to prevent.
    """
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
    """A filesystem-safe file/directory component, or ``fallback`` if nothing survives.

    Runs of unsafe characters collapse to one ``_`` and leading/trailing ``_``
    are trimmed, so ``feedface-`` GUIDs (the synthetic fixture prefix) and any
    plain ASCII id pass through unchanged while an exotic value cannot escape
    its slot. An empty/blank input — or one made entirely of unsafe characters —
    yields ``fallback`` rather than an empty name.

    The result is never longer than :data:`MAX_NAME_CHARS`: a longer value is
    cut and tagged with ``-<16 hex of sha256>`` (see :func:`_hash_tagged`), so an
    unbounded source id cannot produce a component the filesystem rejects. A
    value at or under the cap is returned exactly as before this bound existed.

    Load-bearing: this is the single definition behind rendered chart filenames
    (:mod:`anastomosis.reconstruct.engine`), archive/bundle patient directories,
    and C-CDA export filenames. Changing it renames delivered output.
    """
    cleaned = _UNSAFE_NAME_RE.sub("_", (value or "").strip()).strip("_")
    return _hash_tagged(cleaned or fallback, MAX_NAME_CHARS)


def budgeted_name(
    value: str | None,
    fallback: str,
    *,
    parent: Path,
    suffix: str = "",
    reserve: int = 0,
) -> str:
    """:func:`safe_name`, cut further so ``parent/<name><suffix>`` stays inside
    :data:`MAX_PATH_CHARS`.

    ``suffix`` (``".html"``, ``".xml"``, …) is *budgeted* but NOT appended: the
    caller composes the final name exactly as it does with :func:`safe_name`, so
    a page's filename and the link written to it can share one call site.
    ``reserve`` is budgeted the same way, for a component that is a DIRECTORY:
    it is the room the caller needs left over for the deepest child it will
    write underneath (an archive patient directory reserves its
    ``encounters/<id>.html`` page). Without it a maximal directory name would
    fit while none of its contents did.

    ``parent`` is measured as the writer will use it — pass the real output
    directory (the deliverers pass the
    :func:`~anastomosis.core.output.secure_output_dir` result), not a display
    form of it.

    A component that already fits is returned unchanged, so short ids — every
    real one — keep their delivered names. When the parent is so deep that not
    even a hash tag fits, this raises rather than returning a name that could
    collide; the caller's writer fails loudly instead of quietly filing two
    charts as one.
    """
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
# renders as a stray gap (the predecessor's empty-block patterns).
_EMPTY_BLOCK_PATTERNS = (
    r"<p[^>]*>\s*(?:&nbsp;|&#160;|<br\s*/?>)?\s*</p>",
    r"<div[^>]*>\s*(?:&nbsp;|&#160;|<br\s*/?>)?\s*</div>",
    r"<h([1-6])[^>]*>\s*(?:&nbsp;|&#160;|<br\s*/?>)?\s*</h\1>",
)
# A stray \n that is NOT immediately adjacent to a block tag boundary becomes a
# <br> — inline line breaks (e.g. numbered injection
# sites) must survive into the rendered chart.
_STRAY_NEWLINE_RE = re.compile(r"\n(?!</(p|div|ul|ol|li|h[1-6])>)(?!<(p|div|ul|ol|li|h[1-6])[ >])")


# Tags whose CONTENT is dropped too (the body never reaches the renderer).
# Anything in this set is a known XSS or info-disclosure carrier in the
# local-Chromium PDF render context, so we strip both the tag and its body.
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
# Tags that pass through. Chosen to cover what clinical SOAP HTML legitimately
# carries (paragraphs, line breaks, inline emphasis, lists, headings, tables);
# everything outside this set is dropped, but its TEXT children still flow
# through, so unrecognized wrappers degrade gracefully instead of disappearing.
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
# The only attribute we let through on any allowed tag. The PF/generic SOAP
# templates style on class hooks (e.g. ``pf-rich-text``); they do not consume
# style/href/src/event handlers from source HTML, and admitting any of those
# is exactly the XSS surface this sanitizer exists to close.
_ALLOWED_ATTRS = frozenset({"class"})
# Void elements among _ALLOWED_TAGS — no closing tag emitted.
_VOID_ALLOWED = frozenset({"br", "hr", "col"})
# Full HTML5 void-element set — drop-content tags that fall in here have no
# end-tag in source, so they must NOT enter drop mode (or we silently swallow
# everything after them). Sources: html.spec.whatwg.org/#void-elements.
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
    """Stdlib-only allowlist sanitizer for clinical SOAP-note HTML.

    Built on :class:`html.parser.HTMLParser` (same parser :func:`html_to_text`
    already uses) so behavior matches the rest of this module rather than
    introducing a fresh tokenizer with its own quirks. The output is whatever
    fragment of the input survived the allowlist; the wrapper
    :func:`sanitize_soap_html` then layers the PF-specific repairs
    (``\\n``→``<br>``, empty-block strip, ``pf-rich-text`` wrap) on top.

    Three behaviors, ordered by safety contribution:

    1. **Drop-content tags** (script/style/iframe/object/embed/etc.) are
       removed *along with their body* — the body would otherwise become
       visible text after the open/close tags are stripped, which is worse
       than the original (a stored ``alert(1)`` literal in a chart).
    2. **Non-allowlist tags** are removed but their text children survive
       (an unrecognized ``<font color="red">x</font>`` collapses to ``x``,
       not nothing) so unknown markup degrades visibly, not silently.
    3. **Allowlist tags** pass through with only :data:`_ALLOWED_ATTRS`;
       everything else (event handlers like ``onclick``, URL-bearing attrs
       like ``href``/``src``/``formaction``, ``style``) is stripped at the
       attribute level. Entity references are preserved verbatim
       (``convert_charrefs=False``) so the existing empty-block regex can
       still match ``&nbsp;`` fillers downstream.

    This is a *defense-in-depth* boundary for a local-Chromium render
    context, not a general web sanitizer; it makes no claim against
    mutation-XSS that exploits parser disagreement between
    :class:`HTMLParser` and Chromium. The strongest empirical guarantee is
    that an adversarial fixture rendered through the full pipeline produces
    a PDF whose extracted text contains *no* injected payload — exercised
    by the integration tests in this PR.
    """

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
            # Void drop tags (<embed>, <input>, <link>, <meta>, ...) have no
            # closing tag in source, so they MUST NOT enter drop mode — doing
            # so would silently swallow everything that follows. They contain
            # no body anyway, so dropping the tag itself is the complete fix.
            if tag in _HTML5_VOID or self_closing:
                return
            self._drop_depth += 1
            return
        if self._drop_depth or tag not in _ALLOWED_TAGS:
            return
        # When every attribute on the input tag is in _ALLOWED_ATTRS the
        # attribute filter is a no-op, and we can emit the source's original
        # tag text verbatim (preserving e.g. ``<br/>`` vs ``<br>`` vs
        # ``<br />``). This is what keeps benign clinical HTML byte-identical
        # round-tripping through the sanitizer — critical for the PF/Tebra
        # e2e goldens. The fallback reconstructs from the parsed attr list
        # whenever something had to be filtered — or whenever the source open
        # tag spans multiple lines (a pretty-printed EHR export wrapping a
        # long attribute list): emitting the multi-line source verbatim would
        # let the downstream stray-newline regex inject ``<br>`` *inside* the
        # open tag, corrupting it. Reconstruction collapses to a single line.
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
    """Allowlist-clean PF semantic HTML, then apply the PF rendering repairs.

    Ported from the predecessor's ``sanitize_soap_html``
    with an added :class:`_SoapHtmlSanitizer` allowlist pass at the front so
    script/style/event handlers/non-allowlist URL attributes cannot reach the
    local-Chromium PDF renderer (the section is templated with Jinja's
    ``| safe``, autoescape off, *by design* — exactly so legitimate inline
    formatting survives, which is exactly why an allowlist is required).

    For benign HTML that only uses allowlisted tags + attributes — e.g. PF's
    fixture content (``<p>``, ``<br>``, no attrs) — the sanitizer's output
    is character-equivalent to the input, so the existing repairs
    (``\\n``→``<br>``, empty-block strip, ``pf-rich-text`` wrap) behave
    identically and the e2e goldens are byte-identical.

    The EHI TSV export converts ``<br>`` to ``\\n`` — we restore them so line
    breaks within inline content render correctly in the browser. Output is
    HTML intended for ``autoescape=False`` rendering, wrapped in
    ``pf-rich-text``.

    This is the *rendering* path; :func:`html_to_text` remains the plain-text
    path for search/QA/addendum bodies.
    """
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
