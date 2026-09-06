"""The Charts view: the rebuild surface, end to end.

Loads the app behind the stubbed bridge, checks Charts rendered from ``info()``,
fires the run button, and then plays a real event sequence through
``window.anastEvent`` — the events built with the SAME constructors the
controller emits with — asserting the stages, the counts, the progress frame and
the per-patient roll-up all land where they belong. Including while the operator
is looking at another view.
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright", reason="the GUI lane needs playwright + chromium")

from stub import CANNED_PATIENTS

from anastomosis.gui.consoles.runs import MigrationConsole, PipelineConsole
from anastomosis.gui.events import done_event, error_event, progress_event, stage_event

pytestmark = pytest.mark.gui_e2e

# The flow this view owns, and the one it must ignore — read off the consoles
# themselves so a rename in Python fails here instead of silently unhooking the
# dispatcher.
_FLOW = PipelineConsole._FLOW
_OTHER_FLOW = MigrationConsole._FLOW


def test_charts_populates_its_pickers_and_sections(gui) -> None:
    """info() drives the layout picker, the format picker and the sections."""
    app = gui()
    page = app.page

    assert app.called("info"), "the app never called info()"
    # A person reads the name; the id they would quote to support is the row's
    # caption. Both, because the <select> this replaced had room for only one
    # and the id won.
    layouts = app.choices("#charts-pack")
    assert "Generic SOAP" in layouts, layouts
    assert "generic_soap" not in layouts
    assert "generic_soap" in app.notes("#charts-pack")

    # And the name comes from the registration, not from re-casing the id: no
    # re-casing of "ccda" produces "C-CDA" (#164).
    formats = app.choices("#charts-source")
    assert "C-CDA" in formats, formats
    assert "Practice Fusion / Tebra" in formats, formats
    assert "Ccda" not in formats and "Pf Tebra" not in formats, formats

    assert page.locator("#charts-sections input[data-section]").count() > 0
    app.choose("#charts-pack", "practice_fusion_soap")
    page.wait_for_timeout(80)
    assert page.locator("#charts-sections input[data-section]").count() > 0
    # The out-of-date filing-assistant notice surfaced from pack_freshness(),
    # in plain language — never the terminal command the controller suggests.
    toast = page.locator("#freshness-toast")
    assert "show" in (toast.get_attribute("class") or "")
    assert "filing assistant" in (toast.text_content() or "")
    page.click("#freshness-dismiss")
    assert "show" not in (toast.get_attribute("class") or "")


def test_run_button_dispatches_the_async_call(gui) -> None:
    """Clicking run hands the shared form to run_pipeline_async and locks it."""
    app = gui()
    page = app.page

    page.fill("#charts-export-dir", "/synthetic/export")
    page.fill("#charts-out-dir", "/synthetic/out")
    page.click("label.toggle:has(#charts-qa)")  # by mouse, on the switch's label row
    page.click("#charts-run")
    page.wait_for_timeout(120)

    args = app.last_args("run_pipeline_async")
    # Positional order per GuiController.run_pipeline: export, out, pack, source,
    # sections, include, qa, archive, bundle, ccda, force, pack_dirs, trust_new,
    # manifest.
    assert args[0] == "/synthetic/export"
    assert args[1] == "/synthetic/out"
    assert args[3] is None, "an unset format picker must mean detect, not a name"
    assert isinstance(args[4], dict) and args[4], "the section matrix was not gathered"
    assert args[5] == [], "no selection rule was unticked, so none is being included"
    assert args[6] is False, "the double-check toggle did not reach the run call"
    assert page.locator("#charts-run").is_disabled()
    assert (page.locator("#charts-run").text_content() or "").strip() == "Rebuilding…"


def test_progress_events_paint_the_stages_and_finish_the_run(gui) -> None:
    """A full stage → progress → done sequence renders and terminates cleanly."""
    app = gui()
    page = app.page
    page.fill("#charts-export-dir", "/synthetic/export")
    page.fill("#charts-out-dir", "/synthetic/out")
    page.click("#charts-run")

    app.emit(stage_event(_FLOW, "ingest", "start"))
    assert app.text("#charts-current") == "Reading records…"
    app.emit(progress_event(_FLOW, "ingest", patients=3, encounters=7))
    app.emit(stage_event(_FLOW, "ingest", "done"))
    assert page.locator("#charts-stage-ingest").get_attribute("data-state") == "done"
    # The technical stage id stays available on the tooltip, never as the label.
    assert page.locator("#charts-stage-ingest").get_attribute("title") == "ingest"
    counts = page.locator("#charts-stage-ingest .stage-counts").text_content() or ""
    assert "patients 3" in counts and "encounters 7" in counts

    app.emit(progress_event(_FLOW, "deliver", deliverer="archive", patients=3))
    assert "archive" in (page.locator("#charts-stage-deliver .stage-counts").text_content() or "")

    app.emit(done_event(_FLOW, summary_id="feedfacefeedface", patients=3, rendered=7))
    assert app.text("#charts-current") == "Finished."
    assert not page.locator("#charts-run").is_disabled()
    assert page.locator("#charts-fill").evaluate("node => node.style.width") == "100%"
    # The activity strip carries the counts — and NOT the opaque summary id.
    strip = app.text("#log-strip-msg")
    assert "patients 3" in strip and "summary" not in strip
    # The per-patient roll-up was fetched with THIS run's id and rendered.
    assert app.last_args("last_run_summary") == ["feedfacefeedface"]
    assert not page.locator("#charts-patients").is_hidden()
    body = page.locator("#charts-patients-body").text_content() or ""
    assert str(CANNED_PATIENTS[0]["display_name"]) in body


def test_a_run_keeps_streaming_while_another_view_is_up(gui) -> None:
    """The whole point of the single document: navigation cannot orphan a run."""
    app = gui()
    page = app.page
    page.fill("#charts-export-dir", "/synthetic/export")
    page.click("#charts-run")
    app.show("teach")

    app.emit(stage_event(_FLOW, "reconstruct", "start"))
    app.emit(progress_event(_FLOW, "reconstruct", rendered=5))
    assert "Building charts" in app.text("#log-strip-msg")
    app.emit(done_event(_FLOW, summary_id="feedfacefeedface", patients=2))

    app.show("charts")
    assert app.text("#charts-current") == "Finished."
    assert "rendered 5" in (
        page.locator("#charts-stage-reconstruct .stage-counts").text_content() or ""
    )
    assert not page.locator("#charts-patients").is_hidden()


def test_error_event_banners_the_failure_and_releases_the_button(gui) -> None:
    """An error marks its stage, banners the diagnosis, and ends the run."""
    app = gui()
    page = app.page
    page.click("#charts-run")

    app.emit(error_event(_FLOW, "qa", "QaFailed"))

    assert page.locator("#charts-stage-qa").get_attribute("data-state") == "error"
    banner = page.locator("#banner").text_content() or ""
    assert "QaFailed" in banner and "Double-checking" in banner
    assert app.text("#charts-current") == "Stopped."
    assert not page.locator("#charts-run").is_disabled()


def test_charts_ignores_another_flow_terminal_event(gui) -> None:
    """The per-flow guard: a migration `done` must not end the Charts
    run. The dispatcher — not a page boundary, since both views live in
    one document — routes the event only to the view that registered
    that flow."""
    app = gui()
    page = app.page
    # Filled because a run has to actually start for this test to have a
    # subject.
    page.fill("#charts-export-dir", "/synthetic/export")
    page.fill("#charts-out-dir", "/synthetic/out")
    page.click("#charts-run")
    page.wait_for_timeout(120)

    app.emit(done_event(_OTHER_FLOW, summary_id="deadbeef", patients=9))

    assert page.locator("#charts-run").is_disabled(), "the migration ended the Charts run"
    assert app.text("#charts-current") == "Rebuilding…"
    assert page.locator("#charts-patients").is_hidden()
    assert not (page.locator("#charts-patients-body").text_content() or "").strip()
    # It landed where it belonged instead. (Checked on the attribute: the whole
    # Migrate section is off screen, so Playwright calls all of it invisible.)
    assert app.last_args("last_run_summary") == ["deadbeef"]
    assert page.locator("#migrate-patients").get_attribute("hidden") is None


def test_a_skipped_double_check_is_never_painted_as_done(gui) -> None:
    """A verification that did not run must not close with a tick: when
    PyMuPDF is missing, the pipeline downgrades QA to a no-op, and the
    bridge must not assert a check ran when it never read a chart."""
    app = gui()
    page = app.page
    page.fill("#charts-export-dir", "/synthetic/export")
    page.fill("#charts-out-dir", "/synthetic/out")
    page.click("#charts-run")

    app.emit(stage_event(_FLOW, "qa", "start"))
    app.emit(progress_event(_FLOW, "qa"))
    app.emit(stage_event(_FLOW, "qa", "skipped"))

    card = page.locator("#charts-stage-qa")
    assert card.get_attribute("data-state") == "skipped"
    # The tick is reserved for a stage that ran.
    assert card.get_attribute("data-state") != "done"
    note = card.locator(".stage-counts").text_content() or ""
    assert "cannot double-check" in note

    app.emit(done_event(_FLOW, summary_id="feedfacefeedface", patients=3, rendered=7))
    current = app.text("#charts-current")
    assert "Finished" in current and "did not run" in current


def test_qa_rail_explains_a_nonzero_not_carried_count(gui) -> None:
    """``not_carried`` rides the QA progress event's counts like every
    other QA number; the rail must say what the CLI says, not a bare
    key-dump like "not carried 13" (#297)."""
    app = gui()
    page = app.page
    page.fill("#charts-export-dir", "/synthetic/export")
    page.fill("#charts-out-dir", "/synthetic/out")
    page.click("#charts-run")

    app.emit(stage_event(_FLOW, "qa", "start"))
    app.emit(progress_event(_FLOW, "qa", **{"pass": 2, "warn": 0, "fail": 0, "not_carried": 13}))

    note = page.locator("#charts-stage-qa .stage-counts").text_content() or ""
    assert note == (
        "pass 2 · warn 0 · fail 0 · 13 fact(s) carried by the record summary, not the visit charts"
    )
    # The activity strip reads the same event and must not disagree with the rail.
    assert "13 fact(s) carried by the record summary, not the visit charts" in (
        app.text("#log-strip-msg")
    )


def test_qa_rail_stays_short_when_nothing_was_left_out(gui) -> None:
    """A run that abbreviates nothing gets the counts line it always had."""
    app = gui()
    page = app.page
    page.fill("#charts-export-dir", "/synthetic/export")
    page.fill("#charts-out-dir", "/synthetic/out")
    page.click("#charts-run")

    app.emit(stage_event(_FLOW, "qa", "start"))
    app.emit(progress_event(_FLOW, "qa", **{"pass": 2, "warn": 0, "fail": 0}))

    note = page.locator("#charts-stage-qa .stage-counts").text_content() or ""
    assert note == "pass 2 · warn 0 · fail 0"
    assert "fact(s)" not in note


def test_the_selection_rules_follow_the_chosen_export_format(gui) -> None:
    """Which visits get skipped is the FORMAT's rule, so the matrix is the
    format's — and Detect has no format to ask, which is its own empty state
    rather than an empty box reading as "nothing gets skipped"."""
    app = gui()
    page = app.page

    matrix = page.locator("#charts-selection")
    assert matrix.locator("input[data-section]").count() == 0
    assert "Choose an export format" in (matrix.text_content() or "")

    app.choose("#charts-source", "pf-tebra")
    page.wait_for_timeout(80)
    rules = page.eval_on_selector_all(
        "#charts-selection input[data-section]", "boxes => boxes.map(b => b.dataset.section)"
    )
    assert sorted(rules) == ["empty-soap", "growth-charts"], rules
    # Every rule on, because every rule is on by default — the tick is the rule
    # doing what it has always done.
    assert page.eval_on_selector_all(
        "#charts-selection input[data-section]", "boxes => boxes.every(b => b.checked)"
    )

    # A format that keeps everything says so rather than showing an empty box.
    app.choose("#charts-source", "ccda")
    page.wait_for_timeout(80)
    assert "keeps every visit" in (matrix.text_content() or "")


def test_unticking_a_rule_asks_the_run_to_keep_those_visits(gui) -> None:
    """The one thing this matrix is for: an unticked rule reaches the run as
    the ``--include`` name that switches it off."""
    app = gui()
    page = app.page
    page.fill("#charts-export-dir", "/synthetic/export")
    page.fill("#charts-out-dir", "/synthetic/out")
    app.choose("#charts-source", "pf-tebra")
    page.wait_for_timeout(80)
    page.click("#charts-selection label.toggle:has(input[data-section='growth-charts'])")
    page.click("#charts-run")
    page.wait_for_timeout(120)

    args = app.last_args("run_pipeline_async")
    assert args[3] == "pf-tebra"
    assert args[5] == ["growth-charts"], args[5]


def test_selection_choices_survive_a_format_round_trip(gui) -> None:
    """The same promise the section matrix makes: a rule someone switched off
    must not come back on because they looked at another format."""
    app = gui()
    page = app.page
    app.choose("#charts-source", "pf-tebra")
    page.wait_for_timeout(80)
    page.click("#charts-selection label.toggle:has(input[data-section='growth-charts'])")

    app.choose("#charts-source", "ccda")
    page.wait_for_timeout(80)
    app.choose("#charts-source", "pf-tebra")
    page.wait_for_timeout(80)

    state = page.eval_on_selector_all(
        "#charts-selection input[data-section]",
        "boxes => Object.fromEntries(boxes.map(b => [b.dataset.section, b.checked]))",
    )
    assert state == {"empty-soap": True, "growth-charts": False}, state


def test_section_choices_survive_a_layout_round_trip(gui) -> None:
    """A section the physician turned off must not come back on its own,
    even after switching to another layout and back."""
    app = gui()
    page = app.page
    packs = app.choices("#charts-pack")
    assert len(packs) >= 2, f"need two layouts to switch between, got {packs}"

    first, second = packs[0], packs[1]
    app.choose_label("#charts-pack", first)
    boxes = page.locator("#charts-sections input[data-section]")
    assert boxes.count() >= 1

    # Turn every section off for this layout by clicking the toggle the way a
    # person does — the custom track sits over the checkbox itself.
    labels = page.locator("#charts-sections label.toggle")
    for i in range(boxes.count()):
        if boxes.nth(i).is_checked():
            labels.nth(i).click()
    chosen = {
        b["k"]: b["v"]
        for b in page.evaluate(
            "() => [...document.querySelectorAll('#charts-sections input[data-section]')]"
            ".map(i => ({k: i.dataset.section, v: i.checked}))"
        )
    }
    assert chosen and not any(chosen.values()), chosen

    app.choose_label("#charts-pack", second)
    app.choose_label("#charts-pack", first)

    back = {
        b["k"]: b["v"]
        for b in page.evaluate(
            "() => [...document.querySelectorAll('#charts-sections input[data-section]')]"
            ".map(i => ({k: i.dataset.section, v: i.checked}))"
        )
    }
    assert back == chosen, f"the layout's defaults came back over the choice: {back}"


def test_the_rail_shows_only_the_stages_this_run_will_perform(gui) -> None:
    """A stage that will not run must not sit grey under "Finished.": on
    the shipped defaults no deliverer runs, so the rail must not
    advertise "Saving results" (or "Double-checking" with it off)."""
    app = gui()
    page = app.page
    page.fill("#charts-export-dir", "/synthetic/export")
    page.fill("#charts-out-dir", "/synthetic/out")

    def rail_ids() -> list[str]:
        return page.evaluate(
            "() => [...document.querySelectorAll('#charts-rail .stage')].map(c => c.id)"
        )

    # The rail is painted by the run's first event, not by the click — a submit
    # the controller refuses must leave the last run's results standing.
    # Defaults: the double-check is on, no extra artifact is requested.
    page.click("#charts-run")
    app.emit(stage_event(_FLOW, "ingest", "start"))
    ids = rail_ids()
    assert "charts-stage-qa" in ids, ids
    assert "charts-stage-deliver" not in ids, ids
    assert "charts-stage-ingest" in ids and "charts-stage-reconstruct" in ids, ids

    # Asking for an archive brings the saving stage back.
    app.emit(done_event(_FLOW, summary_id="feedfacefeedface", patients=1, rendered=1))
    archive = page.locator("#charts-archive")
    if archive.count():
        page.evaluate(
            "() => { const b = document.getElementById('charts-archive');"
            " b.checked = true; b.dispatchEvent(new Event('change', {bubbles: true})); }"
        )
        page.click("#charts-run")
        app.emit(stage_event(_FLOW, "ingest", "start"))
        assert "charts-stage-deliver" in rail_ids(), rail_ids()


def test_a_run_that_stopped_does_not_leave_a_full_progress_bar(gui) -> None:
    """The bar measures stages settled out of stages planned, so where a
    stopped run stops IS how far it got — never a full brand-coloured
    bar reading as "going fine"."""
    app = gui()
    page = app.page
    page.fill("#charts-export-dir", "/synthetic/export")
    page.fill("#charts-out-dir", "/synthetic/out")

    def bar() -> tuple[str, str | None, bool]:
        return (
            page.locator("#charts-fill").evaluate("n => n.style.width"),
            page.locator("#charts-progress .progress-bar").get_attribute("aria-valuenow"),
            page.locator("#charts-progress").evaluate("n => n.classList.contains('is-stopped')"),
        )

    page.click("#charts-run")
    app.emit(stage_event(_FLOW, "ingest", "start"))
    assert bar() == ("0%", "0", False)

    # One of the three planned stages settles (the double-check is on by default,
    # no deliverer is, so the plan is ingest + reconstruct + qa).
    app.emit(stage_event(_FLOW, "ingest", "done"))
    width, aria, stopped = bar()
    assert width.startswith("33"), width
    assert aria == "33", aria
    assert not stopped

    app.emit(stage_event(_FLOW, "reconstruct", "start"))
    app.emit(error_event(_FLOW, "reconstruct", "RenderFailed"))
    width, aria, stopped = bar()
    assert width.startswith("33"), f"the failed run filled the bar to {width}"
    assert aria == "33", aria
    assert stopped, "a stopped run kept the running colour"
    assert app.text("#charts-current") == "Stopped."


def test_a_finished_run_survives_a_click_the_controller_refuses(gui) -> None:
    """One busy guard covers every view: a click on a finished view must
    stay live but be rejected without wiping its own rail counts or
    patient table, and the refusal must not read as "Ready."."""
    app = gui()
    page = app.page
    page.fill("#charts-export-dir", "/synthetic/export")
    page.fill("#charts-out-dir", "/synthetic/out")

    page.click("#charts-run")
    app.emit(stage_event(_FLOW, "ingest", "start"))
    app.emit(progress_event(_FLOW, "ingest", patients=3, encounters=7))
    app.emit(stage_event(_FLOW, "ingest", "done"))
    app.emit(done_event(_FLOW, summary_id="feedfacefeedface", patients=3, rendered=7))
    page.wait_for_selector("#charts-patients:not([hidden])")

    def snapshot() -> dict[str, object]:
        return {
            "current": app.text("#charts-current"),
            "rail": page.evaluate(
                "() => [...document.querySelectorAll('#charts-rail .stage')]"
                ".map(c => [c.id, c.dataset.state || '', c.querySelector('.stage-counts')"
                ".textContent])"
            ),
            "patients": page.locator("#charts-patients").get_attribute("hidden"),
            "fill": page.locator("#charts-fill").evaluate("n => n.style.width"),
        }

    before = snapshot()
    page.evaluate(
        "() => { window.pywebview.api.run_pipeline_async ="
        " () => Promise.resolve({ok: false, error: 'Busy'}); }"
    )
    page.click("#charts-run")
    page.wait_for_selector("#banner.show")

    assert snapshot() == before, "a refused click changed the finished run on screen"
    assert not page.locator("#charts-run").is_disabled()
    banner = app.text("#banner")
    assert "Busy" not in banner, f"the sentinel reached the screen: {banner!r}"
    assert "already working on something else" in banner, banner


def test_a_blank_form_says_what_is_missing_instead_of_running(gui) -> None:
    """A blank form must refuse with a reason, not silently call
    `run_pipeline_async("", "", …)`, lock the button, and take the
    process-wide busy guard for nothing."""
    app = gui()
    page = app.page

    page.click("#charts-run")
    page.wait_for_timeout(200)

    assert not app.called("run_pipeline_async"), "a blank form reached the controller"
    banner = app.text("#banner")
    assert "the folder your export is in" in banner
    assert "the folder to put the charts in" in banner
    # And the button is usable again — a dead screen is the worse bug.
    assert not page.locator("#charts-run").is_disabled()


def test_the_banner_names_only_the_blank_half(gui) -> None:
    """Half-filled is the common case, and naming the field already filled in
    sends the operator looking in the wrong place."""
    app = gui()
    page = app.page
    page.fill("#charts-export-dir", "/synthetic/export")

    page.click("#charts-run")
    page.wait_for_timeout(200)

    banner = app.text("#banner")
    assert "the folder to put the charts in" in banner
    assert "the folder your export is in" not in banner, banner


READING = [
    "Across 1 document the source offered 10 sections: 9 became data, 1 kept as text only, "
    "0 not credited as data, 0 empty in the source.",
    "Those sections carried 13 coded entries: 13 became data, 0 kept as text only.",
]


def test_the_source_reading_reaches_the_panel_and_stays_out_of_the_strip(gui) -> None:
    """#315's charts surface, driven for real: the done event's sentences fill
    the reading panel; a done WITHOUT the key leaves it hidden; the next run
    clears it; and the activity strip never stringifies the list into its
    counts line. The reviewer proved the render call and the NON_COUNT_KEYS
    entry were both deletable without failing a test — this is that test."""
    app = gui()
    page = app.page
    box = page.locator("#charts-reading")
    assert box.get_attribute("hidden") is not None

    page.fill("#charts-export-dir", "/synthetic/export")
    page.fill("#charts-out-dir", "/synthetic/out")
    page.click("#charts-run")
    page.wait_for_timeout(120)
    done = done_event(_FLOW, summary_id="feedfacefeedface", patients=1, rendered=3)
    done["source_reading"] = READING
    app.emit(done)

    assert box.is_visible()
    text = box.text_content() or ""
    assert "What the source offered, and what arrived" in text
    assert READING[0] in text and READING[1] in text
    # The strip speaks counts; the sentences must not be flattened into it.
    strip = app.text("#log-strip-msg")
    assert "became data" not in strip
    assert "source reading" not in strip.lower()

    # A run over a source that keeps no ledger says nothing — no empty frame.
    page.click("#charts-run")
    page.wait_for_timeout(120)
    app.emit(stage_event(_FLOW, "ingest", "start"))
    assert not box.is_visible(), "last run's account survived into this run"
    app.emit(done_event(_FLOW, summary_id="feedfacefeedface", patients=1, rendered=3))
    assert not box.is_visible()
