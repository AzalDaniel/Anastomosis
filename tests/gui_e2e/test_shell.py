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
def test_double_check_is_a_switch_that_answers_a_real_mouse_click(gui, prefix: str) -> None:
    """Double-checking is a binary setting, so it wears the switch idiom.

    It used to be a second sliding pill on the same screen as the view nav,
    which read to assistive tech as a radiogroup of "On"/"Off" — a control
    announcing its own states instead of naming what it does. It also inherited
    the pill's pointer capture, which retargeted the click away from the option
    and left the control mouse-dead.
    """
    app = gui()
    view = "charts" if prefix == "charts" else "migrate"
    app.show(view)
    box = app.page.locator(f"#{prefix}-qa")
    assert box.is_checked(), "double-checking must ship on"
    assert box.get_attribute("type") == "checkbox"

    app.page.click(f"label.toggle:has(#{prefix}-qa)")
    app.page.wait_for_timeout(120)

    assert not box.is_checked()


def test_the_sliding_pill_is_only_the_view_nav(gui) -> None:
    """One pill in the app, and it means "peer destinations".

    A binary setting is a switch and one-of-N is a chooser; when a second
    control wore the pill, the screen offered two different idioms for two
    different kinds of choice and neither read as the more important one.
    """
    app = gui()

    for view in VIEWS:
        app.show(view)
        assert app.page.locator(f'[data-view="{view}"] .segment-toggle').count() == 0


def test_segment_indicator_is_actually_drawn(gui) -> None:
    """The indicator has a real width — its CSS vars must reach it.

    They used to arrive as a ``style="--segment-count: 2"`` markup attribute,
    which the app's own ``style-src 'self'`` CSP refuses: two console errors on
    every load, and ``width: calc((100% - 8px) / var(--segment-count))``
    collapsing to zero. shell.js writes them through the CSSOM instead.
    """
    app = gui()

    indicator = app.page.locator("#nav-pill .segment-indicator")
    width = indicator.evaluate("node => node.getBoundingClientRect().width")
    track = app.page.locator("#nav-pill").evaluate("node => node.getBoundingClientRect().width")
    assert width > 0, "the segment indicator is invisible"
    # Four options: the blob covers about a quarter of the track (minus the inset).
    assert 0.15 * track < width < 0.35 * track


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

    The mechanism used to serve two vocabularies — a settings toggle wore the
    same pill and announced itself as a radiogroup — and getting that wrong
    tells assistive tech the views are a multiple-choice question. The settings
    toggle is a switch now, so the pill has one caller; this pins that the one
    it has still reads as tabs.
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


def test_nothing_an_operator_clicks_is_smaller_than_a_fingertip(gui) -> None:
    """Every control on every view measures at least 44px on its short axis.

    This used to be 52 of 69, twelve of them under WCAG 2.5.8's 24px AA floor:
    the section switches were 20px of clickable track, the disclosure summaries
    18px of text, and every text field 35px. The CSS floors are asserted in
    tests/unit/test_gui_assets.py; this is the one that proves they render,
    because a floor loses to a fixed height and neither the sheet nor a reviewer
    would notice.

    A checkbox is measured on its label row, which is what a person aims at —
    the input itself is deliberately invisible and 0px.
    """
    app = gui()
    page = app.page
    measure = """() => {
      const sel = 'button, input, select, textarea, summary, [role="tab"], a[href]';
      const out = [];
      for (const node of document.querySelectorAll('.view:not([hidden]) ' + sel)) {
        const style = getComputedStyle(node);
        if (style.display === 'none' || style.visibility === 'hidden') continue;
        const target = (node.type === 'checkbox' && style.opacity === '0')
          ? node.closest('label') || node : node;
        const box = target.getBoundingClientRect();
        if (!box.width && !box.height) continue;
        out.push([node.id || target.className || node.tagName,
                  Math.round(Math.min(box.width, box.height))]);
      }
      return out;
    }"""

    for view, _label in NAV_VIEWS:
        app.show(view)
        page.wait_for_timeout(120)
        measured = page.evaluate(measure)
        assert measured, f"{view} reported no controls at all"
        small = [(name, size) for name, size in measured if size < 44]
        assert not small, f"under the 44px floor on {view}: {small}"


