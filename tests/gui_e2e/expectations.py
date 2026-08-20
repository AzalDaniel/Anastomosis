"""What a live Anastomosis dashboard must look like — asserted by BOTH lanes.

Lane 1 (``tests/gui_e2e``) drives the bundled pages in headless Chromium behind
a stubbed bridge; lane 2 (``packaging/smoke_windows.py``) drives the INSTALLED
Windows app in its real WebView2 window over CDP. Both end up holding a
Playwright ``Page``, so the DOM expectations live here once and are checked in
both places: a selector this file names cannot rot in the installed app without
lane 1 — which runs on every CI push — going red first.

Everything here must hold with the REAL controller behind the bridge, not just
the stub, so the checks stay to (a) static markup the pages ship and (b) the
handful of live signals that prove the bridge round-tripped: the version filled
in, the "no bridge" notice cleared, the run button armed, the pickers populated.

Deliberately dependency-free (stdlib + a duck-typed ``page``): lane 2 loads this
module by path from a checkout, with no pytest and no package install.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from playwright.sync_api import Page

__all__ = [
    "DASHBOARD_HEADING",
    "NAV_LINKS",
    "PAGES",
    "TITLE_BAND",
    "VERSION_PLACEHOLDER",
    "check_dashboard",
]

#: The frameless title band's text (the window's own identity strip).
TITLE_BAND = "Anastomosis"

#: The dashboard's page heading.
DASHBOARD_HEADING = "Anastomosis"

#: The workspace nav, in order: (href, link label). Every page ships this same
#: nav, so a renamed page file breaks the check on whichever page dropped it.
NAV_LINKS: tuple[tuple[str, str], ...] = (
    ("index.html", "Dashboard"),
    ("wizard.html", "Migration wizard"),
    ("console.html", "Upload console"),
    ("packgen.html", "Pack from samples"),
    ("source.html", "Learn a source"),
)

#: The bundled page files, in nav order.
PAGES: tuple[str, ...] = tuple(href for href, _label in NAV_LINKS)

#: What ``#version`` reads before ``info()`` answers. Seeing it after load means
#: the bridge never round-tripped (in the packaged app: a dead GUI).
VERSION_PLACEHOLDER = "—"


def _text(page: Page, selector: str) -> str:
    """The trimmed text of the first match, or "" when the node is missing."""
    node: Any = page.locator(selector).first
    if node.count() == 0:
        return ""
    return (node.text_content() or "").strip()


def check_dashboard(page: Page) -> list[str]:
    """Every dashboard expectation, as a list of failures (empty list = good).

    Returns human-readable problems rather than raising, so lane 2 can print one
    PASS/FAIL line per finding and still report the rest.
    """
    problems: list[str] = []

    band = _text(page, ".title-bar .title-text")
    if band != TITLE_BAND:
        problems.append(f"title band reads {band!r}, expected {TITLE_BAND!r}")

    heading = _text(page, "main.app-shell h1")
    if heading != DASHBOARD_HEADING:
        problems.append(f"page heading reads {heading!r}, expected {DASHBOARD_HEADING!r}")

    links = page.locator("nav.nav a.nav-link")
    if links.count() != len(NAV_LINKS):
        problems.append(f"nav has {links.count()} links, expected {len(NAV_LINKS)}")
    else:
        for index, (href, label) in enumerate(NAV_LINKS):
            link = links.nth(index)
            actual_href = link.get_attribute("href") or ""
            actual_label = (link.text_content() or "").strip()
            if actual_href != href or actual_label != label:
                problems.append(
                    f"nav[{index}] is ({actual_label!r} -> {actual_href!r}), "
                    f"expected ({label!r} -> {href!r})"
                )
        current = links.nth(0).get_attribute("aria-current")
        if current != "page":
            problems.append(f"dashboard nav link aria-current is {current!r}, expected 'page'")

    # --- the live signals: the bridge answered and the page came alive -------
    version = _text(page, "#version")
    if not version or version == VERSION_PLACEHOLDER:
        problems.append(
            f"#version still reads {version!r} — info() never reached the page "
            "(the bridge did not attach, or the page never retried after it did)"
        )

    no_api_class = page.locator("#no-api").first.get_attribute("class") or ""
    if "show" in no_api_class.split():
        problems.append(
            "the 'runs inside the desktop app' notice is showing even though the "
            "bridge is live — the page bootstrapped before pywebview attached and "
            "never recovered"
        )

    run_button = page.locator("#run-btn").first
    if run_button.count() == 0:
        problems.append("#run-btn is missing")
    else:
        if run_button.is_disabled():
            problems.append("#run-btn is disabled with a live bridge")
        label = (run_button.text_content() or "").strip()
        if label != "run pipeline":
            problems.append(f"#run-btn reads {label!r}, expected 'run pipeline'")

    packs = page.locator("#pack option").count()
    if packs < 1:
        problems.append("the template-pack picker is empty — info().packs never rendered")

    sources = page.locator("#source option").all_text_contents()
    if not sources or sources[0].strip() != "auto-detect":
        problems.append(f"the source picker does not lead with 'auto-detect' (got {sources!r})")

    status = _text(page, "#status-text")
    if status != "ready":
        problems.append(f"status bar reads {status!r}, expected 'ready'")

    return problems
