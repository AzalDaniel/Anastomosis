"""Console glyphs and brand palette that survive non-UTF-8 terminals: a
Windows legacy code page cannot encode ``✓``/``✗``/``→`` and raises
:class:`UnicodeEncodeError` mid-output, so this module is the one place
that picks the Unicode glyphs on a UTF-8 stream and an ASCII fallback
elsewhere. It also carries the text surfaces' color tokens and the one
question every text frontend must ask before drawing: is anybody there.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

__all__ = [
    "ASCII_GLYPHS",
    "BRAND_PALETTE",
    "UNICODE_GLYPHS",
    "Glyphs",
    "Palette",
    "attached_to_a_terminal",
    "terminal_colour_depth",
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
    """The CLI's colors, named so the TERMINAL resolves them: every value
    is an ANSI name or attribute, never an absolute one, since only the
    terminal's own theme knows what is legible against the background its
    owner chose. Color is never the only signal; the words carry the
    meaning."""

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
    """The glyph set safe to write to ``stream`` (anything exposing an
    ``encoding`` attribute): UTF-8 yields :data:`UNICODE_GLYPHS`, a
    missing or other encoding yields the portable :data:`ASCII_GLYPHS`,
    so the glyphs never raise :class:`UnicodeEncodeError`."""
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return ASCII_GLYPHS
    normalized = encoding.lower().replace("-", "").replace("_", "")
    return UNICODE_GLYPHS if normalized in _UTF8_ALIASES else ASCII_GLYPHS


def terminal_colour_depth(console: Any) -> str:
    """How much colour this console can deliver: truecolor, 256, or none.
    ``isatty``, never ``is_terminal`` (:func:`attached_to_a_terminal`),
    decides if a person is there; ``console.no_color`` catches
    ``NO_COLOR=1``, which ``color_system`` alone would miss.
    ``"standard"``/``"windows"`` come back ``"none"``."""
    if not attached_to_a_terminal(getattr(console, "file", None)):
        return "none"
    if getattr(console, "no_color", False):
        return "none"
    system = getattr(console, "color_system", None)
    return system if system in ("truecolor", "256") else "none"


def attached_to_a_terminal(stream: Any) -> bool:
    """Whether ``stream`` is really a terminal — asked of the stream
    itself, never of anything reporting colour capability. ``FORCE_COLOR``/
    ``TTY_COMPATIBLE``, exported by ordinary CI/build tooling, make Rich's
    ``is_terminal`` answer True for a plain piped file, which would hang
    an unattended run on a prompt nobody can see."""
    try:
        return bool(stream is not None and stream.isatty())
    except (AttributeError, OSError, ValueError):
        # A closed or exotic stream is neither a keyboard nor a screen.
        return False


#: What a terminal injects, as opposed to what a person types. Sequence forms
#: first, so an arrow key's ``ESC [ A`` is removed as one unit rather than
#: leaving ``[A`` behind, then any other escape pair (Alt-chords arrive as
#: ``ESC x``), then the loose C0 controls and DEL.
_INJECTED = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI: arrows, Home/End, function keys
    r"|\x1bO[@-~]"  # SS3: the same keys in application mode
    r"|\x1b."  # any other escape pair, and a trailing lone ESC
    r"|[\x00-\x1f\x7f]"  # remaining control bytes
)


def as_typed(raw: str) -> str:
    """``raw`` with everything the terminal injected removed: an arrow key
    arrives as ``ESC [ A``, invisible on screen but three real characters
    on their way into a filename. What a person can see is what this
    keeps."""
    return _INJECTED.sub("", raw)