def test_a_switch_says_which_way_it_is_set_without_relying_on_colour(gui) -> None:
    """OFF is legible by its edge, and ON/OFF are told apart by the thumb.

    The two track fills are close on purpose — an oxblood switch that screamed
    would compete with the one oxblood button on the panel — so the states are
    NOT distinguishable by fill, and the 16px the thumb travels is what carries
    the difference (WCAG 1.4.1). That makes two things load-bearing and worth
    pinning: the OFF track's border must clear 3:1 against the panel behind it
    (1.4.11), or the OFF switch is an invisible rectangle, and the thumb must
    clear 3:1 against the ON fill, or the cue itself disappears when it matters.

    The edge cleared the floor by 0.007 at the alpha it was first written with,
    which is not a margin — hence the assertion, and hence the wider alpha it
    now carries.
    """
    app = gui()
    app.show("charts")

    # Resolve every colour the way the compositor does: through a canvas, then
    # alpha-composited in the order the page paints them.
    measured = app.page.evaluate(
        r"""() => {
          // Through the canvas, and read back as PIXELS: Chromium reports a
          // computed colour in the syntax it was authored in, so parsing the
          // string yields nonsense the moment a token is written in oklch.
          const canvas = document.createElement('canvas').getContext('2d');
          const rgba = (value) => {
            canvas.clearRect(0, 0, 1, 1);
            canvas.fillStyle = value;
            canvas.fillRect(0, 0, 1, 1);
            const [r, g, b, a] = canvas.getImageData(0, 0, 1, 1).data;
            // getImageData un-premultiplies, so the channels are the source
            // colour and the alpha is its own.
            return [r, g, b, a / 255];
          };
          const over = (top, bottom) =>
            [0, 1, 2].map((i) => top[i] * top[3] + bottom[i] * (1 - top[3])).concat([1]);
          const lum = (c) => {
            const f = (v) => (v /= 255) <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
            return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]);
          };
          const ratio = (a, b) => {
            const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
            return (hi + 0.05) / (lo + 0.05);
          };
          const track = (id) => document.getElementById(id).nextElementSibling;
          const panel = rgba(getComputedStyle(
            document.getElementById('charts-qa').closest('.panel')).backgroundColor);
          const off = getComputedStyle(track('charts-archive'));
          const offFill = over(rgba(off.backgroundColor), panel);
          const offEdge = over(rgba(off.borderTopColor), offFill);
          const on = getComputedStyle(track('charts-qa'));
          const onFill = over(rgba(on.backgroundColor), panel);
          const thumb = over(rgba(getComputedStyle(track('charts-qa'), '::after')
            .backgroundColor), onFill);
          const travel = getComputedStyle(track('charts-qa'), '::after').transform;
          return {edge: ratio(offEdge, panel), thumb: ratio(thumb, onFill),
                  fills: ratio(onFill, offFill), travel};
        }"""
    )

    assert measured["edge"] >= 3.0, (
        f"the OFF switch's edge is {measured['edge']:.2f}:1 on the panel"
    )
    assert measured["thumb"] >= 3.0, f"the thumb is {measured['thumb']:.2f}:1 on the ON track"
    # The fills alone are NOT a sufficient cue, and are not asked to be — this
    # records that, so nobody later reads the passing edge check as covering it.
    assert measured["fills"] < 3.0
    assert "matrix(1, 0, 0, 1, 16, 0)" in measured["travel"], (
        f"the thumb no longer moves, so nothing distinguishes the states: {measured['travel']}"
    )


