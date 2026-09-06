"""The bridge seam itself: surface drift, the late attach, the no-bridge
preview (rule 74). If ``GuiApi`` gains, loses or renames a method, the
stub's surface and canned fixtures must move with it, or every view test
would keep passing against a stale api.

The single document attaches the bridge ONCE, in shell.js, so the
late-attach race runs once, and every view has to come alive off that one
bootstrap.
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
    """The stub answers exactly the real ``GuiApi`` methods — no more, no
    less: a renamed or removed method leaves a canned key with nothing
    behind it, a new one arrives with no fixture, and either way this
    fails loudly rather than the lane passing against a stale seam."""
    assert sorted(canned_returns()) == api_surface()


def test_every_bridge_call_in_the_shipped_js_exists_on_the_api() -> None:
    """No view calls a controller method the js_api facade does not expose:
    ``window.pywebview.api.foo()`` against a missing ``foo`` is a
    TypeError in the live app — a dead button with a console error."""
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
    """A pywebview attach that lands AFTER DOM ready must still wake the
    app: pywebview installs ``window.pywebview.api`` asynchronously and
    announces it with ``pywebviewready``, so a bridge probe that only fires
    at DOMContentLoaded can lose the race. One bootstrap re-runs for the
    whole document."""
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
    assert len(app.choices("#charts-pack")) > 0
    app.show("migrate")
    assert len(app.choices("#migrate-destination")) > 0


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
    """The api-less run button must be disabled WITHOUT mislabelling
    itself as "running…" for a run that does not exist and never could."""
    app = gui(bridge=BRIDGE_NONE)

    run_button = app.page.locator("#charts-run")
    assert run_button.is_disabled()
    assert (run_button.text_content() or "").strip() == "Rebuild charts"
