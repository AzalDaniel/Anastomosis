"""What the keyboard can reach, and what it can get back out of.

Three panels — About, the activity drawer, the error-kinds flyout — must
each support Escape, `aria-expanded`, and focus entering/leaving on
open/close. Teach's mode tabs must be a single tab stop with working
arrow-key navigation, not `role="tab"` with only a click handler.

These tests press keys. Where the point is that focus MOVED, the
assertion is on ``document.activeElement``, not an attribute the markup
just set."""

from __future__ import annotations

import pytest

pytest.importorskip("playwright", reason="the GUI lane needs playwright + chromium")

pytestmark = pytest.mark.gui_e2e


def _focus_id(app) -> str:
    return str(app.page.evaluate("() => document.activeElement.id || ''"))


def _inside(app, selector: str) -> bool:
    return bool(
        app.page.evaluate(
            "sel => !!document.activeElement.closest(sel)",
            selector,
        )
    )


# ── the activity drawer ────────────────────────────────────────────────────


def test_opening_the_drawer_puts_you_in_it(gui) -> None:
    """A dialog you cannot reach is a dialog that did not open."""
    app = gui()

    app.page.click("#log-strip")
    app.page.wait_for_timeout(120)

    assert app.page.locator("#log-drawer").is_visible()
    assert app.page.locator("#log-strip").get_attribute("aria-expanded") == "true"
    assert _inside(app, "#log-drawer"), f"focus stayed outside, on {_focus_id(app)!r}"


def test_escape_closes_the_drawer_and_hands_focus_back(gui) -> None:
    """Every dismissable panel needs its own Escape handler, not just the
    About popover's."""
    app = gui()
    app.page.click("#log-strip")
    app.page.wait_for_timeout(120)

    app.page.keyboard.press("Escape")
    app.page.wait_for_timeout(120)

    assert app.page.locator("#log-drawer").is_hidden()
    assert app.page.locator("#log-strip").get_attribute("aria-expanded") == "false"
    assert _focus_id(app) == "log-strip", "focus was left in a panel that is gone"


def test_the_l_key_still_works_and_now_lands_somewhere(gui) -> None:
    """The shortcut the strip advertises, with the focus move behind it."""
    app = gui()

    app.page.keyboard.press("l")
    app.page.wait_for_timeout(120)
    assert app.page.locator("#log-drawer").is_visible()
    assert _inside(app, "#log-drawer")

    app.page.keyboard.press("l")
    app.page.wait_for_timeout(120)
    assert app.page.locator("#log-drawer").is_hidden()


def test_a_click_elsewhere_closes_it_without_dragging_focus_back(gui) -> None:
    """The click decides where focus goes, and focus never stays in the
    panel. Not pinned here: the outside-click path's `restore` argument —
    the browser focuses the click target (or the body) regardless, so a
    wrong `restore` value looks identical from outside; that branch is
    intent, documented in shell.js, not asserted here as observable."""
    app = gui()

    app.page.click("#log-strip")
    app.page.wait_for_timeout(120)
    app.page.click("#charts-panel h2")
    app.page.wait_for_timeout(120)

    assert app.page.locator("#log-drawer").is_hidden()
    assert not _inside(app, "#log-drawer"), "focus was left in a panel that is gone"

    # And a click that lands somewhere focusable keeps that focus.
    app.page.click("#log-strip")
    app.page.wait_for_timeout(120)
    app.page.click("#charts-export-dir")
    app.page.wait_for_timeout(120)

    assert app.page.locator("#log-drawer").is_hidden()
    assert _focus_id(app) == "charts-export-dir"


# ── About ──────────────────────────────────────────────────────────────────


def test_about_is_no_longer_twenty_tab_stops_away(gui) -> None:
    """`#about-popover` is last in the document; focus has to be moved to it.

    It carries the "Reduce visual effects" setting — the app's one preference —
    and reaching it meant Tabbing the whole page from the button that opened it.
    """
    app = gui()

    app.page.click("#about-btn")
    app.page.wait_for_timeout(120)

    assert _inside(app, "#about-popover"), f"focus stayed on {_focus_id(app)!r}"

    app.page.keyboard.press("Escape")
    app.page.wait_for_timeout(120)
    assert app.page.locator("#about-popover").is_hidden()
    assert _focus_id(app) == "about-btn"


# ── the error-kinds flyout ─────────────────────────────────────────────────


def test_the_error_kinds_flyout_says_it_is_open_and_can_be_closed(gui) -> None:
    """It slid open on a class and stayed there: no state, no way out."""
    app = gui()
    app.show("uploads")
    btn = app.page.locator("#uploads-kinds-btn")

    assert btn.get_attribute("aria-expanded") == "false"
    btn.click()
    app.page.wait_for_timeout(120)
    assert btn.get_attribute("aria-expanded") == "true"
    assert _inside(app, "#uploads-kinds"), f"focus stayed on {_focus_id(app)!r}"

    app.page.keyboard.press("Escape")
    app.page.wait_for_timeout(120)
    assert btn.get_attribute("aria-expanded") == "false"
    assert "show" not in (app.page.locator("#uploads-kinds").get_attribute("class") or "")
    assert _focus_id(app) == "uploads-kinds-btn"


# ── Teach's mode tabs ──────────────────────────────────────────────────────


def _tab_state(app):
    return app.page.evaluate(
        """() => [...document.querySelectorAll('.mode-tabs [role=tab]')].map((t) => ({
             id: t.id,
             selected: t.getAttribute('aria-selected'),
             tabIndex: t.tabIndex,
           }))"""
    )


def test_the_tablist_is_one_tab_stop(gui) -> None:
    """Two tabs, two stops, was what "tab, 1 of 2" was competing with."""
    app = gui()
    app.show("teach")

    assert [t["tabIndex"] for t in _tab_state(app)] == [0, -1]


def test_the_arrows_move_between_modes(gui) -> None:
    """The contract the nav pill already keeps, on the tablist that lacked it."""
    app = gui()
    app.show("teach")
    app.page.focus("#teach-tab-layout")

    app.page.keyboard.press("ArrowRight")
    app.page.wait_for_timeout(120)
    assert _focus_id(app) == "teach-tab-format"
    assert app.page.locator("#teach-format").is_visible()
    assert app.page.locator("#teach-layout").is_hidden()
    assert [t["tabIndex"] for t in _tab_state(app)] == [-1, 0]

    # Wraps, as a tablist does.
    app.page.keyboard.press("ArrowRight")
    app.page.wait_for_timeout(120)
    assert _focus_id(app) == "teach-tab-layout"

    app.page.keyboard.press("End")
    app.page.wait_for_timeout(120)
    assert _focus_id(app) == "teach-tab-format"

    app.page.keyboard.press("Home")
    app.page.wait_for_timeout(120)
    assert _focus_id(app) == "teach-tab-layout"
    assert app.page.locator("#teach-layout").is_visible()


def test_each_mode_panel_is_named_by_its_tab(gui) -> None:
    """The panels carried a second copy of the tab's own words as an aria-label."""
    app = gui()
    app.show("teach")

    for panel, tab in (("teach-layout", "teach-tab-layout"), ("teach-format", "teach-tab-format")):
        node = app.page.locator(f"#{panel}")
        assert node.get_attribute("aria-labelledby") == tab
        assert node.get_attribute("aria-label") is None
