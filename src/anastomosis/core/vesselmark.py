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
import sys
from contextlib import contextmanager
from typing import IO, TYPE_CHECKING, Any, cast

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
    from collections.abc import Iterator, Sequence

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
    "home_stops",
    "mark_levels",
    "pulse_frame",
    "render",
    "show_greeting",
    "wave",
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

#: The entrance, in frames and seconds per frame. Under a second in total: an
#: identity moment that outlasts the eye's patience is a delay, not a greeting.
#: What follows the entrance is not bounded the same way — see :func:`_play`.
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


#: A rectangle of per-cell integers — levels or stops, same shape either way.
_Grid = tuple[tuple[int, ...], ...]


# --- the perfusion field -----------------------------------------------------

#: The anastomosis hub — where the two cut vessels meet the trunk, which is the
#: thing this product is named for. Derived, not chosen: ``make_vessel.build``
#: puts it at (0.500, 0.615) of a 1024 canvas, cells are 1024/21 x 1024/11, and
#: ``sample_matrix`` trims one all-zero row off the top.
HUB_COL, HUB_ROW = 10.0, 5.265

#: Base pulse, in hertz, and the three temporal ratios riding it. The ratios
#: are INCOMMENSURABLE on purpose: 0.618 and 1.481 share no small-integer
#: relation with 1.0, so the superposition has no period and the mark never
#: repeats itself. That is the whole reason this can loop without looking like
#: a loop.
_F0 = 0.85
_RATIO = (1.000, 0.618, 1.481)
#: Spatial wavelengths, in the doubled-row world units distance is measured in.
#: Nyquist-bounded: rows sample every 2 units, so 6.1 gets 3.05 samples per
#: cycle vertically. Below about 5 it aliases into speckle; above R_MAX no
#: crest fits inside the mark at all.
_LAM = (9.0, 6.1, 13.7)
_AMP = (0.60, 0.28, 0.16)
#: The glyph gain floor. At the trough a cell keeps 55 % of its settled level,
#: which is what stops the outline flickering: levels 1 and 2 hold while 3 and
#: 4 breathe, so the capillary rim carries the silhouette steady.
_G0 = 0.55
#: Frames the amplitude takes to come up after the entrance, and the ceiling on
#: an unwatched greeting.
#:
#: The ceiling is a real cost of running continuously and it was measured, not
#: guessed: the pty test drives ``anast`` for real, and at a 1200-frame cap the
#: run never reached the menu inside sixty seconds. Nothing was broken — the
#: mark was perfusing exactly as designed, and the question underneath it was
#: waiting for the animation to finish. A greeting that holds the prompt is not
#: alive, it is in the way.
#:
#: So: any keystroke ends it instantly, and failing that eight seconds does.
#: Nobody ever sees it repeat, because the field has no period — eight seconds
#: is simply the longest this may make somebody wait who typed ``anast`` and
#: then looked away. Genuinely endless motion needs the mark to live UNDER the
#: prompt rather than before it, which needs the self-echoing reader in #330;
#: until that lands, this is where continuous honestly stops.
_RAMP_IN = 6
_IDLE_FRAMES = 160


def _phase_offset(col: int, row: int) -> float:
    """A smooth, lattice-free phase shift per cell: two incommensurable
    plane waves rather than a coordinate hash (white noise looks like
    static, a smooth field looks like tissue), and it breaks the ring
    coherence a pure radial term would give, separating a pulse from a
    sonar ping."""
    u, v = float(col), 2.0 * row
    return 0.45 * (0.9 * math.sin(0.70 * u + 1.30 * v) + 0.6 * math.sin(-1.10 * u + 0.53 * v))


def wave(col: int, row: int, seconds: float, seed: float = 0.0) -> float:
    """The field at one cell at one instant, in ``[0, 1]``. The minus sign
    on time makes crests travel OUTWARD from the hub; the smoothstep turns
    a machine-reading sine into fast attack, slow decay. ``seed`` shifts
    where in the endless field a run begins, so nobody sees the same
    opening twice."""
    distance = math.hypot(col - HUB_COL, 2.0 * (row - HUB_ROW))
    phase = _phase_offset(col, row)
    # The mark is mirror-symmetric about column 10 and distance therefore reads
    # identically on both cut vessels. Without this term both arms pulse in
    # lockstep, which is visibly mechanical. It has no seam at the hub.
    lateral = 0.15 * (col - HUB_COL)
    raw = 0.0
    for amp, lam, ratio in zip(_AMP, _LAM, _RATIO, strict=True):
        angle = math.tau * distance / lam - math.tau * _F0 * ratio * seconds
        raw += amp * math.sin(angle + phase * ratio + lateral + seed * ratio)
    unit = 0.5 + 0.5 * raw / 1.04
    return unit * unit * (3.0 - 2.0 * unit)


def _amplitude(frame: int) -> float:
    """How much of the field reaches the mark at ``frame``: zero through
    the entrance (the wave's phase runs the whole time, it simply has no
    reach yet), then smoothstepped up over ``_RAMP_IN`` frames and held.
    Settling is an exit, taken when somebody presses a key."""
    if frame < FRAMES:
        return 0.0
    ramp = min(1.0, (frame - FRAMES + 1) / _RAMP_IN)
    return ramp * ramp * (3.0 - 2.0 * ramp)


