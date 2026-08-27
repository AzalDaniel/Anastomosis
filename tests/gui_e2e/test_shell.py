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
        tab = app.page.locator(f'.navpill [data-view-target="{view}"]')
        assert tab.get_attribute("aria-selected") == "true"
        # One name per layer: the pill names the view in the chrome, the h1
        # names it in the content. The title band that printed it a third time
        # is gone, so the name must appear exactly once outside the nav.
        outside = app.page.evaluate(
            "label => [...document.querySelectorAll('.view:not([hidden]) h1')]"
            ".filter(h => h.textContent.trim() === label).length",
            label,
        )
        assert outside == 1, f"{label!r} appears {outside} times outside the nav"

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


def test_bridge_attaching_without_the_ready_event_still_goes_live(_browser) -> None:
    """The frozen app's race: pywebview can attach the api and fire its one-shot
    ready event BEFORE shell.js registers a listener. A missed event must not
    strand the app offline — the boot poll has to see the late api on its own,
    with no `pywebviewready` ever dispatched."""
    import time

    from anastomosis.gui.shell import _WEB_DIR

    page = _browser.new_page(bypass_csp=True)
    try:
        page.goto((_WEB_DIR / "index.html").as_uri())

        def _bridge() -> str | None:
            return page.get_attribute("html", "data-bridge")

        deadline = time.monotonic() + 5.0
        while _bridge() != "offline":
            assert time.monotonic() < deadline, f"never offline: {_bridge()!r}"
            time.sleep(0.05)
        page.evaluate(
            """() => {
              window.pywebview = { api: { info: () => Promise.resolve({
                version: '0.0-test', sources: [], packs: [], destinations: [],
              }) } };
            }"""
        )
        deadline = time.monotonic() + 5.0
        while _bridge() != "live":
            assert time.monotonic() < deadline, (
                f"the boot poll never saw the late api: bridge={_bridge()!r}"
            )
            time.sleep(0.05)
    finally:
        page.close()


def test_the_nav_is_one_tablist_that_never_scrolls_away(gui) -> None:
    """Four opaque boxes became one floating pill, and it stays put.

    The old nav scrolled with the content: at scrollTop 400 it sat at y = -354 on
    Charts and Migrate, entirely off-screen, which the HIG names directly ("make
    sure the tab bar is visible when people navigate to different sections").
    It also had no material — its background sampled byte-identical to the page
    ground, 1.00:1 — so nothing marked it as chrome at all.
    """
    app = gui()
    page = app.page

    pill = page.locator(".navpill")
    assert pill.count() == 1, "there is exactly one pill in the app, and it is the nav"
    for option in page.locator(".navpill .segment-option").all():
        box = option.bounding_box()
        assert box is not None
        assert box["height"] >= 44, f"tab {option.text_content()!r} is {box['height']}px tall"
        assert box["width"] >= 104, f"tab {option.text_content()!r} is {box['width']}px wide"

    for view, _label in NAV_VIEWS:
        app.show(view)
        page.evaluate("() => document.querySelector('.app-shell').scrollTo(0, 400)")
        page.wait_for_timeout(150)
        top = page.locator(".navpill").bounding_box()
        assert top is not None and top["y"] >= 0, f"the nav scrolled off {view} (y={top})"
        page.evaluate("() => document.querySelector('.app-shell').scrollTo(0, 0)")


