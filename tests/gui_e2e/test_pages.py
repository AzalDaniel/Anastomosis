"""Cross-page invariants: what every shipped page owes the operator.

The per-page files walk each workspace's own flow; these are the rules that hold
everywhere, and each one is here because breaking it produced a real defect: a
panel that ships ``hidden`` but renders anyway, a workspace the nav cannot
reach, and the gooey segment toggle that looked alive and was not.
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright", reason="the GUI lane needs playwright + chromium")

from expectations import NAV_LINKS, PAGES

pytestmark = pytest.mark.gui_e2e

# The pages that host the QA segment toggle (the dashboard and the wizard).
_SEGMENT_PAGES = ("index.html", "wizard.html")


@pytest.mark.parametrize("page_name", PAGES)
def test_hidden_panels_are_actually_hidden(gui, page_name: str) -> None:
    """A panel marked ``hidden`` must not render before it has content.

    The attribute is expressed by a UA-stylesheet rule that ANY author
    ``display`` declaration outranks, and the panels are all flex/grid
    components — so the console once opened on a full grid of zero counters and
    the learn wizards on empty result scaffolds.
    """
    page = gui(page_name).page

    rendered = page.evaluate(
        """() => Array.from(document.querySelectorAll('[hidden]'))
              .filter(node => getComputedStyle(node).display !== 'none')
              .map(node => node.id || node.className)"""
    )
    assert rendered == [], f"{page_name} renders elements it marked hidden: {rendered}"


@pytest.mark.parametrize(("href", "label"), NAV_LINKS[1:])
def test_workspace_nav_reaches_every_page(gui, href: str, label: str) -> None:
    """Every nav link opens its workspace, live, from the dashboard."""
    dashboard = gui("index.html")
    page = dashboard.page

    page.click(f'nav.nav a.nav-link[href="{href}"]')
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(150)

    assert page.url.endswith(href)
    # The workspace came up live on the other side of the navigation.
    assert "show" not in (page.locator("#no-api").get_attribute("class") or "")
    assert page.locator("#version").text_content() != "—"
    current = page.locator(f'nav.nav a.nav-link[href="{href}"]').get_attribute("aria-current")
    assert current == "page", f"{label} does not mark itself as the current workspace"


@pytest.mark.parametrize("page_name", _SEGMENT_PAGES)
def test_qa_segment_toggle_answers_a_real_mouse_click(gui, page_name: str) -> None:
    """Clicking a segment option selects it — with a real pointer, not a script.

    The toggle takes pointer capture on ``pointerdown`` to drive its drag
    physics, and the browser then retargets the following ``click`` to the
    capture element — so the per-option click handler never fired for a real
    press and the control was mouse-dead (keyboard and drag still worked, which
    is exactly why it read as fine).
    """
    page = gui(page_name).page
    toggle = page.locator('.segment-toggle[data-name="qa"]')
    assert toggle.get_attribute("data-value") == "on"

    page.click('.segment-option[data-value="off"]')
    page.wait_for_timeout(100)

    assert toggle.get_attribute("data-value") == "off"
    assert page.evaluate("window.AnastShell.segmentValue('qa', 'on')") == "off"
    assert page.locator('.segment-option[data-value="off"]').get_attribute("aria-pressed") == "true"

    page.click('.segment-option[data-value="on"]')
    page.wait_for_timeout(100)
    assert toggle.get_attribute("data-value") == "on"


@pytest.mark.parametrize("page_name", _SEGMENT_PAGES)
def test_segment_indicator_is_actually_drawn(gui, page_name: str) -> None:
    """The coral indicator has a real width — its CSS vars must reach it.

    They used to arrive as a ``style="--segment-count: 2"`` markup attribute,
    which the page's own ``style-src 'self'`` CSP refuses: two console errors on
    every load, and ``width: calc((100% - 8px) / var(--segment-count))``
    collapsing to zero. shell.js writes them through the CSSOM instead.
    """
    page = gui(page_name).page

    indicator = page.locator(".segment-indicator").first
    width = indicator.evaluate("node => node.getBoundingClientRect().width")
    toggle_width = page.locator('.segment-toggle[data-name="qa"]').evaluate(
        "node => node.getBoundingClientRect().width"
    )
    assert width > 0, "the segment indicator is invisible"
    # Two options: the blob covers about half the track (minus the 4px inset).
    assert 0.3 * toggle_width < width < 0.7 * toggle_width
