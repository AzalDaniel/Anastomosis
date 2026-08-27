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
    formats = app.choices("#migrate-source")
    assert formats[0] == "Choose the export format…", "a migration's format is never guessed"
    assert CANNED_SOURCE not in formats, "a raw format id is not a label"
    # The row's caption is the format's own description; the id is the tooltip.
    assert CANNED_SOURCE in app.titles("#migrate-source")
    assert any("Practice Fusion" in note for note in app.notes("#migrate-source"))
    pages = app.choices("#migrate-render")
    assert pages[0].startswith("Rendered pages") and pages[1].startswith("Data only")
    destinations = app.choices("#migrate-destination")
    assert destinations[0] == "Choose a destination…"
    assert CANNED_DESTINATION in app.notes("#migrate-destination")
    # Rendered pages open on the standard layout's sections...
    assert page.locator("#migrate-sections input[data-section]").count() > 0
    # ...and the data-only view honestly exposes none.
    app.choose("#migrate-render", "ccda-standard")
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
    assert app.chosen("#migrate-source") == CANNED_SOURCE
    assert CANNED_SOURCE in app.text("#log-strip-msg")


def test_destination_choice_lists_the_routes_in_plain_language(gui) -> None:
    """Every route option becomes a card; the chosen one is marked recommended."""
    app = _open(gui)
    page = app.page
    transit = _transit()

    app.choose("#migrate-destination", CANNED_DESTINATION)
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
    app.choose("#migrate-destination", CANNED_DESTINATION)
    page.wait_for_timeout(200)

    page.click("#migrate-continue")
    page.wait_for_timeout(400)

    assert app.visible("uploads") and not app.visible("migrate")
    assert page.locator("#uploads-assistant").input_value() == CANNED_DESTINATION
    assert page.locator("#uploads-results-dir").input_value() == "/synthetic/out"
    # It overwrote two fields the operator can see, so it says so once.
    assert "taken from the migration" in app.text("#log-strip")


def test_a_spent_handoff_never_retargets_uploads_again(gui) -> None:
    """The offer is made once. Coming back to Uploads must not re-apply it.

    The handoff used to live in a shell-global that was never cleared, and
    Uploads re-read it on EVERY arrival. An operator who retargeted the results
    folder and the filing assistant by hand for a second batch, glanced at
    another view and came back had both silently reverted to the migration's —
    and "Start filing" then drove the wrong folder into the wrong destination,
    with no banner and no line in the strip. That is the promise this product
    makes first: never silently misfile a chart.
    """
    app = _open(gui)
    page = app.page
    page.fill("#migrate-out-dir", "/synthetic/batch-a")
    app.choose("#migrate-destination", CANNED_DESTINATION)
    page.wait_for_timeout(200)
    page.click("#migrate-continue")
    page.wait_for_timeout(400)
    assert page.locator("#uploads-results-dir").input_value() == "/synthetic/batch-a"

    # The operator retargets Uploads by hand, for a different batch.
    page.fill("#uploads-results-dir", "/synthetic/batch-b")
    page.evaluate(
        "() => { const a = document.getElementById('uploads-assistant');"
        " a.value = ''; a.dispatchEvent(new Event('change', {bubbles: true})); }"
    )

    # ... glances at another view, and comes back.
    app.show("charts")
    app.show("uploads")
    page.wait_for_timeout(300)

    assert page.locator("#uploads-results-dir").input_value() == "/synthetic/batch-b", (
        "a spent handoff reverted the folder the charts would be filed from"
    )
    assert page.locator("#uploads-assistant").input_value() == "", (
        "a spent handoff reverted the destination the charts would be filed into"
    )


def test_run_migration_dispatches_and_reports_the_honest_verdict(gui) -> None:
    """The run call carries the choices; `done` says what really happened."""
    app = _open(gui)
    page = app.page
    page.fill("#migrate-export-dir", "/synthetic/export")
    page.fill("#migrate-out-dir", "/synthetic/out")
    app.choose("#migrate-source", CANNED_SOURCE)
    app.choose("#migrate-destination", CANNED_DESTINATION)
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
    app.choose("#migrate-source", CANNED_SOURCE)
    app.choose("#migrate-destination", CANNED_DESTINATION)
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
    app.choose("#migrate-source", CANNED_SOURCE)
    app.choose("#migrate-destination", CANNED_DESTINATION)
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


