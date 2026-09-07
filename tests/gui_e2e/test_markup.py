"""Four places the markup claimed a relationship it did not have.

None of these is dramatic on its own; together they are the difference between
a form a screen reader can explain and one it can only read out. Each is
checked through Chromium's *computed* accessibility tree rather than the
attributes that produced it, because the whole class of bug here is an
attribute that looked right and was ignored.
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright", reason="the GUI lane needs playwright + chromium")

from stub import CANNED_PATIENTS

from anastomosis.gui.consoles.runs import PipelineConsole
from anastomosis.gui.consoles.source import SourceConsole
from anastomosis.gui.events import stage_event

pytestmark = pytest.mark.gui_e2e

_FLOW = PipelineConsole._FLOW


def test_chart_sections_is_a_named_group_not_a_run_of_switches(gui) -> None:
    """`<label for>` only reaches a labelable control, and a <div> is
    not one: "Chart sections" points at the <div> holding the four
    checkboxes, so the group needs an accessible name some other way,
    or the switches read as four unexplained toggles."""
    app = gui()

    field = app.page.evaluate(
        """() => {
          const label = [...document.querySelectorAll('#charts-form label')]
            .find((l) => l.textContent.trim() === 'Chart sections');
          if (!label) return null;
          const group = label.parentElement.querySelector('.section-matrix');
          return {
            labelFor: label.getAttribute('for'),
            role: group && group.getAttribute('role'),
            labelledBy: group && group.getAttribute('aria-labelledby'),
            labelId: label.id,
          };
        }"""
    )

    assert field is not None, "Charts has no chart-sections field"
    assert field["labelFor"] is None, "a `for` that names a div is a `for` that does nothing"
    assert field["role"] == "group"
    assert field["labelledBy"] == field["labelId"]

    # And Chromium agrees the group is named. Addressed by id, not by class:
    # Charts carries two matrices now (the layout's sections and the format's
    # selection rules), and a locator that matched both would be asserting
    # about whichever came first.
    snapshot = app.page.locator("#charts-sections").aria_snapshot()
    assert snapshot.startswith('- group "Chart sections"'), snapshot


def test_visits_to_skip_is_a_named_group_too(gui) -> None:
    """The second matrix is the same control, so it earns the same
    name: "Visits to skip" holds the source's own render-selection
    rules, the ingest-side twin of the section flags, and needs the
    same accessible-name fix as the sibling test above."""
    app = gui()

    field = app.page.evaluate(
        """() => {
          const label = [...document.querySelectorAll('#charts-form label')]
            .find((l) => l.textContent.trim() === 'Visits to skip');
          if (!label) return null;
          const group = label.parentElement.querySelector('.section-matrix');
          return {
            labelFor: label.getAttribute('for'),
            role: group && group.getAttribute('role'),
            labelledBy: group && group.getAttribute('aria-labelledby'),
            labelId: label.id,
          };
        }"""
    )

    assert field is not None, "Charts has no visits-to-skip field"
    assert field["labelFor"] is None
    assert field["role"] == "group"
    assert field["labelledBy"] == field["labelId"]
    snapshot = app.page.locator("#charts-selection").aria_snapshot()
    assert snapshot.startswith('- group "Visits to skip"'), snapshot


def test_a_plain_field_still_uses_a_real_label(gui) -> None:
    """The fix must not turn every field into a group — inputs keep `for`."""
    app = gui()

    pair = app.page.evaluate(
        """() => {
          const input = document.getElementById('charts-export-dir');
          const label = document.querySelector('label[for="charts-export-dir"]');
          return {
            hasLabel: !!label,
            role: input.getAttribute('role'),
          };
        }"""
    )

    assert pair["hasLabel"], "the export-dir field lost its label"
    assert pair["role"] is None, "an input was given a group role it does not need"


def test_the_patients_table_carries_its_columns(gui) -> None:
    """Four headings in a bare first row are four words, not column headers."""
    app = gui()
    app.emit({"type": "done", "flow": _FLOW, "summary_id": "sum-feedface"})
    app.page.wait_for_timeout(200)

    shape = app.page.evaluate(
        """() => {
          const table = document.querySelector('#charts-patients .patients-table');
          if (!table) return null;
          return {
            heads: [...table.querySelectorAll('thead th')].map((th) => th.scope),
            bodyRows: table.querySelectorAll('tbody tr').length,
          };
        }"""
    )

    assert shape is not None, "no patients table was rendered"
    assert shape["heads"] == ["col"] * 4
    assert shape["bodyRows"] == len(CANNED_PATIENTS)


def test_the_column_mapping_reads_as_a_table(gui) -> None:
    """`#format-mapping` is a CSS grid of divs that looks exactly like a table."""
    app = gui()
    app.show("teach")
    app.page.click('.mode-tab[data-mode="format"]')
    app.page.wait_for_timeout(120)
    app.page.fill("#format-example", "/synthetic/export.csv")
    app.page.fill("#format-name", "acme_csv")
    app.page.click("#format-analyze")
    app.page.wait_for_timeout(150)
    # The proposal is painted from the controller's answer, which arrives with
    # the stage event rather than the call.
    app.emit(stage_event(SourceConsole._FLOW, "source", "done"))

    snapshot = app.page.locator("#format-mapping").aria_snapshot()
    assert snapshot.startswith('- table "What each column in your file becomes"'), snapshot
    assert snapshot.count("- columnheader") == 4, snapshot
    assert "- cell" in snapshot, snapshot


def test_a_closed_chooser_names_no_active_row(gui) -> None:
    """`aria-activedescendant=""` is an IDREF pointing at nothing."""
    app = gui()
    trigger = app.page.locator("#charts-pack")

    assert trigger.get_attribute("aria-activedescendant") is None

    app.page.click("#charts-pack")
    app.page.wait_for_timeout(80)
    named = trigger.get_attribute("aria-activedescendant")
    assert named, "an open chooser names no active row"
    assert app.page.locator(f"#{named}").count() == 1

    app.page.keyboard.press("Escape")
    app.page.wait_for_timeout(80)
    assert trigger.get_attribute("aria-activedescendant") is None
