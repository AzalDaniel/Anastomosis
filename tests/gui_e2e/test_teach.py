"""The Teach view: one workspace, two modes (layout, format), the same gate —
look (refuses to write, hands back a review), confirm, then write — required
in each mode, and a fresh look must revoke it.

The format mode's proposal is also the edit surface: observe the wrong
proposal, correct it in place, save, and assert what crossed the bridge —
the WIRE, not the world. That a corrected mapping conserves identity, visits
and columns is proved once, against the real backend, in
``tests/unit/test_source_init_command.py``.
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

#: And its sentence for a spec a review made invalid — a pydantic validator's,
#: field paths and quoted ids. Same rule: a pointer, never the message.
_BUILD_DETAIL = (
    "review for mapping 'acme_csv' is not a valid spec (1 error(s)): spec: Value error, "
    "columns 'VisitId' and 'VisitDate' both target 'encounter.date_of_service'; each "
    "canonical field may be mapped at most once"
)


def _row(column: str) -> str:
    """One row of the match-up, addressed as the page's own anchoring does."""
    return f'.mapping-row[data-source="{column}"]'


def _open(gui, mode: str = "layout", **opened):
    app = gui(**opened)
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
    Built from the same canned proposal the look step rendered, since every
    outcome after a successful analysis rides that proposal."""
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
    # Warns about the labels rather than vouching for them (rule 5, #200).
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
    """A written layout is offered immediately, not after a restart: the run
    forms populate from one ``info()`` call at boot, so writing one mid-session
    must ask again or it stays unselectable in both choosers."""
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
    # Grouping is three controls that stay true as the operator changes one,
    # not a sentence to skim past.
    grouping = app.text("#format-grouping")
    assert "CSV file" in grouping and "4 columns" in grouping
    assert "=" not in grouping
    assert app.chosen("#format-patient-key") == "MRN"
    assert app.chosen("#format-visit-key") == ""
    assert app.chosen("#format-row-scope") == "encounter"
    # One header row plus one row per column, unmatched columns included.
    rows = page.locator("#format-mapping .mapping-row")
    assert rows.count() == 5
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
    """The losslessness refusal points at the GROUPING, not the column: a
    column loses values because the row grain collapses it, not because the
    column is wrong, and marking the column would send the operator to fix
    something that already is correct."""
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
    """The scorer aims a visit IDENTIFIER at the visit DATE, unreadable as
    one. Correcting it in place and saving puts a COMPLETE review on the
    bridge: all three grouping answers, and every column's current
    decision."""
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
    """Every noun in the composed sentence is a column name, target label,
    transform label or profiler mask — the same tables that filled the
    choosers, so the refusal cannot drift into different words than the fix.
    The controller's own sentence is a pointer only; it must never reach
    the document."""
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


def test_a_wording_on_a_column_nothing_reads_does_not_block_the_save(gui) -> None:
    """The empty-wording guard only fires for a transform that reaches the
    loader: `const:` with no wording cannot parse. A column kept as extra
    data sends no transform at all, so there is nothing to parse and
    nothing to stop."""
    app = _open(gui, "format")
    page = _look(app)
    # Complaint stays "Keep as extra data (not mapped)"; only the way-of-reading
    # is touched, which for an unmapped column never crosses the bridge.
    app.choose_label(
        f'{_row("Complaint")} [data-pick="transform"]', "Always write the same wording"
    )
    page.click("label.toggle:has(#format-confirm)")
    page.click("#format-save")
    page.wait_for_timeout(150)

    saved = app.last_args("source_init_async")
    assert saved[3] is True
    assert saved[5] is None, "a change the review cannot carry is not a change"
    assert app.text("#banner") == ""

    # The same blank wording on a column that IS being sent still stops it.
    app.choose(f'{_row("Complaint")} [data-pick="target"]', "encounter.chief_complaint")
    page.click("label.toggle:has(#format-confirm)")
    page.click("#format-save")
    page.wait_for_timeout(150)

    assert app.text("#banner") == "Fill in the wording to use."
    assert len(app.calls("source_init_async")) == 2, "a const with no wording reached the wire"


def test_a_grouping_refusal_opens_the_grouping_controls_not_a_row(gui) -> None:
    """A key that does not identify what it claims to is not a column's
    fault: the wire flags load refusals about keys or row grain as
    `grouping`, and they open the controls actually at fault, never a
    column row."""
    app = _open(gui, "format")
    page = _look(app)

    _stash(
        app,
        _refusal(
            error="MappingLoadFailed",
            detail=_LOAD_DETAIL,
            detail_column="MRN",
            detail_target=None,
            detail_transform=None,
            detail_scope="grouping",
        ),
    )
    app.emit(stage_event(SourceConsole._FLOW, "source", "done"))

    assert page.locator(".mapping-row.row-attention").count() == 0, "a column took the blame"
    assert "row-attention" in (page.locator("#format-structure").get_attribute("class") or "")
    assert app.text("#format-structure .row-note") == (
        "The rows could not be grouped into patients. MRN is what identifies a patient "
        "here, and on at least one row it is blank, or it repeats in a way this grouping "
        "does not allow. Change which column identifies a patient, or what one row of "
        "the file is."
    )
    assert "the marked controls say why" in app.text("#banner")
    # Guards against re-adding the removed advice (#335).
    assert "look at the example again" not in (page.locator("body").text_content() or "")
    assert _LOAD_DETAIL not in (page.locator("body").text_content() or "")


def test_a_build_refusal_is_composed_here_too(gui) -> None:
    """The build refusal follows the load refusal's rule: the controller's
    sentence for an invalid spec is a validator's, naming pydantic field
    paths and quoted ids, and must never reach the banner. The page composes
    its own words from the collision visible on screen."""
    app = _open(gui, "format")
    page = _look(app)
    # Aim a second column at the field VisitId is already going to.
    app.choose(f'{_row("VisitDate")} [data-pick="target"]', "encounter.date_of_service")

    _stash(app, _refusal(error="CannotBuildMapping", detail=_BUILD_DETAIL))
    app.emit(stage_event(SourceConsole._FLOW, "source", "done"))

    marked = [
        n.get_attribute("data-source") for n in page.locator(".mapping-row.row-attention").all()
    ]
    assert marked == ["VisitId", "VisitDate"]
    assert app.text("#banner") == (
        "Two columns cannot go to the same field. VisitId and VisitDate are both going "
        "to Date Of Service. Send one of them somewhere else, or keep it as extra data."
    )
    assert _BUILD_DETAIL not in (page.locator("body").text_content() or "")
    assert "not a valid spec" not in (page.locator("body").text_content() or "")


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


def _arm(page) -> None:
    """Tick the confirmation, and prove the gate really is open before an edit."""
    page.click("label.toggle:has(#format-confirm)")
    assert page.locator("#format-confirm").is_checked()
    assert not page.locator("#format-save").is_disabled()


def _revoked(page, what: str) -> None:
    assert not page.locator("#format-confirm").is_checked(), f"{what} left the box ticked"
    assert page.locator("#format-save").is_disabled(), f"{what} left Save armed"


def test_every_editable_control_revokes_the_confirmation(gui) -> None:
    """Every control on this panel can change what Save sends — including
    the two nested choosers a parametric transform reveals and the
    free-text field — so each is driven here, arming the gate before and
    checking it shut after. Row controls run before grouping ones because
    a key change rebuilds the table underneath."""
    app = _open(gui, "format")
    page = _look(app)
    complaint = _row("Complaint")

    _arm(page)
    app.choose(f'{complaint} [data-pick="target"]', "encounter.chief_complaint")
    _revoked(page, "the destination chooser")

    _arm(page)
    app.choose_label(f'{complaint} [data-pick="transform"]', "Read as a date in one set pattern")
    _revoked(page, "the way-of-reading chooser")

    _arm(page)
    app.choose_label(f'{complaint} [data-pick="pattern"]', "22/07/2024")
    _revoked(page, "the date-pattern chooser")

    _arm(page)
    app.choose_label(
        f'{complaint} [data-pick="transform"]', "Take one piece of a value that has separators"
    )
    _revoked(page, "the way-of-reading chooser")

    _arm(page)
    app.choose_label(f'{complaint} [data-pick="delimiter"]', "Vertical bar")
    _revoked(page, "the separator chooser")

    _arm(page)
    app.choose_label(f'{complaint} [data-pick="position"]', "Last piece")
    _revoked(page, "the which-piece chooser")

    _arm(page)
    app.choose_label(f'{complaint} [data-pick="transform"]', "Always write the same wording")
    _revoked(page, "the way-of-reading chooser")

    _arm(page)
    page.fill(f'{complaint} [data-pick="literal"]', "Routine visit")
    page.wait_for_timeout(80)
    _revoked(page, "the wording field")

    _arm(page)
    app.choose("#format-row-scope", "patient")
    _revoked(page, "the row-grain chooser")

    _arm(page)
    app.choose("#format-visit-key", "VisitId")
    _revoked(page, "the visit-column chooser")

    _arm(page)
    app.choose("#format-patient-key", "VisitDate")
    _revoked(page, "the patient-column chooser")


def test_renaming_the_format_revokes_the_confirmation_and_keeps_the_work(gui) -> None:
    """The name is what the mapping will be CALLED, not what is in the file.

    So a rename revokes consent — it is an edit, and the tick must be re-earned
    — but the corrections are about the file's columns and still stand.
    """
    app = _open(gui, "format")
    page = _look(app)
    app.choose(f'{_row("Complaint")} [data-pick="target"]', "encounter.chief_complaint")
    _arm(page)

    page.fill("#format-name", "other_csv")
    page.wait_for_timeout(80)

    _revoked(page, "renaming the format")
    assert not page.locator("#format-proposal").is_hidden(), "the proposal is still about this file"
    assert app.chosen(f'{_row("Complaint")} [data-pick="target"]') == "encounter.chief_complaint"


def test_repointing_at_another_file_takes_the_proposal_with_it(gui) -> None:
    """A review of file A must never ride a confirmed save of file B:
    revoking consent alone is not enough, since the box can simply be
    ticked again while the panel still shows file A's columns and
    corrections. The proposal goes with the file it describes; the only
    way forward is to look again."""
    app = _open(gui, "format")
    page = _look(app)
    app.choose(f'{_row("Complaint")} [data-pick="target"]', "encounter.chief_complaint")
    _arm(page)

    page.fill("#format-example", "/synthetic/OTHER-hospital.csv")
    page.wait_for_timeout(80)

    assert page.locator("#format-proposal").is_hidden(), "file A's match-up outlived file A"
    assert page.locator("#format-mapping .mapping-row").count() == 0
    assert "Step 1 of 2" in app.text("#format-step")
    # Nothing left to confirm, so nothing can be saved without looking again.
    # The button is unreachable by pointer now, so the event is dispatched at it
    # directly: what is under test is the handler's own refusal, not the CSS.
    assert not page.locator("#format-confirm").is_checked()
    assert page.locator("#format-save").is_disabled()
    page.locator("#format-save").dispatch_event("click")
    page.wait_for_timeout(150)
    assert len(app.calls("source_init_async")) == 1, "a save escaped a discarded proposal"


def test_teach_says_what_it_already_knows(gui) -> None:
    """The list shows what Teach already knows, so "another" has a "than
    what": static rows, no tint, because every row shares one status and a
    tint that never varies carries nothing (§4.10 rule 2)."""
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
    """A blank form's click must never reach the controller, and the banner
    must say what is missing."""
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