def test_a_finished_migration_survives_a_click_the_controller_refuses(gui) -> None:
    """The busy guard is shared with every other view, so this click is rejected.

    Migrate had the same shape as Charts: it cleared the patient table and wrote
    "Rebuilding…" over the verdict before asking, then reported the refusal as
    the bare sentinel and left "Rebuilding…" standing over a run that had
    already finished and one that never started.
    """
    app = _open(gui)
    page = app.page
    app.choose("#migrate-source", CANNED_SOURCE)
    app.choose("#migrate-destination", CANNED_DESTINATION)
    page.wait_for_timeout(150)

    page.click("#migrate-run")
    app.emit(done_event(_FLOW, summary_id="feedfacefeedface", patients=2, rendered=4))
    page.wait_for_selector("#migrate-patients:not([hidden])")

    verdict = page.locator("#migrate-result").text_content()
    assert verdict and "Nothing has been sent yet" in verdict

    page.evaluate(
        "() => { window.pywebview.api.run_migration_async ="
        " () => Promise.resolve({ok: false, error: 'Busy'}); }"
    )
    page.click("#migrate-run")
    page.wait_for_selector("#banner.show")

    assert page.locator("#migrate-result").text_content() == verdict
    assert page.locator("#migrate-patients").get_attribute("hidden") is None
    assert not page.locator("#migrate-run").is_disabled()
    banner = app.text("#banner")
    assert "Busy" not in banner, f"the sentinel reached the screen: {banner!r}"
    assert "already working on something else" in banner, banner


def test_section_choices_survive_a_layout_round_trip_here_too(gui) -> None:
    """The fix for #129 landed on Charts and never reached this view.

    Both views compose the SAME run form, and its section memory is keyed by
    layout — but the key is an argument, and Migrate was calling `setSections`
    with two of the three. So nothing was ever remembered here: every change of
    the "Chart pages" picker put the layout's defaults back, and the run was
    submitted from the reinstated values. A physician who turned Vitals off got
    a transfer document with Vitals in it.

    This asserts the end that matters — what reaches the controller — not just
    what the checkboxes look like afterwards.
    """
    app = _open(gui)
    page = app.page
    app.choose("#migrate-source", CANNED_SOURCE)
    app.choose("#migrate-destination", CANNED_DESTINATION)
    page.wait_for_timeout(200)

    layouts = [
        value
        for value in page.evaluate(
            "() => [...document.querySelectorAll('#migrate-render + .chooser-list"
            " .chooser-row')].map(o => o.dataset.value).filter(Boolean)"
        )
        if value != "ccda-standard"  # a data-only document has no sections
    ]
    assert len(layouts) >= 2, f"need two layouts to switch between, got {layouts}"

    def sections() -> dict[str, bool]:
        return {
            b["k"]: b["v"]
            for b in page.evaluate(
                "() => [...document.querySelectorAll('#migrate-sections input[data-section]')]"
                ".map(i => ({k: i.dataset.section, v: i.checked}))"
            )
        }

    app.choose("#migrate-render", layouts[0])
    page.wait_for_timeout(150)
    labels = page.locator("#migrate-sections label.toggle")
    boxes = page.locator("#migrate-sections input[data-section]")
    assert boxes.count() >= 1
    for i in range(boxes.count()):
        if boxes.nth(i).is_checked():
            labels.nth(i).click()
    chosen = sections()
    assert chosen and not any(chosen.values()), chosen

    app.choose("#migrate-render", layouts[1])
    page.wait_for_timeout(150)
    app.choose("#migrate-render", layouts[0])
    page.wait_for_timeout(150)
    assert sections() == chosen, f"the layout's defaults came back over the choice: {sections()}"

    # And the run is built from what is on screen.
    page.fill("#migrate-export-dir", "/synthetic/export")
    page.fill("#migrate-out-dir", "/synthetic/out")
    page.click("#migrate-run")
    page.wait_for_timeout(300)
    sent = app.last_args("run_migration_async")
    assert sent, "the run never reached the controller"
    submitted = next(a for a in sent if isinstance(a, dict))
    assert not any(submitted.values()), (
        f"the run was submitted with sections the physician turned off: {submitted}"
    )
