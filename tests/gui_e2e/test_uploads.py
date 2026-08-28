"""The Uploads view: reading the record, and driving the filing engine.

Reads the canned record (the four buckets, the plain-English states, the search
over visit ids, the calendar), drives filing through ``upload_start``, and
closes it with the controller's own terminal events. Every value this view
renders is a count, a state name, an id, an ISO stamp or an exception TYPE —
the assertions below deliberately touch nothing else.
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright", reason="the GUI lane needs playwright + chromium")

from stub import CANNED_LEDGER_COUNTS, CANNED_RUN

from anastomosis.core.upload_command import DEFAULT_MAX_ATTEMPTS
from anastomosis.gui.consoles.upload import UploadConsole
from anastomosis.gui.events import error_event, stage_event

pytestmark = pytest.mark.gui_e2e

_FLOW = UploadConsole._FLOW
_OUT_DIR = "/synthetic/out"
#: The record the controller keeps beside the charts — derived from the results
#: folder, exactly as the view derives it.
_RECORD = "/synthetic/out/upload_ledger.sqlite"


def _open(gui, **kwargs):
    app = gui(**kwargs)
    app.show("uploads")
    return app


def _load(gui):
    """Open Uploads and read the canned record (the view's entry move)."""
    app = _open(gui)
    app.page.fill("#uploads-results-dir", _OUT_DIR)
    app.page.click("#uploads-refresh")
    app.page.wait_for_timeout(250)
    return app


def test_uploads_surfaces_the_shared_machine_warning(gui) -> None:
    """The safety notice is fetched from its single source, not re-typed."""
    app = _open(gui)

    assert app.called("upload_safety_notice")
    assert "never stores your EHR sign-in" in app.text("#uploads-safety")
    # And it sits beside the button it is about. At the top of the panel it was
    # read before the form was filled in, which is before it means anything.
    order = app.page.evaluate(
        """() => {
          const warning = document.getElementById('uploads-safety');
          const start = document.getElementById('uploads-start');
          return {before: !!(warning.compareDocumentPosition(start)
                    & Node.DOCUMENT_POSITION_FOLLOWING),
                  gap: Math.round(start.getBoundingClientRect().top
                    - warning.getBoundingClientRect().bottom)};
        }"""
    )
    assert order["before"], "the warning is no longer above Start filing"
    assert order["gap"] < 40, f"the warning is {order['gap']}px from the button it warns about"


def test_reading_the_record_fills_the_buckets_and_the_calendar(gui) -> None:
    """upload_status() drives the four buckets, the states and the run detail."""
    app = _load(gui)
    page = app.page

    assert app.last_args("upload_status") == [_RECORD]
    # The canned record: 4 pending, 1 uploading, 2 completed, 1 failed. The
    # buckets are Filed / Needs attention / In progress / Waiting — "terminal"
    # (which mixed success and failure) is gone, and green means FILED only.
    assert app.text("#uploads-count-filed") == "2"
    assert app.text("#uploads-count-attention") == "1"
    assert app.text("#uploads-count-progress") == "1"
    assert app.text("#uploads-count-waiting") == "4"
    total = sum(CANNED_LEDGER_COUNTS.values())
    assert f"{total}" in app.text("#uploads-meta")
    # A zero is never coloured, whatever bucket it is in.
    zeroes = page.evaluate(
        """() => [...document.querySelectorAll('#uploads-counters .value')]
             .map(v => [v.dataset.bucket, v.dataset.zero])"""
    )
    assert dict(zeroes) == {
        "filed": "false",
        "attention": "false",
        "progress": "false",
        "waiting": "false",
    }, zeroes

    # One cell per non-empty state, in plain English, with the technical id
    # available on the tooltip.
    cells = page.locator("#uploads-states .state-cell")
    assert cells.count() == len(CANNED_LEDGER_COUNTS)
    text = page.locator("#uploads-states").text_content() or ""
    assert "Filed and confirmed" in text and "Could not file" in text
    assert "_" not in text, "a raw state id is being shown as a label"
    ids = {cells.nth(i).get_attribute("title") for i in range(cells.count())}
    assert ids == set(CANNED_LEDGER_COUNTS)

    # When the run happened is a sentence: a timestamp is not a value display.
    when = app.text("#uploads-when")
    assert str(CANNED_RUN["started_at"]) in when and "still running" in when
    # The finished-chart count came from the results folder.
    assert app.last_args("upload_manifest_preview") == [_OUT_DIR]
    meta = app.text("#uploads-meta")
    assert "Ready to file" in meta and "7" in meta

    # The calendar opens on the run's month with a halo on its start day.
    assert app.text("#uploads-cal-title") == "August 2026"
    assert page.locator("#uploads-cal-grid .calendar-cell").count() == 42
    assert page.locator("#uploads-cal-grid .calendar-cell--has-data").count() == 1
    page.click("#uploads-cal-next")
    assert app.text("#uploads-cal-title") == "September 2026"


def test_search_finds_an_upload_by_visit_id(gui) -> None:
    """The visible search replaced the hidden palette — same filtering, findable."""
    app = _load(gui)
    page = app.page

    assert app.called("upload_item_keys")
    rows = page.locator("#uploads-search-results .search-result").all_text_contents()
    assert any("enc-0001" in row for row in rows)

    page.fill("#uploads-search", "enc-0002")
    page.wait_for_timeout(80)
    rows = page.locator("#uploads-search-results .search-result").all_text_contents()
    assert rows and all("enc-0002" in row for row in rows)

    page.fill("#uploads-search", "no-such-visit")
    page.wait_for_timeout(80)
    assert "No upload matches" in (page.locator("#uploads-search-results").text_content() or "")


def test_error_kinds_list_type_names_only(gui) -> None:
    """The flyout carries exception TYPE names and counts, never patient data."""
    app = _load(gui)
    page = app.page

    page.click("#uploads-kinds-btn")
    page.wait_for_timeout(150)

    assert "show" in (page.locator("#uploads-kinds").get_attribute("class") or "")
    assert "PlaywrightTimeoutError" in (page.locator("#uploads-kinds-body").text_content() or "")


def test_start_filing_dispatches_the_drive_and_completes(gui) -> None:
    """The drive call carries the operator's inputs; `done` closes the run."""
    app = _load(gui)
    page = app.page

    page.fill("#uploads-assistant", "tebra")
    # The skip list lives behind the one Advanced disclosure, closed by default.
    page.click('[data-view="uploads"] .advanced > summary')
    page.wait_for_timeout(80)
    page.fill("#uploads-skiplist", "enc-0002\n# a note to myself\n")
    page.click("#uploads-start")
    page.wait_for_timeout(200)

    args = app.last_args("upload_start")
    # Positional order per UploadConsole.upload_start: out_dir, cdp_url, pack,
    # pack_dirs, skiplist, max_attempts, verify.
    assert args[0] == _OUT_DIR
    assert args[1] == "127.0.0.1:9222", "the browser connection ships pre-filled"
    assert args[2] == "tebra"
    assert args[4] == ["enc-0002", "# a note to myself"], "the skip list must reach the controller"
    assert args[5] == DEFAULT_MAX_ATTEMPTS, "the retry budget must come from gui_config()"
    assert args[6] is True, "double-checking each chart ships ON"
    assert "filing started" in app.text("#log-strip-msg")

    app.emit(stage_event(_FLOW, "upload", "done"))

    assert "Filing charts" in app.text("#log-strip-msg")


def test_abort_event_stops_and_banners(gui) -> None:
    """A safety abort surfaces its TYPE name and stops the polling."""
    app = _load(gui)
    page = app.page
    page.fill("#uploads-assistant", "tebra")
    page.click("#uploads-start")
    page.wait_for_timeout(200)

    app.emit(error_event(_FLOW, "upload", "WrongPatientBanner"))

    assert "WrongPatientBanner" in (page.locator("#banner").text_content() or "")
    # The terminal event re-reads the record once more (the counts never ride
    # the event) and then stops polling: no further reads after the settle.
    reads = len(app.calls("upload_status"))
    page.wait_for_timeout(500)
    assert len(app.calls("upload_status")) == reads


def test_stop_requests_a_cooperative_stop(gui) -> None:
    """The stop button asks the controller; it never touches the record itself."""
    app = _load(gui)

    app.page.click("#uploads-stop")
    app.page.wait_for_timeout(150)

    assert app.called("upload_stop")
    assert "stopping after the current chart" in app.text("#log-strip-msg")


def test_filing_refuses_without_the_folder_and_the_assistant(gui) -> None:
    """A missing input is refused loudly, and nothing is driven."""
    app = _open(gui)

    app.page.click("#uploads-start")
    app.page.wait_for_timeout(150)

    assert not app.called("upload_start")
    assert "Fill in" in (app.page.locator("#banner").text_content() or "")
