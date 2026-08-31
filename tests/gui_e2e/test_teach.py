"""The Teach view: one workspace, two modes, the same two-step shape.

Teaching a document layout and teaching an export format were two separate
workspaces that mirrored each other function for function. They are now two
modes of one view, and both walk the same gate: look (the controller refuses to
write and hands back something to review), confirm, then write. These tests
walk that gate in each mode — the confirmation must be REQUIRED, and a fresh
look must revoke it.

The format mode's proposal is also the EDIT surface, so the rest of this module
walks the correction arc the way an operator does: observe the wrong proposal,
fix it in place, save, and assert what crossed the bridge. It asserts the WIRE,
not the world — that a corrected mapping actually conserves identity, visits and
columns is proved against the real backend in
``tests/unit/test_source_init_command.py``, and proving it twice against a
stubbed bridge would only prove the stub.
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright", reason="the GUI lane needs playwright + chromium")

from stub import canned_returns

from anastomosis.gui.consoles.packgen import PackgenConsole
from anastomosis.gui.consoles.source import SourceConsole
from anastomosis.gui.events import stage_event

pytestmark = pytest.mark.gui_e2e

#: The controller's OWN sentence for a load refusal. It is a pointer for the
#: page to act on, never the message a physician reads — so it must not appear
#: anywhere in the document.
_LOAD_DETAIL = (
    "learned mapping 'acme_csv': column 'VisitId' -> encounter.date_of_service "
    "could not be built (parse_date)"
)


def _row(column: str) -> str:
    """One row of the match-up, addressed as the page's own anchoring does."""
    return f'.mapping-row[data-source="{column}"]'


def _open(gui, mode: str = "layout"):
    app = gui()
    app.show("teach")
    if mode != "layout":
        app.page.click(f'.mode-tab[data-mode="{mode}"]')
        app.page.wait_for_timeout(120)
    return app


def _look(app):
    """Look at the example, and take the proposal the scorer hands back."""
    app.page.fill("#format-example", "/synthetic/export.csv")
    app.page.fill("#format-name", "acme_csv")
    app.page.click("#format-analyze")
    app.page.wait_for_timeout(150)
    app.emit(stage_event(SourceConsole._FLOW, "source", "done"))
    return app.page


def _stash(app, result: dict[str, object]) -> None:
    """Replace what the next ``last_source_result`` fetch answers with."""
    app.page.evaluate(
        "answer => { window.pywebview.api.last_source_result = () => Promise.resolve(answer); }",
        result,
    )


def _refusal(**fields: object) -> dict[str, object]:
    """A post-analyze refusal: the proposal it still carries, plus the pointer.

    Built from the SAME canned proposal the look step rendered, because that is
    what the controller does — every outcome after a successful analysis rides
    the proposal, so the page always has something to point at.
    """
    payload = dict(canned_returns()["last_source_result"])
    payload.update(fields)
    return payload


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


def test_format_mode_shows_the_match_up_before_saving(gui) -> None:
    """The proposal renders column names and how they are read — never a value."""
    app = _open(gui, "format")
    page = app.page

    page.fill("#format-example", "/synthetic/export.csv")
    page.fill("#format-name", "acme_csv")
    page.click("#format-analyze")
    page.wait_for_timeout(150)

    assert app.last_args("source_init_async") == [
        "/synthetic/export.csv",
        "acme_csv",
        None,
        False,
        None,
        None,
    ]

    app.emit(stage_event(SourceConsole._FLOW, "source", "done"))

    assert app.called("last_source_result")
    assert not page.locator("#format-proposal").is_hidden()
    # What the file IS. How it is grouped is no longer a sentence to skim past:
    # it is three controls, which stay true when the operator changes one.
    grouping = app.text("#format-grouping")
    assert "CSV file" in grouping and "4 columns" in grouping
    assert "=" not in grouping
    assert app.chosen("#format-patient-key") == "MRN"
    assert app.chosen("#format-visit-key") == ""
    assert app.chosen("#format-row-scope") == "encounter"
    # One header row plus one row per column, unmatched columns included.
    rows = page.locator("#format-mapping .mapping-row")
    assert rows.count() == 5
    # A key column is spoken for, not unmatched — the table used to tell a
    # physician the opposite about their own patient identifier.
    assert "Used as: Patient ID" in (rows.nth(1).text_content() or "")
    # An unmatched column is not a dead cell either: it is a chooser resting on
    # the entry that says nothing is dropped.
    assert "Keep as extra data (not mapped)" in (rows.nth(4).text_content() or "")
    assert page.locator("#format-save").is_disabled()

    page.click("label.toggle:has(#format-confirm)")
    assert not page.locator("#format-save").is_disabled()
    page.click("#format-save")
    page.wait_for_timeout(150)

    saved = app.last_args("source_init_async")
    assert saved[3] is True, "the save step must confirm"
    assert saved[5] is None, "an untouched proposal must carry no review"


