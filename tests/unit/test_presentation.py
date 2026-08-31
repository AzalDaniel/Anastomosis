"""The shared console-glyph selector and the non-UTF-8 console safety net.

These pin the Windows-console crash fix: the status dingbats and arrow the CLI
prints (``✓`` U+2713, ``✗`` U+2717, ``→`` U+2192) are not encodable in CP-1252,
so a text frontend must fall back to ASCII on such a stream rather than abort
the command with :class:`UnicodeEncodeError`.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from anastomosis.core.presentation import (
    ASCII_GLYPHS,
    UNICODE_GLYPHS,
    Glyphs,
    terminal_glyphs,
)
from anastomosis.deliver.router import plan_route
from anastomosis.destinations.registry import DestinationRegistry


class _Stream:
    """A minimal stand-in exposing just ``.encoding`` (like a text stream)."""

    def __init__(self, encoding: str | None) -> None:
        self.encoding = encoding


@pytest.mark.parametrize("encoding", ["utf-8", "UTF-8", "utf8", "utf-8-sig", "UTF8", "utf_8"])
def test_utf8_streams_get_unicode_glyphs(encoding: str) -> None:
    assert terminal_glyphs(_Stream(encoding)) is UNICODE_GLYPHS


@pytest.mark.parametrize("encoding", ["cp1252", "cp437", "latin-1", "ascii", "iso-8859-1"])
def test_legacy_codepages_get_ascii_glyphs(encoding: str) -> None:
    assert terminal_glyphs(_Stream(encoding)) is ASCII_GLYPHS


def test_missing_or_absent_encoding_falls_back_to_ascii() -> None:
    assert terminal_glyphs(_Stream(None)) is ASCII_GLYPHS
    assert terminal_glyphs(_Stream("")) is ASCII_GLYPHS
    assert terminal_glyphs(object()) is ASCII_GLYPHS  # no .encoding attribute at all


def test_ascii_fallback_survives_cp1252_but_unicode_does_not() -> None:
    # The whole point: every fallback glyph encodes cleanly on a CP-1252 console.
    for glyph in (ASCII_GLYPHS.ok, ASCII_GLYPHS.fail, ASCII_GLYPHS.arrow):
        glyph.encode("cp1252")  # must not raise
    # And the pretty set genuinely does NOT — the crash this fix prevents.
    for glyph in (UNICODE_GLYPHS.ok, UNICODE_GLYPHS.fail, UNICODE_GLYPHS.arrow):
        with pytest.raises(UnicodeEncodeError):
            glyph.encode("cp1252")


def test_transit_map_markers_survive_print_to_cp1252_console() -> None:
    transit = plan_route("tebra", DestinationRegistry.load())
    buf = io.BytesIO()
    console = Console(file=io.TextIOWrapper(buf, encoding="cp1252", newline=""))

    glyphs = terminal_glyphs(console.file)
    assert glyphs is ASCII_GLYPHS
    rendered = transit.render(glyphs)

    # End-to-end through Rich: print to the CP-1252 stream and read back the
    # operator-visible bytes. The markers must SURVIVE markup parsing — a
    # bracketed marker like "[ok]" would be eaten by rich.console.print.
    console.print(rendered)
    console.file.flush()
    printed = buf.getvalue().decode("cp1252")

    # Target the marker COLUMN ("  <mark> <kind>") so an incidental '+'/'x' in
    # route text cannot mask a dropped marker. tebra has both a viable route
    # (ccda_import) and unviable ones (vendor_api, browser), so both appear.
    lines = printed.splitlines()
    assert any(ln.startswith(f"  {ASCII_GLYPHS.ok} ") for ln in lines), printed
    assert any(ln.startswith(f"  {ASCII_GLYPHS.fail} ") for ln in lines), printed
    assert "✓" not in printed and "✗" not in printed  # no crash glyph leaked through


def test_unicode_render_would_crash_a_cp1252_console() -> None:
    # Demonstrates the bug the fallback prevents (the default render is Unicode).
    transit = plan_route("tebra", DestinationRegistry.load())
    rendered = transit.render()
    assert "✓" in rendered  # the default keeps the pretty marker
    with pytest.raises(UnicodeEncodeError):
        rendered.encode("cp1252")


def test_render_default_glyphs_are_unicode() -> None:
    # No-arg render stays byte-identical for UTF-8 callers and existing tests.
    transit = plan_route("tebra", DestinationRegistry.load())
    assert transit.render() == transit.render(UNICODE_GLYPHS)


def test_cli_glyphs_follow_the_live_console_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    from anastomosis import cli

    cp1252 = Console(file=io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))
    monkeypatch.setattr(cli, "console", cp1252)
    assert cli._glyphs() is ASCII_GLYPHS

    utf8 = Console(file=io.TextIOWrapper(io.BytesIO(), encoding="utf-8"))
    monkeypatch.setattr(cli, "console", utf8)
    assert cli._glyphs() is UNICODE_GLYPHS


def test_glyphs_dataclass_is_frozen() -> None:
    g = Glyphs(ok="a", fail="b", arrow="c")
    with pytest.raises(AttributeError):
        g.ok = "z"  # type: ignore[misc]  # frozen dataclass


def test_as_typed_keeps_what_a_person_can_see() -> None:
    """The whole escape sequence goes, not just its first byte.

    An arrow key is ESC [ A — removing the ESC alone would leave ``[A`` in a
    filename, which is the same defect wearing fewer bytes. Sequences are
    swept as units, whatever mode the terminal was in, and plain text in any
    script passes through untouched.
    """
    from anastomosis.core.presentation import as_typed

    cases = {
        "abc\x1b[Adef": "abcdef",  # up-arrow, CSI mode
        "up\x1b[A down\x1b[B": "up down",
        "home\x1bOH end\x1bOF": "home end",  # the same keys in application mode
        "fn\x1b[15~key": "fnkey",  # a function key's longer sequence
        "alt\x1bx": "alt",  # an Alt chord arrives as ESC x
        "trail\x1b": "trail",  # a lone ESC right before Enter
        "del\x7fete": "delete",
        "tab\there": "tabhere",
        "plain café 病歷": "plain café 病歷",
    }
    for raw, expected in cases.items():
        assert as_typed(raw) == expected, raw.encode()


def test_as_typed_guards_every_prompt_the_product_asks() -> None:
    """The seven guided prompts and the destination wizard all read through it.

    The sweep only protects call sites that use it; this pins the two modules
    that read free-typed answers to the one door, so an eighth prompt added
    without the sweep shows up here as a missing anchor rather than as an
    escape sequence in somebody's mapping id.
    """
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src" / "anastomosis"
    guide = (src / "cli_commands" / "guide.py").read_text(encoding="utf-8")
    destination = (src / "cli_commands" / "destination.py").read_text(encoding="utf-8")
    assert "as_typed(answer)" in guide, "the guided reader must sweep what it reads"
    assert guide.count("console.input(") == 1, (
        "every guided prompt funnels through the one swept reader"
    )
    assert "as_typed(typer.prompt" in destination, (
        "the destination wizard reads free text and must sweep it too"
    )
