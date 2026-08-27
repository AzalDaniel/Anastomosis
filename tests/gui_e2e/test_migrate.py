"""The Migrate view: destination → routes → run → hand off to Uploads.

Walks the flow against the stubbed bridge — the routes drawn from the REAL
``destination_status`` payload, the detect that arms the format picker, then the
run — and plays the migration event sequence through, including the per-flow
guard that keeps a Charts run from finishing a migration and the handoff that
carries the chosen route's context into Uploads.
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

# The plain-language name the view paints for each router route kind.
_ROUTE_NAMES = {
    "vendor_api": "Direct connection",
    "ccda_import": "Transfer document (C-CDA)",
    "browser": "Filing assistant",
}


def _transit() -> dict:
    status = canned_returns()["destination_status"]
    assert isinstance(status, dict)
    transit = status["transit"]
    assert isinstance(transit, dict)
    return transit


def _open(gui):
    app = gui()
    app.show("migrate")
    return app


def test_migrate_populates_its_pickers(gui) -> None:
    """info() + routes() fill the format, chart-pages and destination pickers."""
    app = _open(gui)
    page = app.page

    assert app.called("info") and app.called("routes")
    formats = page.locator("#migrate-source option").all_text_contents()
    assert formats[0] == "Choose the export format…", "a migration's format is never guessed"
    assert CANNED_SOURCE in formats
    pages = page.locator("#migrate-render option").all_text_contents()
    assert pages[0].startswith("Rendered pages") and pages[1].startswith("Data only")
    destinations = page.locator("#migrate-destination option").all_text_contents()
    assert destinations[0] == "Choose a destination…"
    assert CANNED_DESTINATION in destinations
    # Rendered pages open on the standard layout's sections...
    assert page.locator("#migrate-sections input[data-section]").count() > 0
    # ...and the data-only view honestly exposes none.
    page.select_option("#migrate-render", "ccda-standard")
    page.wait_for_timeout(80)
    assert page.locator("#migrate-sections input[data-section]").count() == 0
    assert "no sections" in (page.locator("#migrate-sections").text_content() or "")


def test_detect_arms_the_format_picker(gui) -> None:
    """Detect reports the format into the activity strip AND fills the picker."""
    app = _open(gui)
    page = app.page

    page.fill("#migrate-export-dir", "/synthetic/export")
    page.click("#migrate-detect")
    page.wait_for_timeout(150)

    assert app.last_args("detect") == ["/synthetic/export"]
    assert page.locator("#migrate-source").input_value() == CANNED_SOURCE
    assert CANNED_SOURCE in app.text("#log-strip-msg")


def test_destination_choice_lists_the_routes_in_plain_language(gui) -> None:
    """Every route option becomes a card; the chosen one is marked recommended."""
    app = _open(gui)
    page = app.page
    transit = _transit()

    page.select_option("#migrate-destination", CANNED_DESTINATION)
    page.wait_for_timeout(200)

    cards = page.locator("#migrate-routes .route-card")
    assert cards.count() == len(transit["options"]), "a route option was dropped"
    chosen = page.locator(
        '#migrate-routes .route-card[data-chosen="true"] .route-name'
    ).all_text_contents()
    expected = transit["chosen"]
    assert chosen == ([_ROUTE_NAMES[expected]] if expected else [])
    assert "Available — recommended" in (page.locator("#migrate-routes").text_content() or "")
    # The registry's own evidence is kept, one disclosure down — nothing dropped.
    detail = page.locator("#migrate-routes .route-detail").first.text_content() or ""
    assert detail.strip(), "the route evidence was dropped instead of tucked away"
    # This destination ships a filing assistant, so the handoff is offered.
    assert not page.locator("#migrate-handoff-actions").is_hidden()
    guidance = page.locator("#migrate-guidance").text_content() or ""
    assert "filing assistant" in guidance.lower()


def test_continue_on_uploads_carries_the_context(gui) -> None:
    """The handoff switches view AND pre-fills what Uploads would ask for."""
    app = _open(gui)
    page = app.page
    page.fill("#migrate-out-dir", "/synthetic/out")
    page.select_option("#migrate-destination", CANNED_DESTINATION)
    page.wait_for_timeout(200)

    page.click("#migrate-continue")
    page.wait_for_timeout(400)

    assert app.visible("uploads") and not app.visible("migrate")
    assert page.locator("#uploads-assistant").input_value() == CANNED_DESTINATION
    assert page.locator("#uploads-results-dir").input_value() == "/synthetic/out"


def test_run_migration_dispatches_and_reports_the_honest_verdict(gui) -> None:
    """The run call carries the choices; `done` says what really happened."""
    app = _open(gui)
    page = app.page
    page.fill("#migrate-export-dir", "/synthetic/export")
    page.fill("#migrate-out-dir", "/synthetic/out")
    page.select_option("#migrate-source", CANNED_SOURCE)
    page.select_option("#migrate-destination", CANNED_DESTINATION)
    page.wait_for_timeout(150)
    page.click("#migrate-run")
    page.wait_for_timeout(120)

    args = app.last_args("run_migration_async")
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
    assert page.locator("#migrate-run").is_disabled()

    app.emit(stage_event(_FLOW, "ingest", "start"))
    assert "Reading records" in app.text("#log-strip-msg")

    # The controller's `done` carries a notice written for the terminal; the
    # view must state the verdict in its own register instead of echoing it.
    done = done_event(_FLOW, summary_id="feedfacefeedface", ccda_patients=2)
    done["notice"] = "migration artifacts prepared — run 'anast upload' to file the charts"
    done["outcome"] = "prepared"
    app.emit(done)

    assert not page.locator("#migrate-run").is_disabled()
    result = page.locator("#migrate-result").text_content() or ""
    assert "Nothing has been sent yet" in result
    assert "anast" not in result
    assert app.last_args("last_run_summary") == ["feedfacefeedface"]
    assert str(CANNED_PATIENTS[0]["display_name"]) in (
        page.locator("#migrate-patients-body").text_content() or ""
    )


def test_no_automatic_route_is_reported_as_an_outcome(gui) -> None:
    """The manual-import verdict is loud, but it is not a crash."""
    app = _open(gui)
    page = app.page
    page.select_option("#migrate-source", CANNED_SOURCE)
    page.select_option("#migrate-destination", CANNED_DESTINATION)
    page.wait_for_timeout(150)
    page.click("#migrate-run")

    app.emit(error_event(_FLOW, "deliver", "no viable automated route to 'x' — import by hand"))

    result = page.locator("#migrate-result").text_content() or ""
    assert "No automatic route" in result
    assert "viable" not in result.lower(), "the controller's wording leaked into the view"
    assert not page.locator("#migrate-run").is_disabled()


def test_migrate_ignores_another_flow_terminal_event(gui) -> None:
    """The per-flow guard: a Charts `done` must not announce a migration.

    Both views now live in one document, so the dispatcher — not a page
    boundary — is what keeps them apart: the event goes to the ONE view that
    registered that flow, and the other one carries on untouched.
    """
    app = _open(gui)
    page = app.page
    page.select_option("#migrate-source", CANNED_SOURCE)
    page.select_option("#migrate-destination", CANNED_DESTINATION)
    page.wait_for_timeout(150)
    page.click("#migrate-run")
    page.wait_for_timeout(120)

    app.emit(done_event(_OTHER_FLOW, summary_id="deadbeef", patients=9))

    assert page.locator("#migrate-run").is_disabled(), "a Charts run ended the migration"
    assert (page.locator("#migrate-result").text_content() or "").strip() == "Rebuilding…"
    assert page.locator("#migrate-patients").is_hidden()
    # It landed where it belonged instead. (Checked on the attribute: the whole
    # Charts section is off screen, so Playwright calls all of it invisible.)
    assert app.last_args("last_run_summary") == ["deadbeef"]
    assert page.locator("#charts-patients").get_attribute("hidden") is None
