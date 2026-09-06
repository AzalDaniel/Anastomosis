"""The vessel mark the guided session greets with, and the promises it
keeps: a script never sees it (only a real terminal gets it, plain
header otherwise, byte for byte); a legacy console never sees mojibake
(pure ASCII ramp, CP-1252-safe); the entrance ends exactly where the
settled mark is (frame-indexed, never clock-driven); it cannot outstay
its welcome (bounded frames, abandoned on the first keystroke); and it
is the logo (re-sampled from the geometry ``tools/make_vessel.py``
writes the icons from, so it cannot drift)."""

from __future__ import annotations

import io
import itertools
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from rich.console import Console
from rich.text import Text

import anastomosis
from anastomosis.core import vesselmark
from anastomosis.core.presentation import (
    ASCII_GLYPHS,
    BRAND_PALETTE,
    UNICODE_GLYPHS,
    terminal_colour_depth,
    terminal_glyphs,
)
from anastomosis.core.vesselmark_data import DENSITY, LEVELS

if TYPE_CHECKING:
    from collections.abc import Callable

REPO_ROOT = Path(__file__).resolve().parents[2]
GREETING = (Text("Anastomosis 0.0.0"), Text("Turn an EHR export into charts."))


class _TerminalFile(io.StringIO):
    """A stream that claims to be a terminal, and says how it encodes."""

    def __init__(self, encoding: str = "utf-8") -> None:
        super().__init__()
        self._encoding = encoding

    @property
    def encoding(self) -> str:
        return self._encoding

    def isatty(self) -> bool:
        return True


def _console(*, terminal: bool = True, width: int = 100, encoding: str = "utf-8") -> Console:
    """A console whose stream answers ``isatty`` the way this test needs."""
    stream: io.StringIO = _TerminalFile(encoding) if terminal else io.StringIO()
    return Console(file=stream, width=width, force_terminal=False)


def _said(console: Console) -> str:
    assert isinstance(console.file, io.StringIO)
    return console.file.getvalue()


def _mark_columns(line: str) -> str:
    """The part of a composed line the mark owns."""
    return line[: vesselmark.MARK_WIDTH]


# --- the mark is the logo ----------------------------------------------------


def test_the_grid_is_still_what_the_geometry_samples_to() -> None:
    """Re-sampling the checked-in grid must match exactly; regenerate with
    ``python tools/make_vessel.py`` if the logo's geometry changes."""
    from tools import make_vessel

    written = (REPO_ROOT / "src" / "anastomosis" / "core" / "vesselmark_data.py").read_text(
        encoding="utf-8"
    )
    assert make_vessel.matrix_module_text() == written, (
        "the terminal mark no longer matches the vessel geometry — "
        "run `python tools/make_vessel.py` and commit the result"
    )


def test_the_grid_is_a_rectangle_of_levels_the_ramp_can_draw() -> None:
    assert len(DENSITY) == vesselmark.MARK_HEIGHT >= 6
    assert {len(row) for row in DENSITY} == {vesselmark.MARK_WIDTH}
    assert vesselmark.MARK_WIDTH >= 15
    levels = {int(digit) for row in DENSITY for digit in row}
    assert levels <= set(range(LEVELS + 1))
    # Both ends of the ramp are really used, or the sampling has gone flat.
    assert {0, LEVELS} <= levels


def test_the_trunk_reaches_the_bottom_row() -> None:
    """The silhouette is a fan on a stem: the bottom row must carry ink in
    exactly one short contiguous run, or the sampling flattened it into a
    blob."""
    bottom = DENSITY[-1]
    inked = [index for index, digit in enumerate(bottom) if digit != "0"]
    assert inked, "the trunk must reach the foot of the mark"
    assert inked == list(range(inked[0], inked[-1] + 1))
    assert len(inked) <= vesselmark.MARK_WIDTH // 4


# --- the ramp ----------------------------------------------------------------