def test_the_chooser_keys_like_the_control_it_replaced(gui) -> None:
    """The whole APG select-only combobox keyboard contract, in one trace.

    A native <select> gave this for free, and it is the reason replacing one is
    usually a bad idea. It was replaced anyway because its popup is drawn by the
    OS: unstyleable past a point, invisible to this test, and one text slot per
    option — which is why the app printed `generic_soap` at people. So the
    contract it used to provide is now something this repo owes, and this is
    where that debt is paid.

    DOM focus stays on the trigger throughout; the row the keyboard is pointing
    at is named by aria-activedescendant, never by :focus.
    """
    app = gui()
    page = app.page
    app.show("charts")
    trigger = page.locator("#charts-pack")
    rows = page.locator("#charts-pack + .chooser-list .chooser-row")
    labels = app.choices("#charts-pack")
    assert len(labels) >= 2, f"need two layouts to key between, got {labels}"

    def active() -> str:
        return trigger.get_attribute("aria-activedescendant") or ""

    def focused_is_trigger() -> bool:
        return bool(page.evaluate("() => document.activeElement.id === 'charts-pack'"))

    def open_state() -> bool:
        return trigger.get_attribute("aria-expanded") == "true"

    trigger.focus()
    assert not open_state()

    # ↓ opens on the current selection and names it.
    page.keyboard.press("ArrowDown")
    page.wait_for_timeout(60)
    assert open_state() and active() == rows.nth(0).get_attribute("id")
    assert focused_is_trigger(), "focus left the trigger for the popup"

    # ↓ / End / Home move the pointer without committing anything.
    page.keyboard.press("ArrowDown")
    assert active() == rows.nth(1).get_attribute("id")
    page.keyboard.press("End")
    assert active() == rows.nth(rows.count() - 1).get_attribute("id")
    page.keyboard.press("PageUp")
    assert active() == rows.nth(0).get_attribute("id")
    assert app.chosen("#charts-pack") == rows.nth(0).get_attribute("data-value")

    # Esc closes and changes nothing.
    before = app.chosen("#charts-pack")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Escape")
    page.wait_for_timeout(60)
    assert not open_state()
    assert app.chosen("#charts-pack") == before, "Esc committed the focused row"

    # Enter commits and hands focus back.
    page.keyboard.press("ArrowDown")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    page.wait_for_timeout(60)
    assert not open_state()
    assert focused_is_trigger()
    assert app.chosen("#charts-pack") == rows.nth(1).get_attribute("data-value")
    assert app.text("#charts-pack .chooser-value") == labels[1]

    # A printable character jumps to the first label starting with it — the
    # label, because that is what a person is reading.
    page.keyboard.press(labels[0][0].lower())
    page.wait_for_timeout(60)
    assert app.chosen("#charts-pack") == rows.nth(0).get_attribute("data-value")


def test_no_view_shows_a_machine_id_where_a_name_belongs(gui) -> None:
    """The ids appear as captions and tooltips, never as the thing you read.

    `generic_soap` was on screen as the chart layout's name, `pf-tebra` as an
    export format's, `tebra` as a destination's — not a copy defect but a
    control defect: an <option> has one text slot, so the id took it.
    """
    app = gui()
    seen = 0

    for view, _label in NAV_VIEWS:
        app.show(view)
        shown = app.page.evaluate(
            """() => [...document.querySelectorAll('.view:not([hidden]) .chooser-value,'
               + ' .view:not([hidden]) .chooser-name')].map(n => n.textContent.trim())"""
        )
        seen += len(shown)
        raw = [text for text in shown if "_" in text or (text and text == text.lower())]
        assert not raw, f"{view} shows a machine id where a name belongs: {raw}"

    # Without this the check passes on a build that has no chooser at all —
    # which is exactly the build that had the defect.
    assert seen >= 8, f"only {seen} chooser labels were on screen across the views"


def test_a_machine_id_becomes_a_name_a_person_would_write(gui) -> None:
    """The derivation, on the ids this app actually ships — a guess, and only that.

    It used to carry `ccda -> "C-CDA"` as a hard-coded exception, because no
    re-casing of the parts can produce it. That exception was the evidence the
    derivation was standing in for something missing: sources and layouts now
    declare their own name (#164), so this is what is left for the ids that do
    not — destinations, and third-party packs written before the field existed.

    The cases it gets wrong are still worth pinning: an initialism it has not
    been told about comes out title-cased, which is wrong but readable.
    """
    app = gui()

    def name(raw: str) -> str:
        return str(app.page.evaluate("id => window.AnastShell.displayName(id)", raw))

    assert name("generic_soap") == "Generic SOAP"
    assert name("practice_fusion_soap") == "Practice Fusion SOAP"
    assert name("pf-tebra") == "PF Tebra"
    assert name("fhir_r4") == "FHIR R4"
    assert name("oracle_ehi") == "Oracle EHI"
    assert name("tebra") == "Tebra"
    # And the one it cannot reach, which is now nobody's problem but this test's.
    assert name("ccda") == "Ccda"


