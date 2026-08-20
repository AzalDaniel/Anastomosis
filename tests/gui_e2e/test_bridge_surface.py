"""The bridge seam itself: surface drift, the late attach, the no-bridge preview.

These are the tests that keep the rest of the lane honest. If ``GuiApi`` gains,
loses, or renames a method, the stub's surface and the canned fixtures must move
with it — otherwise every page test would keep passing against an api the app no
longer has. And the two lifecycle cases (the bridge landing late, the bridge
never landing at all) are where the pages have historically been wrong.
"""

from __future__ import annotations

import re

import pytest

pytest.importorskip("playwright", reason="the GUI lane needs playwright + chromium")

from expectations import PAGES
from stub import BRIDGE_LATE, BRIDGE_NONE, api_surface, canned_returns

from anastomosis.gui.shell import _WEB_DIR

pytestmark = pytest.mark.gui_e2e

# Every `window.pywebview.api.<name>` the shipped page scripts call.
_API_CALL_RE = re.compile(r"pywebview\.api\.([a-z_][a-z0-9_]*)")

_PAGE_SCRIPTS = ("app.js", "wizard.js", "console.js", "packgen.js", "source.js", "shell.js")


def test_canned_fixture_covers_the_whole_api_surface() -> None:
    """The stub answers exactly the real ``GuiApi`` methods — no more, no less.

    A renamed or removed API method leaves a canned key with nothing behind it;
    a NEW method arrives with no fixture and no test. Either way this fails
    loudly here rather than letting the lane pass against a stale seam.
    """
    assert sorted(canned_returns()) == api_surface()


def test_every_bridge_call_in_the_shipped_js_exists_on_the_api() -> None:
    """No page calls a controller method the js_api facade does not expose.

    ``window.pywebview.api.foo()`` against a missing ``foo`` is a TypeError in
    the live app — a dead button with a console error. This catches the drift
    statically, across every shipped script at once.
    """
    surface = set(api_surface())
    unknown: dict[str, set[str]] = {}
    for script in _PAGE_SCRIPTS:
        called = set(_API_CALL_RE.findall((_WEB_DIR / script).read_text(encoding="utf-8")))
        missing = called - surface
        if missing:
            unknown[script] = missing
    assert not unknown, f"pages call bridge methods GuiApi does not expose: {unknown}"


def test_stub_installs_the_generated_surface_in_the_browser(gui) -> None:
    """End-to-end: the injected object really carries every generated method."""
    dashboard = gui("index.html")
    exposed = dashboard.page.evaluate("Object.keys(window.pywebview.api).sort()")
    assert exposed == api_surface()


@pytest.mark.parametrize("page_name", PAGES)
def test_page_recovers_when_the_bridge_attaches_late(gui, page_name: str) -> None:
    """A pywebview attach that lands AFTER DOM ready must still wake the page.

    pywebview installs ``window.pywebview.api`` asynchronously and announces it
    with ``pywebviewready``, so a page that probes for the bridge only at
    DOMContentLoaded can lose the race. It used to lose it permanently: the
    "this runs inside the desktop app" notice stayed up, the version stayed at
    the placeholder, and on the dashboard the run button stayed dead — for the
    whole session. Every page must re-bootstrap when the event lands.
    """
    page = gui(page_name, bridge=BRIDGE_LATE)

    # Before the attach: the honest offline state.
    assert "show" in (page.page.locator("#no-api").get_attribute("class") or "")
    assert page.page.locator("#version").text_content() == "—"

    page.attach_bridge()

    assert "show" not in (page.page.locator("#no-api").get_attribute("class") or "")
    assert page.page.locator("#version").text_content() != "—"
    assert page.called("info"), f"{page_name} never fetched info() after the late attach"


@pytest.mark.parametrize("page_name", PAGES)
def test_page_degrades_cleanly_with_no_bridge_at_all(gui, page_name: str) -> None:
    """Opened in a plain browser, every page explains itself and stays quiet.

    No fake data, no thrown exceptions, no half-live controls — the notice and
    an ``offline`` status, which is exactly what the pages promise.
    """
    page = gui(page_name, bridge=BRIDGE_NONE)

    assert "show" in (page.page.locator("#no-api").get_attribute("class") or "")
    assert page.page.locator("#status-text").text_content() == "offline"
    assert page.page.evaluate("typeof window.pywebview") == "undefined"


def test_offline_dashboard_does_not_claim_a_run_is_in_flight(gui) -> None:
    """The api-less dashboard disables its run button WITHOUT mislabelling it.

    The button used to be parked through the busy path, so a plain browser
    showed "running…" for a run that did not exist and never could.
    """
    dashboard = gui("index.html", bridge=BRIDGE_NONE)

    run_button = dashboard.page.locator("#run-btn")
    assert run_button.is_disabled()
    assert (run_button.text_content() or "").strip() == "run pipeline"
