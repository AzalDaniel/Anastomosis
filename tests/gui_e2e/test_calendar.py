"""The filing calendar: a month you read, not forty-two buttons you cannot press.

Every cell was a ``<button role="gridcell">`` with no click handler and a
pointer cursor — 42 tab stops between the month arrows and the rest of the
page, all of them leading nowhere — inside a ``role="grid"`` that had no
``role="row"`` at all, which reports a row count of zero.

It is a table: seven columns of days, read and not operated. Making the cells
*do* something was the other way out and it is not available here — a day
opens nothing, because ``upload_status`` returns one run, so the histogram has
exactly one day in it. Per-day detail needs a ledger accessor that does not
exist yet (the same one the calendar's month paging is waiting on, #196). A
control that looks pressable and is not is worse than a cell.
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright", reason="the GUI lane needs playwright + chromium")

pytestmark = pytest.mark.gui_e2e

_OUT_DIR = "/synthetic/out"


def _load(gui):
    app = gui()
    app.show("uploads")
    app.page.fill("#uploads-results-dir", _OUT_DIR)
    app.page.click("#uploads-refresh")
    app.page.wait_for_timeout(250)
    return app


def test_the_month_is_not_forty_two_tab_stops(gui) -> None:
    """Nothing in the grid takes focus, and Tab walks straight past it."""
    app = _load(gui)
    page = app.page

    focusable = page.evaluate(
        """() => document.querySelectorAll(
             '#uploads-cal-grid button, #uploads-cal-grid a, #uploads-cal-grid [tabindex]'
           ).length"""
    )
    assert focusable == 0, f"{focusable} focusable things inside the calendar"

    # The keyboard's own account of it: from the last control before the grid,
    # one Tab must land somewhere else entirely.
    page.focus("#uploads-cal-next")
    page.keyboard.press("Tab")
    landed_inside = page.evaluate("() => !!document.activeElement.closest('#uploads-cal-grid')")
    assert not landed_inside, "Tab still walks into the calendar"


def test_the_month_reads_as_a_table_of_weeks(gui) -> None:
    """Six rows of seven under a header row — the structure it always looked like.

    The row wrappers carry `display: contents`, so the cells stay direct
    children of the CSS grid. That the rows survive into the accessibility tree
    anyway is the property worth pinning: it is a rendering detail this markup
    depends on.
    """
    app = _load(gui)

    snapshot = app.page.locator(".cal-table").aria_snapshot()
    assert snapshot.startswith('- table "August 2026"'), snapshot
    assert snapshot.count("- columnheader") == 7, snapshot
    # One header row plus six weeks.
    assert snapshot.count("- row ") == 7, snapshot
    assert snapshot.count("- cell") == 42, snapshot

    # And the rows are semantics ONLY. Drop `display: contents` and each week
    # becomes a block: seven days stacked down the page instead of across it.
    # The accessibility tree above is identical either way, so it has to be the
    # geometry that says so.
    boxes = app.page.evaluate(
        """() => [...document.querySelectorAll('#uploads-cal-grid [role=row]')[0].children]
                  .map((c) => { const r = c.getBoundingClientRect(); return [r.top, r.left]; })"""
    )
    assert len(boxes) == 7
    assert len({round(top) for top, _left in boxes}) == 1, f"the week is not one row: {boxes}"
    lefts = [left for _top, left in boxes]
    assert lefts == sorted(lefts) and lefts[0] < lefts[-1], f"days out of order: {lefts}"


def test_the_table_is_named_by_the_month_on_screen(gui) -> None:
    """The heading is the table's name, so paging renames it with no second copy."""
    app = _load(gui)

    app.page.click("#uploads-cal-next")
    app.page.wait_for_timeout(120)

    assert app.text("#uploads-cal-title") == "September 2026"
    assert app.page.locator(".cal-table").aria_snapshot().startswith('- table "September 2026"')


def test_a_day_with_runs_says_what_happened(gui) -> None:
    """The halo is a colour; the cell has to say the same thing in words.

    The legend that decodes the colours is `aria-hidden`, and stays that way:
    read aloud it is three adjectives with nothing attached to them. The state
    belongs on the day it describes.
    """
    app = _load(gui)

    marked = app.page.locator("#uploads-cal-grid .calendar-cell--has-data")
    assert marked.count() == 1
    # The canned ledger's run finished, so the halo is the "done" green.
    assert marked.get_attribute("aria-label") == "3 August — 1 filing run, filed"
    assert "calendar-cell--halo-done" in (marked.get_attribute("class") or "")

    legend = app.page.locator(".cal-legend")
    assert legend.get_attribute("aria-hidden") == "true"


def test_a_badge_no_longer_reads_as_part_of_the_date(gui) -> None:
    """Day "3" plus a badge of "2" used to compute an accessible name of "32"."""
    app = gui()
    app.show("uploads")
    app.page.evaluate(
        """() => window.AnastShell.renderCalendar({
             gridEl: document.getElementById('uploads-cal-grid'),
             titleEl: document.getElementById('uploads-cal-title'),
             year: 2026, month: 7,
             histogram: {'2026-08-03': {pending: 0, done: 2, errors: 1}},
           })"""
    )

    cell = app.page.locator("#uploads-cal-grid .calendar-cell--has-data")
    assert app.text("#uploads-cal-grid .calendar-count-badge") == "3"
    assert cell.get_attribute("aria-label") == (
        "3 August — 3 filing runs, 1 needs attention and 2 filed"
    )
    # Errors win the halo, as they do in the legend's order of urgency.
    assert "calendar-cell--halo-errors" in (cell.get_attribute("class") or "")
