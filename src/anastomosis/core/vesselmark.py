"""The vessel mark, in dots, for the terminal the guided session opens
in: the grid in :mod:`anastomosis.core.vesselmark_data` laid down beside
the greeting, with a short entrance that fills it from the trunk outward.
Three decisions carry the file: the gradient is density and weight, never
a colour (§11 of ``docs/design/DESIGN_LANGUAGE.md``); the entrance is
unreachable without a person watching (:func:`can_draw` asks the stream
directly); and no clock lives inside a frame — :func:`frame_levels` takes
only a frame index, so a test can drive it deterministically. Time
appears once, in :func:`_play`.
"""

from __future__ import annotations

import math
import os
from typing import TYPE_CHECKING

from rich.text import Text

from anastomosis.core.presentation import (
    BRAND_PALETTE,
    UNICODE_GLYPHS,
    attached_to_a_terminal,
    terminal_colour_depth,
    terminal_glyphs,
)
from anastomosis.core.vesselmark_data import DENSITY, LEVELS

if TYPE_CHECKING:
    from collections.abc import Sequence

    from rich.console import Console

__all__ = [
    "FRAMES",
    "MARK_HEIGHT",
    "MARK_STOPS",
    "MARK_STOPS_256",
    "MARK_WIDTH",
    "beside",
    "can_draw",
    "frame_levels",
    "mark_levels",
    "render",
    "show_greeting",
]

MARK_WIDTH = len(DENSITY[0])
MARK_HEIGHT = len(DENSITY)

#: Blank columns between the mark and the greeting beside it — the design
#: language's 8 px step, in the unit a terminal actually has.
GUTTER = 2
#: The narrowest column of text worth putting beside the mark. Under this the
#: greeting would wrap into two words a line, so the mark stands down instead
#: and the plain header (which wraps gracefully) is printed.
MIN_TEXT_COLUMNS = 24

#: The entrance, in frames and seconds per frame — the whole animation, and
#: under a second of it: an identity moment that outlasts the eye's patience
#: is a delay, not a greeting. Nothing follows it, so there is nothing to
#: interrupt and no keyboard to read.
FRAMES = 14
FRAME_SECONDS = 0.05

#: How much of the entrance is spent arriving rather than filling. Every cell
#: begins somewhere in the first 70 % of the run and takes the remaining 30 %
#: to reach full density, which is what makes the last frame — and only the
#: last frame — identical to the settled mark.
_SPREAD = 0.7

#: Level -> the palette weight that carries it. Three weights, and the mark
#: uses all three: the capillary rim is the supporting register, the body is
#: ordinary ink, and the trunk and hub are identity. No hue anywhere, because
#: ``red`` is this product's refusal colour and identity may not borrow it.
_WEIGHTS = (
    "",
    BRAND_PALETTE.ink_muted,
    BRAND_PALETTE.ink,
    BRAND_PALETTE.ink,
    BRAND_PALETTE.brand_bright,
)

#: Level -> glyph: braille cells carrying 0, 2, 4, 6 and 8 dots. An area ramp,
#: horizontally symmetric, and every step a 33 % change rather than the 2x jump
#: the old level 3 -> 4 made — which is most of why the canopy stopped reading
#: as speckle. Braille also fixes a live bug: `·` U+00B7, `•` U+2022 and `●`
#: U+25CF are all East Asian Width Ambiguous and render DOUBLE WIDTH on any
#: terminal configured for CJK, shearing this 21-column grid. Every codepoint
#: in U+2800-U+28FF is Narrow.
#: Level 0 is a real space, NOT U+2800 BRAILLE PATTERN BLANK. The blank looks
#: identical and is not whitespace, so `beside`'s `rstrip` stops trimming and
#: every empty row ships 21 printing characters into somebody's scrollback.
UNICODE_DOTS = (" ", *(chr(0x2800 | bits) for bits in (0x12, 0x36, 0x3F, 0xFF)))
#: The same ramp for a console that cannot encode those — a legacy Windows
#: code page, a stream with no declared encoding. Pure ASCII, same shape, and
#: five distinct glyphs now that the braille ramp has five.
ASCII_DOTS = (" ", ".", ":", "o", "O")

