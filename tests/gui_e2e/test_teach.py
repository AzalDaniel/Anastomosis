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


def _open(gui, mode: str = "layout", **opened):
    app = gui(**opened)
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
    assert "Before you confirm" in app.text("#layout-caveat")
    # The panel WARNS about the labels rather than vouching for them. It used to
    # say "No patient data is shown below." directly above a list that can
    # contain it: the labels are the strings that recurred across the samples,
    # and a value two patients share recurs (#200).
    proposal = (page.locator("#layout-proposal").text_content() or "").lower()
    assert "read the labels below before confirming" in proposal
    assert "two patients happen to share repeats too" in proposal
    assert "no patient data is shown" not in proposal
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


def test_writing_a_layout_re_asks_for_the_layout_list(gui) -> None:
    """A written layout is offered NOW, not after a restart.

    The run forms populate from one ``info()`` answered at boot, so a layout
    taught during the session was absent from both choosers until the app was
    started again — the app reported writing something its own screens could not
    then select. Writing one asks again.
    """
    written = {
        "ok": True,
        "pack": "acme_soap",
        "pack_dir": "/synthetic/home/.anastomosis/packs/acme_soap",
        "content_hash": "0" * 64,
        "draft_md": "# Draft",
        "summary": ["4 samples analyzed"],
        "sample_count": 4,
        "low_confidence": False,
    }
    app = _open(gui, "layout", canned={"last_pack_result": written})
    boot_calls = len(app.calls("info"))

    app.emit(stage_event(PackgenConsole._FLOW, "packgen", "done"))

    assert not app.page.locator("#layout-result").is_hidden()
    assert len(app.calls("info")) > boot_calls, "the layout lists were never re-asked"
    # And the operator is told where it is now selectable, by the name they pick.
    assert "acme_soap" in app.text("#layout-step")


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


def test_teach_says_what_it_already_knows(gui) -> None:
    """ "Teach it another" needs an "another than WHAT".

    Teach was two forms over empty space: it asked for a layout it did not have
    without ever showing the ones it did, so there was no way to tell whether
    the format in front of you was already covered. The list is static rows —
    no tint, because every row here has the same status and a tint that never
    varies carries nothing (§4.10 rule 2).
    """
    app = gui()
    app.show("teach")

    summary = app.text("#teach-known-summary")
    assert "export format" in summary and "Teach it another" in summary

    rows = app.page.evaluate(
        """() => [...document.querySelectorAll('#teach-known .result')].map(r => ({
             name: r.firstElementChild.textContent.trim(),
             note: r.lastElementChild.textContent.trim(),
             id: r.getAttribute('title'),
             tinted: !!r.dataset.bucket,
           }))"""
    )
    assert len(rows) >= 4, rows
    ids = {row["id"] for row in rows}
    assert "generic_soap" in ids and "pf-tebra" in ids, ids
    for row in rows:
        assert not row["tinted"], f"a list with one status is wearing a tint: {row}"
        assert row["name"] != row["id"], f"a machine id is the visible name: {row}"

    # The two counts in the sentence are the two halves of the list, counted —
    # not a pair typed into the copy that drifts the first time a pack ships.
    layouts = [row for row in rows if row["note"] == "Chart layout"]
    formats = [row for row in rows if row["note"] != "Chart layout"]
    assert layouts and formats, rows
    assert f"reads {len(formats)} export format" in summary, summary
    assert f"out {len(layouts)} way" in summary, summary


# --- required fields, and one analysis per click ------------------------------


def test_a_blank_layout_form_says_what_is_missing(gui) -> None:
    """`pack_init_async("", "", null, false)` used to go straight through, with
    the "Step 1 of 2" line as the only sign anything had happened."""
    app = gui()
    app.show("teach")
    page = app.page

    page.click("#layout-analyze")
    page.wait_for_timeout(200)

    assert not app.called("pack_init_async"), "a blank form reached the controller"
    banner = app.text("#banner")
    assert "sample charts" in banner
    assert "short name" in banner


def test_three_clicks_analyze_once(gui) -> None:
    """The step line is not a live region, so a screen-reader operator got
    nothing at all from a click and would reasonably click again."""
    app = gui()
    app.show("teach")
    page = app.page
    page.fill("#layout-samples", "/synthetic/samples")
    page.fill("#layout-name", "probe_layout")

    for _ in range(3):
        page.click("#layout-analyze", force=True)
    page.wait_for_timeout(300)

    assert len(app.calls("pack_init_async")) == 1, "a second click started another analysis"
