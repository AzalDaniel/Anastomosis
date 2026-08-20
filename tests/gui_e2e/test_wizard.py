"""The migration wizard (wizard.html): source → destination → run.

Walks the three steps against the stubbed bridge — auto-detect, the transit map
drawn from the REAL ``destination_status`` payload, then the run — and plays the
migration event sequence through, including the terminal notice the controller
puts on its ``done`` event and the per-flow guard that keeps a dashboard
pipeline run from finishing the wizard's.
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright", reason="the GUI lane needs playwright + chromium")

from stub import CANNED_DESTINATION, CANNED_PATIENTS, CANNED_SOURCE, canned_returns

from anastomosis.gui.consoles.runs import MigrationConsole, PipelineConsole
from anastomosis.gui.events import done_event, error_event, stage_event

pytestmark = pytest.mark.gui_e2e

_FLOW = MigrationConsole._FLOW
_OTHER_FLOW = PipelineConsole._FLOW

# The route-card labels wizard.js paints for each router route kind.
_ROUTE_LABELS = {
    "vendor_api": "Vendor API",
    "ccda_import": "C-CDA import",
    "browser": "Browser automation",
}


def _transit() -> dict:
    status = canned_returns()["destination_status"]
    assert isinstance(status, dict)
    transit = status["transit"]
    assert isinstance(transit, dict)
    return transit


def test_wizard_populates_its_pickers(gui) -> None:
    """info() + routes() fill the source, render-mode, and destination pickers."""
    wizard = gui("wizard.html")
    page = wizard.page

    assert wizard.called("info") and wizard.called("routes")
    sources = page.locator("#source option").all_text_contents()
    assert sources[0] == "Select a source…", "a migration's source is never guessed"
    assert CANNED_SOURCE in sources
    renders = page.locator("#render option").all_text_contents()
    assert renders[0].startswith("neutral") and renders[1].startswith("ccda-standard")
    destinations = page.locator("#destination option").all_text_contents()
    assert destinations[0] == "Select a destination…"
    assert CANNED_DESTINATION in destinations
    # The neutral render mode opens on the generic_soap pack's sections.
    assert page.locator("#section-matrix input[data-section]").count() > 0
    # ...and the standard C-CDA view honestly exposes none.
    page.select_option("#render", "ccda-standard")
    page.wait_for_timeout(50)
    assert page.locator("#section-matrix input[data-section]").count() == 0
    assert "no togglable sections" in (page.locator("#section-matrix").text_content() or "")


def test_detect_preselects_the_source(gui) -> None:
    """Step 1's auto-detect reports the format AND arms the FROM picker."""
    wizard = gui("wizard.html")
    page = wizard.page

    page.fill("#export-dir", "/synthetic/export")
    page.click("#detect-btn")
    page.wait_for_timeout(100)

    assert wizard.last_args("detect") == ["/synthetic/export"]
    assert CANNED_SOURCE in (page.locator("#detect-result").text_content() or "")
    assert page.locator("#source").input_value() == CANNED_SOURCE


def test_destination_choice_draws_the_transit_map(gui) -> None:
    """Every route option becomes a card, and the chosen one is marked chosen."""
    wizard = gui("wizard.html")
    page = wizard.page
    transit = _transit()

    page.select_option("#destination", CANNED_DESTINATION)
    page.wait_for_timeout(150)

    cards = page.locator("#transit-map .route-card")
    assert cards.count() == len(transit["options"]), "a route option was dropped from the map"
    chosen = page.locator('#transit-map .route-card[data-chosen="true"] .kind').all_text_contents()
    expected_chosen = transit["chosen"]
    assert chosen == ([_ROUTE_LABELS[expected_chosen]] if expected_chosen else [])
    # The step rail advanced, and the status bar names the resolved route.
    assert page.locator("#step-3").get_attribute("data-active") == "true"
    assert str(expected_chosen) in (page.locator("#status-text").text_content() or "")
    # This destination ships a browser pack, so the readiness chip is live.
    assert not page.locator("#pack-chip").is_hidden()
    assert CANNED_DESTINATION in (page.locator("#pack-chip").text_content() or "")


def test_run_migration_dispatches_and_completes(gui) -> None:
    """The run call carries the wizard's choices; the event sequence closes it."""
    wizard = gui("wizard.html")
    page = wizard.page
    page.fill("#export-dir", "/synthetic/export")
    page.fill("#out-dir", "/synthetic/out")
    page.select_option("#source", CANNED_SOURCE)
    page.select_option("#destination", CANNED_DESTINATION)
    page.wait_for_timeout(100)
    page.click("#run-migration-btn")
    page.wait_for_timeout(100)

    args = wizard.last_args("run_migration_async")
    # Positional order per GuiController.run_migration: export, out, source,
    # destination, render, sections, qa, force, pack_dirs, trust_new.
    assert args[:5] == [
        "/synthetic/export",
        "/synthetic/out",
        CANNED_SOURCE,
        CANNED_DESTINATION,
        "neutral",
    ]
    assert isinstance(args[5], dict) and args[5]
    assert page.locator("#run-migration-btn").is_disabled()

    wizard.emit(stage_event(_FLOW, "ingest", "start"))
    assert "ingest" in (page.locator("#status-text").text_content() or "")

    # The controller's `done` carries the PREPARED notice; the wizard must show
    # that verdict rather than claiming a delivery it never executed.
    notice = "migration prepared — charts + C-CDA written; delivery not executed."
    done = done_event(_FLOW, summary_id="feedfacefeedface", ccda_patients=2)
    done["notice"] = notice
    done["outcome"] = "prepared"
    wizard.emit(done)

    assert not page.locator("#run-migration-btn").is_disabled()
    assert (page.locator("#migration-result").text_content() or "") == notice
    assert wizard.last_args("last_run_summary") == ["feedfacefeedface"]
    assert str(CANNED_PATIENTS[0]["display_name"]) in (
        page.locator("#patients-body").text_content() or ""
    )


def test_wizard_ignores_another_flow_terminal_event(gui) -> None:
    """The per-flow guard: a pipeline `done` must not announce a migration."""
    wizard = gui("wizard.html")
    page = wizard.page
    page.select_option("#source", CANNED_SOURCE)
    page.select_option("#destination", CANNED_DESTINATION)
    page.wait_for_timeout(100)
    page.click("#run-migration-btn")
    page.wait_for_timeout(100)

    wizard.emit(done_event(_OTHER_FLOW, summary_id="deadbeef", patients=9))

    assert page.locator("#run-migration-btn").is_disabled(), "a pipeline run ended the migration"
    assert (page.locator("#migration-result").text_content() or "") == "running…"
    assert not wizard.called("last_run_summary")


def test_migration_error_banners_and_releases(gui) -> None:
    """A manual-import / failure event ends the run loudly, not silently."""
    wizard = gui("wizard.html")
    page = wizard.page
    page.select_option("#source", CANNED_SOURCE)
    page.select_option("#destination", CANNED_DESTINATION)
    page.wait_for_timeout(100)
    page.click("#run-migration-btn")

    wizard.emit(error_event(_FLOW, "deliver", "no viable automated route — import by hand"))

    assert "import by hand" in (page.locator("#banner").text_content() or "")
    assert not page.locator("#run-migration-btn").is_disabled()