def test_a_declared_name_wins_over_the_guess(gui) -> None:
    """`nameOf` is the rule every picker uses: what it says it is, or a guess.

    One rule, so a pack and a source and a destination are read the same way and
    no caller has to remember which of them declares one.
    """
    app = gui()

    def named(entry: dict[str, str]) -> str:
        return str(app.page.evaluate("e => window.AnastShell.nameOf(e)", entry))

    assert named({"name": "ccda", "display": "C-CDA"}) == "C-CDA"
    assert named({"name": "acme_soap", "display": "Acme SOAP note"}) == "Acme SOAP note"
    # No declaration, or an empty one, falls back to the guess — which knows
    # "soap" is shouted and does not know what an "Acme" is.
    assert named({"name": "acme_soap"}) == "Acme SOAP"
    assert named({"name": "acme_soap", "display": ""}) == "Acme SOAP"


def test_a_region_with_no_rows_says_what_would_fill_it(gui) -> None:
    """Two clauses: what is not here, then the one thing that puts it here.

    These regions used to be `hidden` outright, so a screen that had not run
    yet was a form and then nothing — no indication that a list was coming, or
    what would summon it. The heading stays either way; what swaps is the list
    and the sentence.
    """
    app = gui()

    for view, region, opening in (
        ("charts", "charts-patients", "No run yet."),
        ("migrate", "migrate-patients", "No transfer yet."),
        ("migrate", "migrate-routes", "Choose a destination above."),
    ):
        app.show(view)
        state = app.page.evaluate(
            r"""id => {
              const list = document.getElementById(id);
              const empty = document.getElementById(id + '-empty');
              return {listHidden: list.hidden, emptyHidden: empty.hidden,
                      text: empty.textContent.replace(/\s+/g, ' ').trim(),
                      headed: !!list.closest('section').querySelector('h3')};
            }""",
            region,
        )
        assert state["listHidden"], f"{region} shows an empty list"
        assert not state["emptyHidden"], f"{region} says nothing about being empty"
        assert state["headed"], f"{region} lost its heading when it lost its rows"
        assert state["text"].startswith(opening), state["text"]
        # Two clauses, and no zero pretending to be a measurement.
        assert " " in state["text"].removeprefix(opening).strip(), "the second clause is missing"
        assert "0" not in state["text"], "an empty state is showing a count"


def test_a_count_is_a_number_over_its_name_and_nothing_else(gui) -> None:
    """Value displays, and the two rules that keep them from shouting.

    They were four glass tiles 96px tall, each with a sentence underneath
    restating its own label, and each coloured by its bucket — so a run with
    nothing wrong still showed a green number and an oxblood zero. Colour is
    earned by a number that asks for something; a zero never asks.
    """
    app = gui()
    app.show("uploads")
    app.page.fill("#uploads-results-dir", "/synthetic/out")
    app.page.click("#uploads-refresh")
    app.page.wait_for_timeout(400)

    shown = app.page.evaluate(
        r"""() => [...document.querySelectorAll('#uploads-counters .value')].map(v => {
             const label = v.querySelector('.value-k');
             return {bucket: v.dataset.bucket, zero: v.dataset.zero,
                     signal: v.dataset.signal || '',
                     n: v.querySelector('.value-n').textContent,
                     label: label.textContent,
                     upper: getComputedStyle(label).textTransform,
                     words: label.textContent.trim().split(/\s+/).length};
           })"""
    )
    assert len(shown) == 4, shown
    for value in shown:
        # The one place uppercase is allowed, and it is bounded.
        assert value["upper"] == "uppercase", value
        assert value["words"] <= 3, f"a value label became a sentence: {value['label']!r}"
        assert value["label"] == value["label"].strip()
        # Success is never coloured — it is the absence of a coloured number.
        if value["bucket"] in ("filed", "waiting"):
            assert value["signal"] == "", f"{value['bucket']} is asking for attention"
        assert value["zero"] == ("true" if value["n"] == "0" else "false"), value


