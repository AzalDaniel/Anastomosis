"""The GUI behaviour lane: the shipped pages, driven the way an operator does.

Everything under ``tests/gui_e2e`` loads the REAL bundled assets
(``anastomosis/gui/web/*.html`` — the same directory
:func:`anastomosis.gui.shell.launch` points the window at) over ``file://`` in
headless Chromium, with the pywebview bridge replaced by a stub generated from
the real :class:`~anastomosis.gui.controller.GuiApi` surface (see ``stub.py``).
Nothing here renders a pixel-diff: the assertions are behavioural — which
controller method a click issues, what the DOM says after an event sequence,
and whether the page logged a console error doing it.

Why a stub and not the live controller: pywebview is not installed in CI (and
would need a display), the heavy run methods are deliberately unreachable from
JS, and a canned bridge lets a test drive states a real run cannot reach on
demand (a stale pack, an aborted upload, a late-attaching bridge). The seam is
kept honest by generating the stub's surface from ``GuiApi`` and by building
every pushed event with the REAL constructors in
:mod:`anastomosis.gui.events` — a rename on either side fails this lane.

Console discipline: every page load and interaction is recorded, and the ``gui``
fixture FAILS the test on teardown if the page logged an error or threw. A
normal operator path must be silent; anything else is a finding.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import pytest
from stub import BRIDGE_NONE, BRIDGE_READY, init_script

# The very directory the shell opens — testing a copy would prove nothing.
from anastomosis.gui.shell import _WEB_DIR

if TYPE_CHECKING:  # pragma: no cover - typing only
    from playwright.sync_api import Browser, ConsoleMessage, Page

__all__ = ["GuiPage"]

# Console noise that is an artifact of loading a packaged page from file:// in a
# bare browser rather than a finding about the app. Kept as an explicit, empty-
# by-default allowlist: entries must be justified here, never waved through in a
# test. (A file:// page requests no favicon in headless Chromium, so nothing has
# earned a slot yet.)
_CONSOLE_ALLOWLIST: tuple[str, ...] = ()


class GuiPage:
    """One opened GUI page plus the seam handles a test needs.

    Thin on purpose: ``page`` is the ordinary Playwright handle (locators,
    clicks, keyboard), and the extras are the three things this app's seam adds
    — what the page asked the controller for, what the controller pushes back,
    and what the console said about it.
    """

    def __init__(self, page: Page, name: str) -> None:
        self.page = page
        self.name = name
        self.console: list[str] = []

    # --- the bridge seam ---------------------------------------------------
    def calls(self, method: str | None = None) -> list[dict[str, Any]]:
        """Every recorded bridge call, oldest first (optionally one method's)."""
        recorded: list[dict[str, Any]] = self.page.evaluate("window.__anastCalls || []")
        if method is None:
            return recorded
        return [call for call in recorded if call["method"] == method]

    def called(self, method: str) -> bool:
        return bool(self.calls(method))

    def last_args(self, method: str) -> list[Any]:
        """The positional arguments of the most recent call to ``method``."""
        calls = self.calls(method)
        assert calls, f"{self.name} never called {method}()"
        args: list[Any] = calls[-1]["args"]
        return args

    def emit(self, event: dict[str, object]) -> None:
        """Push a controller event into the page, as the shell's sink does.

        The shell marshals each event dict into a single
        ``window.anastEvent(<json>)`` call; this is that call, with the payload
        crossing as JSON so the page cannot see anything the real sink would not
        serialize. Build ``event`` with :mod:`anastomosis.gui.events`.
        """
        self.page.evaluate("event => window.anastEvent(event)", event)
        # Let the handler's own async work (a last_run_summary fetch) settle.
        self.page.wait_for_timeout(50)

    def attach_bridge(self) -> None:
        """Install the bridge NOW, replaying pywebview's late attach.

        Used with ``bridge="late"``: the page has already bootstrapped without
        an api and painted its offline notice; this installs
        ``window.pywebview.api`` and fires ``pywebviewready``, which is what the
        real bridge does when it wins the race after DOM ready.
        """
        self.page.evaluate("window.__installAnastBridge()")
        self.page.wait_for_timeout(150)

    # --- console -----------------------------------------------------------
    def console_problems(self) -> list[str]:
        """Recorded console errors / page exceptions, minus the allowlist."""
        return [
            entry
            for entry in self.console
            if not any(allowed in entry for allowed in _CONSOLE_ALLOWLIST)
        ]


@pytest.fixture(scope="session")
def _browser() -> Iterator[Browser]:
    """One headless Chromium for the whole lane (launch cost is per-session)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def gui(_browser: Browser) -> Iterator[Any]:
    """Open a bundled GUI page over ``file://`` with the bridge stub installed.

    ``gui(page_name, bridge=..., canned=...)`` returns a :class:`GuiPage`.
    ``bridge`` is one of ``stub.BRIDGE_READY`` (default), ``BRIDGE_LATE``
    (install it yourself with :meth:`GuiPage.attach_bridge`) or ``BRIDGE_NONE``
    (the plain-browser preview). ``canned`` overrides individual API returns.

    On teardown every opened page must have a clean console: a normal path that
    logs an error is a finding, so the fixture fails the test rather than
    leaving the test author to remember an assertion.
    """
    opened: list[tuple[Any, GuiPage]] = []

    def _open(
        page_name: str,
        *,
        bridge: str = BRIDGE_READY,
        canned: dict[str, Any] | None = None,
    ) -> GuiPage:
        path = _WEB_DIR / page_name
        assert path.is_file(), f"no such bundled GUI page: {path}"
        context = _browser.new_context()
        if bridge != BRIDGE_NONE:
            context.add_init_script(init_script(bridge, canned))
        page = context.new_page()
        gui_page = GuiPage(page, page_name)

        def _on_console(message: ConsoleMessage) -> None:
            if message.type == "error":
                gui_page.console.append(f"console.error: {message.text}")

        page.on("console", _on_console)
        page.on("pageerror", lambda exc: gui_page.console.append(f"pageerror: {exc}"))
        page.goto(path.as_uri())
        page.wait_for_load_state("networkidle")
        # The pages bootstrap on DOMContentLoaded and again on pywebviewready;
        # give the resulting promises a beat to resolve before anyone asserts.
        page.wait_for_timeout(150)
        opened.append((context, gui_page))
        return gui_page

    try:
        yield _open
    finally:
        problems = [
            f"{gui_page.name}: {entry}"
            for _context, gui_page in opened
            for entry in gui_page.console_problems()
        ]
        for context, _gui_page in opened:
            context.close()
        assert not problems, "the GUI logged console errors on a normal path:\n  " + "\n  ".join(
            problems
        )
