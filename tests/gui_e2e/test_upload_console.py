"""The upload console (console.html): ledger inspection + driving an upload.

Inspects the canned ledger (counter tiles, per-state grid, manifest preview,
calendar HUD), drives the upload through ``upload_start``, and closes it with
the controller's own terminal upload events. Every value this page renders is a
count, a state name, an id, an ISO stamp, or an exception TYPE — the assertions
below deliberately touch nothing else.
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright", reason="the GUI lane needs playwright + chromium")

from stub import CANNED_LEDGER_COUNTS, CANNED_RUN

from anastomosis.core.upload_command import DEFAULT_MAX_ATTEMPTS
from anastomosis.gui.consoles.upload import UploadConsole
from anastomosis.gui.events import error_event, stage_event
from anastomosis.gui.shared import _group_states

pytestmark = pytest.mark.gui_e2e

_FLOW = UploadConsole._FLOW
_GROUPS = _group_states(CANNED_LEDGER_COUNTS)
_LEDGER = "/synthetic/out/upload_ledger.sqlite"
_OUT_DIR = "/synthetic/out"


def _inspect(gui):
    """Open the console and load the canned ledger (the page's entry move)."""
    console = gui("console.html")
    console.page.fill("#db-path", _LEDGER)
    console.page.fill("#out-dir", _OUT_DIR)
    console.page.click("#load-btn")
    console.page.wait_for_timeout(200)
    return console


def test_console_surfaces_the_shared_machine_warning(gui) -> None:
    """The CDP safety notice is fetched from its single source, not re-typed."""
    console = gui("console.html")

    assert console.called("upload_safety_notice")
    warning = console.page.locator("#safety-warning").text_content() or ""
    assert "NEVER stores your EHR credentials" in warning


def test_ledger_inspection_fills_the_counters_and_the_calendar(gui) -> None:
    """upload_status() drives the tiles, the state grid, and the run detail."""
    console = _inspect(gui)
    page = console.page

    assert console.last_args("upload_status") == [_LEDGER]
    assert page.locator("#counter-terminal").text_content() == str(_GROUPS["terminal"])
    assert page.locator("#counter-pending").text_content() == str(_GROUPS["pending"])
    assert page.locator("#counter-active").text_content() == str(_GROUPS["active"])
    assert page.locator("#counter-total").text_content() == str(sum(CANNED_LEDGER_COUNTS.values()))
    assert page.locator("#counter-errortypes").text_content() == "1"
    # One cell per non-empty ledger state.
    assert page.locator("#state-grid .state-cell").count() == len(CANNED_LEDGER_COUNTS)
    detail = page.locator("#run-detail").text_content() or ""
    assert str(CANNED_RUN["run_id"]) in detail and "in progress" in detail
    # The manifest preview (counts + bytes, never a filename).
    assert console.last_args("upload_manifest_preview") == [_OUT_DIR]
    assert "renderable PDF(s)" in (page.locator("#manifest-preview").text_content() or "")
    assert page.locator("#counter-renderable").text_content() == "7"
    # The calendar HUD opens on the run's month with a halo on its start day.
    assert page.locator("#cal-title").text_content() == "August 2026"
    assert page.locator("#cal-grid .calendar-cell").count() == 42
    assert page.locator("#cal-grid .calendar-cell--has-data").count() == 1
    page.click("#cal-next")
    assert page.locator("#cal-title").text_content() == "September 2026"


def test_error_inspector_lists_type_names_only(gui) -> None:
    """The flyout histogram carries exception TYPE names and counts."""
    console = _inspect(gui)
    page = console.page

    page.click("#inspect-errors")
    assert "show" in (page.locator("#error-flyout").get_attribute("class") or "")
    hist = page.locator("#error-hist").text_content() or ""
    assert "PlaywrightTimeoutError" in hist


def test_item_key_palette_lists_opaque_keys(gui) -> None:
    """Ctrl+K surfaces the pending item KEYS the controller returned."""
    console = _inspect(gui)
    page = console.page

    page.keyboard.press("Control+k")
    page.wait_for_timeout(250)

    assert console.called("upload_item_keys")
    labels = page.locator("#cmd-palette-list li").all_text_contents()
    assert any("enc-0001" in label for label in labels)


def test_start_upload_dispatches_the_drive_and_completes(gui) -> None:
    """The drive call carries the operator's inputs; `done` closes the run."""
    console = _inspect(gui)
    page = console.page

    page.fill("#cdp-url", "127.0.0.1:9222")
    page.fill("#pack-name", "tebra")
    page.fill("#skiplist", "enc-0002\n# a comment\n")
    page.click("#start-upload-btn")
    page.wait_for_timeout(150)

    args = console.last_args("upload_start")
    # Positional order per UploadConsole.upload_start: out_dir, cdp_url, pack,
    # pack_dirs, skiplist, max_attempts, verify.
    assert args[0] == _OUT_DIR
    assert args[1] == "127.0.0.1:9222"
    assert args[2] == "tebra"
    assert args[4] == ["enc-0002", "# a comment"], "the skiplist lines must reach the controller"
    assert args[5] == DEFAULT_MAX_ATTEMPTS, "the retry budget must come from gui_config()"
    assert args[6] is True, "the L0-L6 ladder ships ON"
    assert "uploading" in (page.locator("#status-text").text_content() or "")

    console.emit(stage_event(_FLOW, "upload", "done"))

    assert page.locator("#status-text").text_content() == "upload complete"
    assert "upload complete" in (page.locator("#log-strip-msg").text_content() or "")


def test_upload_abort_event_stops_and_banners(gui) -> None:
    """A safety abort surfaces its TYPE name and stops the console's polling."""
    console = _inspect(gui)
    page = console.page
    page.fill("#cdp-url", "127.0.0.1:9222")
    page.fill("#pack-name", "tebra")
    page.click("#start-upload-btn")
    page.wait_for_timeout(150)

    console.emit(error_event(_FLOW, "upload", "WrongPatientBanner"))

    assert "WrongPatientBanner" in (page.locator("#banner").text_content() or "")
    assert "WrongPatientBanner" in (page.locator("#status-text").text_content() or "")
    # The terminal event re-reads the ledger once more (the counts never ride
    # the event) and then stops polling: no further reads after the settle.
    reads = len(console.calls("upload_status"))
    page.wait_for_timeout(400)
    assert len(console.calls("upload_status")) == reads


def test_stop_requests_a_cooperative_stop(gui) -> None:
    """The stop button asks the controller; it never touches the ledger itself."""
    console = _inspect(gui)

    console.page.click("#stop-upload-btn")
    console.page.wait_for_timeout(100)

    assert console.called("upload_stop")
    assert "stopping after the current document" in (
        console.page.locator("#status-text").text_content() or ""
    )