#: The mark's ramp, and the ONLY absolute colour this program emits — §11 of
#: docs/design/DESIGN_LANGUAGE.md is amended for exactly this and nothing else.
#: Brand hue (OKLCh H=30, the icon's own measured 29.7) re-derived at terminal
#: luminance: every stop sits inside the 0.175-0.242 window where 3 : 1 holds on
#: a dark AND a light ground. The icon's own oxblood does not — `#701a14`
#: measures 1.23 : 1 on One Dark `#282c34`, which is why the palette could not
#: simply be reused. The gradient travels in chroma rather than lightness
#: because that window is only 0.067 wide, and because "more pigment" reads the
#: same direction on both grounds where "brighter" inverts.
MARK_STOPS: tuple[str, ...] = (
    "#7e7370",
    "#947069",
    "#a86a60",
    "#bc6658",
    "#cf5e4e",
    "#e35544",
)
#: The same ramp at 256 colours, as explicit indices rather than a downgrade:
#: rich memoises a Style's ANSI string on first render and reuses it at any
#: depth, so a hex asked for once can leak truecolor bytes to a 256-colour
#: console. An index is immune — it renders as 38;5;N everywhere. Only 27 of
#: the 240 non-ANSI entries clear 3 : 1 on all nine grounds, which collapses
#: six stops into three pairs; that mirrors the weight ramp, where
#: `_WEIGHTS[2] == _WEIGHTS[3]` already.
MARK_STOPS_256: tuple[str, ...] = (
    "color(243)",
    "color(243)",
    "color(131)",
    "color(131)",
    "color(167)",
    "color(167)",
)

if not len(UNICODE_DOTS) == len(ASCII_DOTS) == len(_WEIGHTS) == LEVELS + 1:
    # The grid is generated and the ramp is written by hand; a regeneration
    # that changes how many levels a cell can carry has to be answered here.
    # Loudly, at import: a ramp one step short draws the densest cells as
    # whatever the last entry happens to be, which looks like a design.
    raise ValueError(
        f"the density ramp does not cover the mark: {LEVELS} levels sampled, "
        f"{len(UNICODE_DOTS) - 1} in the ramp"
    )


def mark_levels() -> tuple[tuple[int, ...], ...]:
    """The settled mark: every cell at the density the logo gives it."""
    return tuple(tuple(int(digit) for digit in row) for row in DENSITY)


def _entrance_offsets() -> tuple[tuple[float, ...], ...]:
    """When each cell starts arriving, as a fraction of the entrance:
    distance from the foot of the trunk, scaled into ``[0, _SPREAD]`` (rows
    count double since a cell is about twice as tall as wide), against the
    furthest INKED cell, not the furthest corner — an empty corner setting
    the scale would let every cell finish early."""
    levels = mark_levels()
    root_row = MARK_HEIGHT - 1
    root_col = max(range(MARK_WIDTH), key=lambda col: levels[root_row][col])
    spans = [
        [math.hypot(col - root_col, 2.0 * (root_row - row)) for col in range(MARK_WIDTH)]
        for row in range(MARK_HEIGHT)
    ]
    inked = [
        span
        for row, cells in zip(spans, levels, strict=True)
        for span, level in zip(row, cells, strict=True)
        if level
    ]
    longest = max(inked) or 1.0
    return tuple(tuple(min(span, longest) / longest * _SPREAD for span in row) for row in spans)


_OFFSETS = _entrance_offsets()


def frame_levels(frame: int) -> tuple[tuple[int, ...], ...]:
    """The density grid for one frame of the entrance. ``frame`` is the
    only input — no clock — so a test can walk it frame by frame and
    assert it only ever gains ink. Frame ``FRAMES - 1`` is the settled
    mark exactly, and any frame past it stays there."""
    if frame < 0:
        raise ValueError(f"frame index must not be negative: {frame}")
    progress = min(1.0, (frame + 1) / FRAMES)
    grid = []
    for row, offsets in zip(mark_levels(), _OFFSETS, strict=True):
        arrived = ((progress - offset) / (1.0 - _SPREAD) for offset in offsets)
        grid.append(
            tuple(
                0 if share <= 0.0 else min(level, math.ceil(level * min(share, 1.0)))
                for level, share in zip(row, arrived, strict=True)
            )
        )
    return tuple(grid)


def render(
    levels: Sequence[Sequence[int]],
    *,
    unicode_dots: bool,
    stops: Sequence[Sequence[int]] | None = None,
    palette: Sequence[str] | None = None,
) -> list[Text]:
    """One :class:`~rich.text.Text` per row of the grid. With no
    ``stops`` and no ``palette`` this draws glyph size and text weight
    only, no absolute value — what a sixteen-colour terminal, a
    ``NO_COLOR`` session, or a redirected stream all get."""
    dots = UNICODE_DOTS if unicode_dots else ASCII_DOTS
    rows = []
    for index, row in enumerate(levels):
        line = Text()
        for column, level in enumerate(row):
            if level and stops is not None and palette is not None:
                style: str | None = palette[stops[index][column]]
            else:
                style = _WEIGHTS[level] or None
            line.append(dots[level], style=style)
        rows.append(line)
    return rows


