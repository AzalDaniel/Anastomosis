"""Console glyphs that survive non-UTF-8 terminals.

Windows' legacy console code pages (CP-1252, CP-437, …) cannot encode the
status dingbats and arrow the CLI likes to print — ``✓`` (U+2713), ``✗``
(U+2717) and ``→`` (U+2192). Writing one of those to such a stream raises
:class:`UnicodeEncodeError` and aborts the command mid-output. This module
centralizes the glyph set so any text frontend picks the Unicode glyphs on a
UTF-8 stream and a plain-ASCII fallback everywhere else — the same decision in
one place, rather than each call site guessing.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["ASCII_GLYPHS", "UNICODE_GLYPHS", "Glyphs", "terminal_glyphs"]


@dataclass(frozen=True)
class Glyphs:
    """The small set of status glyphs the CLI prints in human output."""

    ok: str
    fail: str
    arrow: str


#: The pretty glyphs, used when the target stream is UTF-8.
UNICODE_GLYPHS = Glyphs(ok="✓", fail="✗", arrow="→")
#: The portable fallback, used on a non-UTF-8 (e.g. CP-1252) console.
ASCII_GLYPHS = Glyphs(ok="[ok]", fail="[x]", arrow="->")

# Encodings (normalized: lowercased, separators stripped) that can render the
# Unicode glyphs as a console code page. UTF-16/32 also could, but no real
# console uses them as its code page; UTF-8 covers modern terminals (including
# Windows Terminal and a chcp 65001 console). Everything else — a CP-1252
# console, a stream with no declared encoding — gets the ASCII fallback.
_UTF8_ALIASES = frozenset({"utf8", "utf8sig"})


def terminal_glyphs(stream: object) -> Glyphs:
    """Return the glyph set safe to write to ``stream``.

    ``stream`` is anything exposing an ``encoding`` attribute — a text stream
    such as ``sys.stdout``, or a Rich ``Console``'s ``.file``. A UTF-8 encoding
    yields :data:`UNICODE_GLYPHS`; a missing or non-UTF-8 encoding (a CP-1252
    Windows console, a pipe with no declared encoding) yields the portable
    :data:`ASCII_GLYPHS`, so the glyphs never raise :class:`UnicodeEncodeError`.
    """
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return ASCII_GLYPHS
    normalized = encoding.lower().replace("-", "").replace("_", "")
    return UNICODE_GLYPHS if normalized in _UTF8_ALIASES else ASCII_GLYPHS
