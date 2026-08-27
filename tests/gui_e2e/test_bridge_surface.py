"""The bridge seam itself: surface drift, the late attach, the no-bridge preview.

These are the tests that keep the rest of the lane honest. If ``GuiApi`` gains,
loses, or renames a method, the stub's surface and the canned fixtures must move
with it — otherwise every view test would keep passing against an api the app no
longer has. And the two lifecycle cases (the bridge landing late, the bridge
never landing at all) are where the GUI has historically been wrong.

The single document attaches the bridge ONCE, in shell.js, so the late-attach
race is now run once instead of five times — and every view has to come alive
off that one bootstrap.
"""

from __future__ import annotations

import re

import pytest

pytest.importorskip("playwright", reason="the GUI lane needs playwright + chromium")

from expectations import VIEWS
from stub import BRIDGE_LATE, BRIDGE_NONE, api_surface, canned_returns

from anastomosis.gui.shell import _WEB_DIR

pytestmark = pytest.mark.gui_e2e

# Every `window.pywebview.api.<name>` the shipped scripts call.
_API_CALL_RE = re.compile(r"pywebview\.api\.([a-z_][a-z0-9_]*)")

#: The shipped scripts: the shell plus one per view (Teach hosts two modes).
_SCRIPTS = ("shell.js", "app.js", "wizard.js", "console.js", "packgen.js", "source.js")


def test_canned_fixture_covers_the_whole_api_surface() -> None:
    """The stub answers exactly the real ``GuiApi`` methods — no more, no less.

    A renamed or removed API method leaves a canned key with nothing behind it;
    a NEW method arrives with no fixture and no test. Either way this fails
    loudly here rather than letting the lane pass against a stale seam.
    """
    assert sorted(canned_returns()) == api_surface()


def test_every_bridge_call_in_the_shipped_js_exists_on_the_api() -> None:
    """No view calls a controller method the js_api facade does not expose.

    ``window.pywebview.api.foo()`` against a missing ``foo`` is a TypeError in
    the live app — a dead button with a console error. This catches the drift
    statically, across every shipped script at once.
    """
    surface = set(api_surface())
    unknown: dict[str, set[str]] = {}
    for script in _SCRIPTS:
        called = set(_API_CALL_RE.findall((_WEB_DIR / script).read_text(encoding="utf-8")))
        missing = called - surface
        if missing:
            unknown[script] = missing
    assert not unknown, f"views call bridge methods GuiApi does not expose: {unknown}"


def test_stub_installs_the_generated_surface_in_the_browser(gui) -> None:
    """End-to-end: the injected object really carries every generated method."""
    app = gui()
    exposed = app.page.evaluate("Object.keys(window.pywebview.api).sort()")
    assert exposed == api_surface()


def test_the_app_recovers_when_the_bridge_attaches_late(gui) -> None:
    """A pywebview attach that lands AFTER DOM ready must still wake the app.

    pywebview installs ``window.pywebview.api`` asynchronously and announces it
    with ``pywebviewready``, so an app that probes for the bridge only at
    DOMContentLoaded can lose the race. It used to lose it permanently, on every
    page: the "runs inside the desktop app" notice stayed up, the version stayed
    at its placeholder, and the run button stayed dead — for the whole session.
    One bootstrap now re-runs for the whole document.
    """
    app = gui(bridge=BRIDGE_LATE)
    page = app.page

    # Before the attach: the honest offline state.
    assert "show" in (page.locator("#no-api").get_attribute("class") or "")
    assert page.evaluate("document.documentElement.dataset.bridge") == "offline"
    assert page.locator("#about-version").get_attribute("data-version") == ""
    assert page.locator("#charts-run").is_disabled()

    app.attach_bridge()

    assert "show" not in (page.locator("#no-api").get_attribute("class") or "")
    assert page.evaluate("document.documentElement.dataset.bridge") == "live"
    assert page.locator("#about-version").get_attribute("data-version")
    assert app.called("info"), "the app never fetched info() after the late attach"
    assert not page.locator("#charts-run").is_disabled()
    # And the views that hydrate from the bridge came alive with it.
    assert page.locator("#charts-pack option").count() > 0
    app.show("migrate")
    assert page.locator("#migrate-destination option").count() > 0


@pytest.mark.parametrize("view", VIEWS)
def test_every_view_degrades_cleanly_with_no_bridge_at_all(gui, view: str) -> None:
    """Opened in a plain browser, the app explains itself and stays quiet.

    No fake data, no thrown exceptions, no half-live controls — the notice and
    an offline document, which is exactly what the app promises.
    """
    app = gui(bridge=BRIDGE_NONE)
    app.show(view)

    assert "show" in (app.page.locator("#no-api").get_attribute("class") or "")
    assert app.page.evaluate("document.documentElement.dataset.bridge") == "offline"
    assert app.page.evaluate("typeof window.pywebview") == "undefined"


def test_offline_charts_does_not_claim_a_run_is_in_flight(gui) -> None:
    """The api-less run button is disabled WITHOUT mislabelling itself.

    The button used to be parked through the busy path, so a plain browser
    showed "running…" for a run that did not exist and never could.
    """
    app = gui(bridge=BRIDGE_NONE)

    run_button = app.page.locator("#charts-run")
    assert run_button.is_disabled()
    assert (run_button.text_content() or "").strip() == "Rebuild charts"
