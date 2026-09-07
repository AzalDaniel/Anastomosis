"""What a live Anastomosis window must look like — asserted by BOTH
lanes: ``tests/gui_e2e`` drives the bundled app in headless Chromium
behind a stubbed bridge, and ``packaging/smoke_windows.py`` drives the
INSTALLED Windows app in its real WebView2 window over CDP, both
holding a Playwright ``Page`` so these expectations live here once.

:func:`check_dashboard` keeps its name/signature since lane 2 loads
this module by path and calls exactly that. Deliberately
dependency-free: stdlib plus a duck-typed ``page``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from playwright.sync_api import Page

__all__ = [
    "CHARTS_HEADING",
    "NAV_VIEWS",
    "RUN_BUTTON_LABEL",
    "VERSION_PLACEHOLDER",
    "VIEWS",
    "WINDOW_TITLE",
    "check_dashboard",
]

#: The product name appears exactly ONCE, on the window itself (§10.7).
WINDOW_TITLE = "Anastomosis"

#: The first view's heading.
CHARTS_HEADING = "Charts"

#: The view nav, in order: (view name, button label).
NAV_VIEWS: tuple[tuple[str, str], ...] = (
    ("charts", "Charts"),
    ("migrate", "Migrate"),
    ("uploads", "Uploads"),
    ("teach", "Teach"),
)

#: The four view sections the single document ships.
VIEWS: tuple[str, ...] = tuple(name for name, _label in NAV_VIEWS)

#: The primary action of the Charts view.
RUN_BUTTON_LABEL = "Rebuild charts"

#: What ``#about-version``'s ``data-version`` reads before ``info()`` answers.
#: Seeing it after load means the bridge never round-tripped (in the packaged
#: app: a dead GUI). The About line's visible text carries no dangling dash —
#: the ATTRIBUTE is the machine-readable liveness signal.
VERSION_PLACEHOLDER = ""


def _text(page: Page, selector: str) -> str:
    """The trimmed text of the first match, or "" when the node is missing."""
    node: Any = page.locator(selector).first
    if node.count() == 0:
        return ""
    return (node.text_content() or "").strip()


def _attr(page: Page, selector: str, name: str) -> str:
    node: Any = page.locator(selector).first
    if node.count() == 0:
        return ""
    return node.get_attribute(name) or ""


def check_dashboard(page: Page) -> list[str]:
    """Every first-paint expectation, as a list of failures (empty = good).

    Returns human-readable problems rather than raising, so lane 2 can print one
    PASS/FAIL line per finding and still report the rest.
    """
    problems: list[str] = []

    if page.title() != WINDOW_TITLE:
        problems.append(f"window title reads {page.title()!r}, expected {WINDOW_TITLE!r}")

    heading = _text(page, '[data-view="charts"] .view-head h1')
    if heading != CHARTS_HEADING:
        problems.append(f"charts heading reads {heading!r}, expected {CHARTS_HEADING!r}")

    # --- the four views, and only the first one showing ----------------------
    buttons = page.locator(".navpill [data-view-target]")
    if buttons.count() != len(NAV_VIEWS):
        problems.append(f"nav has {buttons.count()} views, expected {len(NAV_VIEWS)}")
    else:
        for index, (name, label) in enumerate(NAV_VIEWS):
            button = buttons.nth(index)
            actual_name = button.get_attribute("data-view-target") or ""
            actual_label = (button.text_content() or "").strip()
            if actual_name != name or actual_label != label:
                problems.append(
                    f"nav[{index}] is ({actual_label!r} -> {actual_name!r}), "
                    f"expected ({label!r} -> {name!r})"
                )
        if buttons.nth(0).get_attribute("aria-selected") != "true":
            problems.append("the Charts tab does not mark itself selected")

    for name in VIEWS:
        section = page.locator(f'[data-view="{name}"]')
        if section.count() != 1:
            problems.append(f"the {name!r} view section is missing")
            continue
        hidden = section.first.get_attribute("hidden") is not None
        if name == "charts" and hidden:
            problems.append("the Charts view is hidden on first paint")
        if name != "charts" and not hidden:
            problems.append(f"the {name!r} view is showing before it was asked for")

    # --- the live signals: the bridge answered and the app came alive --------
    version = _attr(page, "#about-version", "data-version")
    if version == VERSION_PLACEHOLDER:
        problems.append(
            "#about-version carries no data-version — info() never reached the page "
            "(the bridge did not attach, or the app never retried after it did)"
        )
    if _attr(page, "html", "data-bridge") != "live":
        problems.append("the document does not report a live bridge")

    no_api_class = _attr(page, "#no-api", "class")
    if "show" in no_api_class.split():
        problems.append(
            "the 'desktop app is not connected' notice is showing even though the "
            "bridge is live — the app bootstrapped before pywebview attached and "
            "never recovered"
        )

    run_button = page.locator("#charts-run").first
    if run_button.count() == 0:
        problems.append("#charts-run is missing")
    else:
        if run_button.is_disabled():
            problems.append("#charts-run is disabled with a live bridge")
        label = (run_button.text_content() or "").strip()
        if label != RUN_BUTTON_LABEL:
            problems.append(f"#charts-run reads {label!r}, expected {RUN_BUTTON_LABEL!r}")

    if page.locator("#charts-pack + .chooser-list .chooser-row").count() < 1:
        problems.append("the chart-layout picker is empty — info().packs never rendered")

    sources = page.locator("#charts-source + .chooser-list .chooser-name").all_text_contents()
    if not sources or sources[0].strip() != "Detect":
        problems.append(f"the export-format picker does not lead with 'Detect' (got {sources!r})")

    if not _text(page, "#log-strip-msg"):
        problems.append("the activity strip says nothing at all")

    return problems
