"""Console glyphs — and the brand palette — that survive non-UTF-8 terminals.

Windows' legacy console code pages (CP-1252, CP-437, …) cannot encode the
status dingbats and arrow the CLI likes to print — ``✓`` (U+2713), ``✗``
(U+2717) and ``→`` (U+2192). Writing one of those to such a stream raises
:class:`UnicodeEncodeError` and aborts the command mid-output. This module
centralizes the glyph set so any text frontend picks the Unicode glyphs on a
UTF-8 stream and a plain-ASCII fallback everywhere else — the same decision in
one place, rather than each call site guessing.

It carries the text surfaces' half of the design language's color tokens for the
same reason: one definition of "oxblood" and "porcelain", not one per frontend —
and, for the same reason again, the one question every text frontend has to ask
before it draws anything for a person: is anybody there.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "ASCII_GLYPHS",
    "BRAND_PALETTE",
    "UNICODE_GLYPHS",
    "Glyphs",
    "Palette",
    "attached_to_a_terminal",
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
    """The CLI's colors, named so the TERMINAL resolves them.

    Every value here is an ANSI name or an attribute, never an absolute one.
    The GUI can use the design language's ``oklch`` tokens directly because it
    draws its own dark ground; a terminal application cannot. The background
    belongs to the person running it, and the only colors readable against
    whatever they chose are the ones their terminal maps itself — a light
    theme's ``yellow`` is a dark mustard, a dark theme's is bright, and both
    are legible because the theme's author made them so.

    Measured against white, the truecolor tokens this replaced ran from
    1.13 : 1 (primary text) through 1.65 : 1 (an attention line) to 2.90 : 1
    (a refusal). Not a tuning problem: the brand oxblood was 8.30 : 1 on white
    and 2.53 : 1 on black, so no single absolute palette can serve both grounds.

    Color is never the only signal — the words carry the meaning, and these
    only reinforce it.
    """

    #: Identity: the mark and the thing being answered. Weight, not hue —
    #: ``red`` is the refusal color and identity must not borrow it.
    brand_bright: str
    #: Primary text: whatever the terminal uses for ordinary output, which is
    #: readable on that terminal's background by construction.
    ink: str
    #: The supporting register. ``dim`` modulates the reader's own foreground
    #: rather than picking a grey that a light theme would wash out.
    ink_muted: str
    #: The clinical signal family — never reused as decoration.
    ok: str
    attention: str
    stop: str


#: The one palette every text surface shares (§1 of the design language).
BRAND_PALETTE = Palette(
    brand_bright="bold",
    ink="default",
    ink_muted="dim",
    ok="green",
    attention="yellow",
    stop="red",
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


def attached_to_a_terminal(stream: Any) -> bool:
    """Whether ``stream`` is really a terminal — asked of the stream itself.

    Never of anything that reports colour capability. ``FORCE_COLOR`` and
    ``TTY_COMPATIBLE`` make Rich answer ``is_terminal`` True for a plain file
    (``typer/rich_utils.py`` sets them up), and both are exported by ordinary
    tooling — CI images, ``just``/``make`` wrappers, terminal multiplexers.
    With one of them set, ``anast > log`` from a shell used to open the guided
    session, write the menu into the log file, and then block on a question
    nobody could see.

    Whether output can carry colour and whether a person is reading it are
    different questions with different answers; letting the first stand in for
    the second is what made an unattended run hang. Two callers ask it now —
    the guided session's gate and the greeting mark's — and one wrong copy of
    this is one too many.
    """
    try:
        return bool(stream is not None and stream.isatty())
    except (AttributeError, OSError, ValueError):
        # A closed or exotic stream is neither a keyboard nor a screen.
        return False