def test_the_nav_reads_as_tabs_and_keys_like_tabs(gui) -> None:
    """A group of peer destinations is a tablist, not a radiogroup.

    The pill shares its sliding/dragging mechanism with the settings toggles, so
    the thing that has to be pinned is that it does NOT share their ARIA: the
    same code has to say `tab`/`aria-selected` here and `radio`/`aria-pressed`
    there, or assistive tech is told the views are a multiple-choice question.
    """
    app = gui()
    page = app.page

    tabs = page.locator(".navpill [role='tab']")
    assert tabs.count() == len(NAV_VIEWS)
    assert page.locator(".navpill[role='tablist']").count() == 1
    assert page.locator(".navpill [role='radio']").count() == 0

    def tabindexes() -> list[int]:
        return page.evaluate(
            "() => [...document.querySelectorAll('.navpill .segment-option')].map(o => o.tabIndex)"
        )

    # Roving tabindex: only the selected tab is in the tab order.
    assert tabindexes() == [0, -1, -1, -1]
    page.locator('.navpill [data-view-target="charts"]').focus()
    page.keyboard.press("ArrowRight")
    page.wait_for_timeout(300)
    page.keyboard.press("ArrowRight")
    page.wait_for_timeout(400)
    assert tabindexes() == [-1, -1, 0, -1]
    assert app.visible("uploads"), "the arrow keys moved focus but not the view"

    page.keyboard.press("End")
    page.wait_for_timeout(400)
    assert app.visible("teach")
    page.keyboard.press("Home")
    page.wait_for_timeout(400)
    assert app.visible("charts")


def test_reduced_motion_fades_the_lozenge_rather_than_teleporting_it(gui) -> None:
    """The global transition-zeroing rule is a deletion, not a fallback.

    A selection indicator that vanishes and reappears somewhere else is harder
    to follow than one that travels, so travel is replaced by a fade — which is
    what the HIG asks for ("replacing transitions in x-, y-, and z-axes with
    fades"), not what zeroing every duration produces.
    """
    app = gui()
    page = app.page
    # Set after load on purpose: shell.js checks the query per activation rather
    # than caching it, because the operator can change the system setting while
    # the app is open.
    page.emulate_media(reduced_motion="reduce")
    page.wait_for_timeout(100)

    seen: list[str] = []
    page.expose_function("__navClass", lambda cls: seen.append(cls))
    page.evaluate(
        "() => { const p = document.querySelector('.navpill');"
        " new MutationObserver(() => window.__navClass(p.className))"
        "   .observe(p, {attributes: true, attributeFilter: ['class']}); }"
    )
    app.show("teach")
    page.wait_for_timeout(500)

    assert any("is-settling" in c for c in seen), f"no fade was used: {seen}"
    assert not any("is-stretching" in c for c in seen), f"the stretch ran anyway: {seen}"
    assert app.visible("teach"), "the view did not switch under reduced motion"


def test_the_activity_strip_floats_instead_of_landing_on_the_last_panel(gui) -> None:
    """Sticky kept the strip in the scroll flow, so it parked on the content.

    At the end of a view it settled ON TOP of the last panel and painted a
    translucent film over it. That is not only ugly: it was the sole cause of
    five WCAG failures — the strip's own timestamp and hint, and the help line
    underneath it, measured 4.04:1 on a surface that reads 4.58:1 when nothing
    covers it. It is chrome, so it floats like the rest of the chrome, and the
    shell's bottom padding is the gutter that keeps content clear of it.
    """
    app = gui()
    page = app.page

    for view, _label in NAV_VIEWS:
        app.show(view)
        page.evaluate(
            "() => { const s = document.querySelector('.app-shell');"
            " s.scrollTo(0, s.scrollHeight); }"
        )
        page.wait_for_timeout(200)
        gap = page.evaluate(
            "() => { const strip = document.querySelector('.log-strip').getBoundingClientRect();"
            " const panels = [...document.querySelectorAll('.view:not([hidden]) .panel')];"
            " const last = panels.length"
            "   ? Math.max(...panels.map(p => p.getBoundingClientRect().bottom)) : 0;"
            " return Math.round(strip.top - last); }"
        )
        assert gap >= 20, f"the strip sits {-gap}px into the last panel on {view}"

    strip = page.locator(".log-strip").bounding_box()
    assert strip is not None
    assert strip["height"] >= 44, f"the strip is {strip['height']}px tall"
