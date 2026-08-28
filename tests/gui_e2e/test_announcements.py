"""What the app says out loud while it works.

Every view narrates a run in one line on screen — "Reading records…", "Step 2
of 2 — confirm.", "Finished." — and none of it reached a screen reader: the
lines carried no role and no ``aria-live``, so a click produced silence. On
Teach that line is the *only* answer a click has, which is the whole reason an
operator pressed the button a second time.

The fix is one always-present polite region (``#announcer``) that
``Shell.setStatus`` writes at the same moment it writes the visible line, plus
an alert (``#banner``) that is now in the accessibility tree before it has
anything to say. These tests pin both, and the two ways the arrangement fails
quietly: a region that is only created when there is news, and a region rewritten
with the words already in it.

No screen reader runs here — none exists in this environment. What is checked is
everything a screen reader reads: the roles, the tree membership, the computed
name, and the mutations.
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright", reason="the GUI lane needs playwright + chromium")

from stub import CANNED_LEDGER_COUNTS

from anastomosis.gui.consoles.runs import PipelineConsole
from anastomosis.gui.events import error_event, stage_event

pytestmark = pytest.mark.gui_e2e

_FLOW = PipelineConsole._FLOW
_OUT_DIR = "/synthetic/out"


def _announced(app) -> str:
    return app.text("#announcer")


def test_the_announcer_is_there_before_there_is_anything_to_announce(gui) -> None:
    """The region exists, empty, from first paint — the property that makes it work.

    A live region is announced when its CONTENTS change. Create the region and
    its first words in one task and there was no region to change: that is the
    ordering assistive technologies handle worst, and it is what a
    ``role="status"`` on ``#migrate-result`` (revealed with ``hidden``) would
    have been. So this one is never hidden and never created late.
    """
    app = gui()

    state = app.page.evaluate(
        """() => {
          const node = document.getElementById('announcer');
          if (!node) return null;
          const css = getComputedStyle(node);
          return {
            role: node.getAttribute('role'),
            live: node.getAttribute('aria-live'),
            text: node.textContent,
            display: css.display,
            visibility: css.visibility,
            width: node.getBoundingClientRect().width,
          };
        }"""
    )

    assert state is not None, "the app has no announcer"
    assert state["role"] == "status"
    assert state["live"] == "polite"
    assert state["text"] == "", "the announcer started with something to say"
    # In the tree, out of the way: `display: none` would take it out of the
    # accessibility tree, which is the one place it needs to be.
    assert state["display"] != "none"
    assert state["visibility"] != "hidden"
    assert state["width"] <= 1, f"the announcer is visible ({state['width']}px wide)"


def test_a_rebuild_says_what_it_is_doing(gui) -> None:
    """The stage line and the announcement are the same words, every step."""
    app = gui()

    app.emit(stage_event(_FLOW, "ingest", "start"))
    assert app.text("#charts-current") == "Reading records…"
    assert _announced(app) == "Reading records…"

    app.emit(stage_event(_FLOW, "ingest", "done"))
    app.emit(stage_event(_FLOW, "reconstruct", "start"))
    assert app.text("#charts-current") == "Building charts…"
    assert _announced(app) == app.text("#charts-current")

    app.emit({"type": "done", "flow": _FLOW, "summary_id": ""})
    assert app.text("#charts-current") == "Finished."
    assert _announced(app) == "Finished."


def test_a_stopped_run_says_stopped(gui) -> None:
    """An error puts words in both channels: the polite one and the alert."""
    app = gui()

    app.emit(error_event(_FLOW, "reconstruct", "UnsupportedSourceError"))

    assert _announced(app) == "Stopped."
    assert "UnsupportedSourceError" in app.text("#banner")


def test_the_banner_was_already_in_the_tree_when_the_error_arrived(gui) -> None:
    """`role="alert"` on a `display: none` box announces nothing reliably.

    The banner used to be `display: none` until `showBanner` added `.show`, so
    the alert and its text arrived together — the mutation happened outside the
    render tree. It is now always rendered and merely empty, which is why
    `hideBanner` clears the text rather than removing the box.

    The alert is `#banner-text`, not the box: the dismiss button sits in the box
    beside it, so pressing it is not announced as part of the message.
    """
    app = gui()

    def banner():
        return app.page.evaluate(
            """() => {
              const box = document.getElementById('banner');
              const text = document.getElementById('banner-text');
              return {
                display: getComputedStyle(box).display,
                role: text.getAttribute('role'),
                text: text.textContent,
                shown: box.classList.contains('show'),
              };
            }"""
        )

    before = banner()
    assert before["display"] != "none", "the alert was not in the tree before it spoke"
    assert before["role"] == "alert"
    assert before["text"] == ""

    app.emit(error_event(_FLOW, "reconstruct", "UnsupportedSourceError"))
    during = banner()
    assert during["shown"] is True
    assert "UnsupportedSourceError" in during["text"]

    # And hiding it empties it: stale text left in a region that is still in the
    # tree is text a screen reader can still walk onto.
    app.page.evaluate("() => window.AnastShell.hideBanner()")
    after = banner()
    assert after["shown"] is False
    assert after["text"] == ""
    assert after["display"] != "none"


def test_the_same_news_twice_is_said_once(gui) -> None:
    """Uploads re-reads the record on a timer; an idle run must go quiet.

    ``textContent = x`` is a mutation whether or not ``x`` is what was already
    there, and a live region announces mutations. Without the guard in
    ``Shell.announce`` a filing run that had not moved repeated its counts every
    poll, forever.
    """
    app = gui()
    app.show("uploads")
    app.page.evaluate(
        """() => {
          window.__saidTimes = 0;
          new MutationObserver(() => { window.__saidTimes += 1; }).observe(
            document.getElementById('announcer'),
            {childList: true, characterData: true, subtree: true}
          );
        }"""
    )
    app.page.fill("#uploads-results-dir", _OUT_DIR)

    for _ in range(3):
        app.page.click("#uploads-refresh")
        app.page.wait_for_timeout(250)

    # pending 4 → waiting, uploading 1 → progress, completed 2 → filed,
    # failed 1 → attention: the sentence reads in urgency order, zeros omitted.
    assert _announced(app) == "Filing: 1 needs attention, 1 in progress, 4 waiting, 2 filed."
    assert app.page.evaluate("() => window.__saidTimes") == 1, "the counters repeated themselves"


def test_the_counters_are_a_sentence_not_four_numbers(gui) -> None:
    """The spoken words come off the labels on screen, so they cannot drift."""
    app = gui()
    app.show("uploads")
    app.page.fill("#uploads-results-dir", _OUT_DIR)
    app.page.click("#uploads-refresh")
    app.page.wait_for_timeout(250)

    said = _announced(app)
    for bucket, label in (
        ("attention", "needs attention"),
        ("progress", "in progress"),
        ("waiting", "waiting"),
        ("filed", "filed"),
    ):
        shown = app.text(f"#uploads-count-{bucket}")
        assert f"{shown} {label}" in said, f"{bucket}: screen says {shown}, speech says {said!r}"
    assert sum(CANNED_LEDGER_COUNTS.values()) == 8


def test_teach_says_which_step_it_is_on(gui) -> None:
    """The step line is the whole of Teach's feedback, so it is the whole test."""
    app = gui()
    app.show("teach")

    assert _announced(app) == ""
    app.page.fill("#layout-samples", "/synthetic/samples")
    app.page.fill("#layout-name", "acme_soap")
    app.page.click("#layout-analyze")
    app.page.wait_for_timeout(250)

    assert _announced(app) == app.text("#layout-step")
    assert _announced(app) != ""