def pulse_frame(frame: int, seed: float = 0.0) -> tuple[_Grid, _Grid]:
    """One frame of the perfusion, as ``(levels, stops)``: two channels
    from one scalar, glyph for form and stop for light, so the mark stays
    complete with colour stripped. At amplitude zero both channels
    reproduce the settled mark EXACTLY, which the exit relies on."""
    seconds = frame * FRAME_SECONDS
    amplitude = _amplitude(frame)
    levels, stops = [], []
    for row, home_row in enumerate(mark_levels()):
        level_row, stop_row = [], []
        for col, home in enumerate(home_row):
            if home == 0:
                level_row.append(0)
                stop_row.append(0)
                continue
            unit = 0.5 + amplitude * (wave(col, row, seconds, seed) - 0.5)
            gain = _G0 + (1.0 - _G0) * unit
            level_row.append(max(1, min(home, math.ceil(home * gain))))
            delta = -1 if unit < 0.25 else (1 if unit > 0.75 else 0)
            stop_row.append(max(0, min(len(MARK_STOPS) - 1, home + delta)))
        levels.append(tuple(level_row))
        stops.append(tuple(stop_row))
    return tuple(levels), tuple(stops)


def home_stops() -> _Grid:
    """Every cell on the stop its density gives it — the settled colouring."""
    return mark_levels()


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
    """Contract: run the greeting and leave the settled mark on screen.
    Bounded twice: a keystroke ends it (swallowed), or ``_IDLE_FRAMES``
    settles an unattended terminal; either way the last write is the
    settled mark. Must never run while a prompt is open — ``Live`` does
    not coordinate with ``Console.input`` on stdin."""
    import time

    from rich.console import Group
    from rich.live import Live

    # Orthogonal to the glyph set on purpose: a CP-1252 Windows Terminal draws
    # the ASCII ramp in 256 colours, and a UTF-8 xterm with NO_COLOR draws
    # braille in none. Encoding and colour depth are different questions.
    palette = _palette(console)
    seed = _seed()

    def block(levels: Sequence[Sequence[int]], stops: Sequence[Sequence[int]]) -> Group:
        drawn = render(levels, unicode_dots=unicode_dots, stops=stops, palette=palette)
        return Group(*beside(drawn, lines))

    original = console.file
    # rich types `console.file` as IO[str]; the wrapper forwards everything it
    # does not implement, and only `write` is on rich's hot path. The cast says
    # that out loud rather than widening the wrapper into a fake file object.
    #
    # NOT on a legacy Windows console. There, rich renders through
    # `legacy_windows_render` and a `LegacyWindowsTerm`, which colours by
    # calling the console API and passes the plain text down to
    # `console.file.write` — us. That console has no VT parser, so the two DEC
    # private sequences the wrapper adds are not ignored, they are PRINTED:
    # `<-[?2026h` at the head of every frame, twenty times a second. The
    # synchronisation is a nicety; the garbage would not be.
    if not console.legacy_windows:
        console.file = cast("IO[str]", _Synchronised(original))
    try:
        with _single_keystrokes(), Live(console=console, auto_refresh=False) as live:
            for frame in range(_IDLE_FRAMES):
                live.update(block(*_grid(frame, seed)), refresh=True)
                time.sleep(FRAME_SECONDS)
                if _key_pressed():
                    break
            live.update(block(mark_levels(), home_stops()), refresh=True)
    finally:
        console.file = original


def _grid(frame: int, seed: float) -> tuple[_Grid, _Grid]:
    """The entrance while it lasts, the perfusion after it."""
    if frame < FRAMES - 1:
        return frame_levels(frame), home_stops()
    return pulse_frame(frame, seed)


def _seed() -> float:
    """Where in the endless field this run begins."""
    import secrets

    return secrets.randbelow(10_000) / 10_000 * math.tau


def _palette(console: Console) -> tuple[str, ...] | None:
    """The stops this console can carry, or ``None`` for weight alone."""
    depth = terminal_colour_depth(console)
    if depth == "truecolor":
        return MARK_STOPS
    if depth == "256":
        return MARK_STOPS_256
    return None


class _Synchronised:
    """DECSET 2026 around each write, so the terminal presents a frame
    atomically — rich has no support for the mode, and a terminal that
    does not implement it just ignores the unknown DEC parameter. Both
    halves go out in one call: tmux freezes a pane for up to a second
    when a begin is never matched by an end."""

    def __init__(self, file: Any) -> None:
        self._file = file

    def write(self, text: str) -> int:
        if not text:
            return 0
        return int(self._file.write(f"\x1b[?2026h{text}\x1b[?2026l"))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._file, name)


@contextmanager
def _single_keystrokes() -> Iterator[None]:
    """Ask the terminal to hand over keys as typed, so the entrance can
    interrupt before the Enter that would normally deliver one. Ctrl-C
    still interrupts (cbreak leaves signal generation on); a terminal that
    will not take the mode change just plays the entrance out."""
    if sys.platform == "win32":  # pragma: no cover - windows reads keys unbuffered
        yield
        return
    import termios
    import tty

    try:
        descriptor = sys.stdin.fileno()
        saved = termios.tcgetattr(descriptor)
    except (AttributeError, OSError, ValueError):
        yield
        return
    tty.setcbreak(descriptor)
    try:
        yield
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, saved)


def _key_pressed() -> bool:
    """Whether a key has been hit, swallowing it if so: the keystroke was
    aimed at the entrance, and letting it fall through would answer the
    next menu question with a character nobody chose."""
    if sys.platform == "win32":  # pragma: no cover - exercised on the windows leg
        import msvcrt

        pressed = False
        while msvcrt.kbhit():
            msvcrt.getwch()
            pressed = True
        return pressed
    import select

    try:
        ready, _writable, _failed = select.select([sys.stdin], [], [], 0)
        if not ready:
            return False
        os.read(sys.stdin.fileno(), 4096)
    except (AttributeError, OSError, ValueError):
        return False
    return True
