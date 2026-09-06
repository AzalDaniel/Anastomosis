"""Four things the app did that were not accessibility, and were still wrong.

Split out of the same audit as the accessibility work (#214): a shortcut that
fired on the wrong key, a list that truncated twice without saying so, a banner
that never left, and two counts of two different things sitting side by side as
if they were one.
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright", reason="the GUI lane needs playwright + chromium")

pytestmark = pytest.mark.gui_e2e

_OUT_DIR = "/synthetic/out"


def _drawer_open(app) -> bool:
    return app.page.locator("#log-drawer").is_visible()


# ── the "L" shortcut ───────────────────────────────────────────────────────


def test_a_chooser_keeps_the_letters_it_is_given(gui) -> None:
    """A chooser trigger is a <button>: a two-tag guard is not enough, so
    every chooser must stop propagation on a printable character too, or
    pressing `l` on one runs the type-ahead AND opens the drawer."""
    app = gui()

    app.page.focus("#charts-pack")
    app.page.keyboard.press("l")
    app.page.wait_for_timeout(120)

    assert not _drawer_open(app), "the drawer opened on top of a chooser's type-ahead"


def test_a_text_field_keeps_the_letters_it_is_given(gui) -> None:
    """The one case the old guard did get right, kept honest."""
    app = gui()

    app.page.focus("#charts-export-dir")
    app.page.keyboard.press("l")
    app.page.wait_for_timeout(120)

    assert not _drawer_open(app)
    assert app.page.locator("#charts-export-dir").input_value() == "l"


def test_the_shortcut_still_works_where_nothing_is_being_typed(gui) -> None:
    """It is advertised on the strip as "L", so a bare letter it stays."""
    app = gui()

    app.page.focus("#charts-run")
    app.page.keyboard.press("l")
    app.page.wait_for_timeout(120)

    assert _drawer_open(app)


def test_ctrl_l_is_left_to_the_browser(gui) -> None:
    """The old handler read the key and no modifier at all."""
    app = gui()

    app.page.keyboard.press("Control+l")
    app.page.wait_for_timeout(120)

    assert not _drawer_open(app)


# ── the search's two cuts ──────────────────────────────────────────────────

_MANY_KEYS = [f"enc-{n:04d}:0f1e2d3c4b5a" for n in range(200)]


def _big_ledger(gui):
    app = gui(
        canned={
            "upload_item_keys": {
                "ok": True,
                "item_keys": _MANY_KEYS,
                "count": len(_MANY_KEYS),
                # The ledger holds far more than the controller sent.
                "total": 3000,
            }
        }
    )
    app.show("uploads")
    app.page.fill("#uploads-results-dir", _OUT_DIR)
    app.page.click("#uploads-refresh")
    app.page.wait_for_timeout(300)
    return app


def test_the_search_says_how_much_it_is_not_showing(gui) -> None:
    """Fifty of two hundred of three thousand must be said, not left silent."""
    app = _big_ledger(gui)

    rows = app.page.locator("#uploads-search-results .search-result")
    assert rows.count() == 50, rows.count()
    note = app.text("#uploads-search-results .search-empty")
    assert "50" in note and "3000" in note, note


def test_a_filtered_search_counts_the_matches_not_the_ledger(gui) -> None:
    """Narrowing changes what "the rest" means, so the sentence changes too."""
    app = _big_ledger(gui)

    # The keys run enc-0000..enc-0199, so this narrows to the ten of enc-019x,
    # which fits inside the fifty rows the view paints.
    app.page.fill("#uploads-search", "enc-019")
    app.page.wait_for_timeout(150)

    rows = app.page.locator("#uploads-search-results .search-result")
    assert rows.count() == 10
    note = app.text("#uploads-search-results .search-empty")
    assert "first 200" in note, note


def test_a_search_that_fits_says_nothing(gui) -> None:
    """The canned ledger has two items; a note about them would be noise."""
    app = gui()
    app.show("uploads")
    app.page.fill("#uploads-results-dir", _OUT_DIR)
    app.page.click("#uploads-refresh")
    app.page.wait_for_timeout(300)

    assert app.page.locator("#uploads-search-results .search-result").count() == 2
    assert app.text("#uploads-search-results .search-empty") == ""


# ── the banner ─────────────────────────────────────────────────────────────


def test_the_banner_can_be_dismissed(gui) -> None:
    """`#banner` had no buttons in it at all."""
    app = gui()
    app.page.evaluate("() => window.AnastShell.showBanner('Something went wrong.')")
    app.page.wait_for_timeout(80)
    assert app.page.locator("#banner").get_attribute("class") == "banner show"

    app.page.click("#banner-dismiss")
    app.page.wait_for_timeout(80)

    assert "show" not in (app.page.locator("#banner").get_attribute("class") or "")
    assert app.text("#banner-text") == ""


def test_a_view_switch_leaves_the_banner_behind(gui) -> None:
    """hideBanner was called by five run entry points and nothing else.

    Uploads never called it, and no view switch did — so a message about a
    problem an operator had already fixed followed them around the app.
    """
    app = gui()
    app.page.evaluate("() => window.AnastShell.showBanner('Something went wrong.')")
    app.page.wait_for_timeout(80)

    app.show("uploads")

    assert "show" not in (app.page.locator("#banner").get_attribute("class") or "")
    assert app.text("#banner-text") == ""


# ── the header over an empty list ──────────────────────────────────────────


def test_a_column_header_does_not_stand_over_an_empty_list(gui) -> None:
    """`setEmpty` toggled the list and its empty state, and left the head up."""
    app = gui()
    app.show("uploads")

    # Nothing read yet: the empty state is showing.
    assert app.page.locator("#uploads-states-empty").is_visible()
    assert app.page.locator("#uploads-states-head").is_hidden()

    app.page.fill("#uploads-results-dir", _OUT_DIR)
    app.page.click("#uploads-refresh")
    app.page.wait_for_timeout(300)

    assert app.page.locator("#uploads-states").is_visible()
    assert app.page.locator("#uploads-states-head").is_visible()