def test_format_mode_refuses_loudly_when_a_column_would_be_lost(gui) -> None:
    """The losslessness refusal keeps its teeth, in plain language.

    And it points at the GROUPING, because that is what it is about: a column
    loses values when the row grain collapses it, not because the column did
    anything wrong. Marking one of those columns would send the operator to
    change something that is already correct.
    """
    app = _open(gui, "format")
    _look(app)

    _stash(app, _refusal(error="WouldDropColumns", dropped=["clinic_widget_code"]))
    app.emit(stage_event(SourceConsole._FLOW, "source", "done"))

    banner = app.page.locator("#banner").text_content() or ""
    assert "Cannot save yet" in banner
    assert "clinic_widget_code" in banner
    assert "Every column must have a home" in banner
    structure = app.page.locator("#format-structure")
    assert "row-attention" in (structure.get_attribute("class") or "")
    assert "what one row of the file is" in app.text("#format-structure .row-note")
    assert app.page.locator(".mapping-row.row-attention").count() == 0


def test_the_wrong_proposal_is_corrected_in_place_and_the_review_rides_the_save(gui) -> None:
    """Observe the wrong proposal, correct it, save — and assert the wire.

    The scorer has aimed a visit IDENTIFIER at the visit DATE and set it to be
    read as a date, which cannot work. Before this, the operator's only move was
    to press Look again and get the same answer forever. Now the proposal is the
    edit surface, and what the corrections put on the bridge is a COMPLETE
    review: all three grouping answers, and every column's current decision.
    """
    app = _open(gui, "format")
    page = _look(app)

    wrong = page.locator(_row("VisitId")).text_content() or ""
    assert "Date Of Service" in wrong, "the wrong destination must be visible, not implied"
    assert "text · NN-NNN" in wrong, "the column's own evidence is what makes it visibly wrong"

    # VisitId is what identifies a visit ...
    app.choose("#format-visit-key", "VisitId")
    assert "Used as: Visit ID" in (page.locator(_row("VisitId")).text_content() or "")
    # ... the date column is the date, and is read as one ...
    app.choose(f'{_row("VisitDate")} [data-pick="target"]', "encounter.date_of_service")
    app.choose_label(
        f'{_row("VisitDate")} [data-pick="transform"]', "Read as a date (common formats)"
    )
    # ... and the complaint is the chief complaint.
    app.choose(f'{_row("Complaint")} [data-pick="target"]', "encounter.chief_complaint")

    # A corrected row stops claiming the machine's confidence in a match the
    # machine did not make.
    assert "Edited" in (page.locator(_row("VisitDate")).text_content() or "")
    # And nothing moves silently: a column that stops being a field mapping
    # because it became a key says so, in prose, before anybody confirms.
    changed = app.text("#format-changes-list")
    assert "Visits are identified by VisitId now" in changed
    assert (
        "VisitId: was going to Date Of Service, read as a date (common formats) "
        "— now used as the visit ID." in changed
    )

    page.click("label.toggle:has(#format-confirm)")
    page.click("#format-save")
    page.wait_for_timeout(150)

    saved = app.last_args("source_init_async")
    assert saved[3] is True
    assert saved[5] == {
        "patient_key": "MRN",
        "encounter_key": "VisitId",
        "row_scope": "encounter",
        "decisions": {
            "VisitDate": ["encounter.date_of_service", "parse_date"],
            "Complaint": ["encounter.chief_complaint", "strip"],
        },
    }


def test_a_load_refusal_marks_the_row_it_is_about_and_says_why(gui) -> None:
    """The diagnosis stops being a sentence to read and becomes a place to look.

    Every noun in the composed sentence is a column name, a target label, a
    transform label or the profiler's mask — the same tables that filled the
    choosers it points at, so the refusal cannot drift into different words than
    the control that fixes it. The controller's own sentence is a pointer, and
    must not reach the document at all.
    """
    app = _open(gui, "format")
    page = _look(app)

    _stash(
        app,
        _refusal(
            error="MappingLoadFailed",
            detail=_LOAD_DETAIL,
            detail_column="VisitId",
            detail_target="encounter.date_of_service",
            detail_transform="parse_date",
        ),
    )
    app.emit(stage_event(SourceConsole._FLOW, "source", "done"))

    marked = page.locator(".mapping-row.row-attention")
    assert marked.count() == 1
    assert marked.get_attribute("data-source") == "VisitId"
    assert app.text(f"{_row('VisitId')} .row-note") == (
        "This column could not be read the way it is set. "
        "VisitId looks like text shaped NN-NNN. "
        "It is set to read as a date (common formats), going to Date Of Service. "
        "Pick a different way to read it, or send it to a different field."
    )
    assert "the marked row says which column" in app.text("#banner")
    # The one string on the wire that could have carried anything but a name.
    assert _LOAD_DETAIL not in (page.locator("body").text_content() or "")
    assert "learned mapping" not in (page.locator("body").text_content() or "")


def test_any_correction_revokes_the_confirmation(gui) -> None:
    """Consent is per-analysis AND per-edit.

    Otherwise an operator ticks the box, keeps adjusting rows, and saves a
    match-up that particular click never looked at.
    """
    app = _open(gui, "format")
    page = _look(app)
    page.click("label.toggle:has(#format-confirm)")
    assert not page.locator("#format-save").is_disabled()

    app.choose("#format-row-scope", "patient")

    assert not page.locator("#format-confirm").is_checked()
    assert page.locator("#format-save").is_disabled()
    # And the change is stated in the physician's vocabulary, not as a diff.
    assert not page.locator("#format-changes").is_hidden()
    assert app.text("#format-changes-list") == (
        "Rows are read as one patient per row now, not one visit per row."
    )


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