def beside(mark: Sequence[Text], lines: Sequence[Text]) -> list[Text]:
    """Compose the mark and the greeting into one block, mark on the
    left. The greeting sits against the middle of the mark, not its top
    row — text hung off the crown reads as a caption that lost its
    picture. A taller greeting keeps going under the mark, same column."""
    top = max(0, (len(mark) - len(lines)) // 2)
    composed = []
    for index in range(max(len(mark), top + len(lines))):
        line = mark[index].copy() if index < len(mark) else Text(" " * MARK_WIDTH)
        if top <= index < top + len(lines):
            line.append(" " * GUTTER)
            line.append(lines[index - top])
        line.rstrip()
        composed.append(line)
    return composed


def can_draw(console: Console) -> bool:
    """Whether this console gets the mark at all — two refusals. A stream
    that is not really a terminal gets the plain header (asking the
    stream itself, the same guard as ``guide.is_interactive_terminal``); a
    console too narrow for the mark plus a readable text column gets it
    too, since a greeting wrapped into the dots is worse than no dots."""
    if not attached_to_a_terminal(getattr(console, "file", None)):
        return False
    return console.width >= MARK_WIDTH + GUTTER + MIN_TEXT_COLUMNS


def _motion_wanted(console: Console) -> bool:
    """Whether the mark may assemble rather than simply appear.
    ``NO_COLOR`` is honoured as a request for unadorned output, since an
    entrance leaves a dozen half-drawn marks in a kept transcript where a
    settled one leaves the mark; a console Rich does not consider a
    terminal (``TERM=dumb``) is drawn once, settled, for the same reason."""
    return console.is_terminal and not os.environ.get("NO_COLOR")


def show_greeting(console: Console, lines: Sequence[Text], *, animate: bool = True) -> bool:
    """Draw the mark with ``lines`` beside it; ``False`` means it stood
    down. The caller owns the words, this owns the object they print
    against."""
    if not can_draw(console):
        return False
    # The glyph set IS the capability decision, and the CLI already makes it in
    # one place: a UTF-8 stream gets the round dots, anything else (a CP-1252
    # console, a stream with no declared encoding) gets the ASCII ramp rather
    # than a line of replacement characters.
    unicode_dots = terminal_glyphs(console.file) is UNICODE_GLYPHS
    wrapped = _wrapped(console, lines)
    if animate and _motion_wanted(console):
        _play(console, wrapped, unicode_dots=unicode_dots)
    else:
        for line in beside(render(mark_levels(), unicode_dots=unicode_dots), wrapped):
            console.print(line)
    return True


def _wrapped(console: Console, lines: Sequence[Text]) -> list[Text]:
    """The greeting, folded to the column left over beside the mark.
    Rich would wrap it back to column zero, under the dots; folding it
    here keeps the whole greeting in its own column."""
    column = console.width - MARK_WIDTH - GUTTER
    folded: list[Text] = []
    for line in lines:
        folded.extend(line.wrap(console, column))
    return folded


def _play(console: Console, lines: Sequence[Text], *, unicode_dots: bool) -> None:
    """Contract: run the entrance and leave the settled mark on screen —
    frame ``FRAMES - 1`` IS that mark, so the last write is no special
    case. Bounded by ``FRAMES`` alone, under a second, nothing after it
    and no keyboard to read. Must never run while a prompt is open:
    ``Live`` does not coordinate with ``Console.input`` on stdin."""
    import time

    from rich.console import Group
    from rich.live import Live

    # Orthogonal to the glyph set on purpose: a CP-1252 Windows Terminal draws
    # the ASCII ramp in 256 colours, and a UTF-8 xterm with NO_COLOR draws
    # braille in none. Encoding and colour depth are different questions.
    palette = _palette(console)
    # Every cell on the stop its own density gives it: the mark is coloured
    # from the moment it arrives, and the colouring never moves.
    stops = mark_levels()
    with Live(console=console, auto_refresh=False) as live:
        for frame in range(FRAMES):
            drawn = render(
                frame_levels(frame), unicode_dots=unicode_dots, stops=stops, palette=palette
            )
            live.update(Group(*beside(drawn, lines)), refresh=True)
            time.sleep(FRAME_SECONDS)


def _palette(console: Console) -> tuple[str, ...] | None:
    """The stops this console can carry, or ``None`` for weight alone."""
    depth = terminal_colour_depth(console)
    if depth == "truecolor":
        return MARK_STOPS
    if depth == "256":
        return MARK_STOPS_256
    return None
