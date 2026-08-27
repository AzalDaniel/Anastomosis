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
    layouts = page.locator("#charts-pack option").all_text_contents()
    assert "generic_soap" in layouts
    assert page.locator("#charts-sections input[data-section]").count() > 0
    page.select_option("#charts-pack", "practice_fusion_soap")
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
    page.click('[data-view="charts"] .segment-option[data-value="off"]')  # by mouse
    page.click("#charts-run")
    page.wait_for_timeout(120)

    args = app.last_args("run_pipeline_async")
    # Positional order per GuiController.run_pipeline: export, out, pack, source,
    # sections, qa, archive, bundle, ccda, force, pack_dirs, trust_new, manifest.
    assert args[0] == "/synthetic/export"
    assert args[1] == "/synthetic/out"
    assert args[3] is None, "an unset format picker must mean detect, not a name"
    assert isinstance(args[4], dict) and args[4], "the section matrix was not gathered"
    assert args[5] is False, "the double-check toggle did not reach the run call"
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
    """The per-flow guard: a migration `done` must not end the Charts run.

    Both views now live in one document, so the dispatcher — not a page
    boundary — is what keeps them apart: the event goes to the ONE view that
    registered that flow, and the other one carries on untouched.
    """
    app = gui()
    page = app.page
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
    """A verification that did not run must not close with a tick.

    The pipeline downgrades QA to a no-op when PyMuPDF is missing. The bridge
    used to close every stage as "done" regardless, so a physician who switched
    the double-check ON saw a green tick over a check that never read a single
    chart, and a plain "Finished." — the app asserting something untrue about
    the safety control they had asked for.
    """
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


def test_section_choices_survive_a_layout_round_trip(gui) -> None:
    """A section the physician turned off must not come back on its own.

    The section matrix was rebuilt from each layout's defaults on every change
    of the layout picker, with no memory of what had been chosen. Turning a
    section off, looking at another layout and coming back silently reinstated
    it — and the reinstated value was what the run was built from, so a chart
    could carry a section that had been deliberately excluded.
    """
    app = gui()
    page = app.page
    packs = page.locator("#charts-pack option").all_text_contents()
    assert len(packs) >= 2, f"need two layouts to switch between, got {packs}"

    first, second = packs[0], packs[1]
    page.select_option("#charts-pack", label=first)
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

    page.select_option("#charts-pack", label=second)
    page.select_option("#charts-pack", label=first)

    back = {
        b["k"]: b["v"]
        for b in page.evaluate(
            "() => [...document.querySelectorAll('#charts-sections input[data-section]')]"
            ".map(i => ({k: i.dataset.section, v: i.checked}))"
        )
    }
    assert back == chosen, f"the layout's defaults came back over the choice: {back}"


def test_the_rail_shows_only_the_stages_this_run_will_perform(gui) -> None:
    """A stage that will not run must not sit grey under "Finished.".

    On the shipped defaults no deliverer is on, so the pipeline emits no
    `deliver` events at all — yet the rail advertised "Saving results" and left
    it grey forever while the charts had in fact been written. With the
    double-check off, "Double-checking" joined it. Two permanent grey ticks
    under a finished run read as "these did not happen".
    """
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
    """A failure used to finish the bar, in the colour that means "going fine".

    Three signals describe a run: the headline, the rail and the bar. The bar was
    driven from a single `finishRun()` called on BOTH the finish and the failure
    branch, so a run that died on stage two showed "Stopped." and a red cross on
    the rail above a full brand-coloured bar. Now the bar measures stages settled
    out of stages planned, so where it stops IS how far the run got.
    """
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
    """One busy guard covers every view, so this click is live but rejected.

    It used to wipe the rail counts and the whole patient table before asking,
    restore nothing, and report the refusal as the bare word `Busy` under a
    status line reading "Ready." — while the work it was refused for was still
    running somewhere else.
    """
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
