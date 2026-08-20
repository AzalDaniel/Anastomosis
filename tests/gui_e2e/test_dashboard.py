"""The dashboard (index.html): the pipeline run surface, end to end.

Loads the page behind the stubbed bridge, checks it rendered from ``info()``,
fires the run button, and then plays a real pipeline event sequence through
``window.anastEvent`` — the events built with the SAME constructors the
controller emits with — asserting the rail, the counters, the progress frame,
the activity log, and the per-patient roll-up all land where they belong.
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright", reason="the GUI lane needs playwright + chromium")

from expectations import check_dashboard
from stub import CANNED_PATIENTS

from anastomosis.gui.consoles.runs import MigrationConsole, PipelineConsole
from anastomosis.gui.events import (
    done_event,
    error_event,
    progress_event,
    stage_event,
)

pytestmark = pytest.mark.gui_e2e

# The flow this page owns, and the one it must ignore — read off the consoles
# themselves so a rename in Python fails here instead of silently unhooking the
# page's event dispatcher.
_FLOW = PipelineConsole._FLOW
_OTHER_FLOW = MigrationConsole._FLOW


def test_dashboard_renders_from_the_controller(gui) -> None:
    """The shared lane-1/lane-2 expectation set — the live dashboard, whole."""
    dashboard = gui("index.html")

    assert check_dashboard(dashboard.page) == []


def test_dashboard_populates_pickers_and_the_section_matrix(gui) -> None:
    """info() drives the pack picker, the source picker, and the section toggles."""
    dashboard = gui("index.html")
    page = dashboard.page

    assert dashboard.called("info"), "the dashboard never called info()"
    # Every available pack is offered, and choosing one repaints its sections.
    packs = page.locator("#pack option").all_text_contents()
    assert "generic_soap" in packs
    assert page.locator("#section-matrix input[data-section]").count() > 0
    page.select_option("#pack", "practice_fusion_soap")
    page.wait_for_timeout(50)
    assert page.locator("#section-matrix input[data-section]").count() > 0
    # The stale-pack toast surfaced from pack_freshness().
    assert "show" in (page.locator("#freshness-toast").get_attribute("class") or "")
    page.click("#freshness-dismiss")
    assert "show" not in (page.locator("#freshness-toast").get_attribute("class") or "")


def test_run_button_dispatches_the_async_pipeline_call(gui) -> None:
    """Clicking run hands the form to run_pipeline_async and locks the button."""
    dashboard = gui("index.html")
    page = dashboard.page

    page.fill("#export-dir", "/synthetic/export")
    page.fill("#out-dir", "/synthetic/out")
    page.click('.segment-option[data-value="off"]')  # QA off, by mouse
    page.click("#run-btn")
    page.wait_for_timeout(100)

    args = dashboard.last_args("run_pipeline_async")
    # Positional order per GuiController.run_pipeline: export, out, pack, source,
    # sections, qa, archive, bundle, ccda, force, pack_dirs, trust_new, manifest.
    assert args[0] == "/synthetic/export"
    assert args[1] == "/synthetic/out"
    assert args[3] is None, "an unset source picker must mean auto-detect, not a name"
    assert isinstance(args[4], dict) and args[4], "the section matrix was not gathered"
    assert args[5] is False, "the QA segment toggle did not reach the run call"
    assert page.locator("#run-btn").is_disabled()


def test_progress_events_paint_the_rail_and_finish_the_run(gui) -> None:
    """A full stage → progress → done sequence renders and terminates cleanly."""
    dashboard = gui("index.html")
    page = dashboard.page
    page.fill("#export-dir", "/synthetic/export")
    page.fill("#out-dir", "/synthetic/out")
    page.click("#run-btn")

    dashboard.emit(stage_event(_FLOW, "ingest", "start"))
    assert page.locator("#progress-current").text_content() == "ingest"
    dashboard.emit(progress_event(_FLOW, "ingest", patients=3, encounters=7))
    dashboard.emit(stage_event(_FLOW, "ingest", "done"))
    assert page.locator("#stage-ingest").get_attribute("data-state") == "done"
    assert "patients=3" in (page.locator("#stage-ingest .counters").text_content() or "")

    dashboard.emit(progress_event(_FLOW, "deliver", deliverer="archive", patients=3))
    assert "deliverer=archive" in (page.locator("#stage-deliver .counters").text_content() or "")

    dashboard.emit(done_event(_FLOW, summary_id="feedfacefeedface", patients=3, rendered=7))
    assert page.locator("#progress-current").text_content() == "— complete —"
    assert not page.locator("#run-btn").is_disabled()
    assert page.locator("#progress-bar-fill").evaluate("node => node.style.width") == "100%"
    # The activity strip carries the counts — and NOT the opaque summary id.
    strip = page.locator("#log-strip-msg").text_content() or ""
    assert "patients=3" in strip and "summary_id" not in strip
    # The per-patient roll-up was fetched with THIS run's id and rendered.
    assert dashboard.last_args("last_run_summary") == ["feedfacefeedface"]
    assert not page.locator("#patients-panel").is_hidden()
    body = page.locator("#patients-body").text_content() or ""
    assert str(CANNED_PATIENTS[0]["display_name"]) in body


def test_error_event_banners_the_failure_and_releases_the_button(gui) -> None:
    """An error marks its stage, banners the diagnosis, and ends the run."""
    dashboard = gui("index.html")
    page = dashboard.page
    page.click("#run-btn")

    dashboard.emit(error_event(_FLOW, "qa", "QA failed"))

    assert page.locator("#stage-qa").get_attribute("data-state") == "error"
    assert "QA failed" in (page.locator("#banner").text_content() or "")
    assert page.locator("#progress-current").text_content() == "— failed —"
    assert not page.locator("#run-btn").is_disabled()


def test_dashboard_ignores_another_flow_terminal_event(gui) -> None:
    """The per-flow guard: a migration `done` must not end the dashboard's run."""
    dashboard = gui("index.html")
    page = dashboard.page
    page.click("#run-btn")
    page.wait_for_timeout(100)

    dashboard.emit(done_event(_OTHER_FLOW, summary_id="deadbeef", patients=9))

    assert page.locator("#run-btn").is_disabled(), "the wizard's run ended the dashboard's"
    assert page.locator("#patients-panel").is_hidden()
    assert not dashboard.called("last_run_summary")