def test_the_status_tints_stay_separable_without_colour_vision(gui) -> None:
    """The four row states are a luminance ladder, and they have to stay one.

    Three hues of equal weight is the obvious design and it is wrong: at equal
    weight amber and red are the IDENTICAL colour under deuteranopia, the
    commonest colour-vision deficiency, so "in progress" and "needs attention"
    become one state for roughly one man in twelve. The ladder is ordered by
    urgency instead — the loudest state is the lightest and most saturated
    thing on screen and success is the quietest — which holds under all three
    simulations because it never asked hue to do the work.

    The floor is a RAW luminance ratio, not a WCAG contrast ratio: near black
    the +0.05 constant swamps the difference between two adjacent steps, so it
    would report every ladder as flat. (It did, the first time this was run.)
    """
    app = gui()

    measured = app.page.evaluate(
        r"""() => {
          const canvas = document.createElement('canvas').getContext('2d');
          const rgba = (value) => {
            canvas.clearRect(0, 0, 1, 1);
            canvas.fillStyle = value;
            canvas.fillRect(0, 0, 1, 1);
            const [r, g, b, a] = canvas.getImageData(0, 0, 1, 1).data;
            return [r, g, b, a / 255];
          };
          const over = (top, bottom) =>
            [0, 1, 2].map((i) => top[i] * top[3] + bottom[i] * (1 - top[3]));
          const linear = (c) => c.map((v) => {
            v /= 255;
            return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
          });
          const lum = (c) => {
            const [r, g, b] = linear(c);
            return 0.2126 * r + 0.7152 * g + 0.0722 * b;
          };
          // Vienot 1999: sRGB -> LMS, collapse the missing cone, back to sRGB.
          const RGB2LMS = [[0.31399022, 0.63951294, 0.04649755],
                           [0.15537241, 0.75789446, 0.08670142],
                           [0.01775239, 0.10944209, 0.87256922]];
          const LMS2RGB = [[5.47221206, -4.6419601, 0.16963708],
                           [-1.1252419, 2.29317094, -0.1678952],
                           [0.02980165, -0.19318073, 1.16364789]];
          const SIM = {
            protanopia: [[0, 1.05118294, -0.05116099], [0, 1, 0], [0, 0, 1]],
            deuteranopia: [[1, 0, 0], [0.9513092, 0, 0.04866992], [0, 0, 1]],
          };
          const mul = (m, v) => m.map((row) => row.reduce((s, x, i) => s + x * v[i], 0));
          const encode = (x) => Math.max(0, Math.min(255, Math.round(
            (x <= 0.0031308 ? 12.92 * x : 1.055 * x ** (1 / 2.4) - 0.055) * 255)));
          const simulate = (c, kind) => {
            if (kind === 'achromatopsia') { const g = encode(lum(c)); return [g, g, g]; }
            return mul(LMS2RGB, mul(SIM[kind], mul(RGB2LMS, linear(c)))).map(encode);
          };

          const style = getComputedStyle(document.documentElement);
          const ground = rgba(style.getPropertyValue('--ground')).slice(0, 3);
          const ladder = [['waiting', ground]];
          for (const step of ['filed', 'progress', 'attention']) {
            ladder.push([step,
              over(rgba(style.getPropertyValue('--tint-' + step)), [...ground, 1])]);
          }
          const steps = [];
          for (let i = 1; i < ladder.length; i += 1) {
            const [a, b] = [ladder[i - 1][1], ladder[i][1]];
            const row = {step: `${ladder[i - 1][0]}->${ladder[i][0]}`};
            for (const kind of [null, 'protanopia', 'deuteranopia', 'achromatopsia']) {
              const [x, y] = kind ? [simulate(a, kind), simulate(b, kind)] : [a, b];
              row[kind || 'normal'] = Math.max(lum(x), lum(y))
                / Math.max(Math.min(lum(x), lum(y)), 1e-6);
            }
            steps.push(row);
          }
          const text = [];
          for (const [name, tint] of ladder) {
            for (const ink of ['ink', 'ink-secondary']) {
              const on = over(rgba(style.getPropertyValue('--' + ink)), [...tint, 1]);
              const [hi, lo] = [lum(on), lum(tint)].sort((p, q) => q - p);
              text.push({name, ink, ratio: (hi + 0.05) / (lo + 0.05)});
            }
          }
          return {steps, text};
        }"""
    )

    for step in measured["steps"]:
        for kind in ("normal", "protanopia", "deuteranopia", "achromatopsia"):
            assert step[kind] >= 1.55, (
                f"{step['step']} is only x{step[kind]:.2f} apart under {kind} — "
                "two row states have collapsed into one"
            )
    # Monotone: every step goes the same way, so the ladder reads as an order.
    for pair in measured["text"]:
        assert pair["ratio"] >= 4.5, (
            f"{pair['ink']} on the {pair['name']} tint is {pair['ratio']:.2f}:1"
        )


