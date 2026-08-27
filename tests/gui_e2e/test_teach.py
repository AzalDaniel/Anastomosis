"""The Teach view: one workspace, two modes, the same two-step shape.

Teaching a document layout and teaching an export format were two separate
workspaces that mirrored each other function for function. They are now two
modes of one view, and both walk the same gate: look (the controller refuses to
write and hands back something to review), confirm, then write. These tests
walk that gate in each mode — the confirmation must be REQUIRED, and a fresh
look must revoke it.
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright", reason="the GUI lane needs playwright + chromium")

from anastomosis.gui.consoles.packgen import PackgenConsole
from anastomosis.gui.consoles.source import SourceConsole
from anastomosis.gui.events import stage_event

pytestmark = pytest.mark.gui_e2e


def _open(gui, mode: str = "layout"):
    app = gui()
    app.show("teach")
    if mode != "layout":
        app.page.click(f'.mode-tab[data-mode="{mode}"]')
        app.page.wait_for_timeout(120)
    return app


def test_the_two_modes_are_one_view(gui) -> None:
    """The tabs swap the modes in place — neither is a separate destination."""
    app = _open(gui)
    page = app.page

    assert not page.locator("#teach-layout").is_hidden()
    assert page.locator("#teach-format").is_hidden()

    page.click('.mode-tab[data-mode="format"]')
    page.wait_for_timeout(120)

    assert page.locator("#teach-layout").is_hidden()
    assert not page.locator("#teach-format").is_hidden()
    assert page.locator('.mode-tab[data-mode="format"]').get_attribute("aria-selected") == "true"
    # Still the Teach view, still the one document.
    assert app.visible("teach")


def test_layout_mode_requires_the_distinct_patients_confirmation(gui) -> None:
    """Look → review → confirm → write, with the gate closed until confirmed."""
    app = _open(gui, "layout")
    page = app.page

    page.fill("#layout-samples", "/synthetic/samples")
    page.fill("#layout-name", "acme_soap")
    page.click("#layout-analyze")
    page.wait_for_timeout(150)

    # Step 1 asks for the UN-confirmed run: the controller refuses to write and
    # stashes the summary instead.
    assert app.last_args("pack_init_async") == ["/synthetic/samples", "acme_soap", None, False]
    assert page.locator("#layout-proposal").is_hidden(), "the review appears only after the event"

    app.emit(stage_event(PackgenConsole._FLOW, "packgen", "done"))

    assert app.called("last_pack_result")
    assert not page.locator("#layout-proposal").is_hidden()
    assert "samples analyzed" in app.text("#layout-summary")
    assert "About the samples" in app.text("#layout-caveat")
    proposal = (page.locator("#layout-proposal").text_content() or "").lower()
    assert "no patient data is shown" in proposal
    assert page.locator("#layout-write").is_disabled(), "writing must be gated on the confirmation"
    assert "Step 2 of 2" in app.text("#layout-step")

    # The checkbox is visually replaced by its track, so an operator clicks the
    # label — which is what this does.
    page.click("label.toggle:has(#layout-confirm)")
    assert not page.locator("#layout-write").is_disabled()
    page.click("#layout-write")
    page.wait_for_timeout(150)

    assert app.last_args("pack_init_async")[3] is True, "the write step must confirm"


def test_layout_mode_revokes_the_confirmation_on_a_fresh_look(gui) -> None:
    """A new review re-arms the gate: consent is per-analysis, never sticky."""
    app = _open(gui, "layout")
    page = app.page
    page.fill("#layout-samples", "/synthetic/samples")
    page.fill("#layout-name", "acme_soap")
    page.click("#layout-analyze")
    app.emit(stage_event(PackgenConsole._FLOW, "packgen", "done"))
    page.click("label.toggle:has(#layout-confirm)")
    assert not page.locator("#layout-write").is_disabled()

    app.emit(stage_event(PackgenConsole._FLOW, "packgen", "done"))

    assert not page.locator("#layout-confirm").is_checked()
    assert page.locator("#layout-write").is_disabled()


def test_format_mode_shows_the_match_up_before_saving(gui) -> None:
    """The proposal renders column names and how they are read — never a value."""
    app = _open(gui, "format")
    page = app.page

    page.fill("#format-example", "/synthetic/export.csv")
    page.fill("#format-name", "acme_csv")
    page.click("#format-analyze")
    page.wait_for_timeout(150)

    assert app.last_args("source_init_async") == ["/synthetic/export.csv", "acme_csv", None, False]

    app.emit(stage_event(SourceConsole._FLOW, "source", "done"))

    assert app.called("last_source_result")
    assert not page.locator("#format-proposal").is_hidden()
    # Prose, not a key=value dump.
    grouping = app.text("#format-grouping")
    assert "CSV file" in grouping and "6 columns" in grouping
    assert "patients identified by patient_id" in grouping
    assert "=" not in grouping
    # One header row plus one row per column, unmatched columns included.
    rows = page.locator("#format-mapping .mapping-row")
    assert rows.count() == 4
    assert "kept, unmatched — nothing is dropped" in (rows.nth(3).text_content() or "")
    assert page.locator("#format-save").is_disabled()

    page.click("label.toggle:has(#format-confirm)")
    assert not page.locator("#format-save").is_disabled()
    page.click("#format-save")
    page.wait_for_timeout(150)

    assert app.last_args("source_init_async")[3] is True, "the save step must confirm"


def test_format_mode_refuses_loudly_when_a_column_would_be_lost(gui) -> None:
    """The losslessness refusal keeps its teeth, in plain language."""
    app = _open(
        gui,
        "format",
    )
    app.page.evaluate("""() => {
        window.pywebview.api.last_source_result = () => Promise.resolve({
          ok: false, error: 'WouldDropColumns', dropped: ['clinic_widget_code'],
        });
    }""")

    app.emit(stage_event(SourceConsole._FLOW, "source", "done"))

    banner = app.page.locator("#banner").text_content() or ""
    assert "Cannot save yet" in banner
    assert "clinic_widget_code" in banner
    assert "Every column must have a home" in banner
