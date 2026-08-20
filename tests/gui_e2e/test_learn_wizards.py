"""The two learn-a-thing wizards: pack-from-samples and learn-a-source.

Both share one shape — analyze (refused, with a PHI-safe summary to review),
confirm, then write — and both get their real result through an event followed
by a ``last_*_result`` fetch. These tests walk that gate on each page: the
confirmation must be REQUIRED, and a fresh analysis must revoke it.
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright", reason="the GUI lane needs playwright + chromium")

from anastomosis.gui.consoles.packgen import PackgenConsole
from anastomosis.gui.consoles.source import SourceConsole
from anastomosis.gui.events import stage_event

pytestmark = pytest.mark.gui_e2e


def test_packgen_requires_the_distinct_patients_confirmation(gui) -> None:
    """analyze → summary → confirm → emit, with the gate closed until confirmed."""
    packgen = gui("packgen.html")
    page = packgen.page

    page.fill("#samples-dir", "/synthetic/samples")
    page.fill("#pack-name", "acme_soap")
    page.click("#analyze-btn")
    page.wait_for_timeout(100)

    # The analyze step asks for the un-confirmed run: the controller refuses to
    # emit and stashes the summary instead.
    assert packgen.last_args("pack_init_async") == ["/synthetic/samples", "acme_soap", None, False]
    assert page.locator("#summary-panel").is_hidden(), "the summary appears only after the event"

    packgen.emit(stage_event(PackgenConsole._FLOW, "packgen", "done"))

    assert packgen.called("last_pack_result")
    assert not page.locator("#summary-panel").is_hidden()
    assert "samples analyzed" in (page.locator("#summary").text_content() or "")
    assert "Same-patient caveat" in (page.locator("#caveat").text_content() or "")
    assert page.locator("#emit-btn").is_disabled(), "writing must be gated on the confirmation"

    # The checkbox is visually replaced by its track, so an operator clicks the
    # label — which is what this does.
    page.click("label.toggle:has(#confirm-distinct)")
    assert not page.locator("#emit-btn").is_disabled()
    page.click("#emit-btn")
    page.wait_for_timeout(100)

    assert packgen.last_args("pack_init_async")[3] is True, "the emit step must confirm"


def test_packgen_revokes_the_confirmation_on_a_fresh_analysis(gui) -> None:
    """A new summary re-arms the gate: consent is per-analysis, never sticky."""
    packgen = gui("packgen.html")
    page = packgen.page
    page.fill("#samples-dir", "/synthetic/samples")
    page.fill("#pack-name", "acme_soap")
    page.click("#analyze-btn")
    packgen.emit(stage_event(PackgenConsole._FLOW, "packgen", "done"))
    page.click("label.toggle:has(#confirm-distinct)")
    assert not page.locator("#emit-btn").is_disabled()

    packgen.emit(stage_event(PackgenConsole._FLOW, "packgen", "done"))

    assert not page.locator("#confirm-distinct").is_checked()
    assert page.locator("#emit-btn").is_disabled()


def test_source_wizard_shows_the_proposed_mapping_before_saving(gui) -> None:
    """The proposal renders column names + transforms — never a cell value."""
    source = gui("source.html")
    page = source.page

    page.fill("#example-path", "/synthetic/export.csv")
    page.fill("#source-name", "acme_csv")
    page.click("#analyze-btn")
    page.wait_for_timeout(100)

    assert source.last_args("source_init_async") == [
        "/synthetic/export.csv",
        "acme_csv",
        None,
        False,
    ]

    source.emit(stage_event(SourceConsole._FLOW, "source", "done"))

    assert source.called("last_source_result")
    assert not page.locator("#proposal-panel").is_hidden()
    grouping = page.locator("#grouping").text_content() or ""
    assert "format csv" in grouping and "6 columns" in grouping
    # One header row plus one row per suggestion, unmapped columns included.
    rows = page.locator("#suggestions .suggestion-row")
    assert rows.count() == 4
    assert "(unmapped → extensions)" in (rows.nth(3).text_content() or "")
    assert page.locator("#save-btn").is_disabled()

    page.click("label.toggle:has(#confirm-review)")
    assert not page.locator("#save-btn").is_disabled()
    page.click("#save-btn")
    page.wait_for_timeout(100)

    assert source.last_args("source_init_async")[3] is True, "the save step must confirm"