def test_the_activity_strip_is_named_by_the_event_not_the_keyboard_hint(gui) -> None:
    """`aria-label` overrode the contents, so the strip announced the hint.

    The visible text read "Charts: stopped — UnsupportedSourceError" while the
    accessible name stayed "Activity — click or press L for the full list". An
    error surfaced only here was surfaced nowhere.
    """
    app = gui()

    app.emit(error_event(_FLOW, "reconstruct", "UnsupportedSourceError"))

    strip = app.page.locator("#log-strip")
    # The computed name, as Chromium builds it — not the attributes it came from.
    snapshot = strip.aria_snapshot()
    assert "UnsupportedSourceError" in snapshot, snapshot
    assert "press L" not in snapshot, snapshot
    assert strip.get_attribute("aria-label") is None

    # The hint is still there, as what it always was: a description.
    described = app.page.evaluate(
        """() => {
          const id = document.getElementById('log-strip').getAttribute('aria-describedby');
          return id ? (document.getElementById(id) || {}).textContent : null;
        }"""
    )
    assert described == "Activity — click or press L for the full list"

    # The timestamp is not part of the name: "14:32:01" read in front of every
    # announcement is the sort of thing that makes people switch the app off.
    stamp = app.text("#log-strip-ts")
    assert stamp and stamp not in snapshot, f"{stamp!r} leaked into the name"


def test_the_progress_bar_has_a_name(gui) -> None:
    """It had aria-valuemin/max/now and nothing saying what it measures."""
    app = gui()

    bar = app.page.locator("#charts-progress .progress-bar")
    assert bar.get_attribute("aria-label") == "Stages finished"
