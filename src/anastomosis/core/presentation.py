"""Console glyphs — and the brand palette — that survive non-UTF-8 terminals.

Windows' legacy console code pages (CP-1252, CP-437, …) cannot encode the
status dingbats and arrow the CLI likes to print — ``✓`` (U+2713), ``✗``
(U+2717) and ``→`` (U+2192). Writing one of those to such a stream raises
:class:`UnicodeEncodeError` and aborts the command mid-output. This module
centralizes the glyph set so any text frontend picks the Unicode glyphs on a
UTF-8 stream and a plain-ASCII fallback everywhere else — the same decision in
one place, rather than each call site guessing.

It carries the text surfaces' half of the design language's color tokens for the
same reason: one definition of "oxblood" and "porcelain", not one per frontend.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "ASCII_GLYPHS",
    "BRAND_PALETTE",
    "UNICODE_GLYPHS",
    "Glyphs",
    "Palette",
    "terminal_glyphs",
]


@dataclass(frozen=True)
class Glyphs:
    """The small set of status glyphs the CLI prints in human output."""

    ok: str
    fail: str
    arrow: str
    #: The identity mark the guided session prints beside the product name — a
    #: solid half-block on a UTF-8 stream, a plain pipe everywhere else.
    #: Defaulted to the portable form so any existing three-field construction
    #: (tests included) keeps working and can never emit an unencodable glyph.
    bar: str = "|"


@dataclass(frozen=True)
class Palette:
    """The design language's colors as Rich style strings.

    Truecolor sRGB approximations of the ``oklch`` tokens in
    ``docs/design/DESIGN_LANGUAGE.md`` §1. Rich maps every one of these down to
    the best match the terminal actually offers (256-color, then the 16-color
    ANSI set, then nothing at all on a plain pipe), so one definition serves
    every console without a per-terminal branch here.
    """

    #: The oxblood of the vessel mark — identity moments only, never decoration.
    brand: str
    #: Interactive brand: the mark, a heading, the thing being answered.
    brand_bright: str
    #: Primary text (porcelain) and the quieter supporting register.
    ink: str
    ink_muted: str
    #: The clinical signal family — never reused as decoration.
    ok: str
    attention: str
    stop: str


#: The one palette every text surface shares (§1 of the design language).
BRAND_PALETTE = Palette(
    brand="#8c2e23",
    brand_bright="#bc4a3c",
    ink="#f5f1ea",
    ink_muted="#9c9890",
    ok="#68d7a1",
    attention="#eec469",
    stop="#ff645f",
)

#: The pretty glyphs, used when the target stream is UTF-8.
UNICODE_GLYPHS = Glyphs(ok="✓", fail="✗", arrow="→", bar="▐")
#: The portable fallback, used on a non-UTF-8 (e.g. CP-1252) console. The
#: markers are deliberately bracket-free: the CLI prints the transit map through
#: ``rich.Console.print``, which would parse ``[ok]``/``[x]`` as style tags and
#: strip them — so a viable/unviable marker must contain no square brackets.
ASCII_GLYPHS = Glyphs(ok="+", fail="x", arrow="->", bar="|")

# Encodings (normalized: lowercased, separators stripped) that can render the
# Unicode glyphs. Modern terminals — Windows Terminal, a UTF-8 code page — report
# a UTF-8 encoding and so get the pretty glyphs. Everything else gets the ASCII
# fallback: a CP-1252 console, a stream with no declared encoding, and even a
# UTF-16/32 or ``cp65001`` stream (all UTF-capable, but rare as a console code
# page) — the fallback is always safe, only occasionally plainer than necessary.
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
