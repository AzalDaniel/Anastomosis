"""The vessel mark the guided session greets with, and the promises it makes.

Five of them, in the order they can hurt someone:

* **A script never sees it.** The mark and its entrance are unreachable
  without a real terminal on the other end; a pipe, a redirect or a CI log
  gets the plain header ``anast`` has always printed, byte for byte.
* **A legacy console never sees mojibake.** The ASCII ramp is pure ASCII, and
  the rendered frames encode on a CP-1252 stream — the Windows console the
  rest of the CLI already degrades for.
* **The entrance ends where the mark is.** Every frame is driven by an index,
  never a clock, so this walks the whole entrance and pins that it only gains
  ink and that its last frame is the settled mark exactly.
* **It cannot outstay its welcome.** Bounded in frames, and abandoned on the
  first keystroke.
* **It is the logo.** The grid is generated from the same geometry
  ``tools/make_vessel.py`` writes the icons from; this re-samples it and fails
  on any difference, so the mark in the terminal cannot drift away from the
  one on the taskbar.
"""

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
from anastomosis.core.presentation import ASCII_GLYPHS, UNICODE_GLYPHS, terminal_glyphs
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
    """Re-sample the mark and compare: the checked-in grid must match.

    ``src/anastomosis/core/vesselmark_data.py`` is generated, and the whole
    reason it is generated rather than drawn is that a hand-made grid drifts
    away from the logo the moment the logo changes. Regenerate with
    ``python tools/make_vessel.py``.
    """
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
    """The silhouette, in one assertion: the mark is a fan on a stem.

    The bottom of the logo is trunk and nothing else, so the last row of the
    grid carries ink in exactly one short run of cells. A sampling change that
    flattened the mark into a blob would lose this.
    """
    bottom = DENSITY[-1]
    inked = [index for index, digit in enumerate(bottom) if digit != "0"]
    assert inked, "the trunk must reach the foot of the mark"
    assert inked == list(range(inked[0], inked[-1] + 1))
    assert len(inked) <= vesselmark.MARK_WIDTH // 4


# --- the ramp ----------------------------------------------------------------


def test_the_ramp_climbs_in_both_channels_at_once() -> None:
    """Density and weight agree about which cell is heavier.

    Colour is never the only signal in this product, and in the mark it is not
    a signal at all: the gradient is glyph size and text weight, ordered the
    same way, so the mark still reads on a terminal that renders neither.
    """
    assert vesselmark._WEIGHTS == ("", "dim", "default", "default", "bold")
    for ramp in (vesselmark.UNICODE_DOTS, vesselmark.ASCII_DOTS):
        assert len(ramp) == LEVELS + 1
        assert ramp[0] == " "
        # Levels 1 and 2 differ by weight alone; 3 and 4 step the glyph too.
        assert ramp[1] == ramp[2]
        assert len({ramp[2], ramp[3], ramp[4]}) == 3


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
    """Run the real entry point on a real terminal and read what came back.

    Everything above drives the renderer directly. This drives ``anast``
    itself, on a pty, and looks for the settled mark in the bytes — the one
    check that fails if the greeting stops being wired to the command an
    operator actually types.
    """
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
    """The Windows leg's proof: a legacy console gets dots, not an exception.

    ``anast`` on a stock Windows console writes through a CP-1252 stdout, and
    one character it cannot encode aborts the command mid-output. Rendered in a
    subprocess with stdio pinned to strict CP-1252, exactly as
    ``test_cli_help_encoding`` pins the help page.
    """
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