def test_the_ramp_climbs_in_both_channels_at_once() -> None:
    """Density and weight agree on which cell is heavier: colour is never
    the only signal, so the gradient must read as glyph size and weight
    alone."""
    assert vesselmark._WEIGHTS == ("", "dim", "default", "default", "bold")
    assert vesselmark._WEIGHTS[2] == vesselmark._WEIGHTS[3]
    for ramp in (vesselmark.UNICODE_DOTS, vesselmark.ASCII_DOTS):
        assert len(ramp) == LEVELS + 1
        assert ramp[0] == " "
        # Five distinct glyphs, which is strictly stronger than what this
        # asserted when levels 1 and 2 shared one. The two channels now
        # stagger — the glyph moves at every step, the weight at 1, 2 and 4 —
        # and that is what "climbs in both channels" was protecting: never a
        # step where both stand still, never a step where they disagree.
        assert len(set(ramp)) == LEVELS + 1


def test_no_colour_enters_the_mark() -> None:
    """§11: the terminal's background belongs to whoever is running this."""
    for weight in vesselmark._WEIGHTS:
        assert "#" not in weight
        assert not any(character.isdigit() for character in weight)
        assert "red" not in weight, "identity is carried by weight, not by hue"


@pytest.mark.parametrize("frame", [0, 3, vesselmark.FRAMES - 1])
def test_the_ascii_ramp_is_pure_ascii(frame: int) -> None:
    """A legacy console gets dots it can encode, never replacement characters."""
    for line in vesselmark.render(vesselmark.frame_levels(frame), unicode_dots=False):
        line.plain.encode("ascii")  # raises the moment the fallback grows a dot
        line.plain.encode("cp1252")


def test_the_unicode_ramp_is_only_reached_on_a_utf8_stream() -> None:
    """The glyph set IS the capability decision the CLI already makes."""
    assert terminal_glyphs(_TerminalFile("utf-8")) is UNICODE_GLYPHS
    assert terminal_glyphs(_TerminalFile("cp1252")) is ASCII_GLYPHS

    console = _console(encoding="cp1252")
    assert vesselmark.show_greeting(console, GREETING, animate=False) is True
    _said(console).encode("cp1252")  # raises if a round dot got through


# --- the entrance ------------------------------------------------------------


def test_the_entrance_only_ever_gains_ink() -> None:
    """Every cell is monotone across the entrance, and so is the whole grid."""
    frames = [vesselmark.frame_levels(index) for index in range(vesselmark.FRAMES)]
    for earlier, later in itertools.pairwise(frames):
        for before, after in zip(earlier, later, strict=True):
            assert all(was <= now for was, now in zip(before, after, strict=True))
    totals = [sum(sum(row) for row in frame) for frame in frames]
    assert totals == sorted(totals)
    assert totals[0] < totals[-1], "the entrance must actually assemble something"


def test_the_last_frame_is_the_settled_mark() -> None:
    """What an operator ends up looking at is the logo, not a frame near it."""
    assert vesselmark.frame_levels(vesselmark.FRAMES - 1) == vesselmark.mark_levels()
    # And nothing past the end drifts off it.
    assert vesselmark.frame_levels(vesselmark.FRAMES + 40) == vesselmark.mark_levels()


def test_the_mark_arrives_at_the_end_of_the_entrance() -> None:
    """An entrance that is over a third of the way in is not an entrance.

    The furthest capillary is still filling on the second-to-last frame; what
    the frames after it buy is a beat on the settled mark before the menu.
    """
    settled = vesselmark.mark_levels()
    arrived = next(
        (index for index in range(vesselmark.FRAMES) if vesselmark.frame_levels(index) == settled),
        None,
    )
    assert arrived is not None, "the entrance never reaches the settled mark"
    assert arrived >= vesselmark.FRAMES - 2, f"the mark was finished by frame {arrived}"


def test_the_entrance_starts_at_the_foot_of_the_trunk() -> None:
    """It perfuses: the first ink is the trunk, not the canopy."""
    first = vesselmark.frame_levels(0)
    lit = [row for row, cells in enumerate(first) for level in cells if level]
    assert lit, "the first frame must show something"
    assert min(lit) >= vesselmark.MARK_HEIGHT - 3