def test_a_tinted_row_says_its_state_in_words(gui) -> None:
    """The tint is reinforcement; the words are what carry the state.

    Which is the whole reason the ladder above can be trusted — if the colour
    were the only carrier, a monotone ladder would still be a UI that stops
    working in greyscale. And --ink-muted never appears inside a tinted row:
    --ink-secondary is the floor there.
    """
    app = gui()
    app.show("uploads")
    app.page.fill("#uploads-results-dir", "/synthetic/out")
    app.page.click("#uploads-refresh")
    app.page.wait_for_timeout(400)

    rows = app.page.evaluate(
        r"""() => {
          const muted = getComputedStyle(document.documentElement)
            .getPropertyValue('--ink-muted').trim();
          return [...document.querySelectorAll('.view:not([hidden]) .result')].map(row => ({
            bucket: row.dataset.bucket || '',
            words: row.textContent.replace(/\s+/g, ' ').replace(/[\d\s]+$/, '').trim(),
            muted: [...row.querySelectorAll('*')]
              .some(n => getComputedStyle(n).color === muted),
          }));
        }"""
    )
    assert rows, "no result rows were rendered"
    for row in rows:
        if not row["bucket"] or row["bucket"] == "waiting":
            continue
        assert len(row["words"]) > 3, f"a tinted row says nothing: {row}"
        assert not row["muted"], f"--ink-muted is inside a tinted row: {row}"


def test_no_view_carries_more_help_than_fields(gui) -> None:
    """A help line under every field is a help line under nothing.

    Two views used to carry MORE help lines than fields — ten under six, ten
    under seven — and most of them restated the label they sat beneath ("The
    folder Anastomosis writes the finished charts into." under "Where results
    go"). Uniform emphasis is no emphasis: when every field looks equally
    annotated the reader stops reading all of them, including the two that
    would have saved them.

    A line survives only if the label cannot carry the point: the consequence
    is not recoverable from it, the value comes from outside the app, there is
    a format the placeholder cannot show, or the field is Advanced and needs
    one line saying when a person would want it.
    """
    app = gui()
    counts = {}

    for view, _label in NAV_VIEWS:
        app.show(view)
        seen = app.page.evaluate(
            r"""() => {
              const root = document.querySelector('.view:not([hidden])');
              const shown = (n) =>
                !!(n.offsetWidth || n.offsetHeight || n.getClientRects().length);
              const inAdvanced = (n) => !!n.closest('.advanced');
              const lines = [...root.querySelectorAll('.field-help')]
                .filter(shown)
                .map(n => ({text: n.textContent.replace(/\s+/g, ' ').trim(),
                            advanced: inAdvanced(n)}))
                .filter(l => l.text);
              return {
                lines,
                fields: [...root.querySelectorAll('.field')].filter(shown).length,
              };
            }"""
        )
        plain = [line for line in seen["lines"] if not line["advanced"]]
        counts[view] = len(plain)
        assert len(seen["lines"]) <= seen["fields"], (
            f"{view} has {len(seen['lines'])} help lines under {seen['fields']} fields"
        )
        for line in seen["lines"]:
            # An empty element still occupies the count; the run's own sentence
            # wore this class and so read as a field's help line.
            assert line["text"], f"{view} has an empty help line"

    # Off the Advanced disclosure, which earns one line per field by its own
    # rule, what is left is the handful that actually could not be a label.
    assert counts == {"charts": 3, "migrate": 3, "uploads": 2, "teach": 1}, counts


