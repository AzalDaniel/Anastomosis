"""The shell: one document, four views, and the rules that hold in all of them.

These are the invariants the five-page GUI could not have: switching views is
not a navigation, the activity strip belongs to every flow at once, and no view
may speak engineering. Each test here exists because breaking it produced a real
defect — a page that flashed on every click, a run whose progress vanished when
the operator looked away, and copy written for a terminal.
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright", reason="the GUI lane needs playwright + chromium")

from expectations import NAV_VIEWS, VIEWS, check_dashboard

from anastomosis.gui.consoles.upload import UploadConsole
from anastomosis.gui.events import stage_event

pytestmark = pytest.mark.gui_e2e

# Vocabulary that must not survive anywhere an operator can read it
# (DESIGN_LANGUAGE §10, docs/design/COPY_MAP.md). Matched case-insensitively.
# "anast " catches CLI invocations leaking into GUI copy without matching the
# product name itself.
BANNED_WORDS: tuple[str, ...] = (
    "pipeline",
    "cdp",
    "manifest",
    "ledger",
    "selectors",
    "item-key",
    "phi-safe",
    "ritual",
    "milestone",
    "viable",
    "anast ",
)

# Every attribute that puts words in front of a person, alongside the text.
_ATTR_TEXT = """
name => {
  const view = document.querySelector(`[data-view="${name}"]`);
  const parts = [view.getAttribute('aria-label') || ''];
  for (const node of view.querySelectorAll('[placeholder],[aria-label],[title]')) {
    parts.push(node.getAttribute('placeholder') || '');
    parts.push(node.getAttribute('aria-label') || '');
    parts.push(node.getAttribute('title') || '');
  }
  return parts.join(' ');
}
"""


def test_first_paint_matches_the_shared_expectations(gui) -> None:
    """The shared lane-1/lane-2 expectation set — the live app, whole."""
    app = gui()

    assert check_dashboard(app.page) == []


def test_nav_switches_views_without_navigating(gui) -> None:
    """Four switches, no document navigation, and the chrome follows along.

    Every "tab" used to be an anchor doing a full same-window document load:
    the stylesheet, the fonts, the SVG filter defs and the JS were re-parsed on
    every click, and the bridge was re-raced each time. GuiPage.show() asserts
    the boot marker survives, so a regression to real navigation fails here.
    """
    app = gui()
    start_url = app.page.url

    for view, label in NAV_VIEWS[1:] + NAV_VIEWS[:1]:
        app.show(view)
        assert app.visible(view)
        for other in VIEWS:
            if other != view:
                assert not app.visible(other), f"{other} stayed up behind {view}"
        assert app.text(".title-bar .title-text") == label
        current = app.page.locator(f'[data-view-target="{view}"]').get_attribute("aria-current")
        assert current == "true"

    assert app.page.url == start_url


def test_view_switch_crossfades(gui) -> None:
    """The outgoing view fades out and the incoming one fades in — in CSS.

    Asserted from the class transitions the router applies (recorded live, since
    they last only 240ms) plus the transition the stylesheet actually declares.
    """
    app = gui()
    app.page.evaluate(
        """() => {
            window.__classSeen = [];
            for (const section of document.querySelectorAll('[data-view]')) {
              new MutationObserver(() => {
                window.__classSeen.push(section.dataset.view + ':' + section.className);
              }).observe(section, {attributes: true, attributeFilter: ['class']});
            }
        }"""
    )
    app.show("migrate")

    seen = app.page.evaluate("window.__classSeen")
    assert any("charts:view view--leaving" in entry for entry in seen), seen
    assert any("migrate:view view--entering" in entry for entry in seen), seen
    duration = app.page.evaluate(
        "getComputedStyle(document.querySelector('[data-view=\"charts\"]')).transitionDuration"
    )
    assert "0.24s" in duration, duration


@pytest.mark.parametrize("view", VIEWS)
def test_hidden_panels_are_actually_hidden(gui, view: str) -> None:
    """A panel marked ``hidden`` must not render before it has content.

    The attribute is expressed by a UA-stylesheet rule that ANY author
    ``display`` declaration outranks, and the panels are all flex/grid
    components — so a view once opened on a full grid of zero counters and the
    Teach modes on empty result scaffolds.
    """
    app = gui()
    app.show(view)

    rendered = app.page.evaluate(
        """name => Array.from(
              document.querySelectorAll(`[data-view="${name}"] [hidden]`)
           ).filter(node => getComputedStyle(node).display !== 'none')
            .map(node => node.id || node.className)""",
        view,
    )
    assert rendered == [], f"the {view} view renders elements it marked hidden: {rendered}"


def test_no_view_speaks_engineering(gui) -> None:
    """No banned vocabulary reaches the operator, in text or in attributes.

    The words below are the ones COPY_MAP retires. This walks all four views —
    hidden panels included, because they are one click from being read.

    Scope note: strings the CONTROLLER produces (a route's registry evidence, a
    migration notice) are Python-owned and are not asserted here; the GUI never
    promotes them to headline copy.
    """
    app = gui()
    offenders: list[str] = []
    for view in VIEWS:
        app.show(view)
        blob = (app.view_text(view) + " " + app.page.evaluate(_ATTR_TEXT, view)).lower()
        offenders += [f"{view}: {word!r}" for word in BANNED_WORDS if word in blob]

    assert not offenders, "banned vocabulary survived in the shipped copy: " + ", ".join(offenders)


def test_activity_strip_carries_every_flow(gui) -> None:
    """A run reports from whichever view is on screen — the strip is global.

    Starting a run and switching views used to orphan its event stream: no page
    was listening for that flow any more, and the one that came up said
    "— idle —" while the run was still in flight.
    """
    app = gui()
    app.show("teach")

    app.emit(stage_event(UploadConsole._FLOW, "upload", "start"))

    strip = app.text("#log-strip-msg")
    assert "Uploads" in strip and "Filing charts" in strip, strip
    # And the history keeps it, whichever view the operator is in.
    app.page.click("#log-strip")
    app.page.wait_for_timeout(150)
    assert "Filing charts" in (app.page.locator("#log-rows").text_content() or "")


@pytest.mark.parametrize("prefix", ["charts", "migrate"])
def test_double_check_toggle_answers_a_real_mouse_click(gui, prefix: str) -> None:
    """Clicking a segment option selects it — with a real pointer, not a script.

    The toggle takes pointer capture on ``pointerdown`` to drive its drag
    physics, and the browser then retargets the following ``click`` to the
    capture element — so the per-option click handler never fired for a real
    press and the control was mouse-dead (keyboard and drag still worked, which
    is exactly why it read as fine).
    """
    app = gui()
    view = "charts" if prefix == "charts" else "migrate"
    app.show(view)
    toggle = app.page.locator(f'[data-view="{view}"] .segment-toggle[data-name="qa"]')
    assert toggle.get_attribute("data-value") == "on"

    app.page.click(f'[data-view="{view}"] .segment-option[data-value="off"]')
    app.page.wait_for_timeout(120)

    assert toggle.get_attribute("data-value") == "off"
    assert (
        app.page.locator(f'[data-view="{view}"] .segment-option[data-value="off"]').get_attribute(
            "aria-pressed"
        )
        == "true"
    )


def test_segment_indicator_is_actually_drawn(gui) -> None:
    """The indicator has a real width — its CSS vars must reach it.

    They used to arrive as a ``style="--segment-count: 2"`` markup attribute,
    which the app's own ``style-src 'self'`` CSP refuses: two console errors on
    every load, and ``width: calc((100% - 8px) / var(--segment-count))``
    collapsing to zero. shell.js writes them through the CSSOM instead.
    """
    app = gui()

    indicator = app.page.locator('[data-view="charts"] .segment-indicator').first
    width = indicator.evaluate("node => node.getBoundingClientRect().width")
    track = app.page.locator('[data-view="charts"] .segment-toggle[data-name="qa"]').evaluate(
        "node => node.getBoundingClientRect().width"
    )
    assert width > 0, "the segment indicator is invisible"
    # Two options: the blob covers about half the track (minus the 4px inset).
    assert 0.3 * track < width < 0.7 * track


def test_about_popover_carries_the_version_and_licence(gui) -> None:
    """The app name and version live in About — once, not on every screen."""
    app = gui()
    assert app.page.locator("#about-popover").is_hidden()

    app.page.click("#about-btn")
    app.page.wait_for_timeout(150)

    line = app.text("#about-version")
    assert line.startswith("Anastomosis ") and "AGPL-3.0" in line
    assert app.page.locator("#about-version").get_attribute("data-version")
    # The name appears exactly once inside the views: it does not.
    for view in VIEWS:
        assert "Anastomosis 0" not in app.view_text(view)