def test_a_negative_frame_is_refused() -> None:
    with pytest.raises(ValueError, match="frame index"):
        vesselmark.frame_levels(-1)


def test_the_entrance_is_bounded_to_about_a_second() -> None:
    assert vesselmark.FRAMES * vesselmark.FRAME_SECONDS <= 1.2


def _recording(seen: list[int]) -> Callable[[int], tuple[tuple[int, ...], ...]]:
    """``frame_levels``, noting which frames were asked for."""
    real = vesselmark.frame_levels

    def _record(frame: int) -> tuple[tuple[int, ...], ...]:
        seen.append(frame)
        return real(frame)

    return _record


def test_a_keystroke_ends_the_entrance_early(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nobody waits on an animation to answer the question underneath it."""
    drawn: list[int] = []
    monkeypatch.setattr(vesselmark, "_key_pressed", lambda: True)
    monkeypatch.setattr(vesselmark, "frame_levels", _recording(drawn))
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    console = _console()
    vesselmark._play(console, GREETING, unicode_dots=True)

    assert drawn == [0], f"the entrance kept playing after a keystroke: {drawn}"
    # And what is left on screen is the settled mark, not the frame it stopped on.
    assert vesselmark.UNICODE_DOTS[LEVELS] in _said(console)


def test_an_uninterrupted_entrance_plays_every_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    drawn: list[int] = []
    monkeypatch.setattr(vesselmark, "_key_pressed", lambda: False)
    monkeypatch.setattr(vesselmark, "frame_levels", _recording(drawn))
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    vesselmark._play(_console(), GREETING, unicode_dots=True)

    assert drawn == list(range(vesselmark.FRAMES - 1))


# --- who gets the mark, and who gets the plain header ------------------------


def test_a_stream_that_is_not_a_terminal_never_gets_the_mark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole safety property: piped, redirected or in CI, nothing moves."""

    def _never(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the entrance must not run without a terminal")

    monkeypatch.setattr(vesselmark, "_play", _never)
    console = _console(terminal=False)
    assert vesselmark.can_draw(console) is False
    assert vesselmark.show_greeting(console, GREETING) is False
    assert _said(console) == "", "a non-terminal must be written nothing at all"


def test_a_console_that_reports_colour_but_is_not_a_terminal_is_refused() -> None:
    """``FORCE_COLOR`` answers a different question from "is anybody there"."""
    console = Console(file=io.StringIO(), width=100, force_terminal=True)
    assert console.is_terminal is True
    assert vesselmark.can_draw(console) is False


@pytest.mark.parametrize("width", [20, 40, vesselmark.MARK_WIDTH + vesselmark.GUTTER])
def test_a_narrow_terminal_gets_the_text_only_greeting(width: int) -> None:
    console = _console(width=width)
    assert vesselmark.can_draw(console) is False
    assert vesselmark.show_greeting(console, GREETING) is False
    assert _said(console) == ""


def test_a_wide_terminal_gets_the_mark_beside_the_words() -> None:
    console = _console(width=100)
    assert vesselmark.show_greeting(console, GREETING, animate=False) is True
    lines = _said(console).splitlines()
    assert len(lines) == vesselmark.MARK_HEIGHT
    assert any("Anastomosis 0.0.0" in line for line in lines)
    # The words sit BESIDE the mark: never in its columns, never above it.
    for line in lines:
        assert set(_mark_columns(line)) <= set(vesselmark.UNICODE_DOTS)


def test_a_long_greeting_keeps_its_own_column() -> None:
    """Wrapped text stays beside the mark instead of folding under the dots."""
    console = _console(width=60)
    long_line = Text("Turn an EHR export into complete, verified charts you can keep or move.")
    assert vesselmark.show_greeting(console, (long_line,), animate=False) is True
    said = _said(console)
    assert "verified" in said
    for line in said.splitlines():
        assert set(_mark_columns(line)) <= set(vesselmark.UNICODE_DOTS)


def test_no_color_settles_the_mark_instead_of_playing_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set by somebody reading this through something. Motion is adornment."""

    def _never(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("NO_COLOR must not get an animation")

    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(vesselmark, "_play", _never)
    console = _console()
    assert vesselmark.show_greeting(console, GREETING) is True
    assert len(_said(console).splitlines()) == vesselmark.MARK_HEIGHT


# --- the header the guided session actually prints ---------------------------


def test_the_plain_header_is_unchanged_where_there_is_no_terminal() -> None:
    """Byte for byte what bare ``anast`` has always printed off a terminal."""
    from anastomosis.cli_commands import guide

    console = Console(file=io.StringIO(), width=100)
    guide._print_header(console)
    assert _said(console) == (
        f"\n| Anastomosis {anastomosis.__version__}\n"
        "  Turn an EHR export into complete, verified charts you can keep or move.\n"
    )


def test_the_header_draws_the_mark_on_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    from anastomosis.cli_commands import guide

    monkeypatch.setenv("NO_COLOR", "1")  # settle it rather than animate it
    console = _console(width=100)
    guide._print_header(console)
    said = _said(console)

    assert f"Anastomosis {anastomosis.__version__}" in said
    assert guide.PURPOSE in said
    assert vesselmark.UNICODE_DOTS[LEVELS] in said
    assert ASCII_GLYPHS.bar not in said, "the mark replaces the bar, it does not join it"


# --- what a real terminal receives -------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="pty is POSIX-only")
def test_the_mark_reaches_a_real_pty() -> None:
    """Drives ``anast`` on a real pty and looks for the settled mark in the
    bytes -- the one check that fails if the greeting stops being wired
    to the real command."""
    import pty
    import select
    import time

    environment = dict(
        os.environ,
        PYTHONPATH=os.pathsep.join(
            [str(REPO_ROOT / "src"), os.environ.get("PYTHONPATH", "")]
        ).strip(os.pathsep),
        COLUMNS="100",
        LINES="40",
        TERM="xterm-256color",
    )
    environment.pop("NO_COLOR", None)
    child, terminal = pty.fork()
    if child == 0:  # pragma: no cover - the child replaces itself immediately
        os.environ.clear()
        os.environ.update(environment)
        # A fixed interpreter path and an argument list: no shell anywhere.
        os.execv(
            sys.executable,
            [
                sys.executable,
                "-c",
                "import sys; sys.argv = ['anast']; from anastomosis.cli import app; app()",
            ],
        )

    captured = bytearray()
    left_the_menu = False
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        ready, _writable, _failed = select.select([terminal], [], [], 0.5)
        if ready:
            try:
                chunk = os.read(terminal, 65536)
            except OSError:  # the child closed the terminal
                break
            if not chunk:
                break
            captured += chunk
        elif captured and not left_the_menu:
            os.write(terminal, b"q\n")
            left_the_menu = True
    os.close(terminal)
    os.waitpid(child, 0)

    plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07", "", captured.decode("utf-8"))
    assert "What would you like to do?" in plain, "the pty run never reached the menu"
    settled = [
        row.plain.rstrip() for row in vesselmark.render(vesselmark.mark_levels(), unicode_dots=True)
    ]
    missing = [row for row in settled if row and row not in plain]
    assert not missing, f"the settled mark never reached the terminal: {missing[:2]}"


def test_the_greeting_renders_on_a_strict_cp1252_console() -> None:
    """A stock Windows console writes through strict CP-1252; one
    unencodable character must not abort the command mid-output."""
    script = (
        "import sys\n"
        "from rich.console import Console\n"
        "from rich.text import Text\n"
        "from anastomosis.core import vesselmark\n"
        "console = Console(file=sys.stdout, width=100)\n"
        "rows = vesselmark.render(vesselmark.mark_levels(), unicode_dots=False)\n"
        "for line in vesselmark.beside(rows, [Text('Anastomosis')]):\n"
        "    console.print(line)\n"
    )
    finished = subprocess.run(
        [sys.executable, "-c", script],
        env=dict(os.environ, PYTHONIOENCODING="cp1252:strict"),
        text=True,
        capture_output=True,
        check=False,
    )
    combined = finished.stdout + finished.stderr
    assert finished.returncode == 0, combined
    assert "Traceback" not in combined
    assert "Anastomosis" in finished.stdout
    assert vesselmark.ASCII_DOTS[LEVELS] in finished.stdout


# --- the amended §11: one ramp, and only this one ----------------------------

#: Nine real terminal grounds: five dark, four light. A decorative object has to
#: clear 3 : 1 (WCAG 1.4.11) on whichever one a stranger is running.
_GROUNDS = (
    "#000000",
    "#1e1e1e",
    "#002b36",
    "#282c34",
    "#171310",
    "#ffffff",
    "#fdf6e3",
    "#f0eade",
    "#f5f5f5",
)


def _relative_luminance(hex_colour: str) -> float:
    """WCAG 2.x relative luminance. Eight lines, and no new dependency."""

    def channel(value: int) -> float:
        unit = value / 255.0
        return unit / 12.92 if unit <= 0.04045 else ((unit + 0.055) / 1.055) ** 2.4

    red, green, blue = (int(hex_colour[index : index + 2], 16) for index in (1, 3, 5))
    return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)


def _contrast(first: str, second: str) -> float:
    high, low = sorted((_relative_luminance(first), _relative_luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _resolved(stop: str) -> str:
    """A `#rrggbb` for either spelling of a stop, index or hex."""
    from rich.color import Color

    triplet = Color.parse(stop).get_truecolor()
    return f"#{triplet.red:02x}{triplet.green:02x}{triplet.blue:02x}"


def test_every_mark_stop_clears_three_to_one_on_both_grounds() -> None:
    """Contract: every mark stop's relative luminance falls in 0.175-0.242,
    the window where it holds >=3:1 (WCAG 1.4.11) against both a dark and
    a light ground at once -- the whole licence a decorative hue needs,
    since a background belongs to whoever runs the terminal."""
    for name, ramp in (("truecolor", vesselmark.MARK_STOPS), ("256", vesselmark.MARK_STOPS_256)):
        for stop in ramp:
            resolved = _resolved(stop)
            luminance = _relative_luminance(resolved)
            assert 0.175 <= luminance <= 0.242, (
                f"{name} stop {stop} has luminance {luminance:.4f}, outside the "
                "0.175-0.242 window where 3 : 1 holds on a dark AND a light ground"
            )
            for ground in _GROUNDS:
                ratio = _contrast(resolved, ground)
                assert ratio >= 3.0, (
                    f"{name} stop {stop} measures {ratio:.2f} : 1 on {ground}; "
                    "a decorative object needs 3 : 1 (WCAG 1.4.11)"
                )


def test_the_mark_does_not_borrow_the_refusal_colour() -> None:
    """Identity may not wear the colour that means "this failed": measured
    as OKLab distance from the refusal reds, since "not red" must be
    judged the way the eye judges it."""

    def oklab(hex_colour: str) -> tuple[float, float, float]:
        def linear(value: int) -> float:
            unit = value / 255.0
            return unit / 12.92 if unit <= 0.04045 else ((unit + 0.055) / 1.055) ** 2.4

        red, green, blue = (linear(int(hex_colour[i : i + 2], 16)) for i in (1, 3, 5))
        long_ = (0.4122214708 * red + 0.5363325363 * green + 0.0514459929 * blue) ** (1 / 3)
        medium = (0.2119034982 * red + 0.6806995451 * green + 0.1073969566 * blue) ** (1 / 3)
        short = (0.0883024619 * red + 0.2817188376 * green + 0.6299787005 * blue) ** (1 / 3)
        return (
            0.2104542553 * long_ + 0.7936177850 * medium - 0.0040720468 * short,
            1.9779984951 * long_ - 2.4285922050 * medium + 0.4505937099 * short,
            0.0259040371 * long_ + 0.7827717662 * medium - 0.8086757660 * short,
        )

    def distance(first: str, second: str) -> float:
        a, b = oklab(first), oklab(second)
        return sum((x - y) ** 2 for x, y in zip(a, b, strict=True)) ** 0.5

    refusals = ("#ff645f", "#cd0000", "#ff0000")  # --stop, ANSI red, pure red
    for stop in vesselmark.MARK_STOPS:
        for refusal in refusals:
            assert distance(stop, refusal) >= 0.06, (
                f"{stop} sits {distance(stop, refusal):.3f} from the refusal colour "
                f"{refusal} in OKLab; identity would read as an error"
            )
        _lightness, a, b = oklab(stop)
        assert (a * a + b * b) ** 0.5 < 0.19, (
            f"{stop} is more saturated than `--stop` itself, which is the one "
            "thing a decorative ramp may never out-shout"
        )
    palette_values = {value for value in vars(BRAND_PALETTE).values() if isinstance(value, str)}
    assert not (set(vesselmark.MARK_STOPS) & palette_values)


class _Capable:
    """A console with exactly the three attributes the gate reads."""

    def __init__(self, system: str | None, *, tty: bool = True, no_color: bool = False) -> None:
        self.color_system = system
        self.no_color = no_color
        self.file = _TerminalFile("utf-8") if tty else io.StringIO()


def test_the_sixteen_colour_floor_takes_no_colour_at_all() -> None:
    """At sixteen colours every stop quantises onto ANSI 9 (bright_red, the
    refusal colour), so the honest answer is no colour at all -- driven
    by the three attributes the gate actually reads, including NO_COLOR
    (which still reports color_system="256") and whether anyone is watching."""
    assert terminal_colour_depth(_Capable("truecolor")) == "truecolor"
    assert terminal_colour_depth(_Capable("256")) == "256"
    for sixteen_or_fewer in ("standard", "windows", None):
        assert terminal_colour_depth(_Capable(sixteen_or_fewer)) == "none", (
            f"{sixteen_or_fewer!r} cannot hold the ramp without landing on bright_red"
        )
    # `NO_COLOR` leaves color_system reporting "256", so branching on depth
    # alone would walk straight past somebody who asked for plain output.
    assert terminal_colour_depth(_Capable("256", no_color=True)) == "none"
    assert terminal_colour_depth(_Capable("truecolor", no_color=True)) == "none"
    # And nobody watching gets nothing, whatever the console claims it can do.
    assert terminal_colour_depth(_Capable("truecolor", tty=False)) == "none"


@pytest.mark.skipif(sys.platform == "win32", reason="rich reads TERM only on POSIX")
def test_rich_reports_the_depth_this_gate_was_built_against() -> None:
    """Rich's `_TERM_COLORS` matches only the last hyphen segment, so
    `alacritty` and `xterm-ghostty` both read `standard` -- the 256 rung
    is the common case, truecolor the exception, and no `TERM_PROGRAM`
    allowlist guesses upward (a wrong guess emits truecolor bytes a
    sixteen-colour terminal renders as garbage)."""
    for environ, expected in (
        ({"TERM": "xterm-256color"}, "256"),
        ({"COLORTERM": "truecolor", "TERM": "xterm-256color"}, "truecolor"),
        ({"TERM": "xterm"}, "none"),
        ({"TERM": "dumb"}, "none"),
        ({"TERM": "alacritty"}, "none"),
        ({"NO_COLOR": "1", "TERM": "xterm-256color"}, "none"),
    ):
        console = Console(file=_TerminalFile("utf-8"), _environ=environ, force_terminal=True)
        assert terminal_colour_depth(console) == expected, f"{environ} should give {expected!r}"


def test_the_mark_is_unchanged_with_the_colour_stripped() -> None:
    """Colour is the second channel, never the only one: every frame's
    CHARACTERS must be identical whether or not a palette was passed."""
    for frame in (0, 5, vesselmark.FRAMES, vesselmark.FRAMES + 40, vesselmark.FRAMES + 137):
        levels, stops = vesselmark.pulse_frame(frame, seed=2.4)
        plain = vesselmark.render(levels, unicode_dots=True)
        coloured = vesselmark.render(
            levels, unicode_dots=True, stops=stops, palette=vesselmark.MARK_STOPS
        )
        assert [line.plain for line in plain] == [line.plain for line in coloured]


# --- the perfusion -----------------------------------------------------------


def test_the_pulse_settles_on_the_still_mark() -> None:
    """Amplitude zero reproduces the settled logo exactly, in both channels.

    This is what the exit stands on: whichever frame a keystroke lands in, what
    is written last is the mark itself, not a frame that happened to be close.
    """
    levels, stops = vesselmark.pulse_frame(0)
    assert levels == vesselmark.mark_levels()
    assert stops == vesselmark.home_stops()


def test_no_cell_ever_outgrows_its_settled_level() -> None:
    """The mark breathes inward. It never swells past the logo's own silhouette."""
    home = vesselmark.mark_levels()
    for frame in range(vesselmark.FRAMES, vesselmark.FRAMES + 120):
        levels, _stops = vesselmark.pulse_frame(frame, seed=1.1)
        pairs = zip(levels, home, strict=True)
        for row, home_row in pairs:
            for level, settled in zip(row, home_row, strict=True):
                assert level <= settled


def test_the_pulse_never_blinks() -> None:
    """Nothing dims or brightens all at once — that is a strobe, not a pulse.

    The cheapest way to catch a regression that multiplies the whole grid by
    something: watch the mean inked level and require it to stay in a band.
    """
    home = vesselmark.mark_levels()
    inked = sum(1 for row in home for level in row if level)
    means = []
    for frame in range(vesselmark.FRAMES, vesselmark.FRAMES + 160):
        levels, _stops = vesselmark.pulse_frame(frame, seed=0.7)
        means.append(sum(level for row in levels for level in row) / inked)
    assert 2.2 <= min(means) and max(means) <= 2.95, f"{min(means):.3f}..{max(means):.3f}"


def test_the_pulse_actually_moves_and_never_repeats() -> None:
    """A frozen animation is invisible in a diff, so churn is asserted; the
    three temporal ratios are incommensurable, so no two frames may
    repeat."""
    home = vesselmark.mark_levels()
    inked = sum(1 for row in home for level in row if level)
    grids, churn = [], []
    for frame in range(vesselmark.FRAMES, vesselmark.FRAMES + 200):
        levels, stops = vesselmark.pulse_frame(frame, seed=1.9)
        grids.append((levels, stops))
        if len(grids) > 1:
            previous = grids[-2]
            changed = sum(
                1
                for r in range(vesselmark.MARK_HEIGHT)
                for c in range(vesselmark.MARK_WIDTH)
                if home[r][c]
                and (levels[r][c], stops[r][c]) != (previous[0][r][c], previous[1][r][c])
            )
            churn.append(changed / inked)
    assert sum(churn) / len(churn) >= 0.08, "the mark is barely moving"
    assert len(set(grids)) == len(grids), "a frame repeated: the field has a period"


def test_the_two_arms_are_visibly_out_of_step() -> None:
    """The two mirrored arms must not beat in lockstep (reads as machinery):
    mean phase divergence over inked mirrored pairs must clear 0.20 --
    "not exactly equal" is too weak a guard, since residual asymmetry
    alone already satisfies it without the lateral term."""
    home = vesselmark.mark_levels()
    deltas = [
        abs(
            vesselmark.wave(col, row, when)
            - vesselmark.wave(vesselmark.MARK_WIDTH - 1 - col, row, when)
        )
        for when in (0.7, 1.3, 2.1, 3.4)
        for row in range(vesselmark.MARK_HEIGHT)
        for col in range(vesselmark.MARK_WIDTH // 2)
        if home[row][col] and home[row][vesselmark.MARK_WIDTH - 1 - col]
    ]
    mean = sum(deltas) / len(deltas)
    assert mean >= 0.20, (
        f"mirrored cells differ by only {mean:.4f} on average; the arms are "
        "beating together, which reads as machinery rather than tissue"
    )


def test_the_hub_is_where_the_geometry_puts_it() -> None:
    """Recomputed from ``make_vessel``'s own constants: if the logo's
    geometry moves, the pulse's hub (where the cut vessels meet the
    trunk) must move with it."""
    from tools.make_vessel import MATRIX_COLS, MATRIX_ROWS, SIZE

    trimmed = MATRIX_ROWS - vesselmark.MARK_HEIGHT
    hub_col = (0.500 * SIZE) / (SIZE / MATRIX_COLS) - 0.5
    hub_row = (0.615 * SIZE) / (SIZE / MATRIX_ROWS) - 0.5 - trimmed
    assert abs(hub_col - vesselmark.HUB_COL) < 1e-6
    assert abs(hub_row - vesselmark.HUB_ROW) < 1e-6


def test_two_runs_do_not_open_alike() -> None:
    """A fresh `anast` starts somewhere else in a field that never repeats."""
    first = vesselmark.pulse_frame(vesselmark.FRAMES + 30, seed=0.0)
    second = vesselmark.pulse_frame(vesselmark.FRAMES + 30, seed=2.2)
    assert first != second
    assert 0.0 <= vesselmark._seed() < 6.284


def test_every_ramp_glyph_is_narrow() -> None:
    """No glyph may render double-width: `·`/`•`/`●` are East Asian Width
    Ambiguous and sheared the 21-column grid on a CJK-configured terminal."""
    import unicodedata

    for ramp in (vesselmark.UNICODE_DOTS, vesselmark.ASCII_DOTS):
        for glyph in ramp:
            assert unicodedata.east_asian_width(glyph) in {"N", "Na"}, (
                f"{glyph!r} (U+{ord(glyph):04X}) can render double-width"
            )


def test_an_unwatched_greeting_still_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bounded cost of running unattended: measured against a real pty run
    (never reached the menu inside sixty seconds at a 1200-frame cap)."""
    drawn: list[int] = []
    monkeypatch.setattr(vesselmark, "_key_pressed", lambda: False)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    real = vesselmark.pulse_frame

    def _record(frame: int, seed: float = 0.0) -> object:
        drawn.append(frame)
        return real(frame, seed)

    monkeypatch.setattr(vesselmark, "pulse_frame", _record)
    vesselmark._play(_console(), GREETING, unicode_dots=True)
    assert max(drawn) < vesselmark._IDLE_FRAMES
    assert vesselmark._IDLE_FRAMES * vesselmark.FRAME_SECONDS <= 10.0, (
        "an unattended greeting may not hold the prompt for more than ten seconds"
    )


def test_a_legacy_windows_console_is_never_sent_the_sync_sequences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DECSET 2026 sync sequences are free on any terminal that parses them,
    but a legacy Windows console has no VT parser at all and would print
    the literal escape bytes instead of obeying them -- driven on both
    states of the flag explicitly, since rich reports ``legacy_windows``
    true for any redirected stream on a Windows runner."""
    monkeypatch.setattr(vesselmark, "_key_pressed", lambda: True)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    # Both halves state the flag outright. Neither may inherit it from the host:
    # on a Windows runner rich reports `legacy_windows` TRUE for a fresh console
    # — the VT probe fails when the stream is redirected — so a test that let
    # the platform decide would assert the modern path on POSIX and the legacy
    # path on Windows while appearing to test one thing.
    modern = _console()
    monkeypatch.setattr(modern, "legacy_windows", False)
    vesselmark._play(modern, GREETING, unicode_dots=True)
    assert "\x1b[?2026h" in _said(modern), (
        "a terminal that can synchronise should still be asked to"
    )

    legacy = _console()
    monkeypatch.setattr(legacy, "legacy_windows", True)
    vesselmark._play(legacy, GREETING, unicode_dots=True)
    said = _said(legacy)
    assert "\x1b[?2026h" not in said and "\x1b[?2026l" not in said, (
        "a legacy Windows console would print these, not obey them"
    )
    assert said.strip(), "the mark must still be drawn there, just unsynchronised"