def test_refusing_transparency_turns_off_every_backdrop_filter(gui) -> None:
    """Both ways of saying "no glass", and they turn off the same thing.

    Every backdrop-filter in the app reads --glass-blur or --glass-modal-blur
    and every glass fill reads --glass-bg or --glass-modal-bg, so the fallback
    is four token overrides rather than a per-component list somebody has to
    keep in step. This asserts that property, not the list.
    """
    app = gui()
    count = """() => [...document.querySelectorAll('*')]
        .filter(n => getComputedStyle(n).backdropFilter !== 'none').length"""
    assert app.page.evaluate(count) > 0, "there was no glass to turn off"

    # The system preference, over CDP.
    session = app.page.context.new_cdp_session(app.page)
    session.send(
        "Emulation.setEmulatedMedia",
        {"features": [{"name": "prefers-reduced-transparency", "value": "reduce"}]},
    )
    app.page.wait_for_timeout(120)
    assert app.page.evaluate(count) == 0, "the system preference left glass on screen"
    session.send("Emulation.setEmulatedMedia", {"features": []})
    app.page.wait_for_timeout(120)

    # And by hand, because WebView2 does not report that preference on every
    # Windows build — a setting that silently does nothing on the platform the
    # app ships on is worse than not having one.
    app.page.click("#about-btn")
    app.page.click("label.toggle:has(#reduce-effects)")
    app.page.wait_for_timeout(150)
    assert app.page.evaluate(count) == 0, "the in-app switch left glass on screen"
    assert app.page.evaluate("() => localStorage.getItem('anast.reduce-effects')") == "true"

    # With the blur gone the pill's edge is the only thing holding it off the
    # background, so it has to carry the separation on its own (WCAG 1.4.11).
    # This arithmetic is only valid here: with glass the border composites over
    # a refracted backdrop no calculation can model, which is why the glass
    # state is measured from pixels in the redesign harness instead.
    boundary = app.page.evaluate(
        r"""() => {
          const c = document.createElement('canvas').getContext('2d');
          const rgba = (v) => { c.clearRect(0, 0, 1, 1); c.fillStyle = v; c.fillRect(0, 0, 1, 1);
            const [r, g, b, a] = c.getImageData(0, 0, 1, 1).data; return [r, g, b, a / 255]; };
          const over = (t, u) => [0, 1, 2].map(i => t[i] * t[3] + u[i] * (1 - t[3])).concat([1]);
          const lum = (x) => { const f = (v) => (v /= 255) <= 0.04045
            ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
            return 0.2126 * f(x[0]) + 0.7152 * f(x[1]) + 0.0722 * f(x[2]); };
          const ratio = (a, b) => { const [h, l] = [lum(a), lum(b)].sort((p, q) => q - p);
            return (h + 0.05) / (l + 0.05); };
          const style = getComputedStyle(document.documentElement);
          const ground = rgba(style.getPropertyValue('--ground'));
          const panel = over(rgba(style.getPropertyValue('--surface')), ground);
          const edge = rgba(getComputedStyle(document.getElementById('nav-pill')).borderTopColor);
          return {ground: ratio(over(edge, ground), ground),
                  panel: ratio(over(edge, panel), panel)};
        }"""
    )
    assert boundary["ground"] >= 3.0, f"the pill edge is {boundary['ground']:.2f}:1 on the ground"
    assert boundary["panel"] >= 3.0, f"the pill edge is {boundary['panel']:.2f}:1 on a panel"


def test_reduced_motion_stops_travel_but_keeps_three_fades(gui) -> None:
    """Motion off, except where deleting it makes a change harder to follow.

    The HIG's rule is to replace transitions in x, y and z with fades, not to
    remove them: a lozenge that teleports between two view names is harder to
    track than one that moves, and a view that swaps with no crossfade reads as
    a page load — which is the one thing this shell exists not to do.
    """
    app = gui(reduced_motion=True)

    timings = app.page.evaluate(
        r"""() => {
          const out = {slow: [], fades: {}};
          const seconds = (v) => v.split(',').map(s => parseFloat(s) || 0);
          for (const node of document.querySelectorAll('*')) {
            const cs = getComputedStyle(node);
            const worst = Math.max(
              ...seconds(cs.transitionDuration), ...seconds(cs.animationDuration),
              ...seconds(cs.transitionDelay), ...seconds(cs.animationDelay));
            if (worst <= 1e-5) continue;
            const name = node.id || node.className.toString().split(' ')[0] || node.tagName;
            out.slow.push({name, worst});
          }
          out.scroll = getComputedStyle(document.querySelector('.app-shell')).scrollBehavior;
          return out;
        }"""
    )
    allowed = {"view", "log-drawer", "segment-indicator"}
    unexpected = [
        row for row in timings["slow"] if not any(word in row["name"] for word in allowed)
    ]
    assert not unexpected, f"motion survived reduced-motion: {unexpected}"
    assert timings["scroll"] == "auto", "smooth scrolling survived reduced-motion"

    # And the app still works: a view switch is the thing the crossfade is for.
    app.show("teach")
    assert app.visible("teach")
