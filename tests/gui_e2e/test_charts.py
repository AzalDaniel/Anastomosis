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
