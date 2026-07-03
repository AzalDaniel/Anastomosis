# AI-assisted: written with Claude agents under the author's direction and review; see DESIGN.md.
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
