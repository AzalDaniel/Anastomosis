"""The GUI behaviour lane: the shipped app, driven the way an operator does.

Everything under ``tests/gui_e2e`` loads the REAL bundled asset
(``anastomosis/gui/web/index.html`` — the same file
:func:`anastomosis.gui.shell.launch` points the window at) over ``file://`` in
headless Chromium, with the pywebview bridge replaced by a stub generated from
the real :class:`~anastomosis.gui.controller.GuiApi` surface (see ``stub.py``).
Nothing here renders a pixel-diff: the assertions are behavioural — which
controller method a click issues, what the DOM says after an event sequence,
and whether the app logged a console error doing it.

The GUI is ONE document with four views, so the fixture opens one page and the
tests switch views through the nav, exactly as an operator does. A view switch
must never be a document navigation: :meth:`GuiPage.show` asserts nothing
reloaded, so the SPA guarantee is checked on every single switch the lane makes.

Why a stub and not the live controller: pywebview is not installed in CI (and
would need a display), the heavy run methods are deliberately unreachable from
JS, and a canned bridge lets a test drive states a real run cannot reach on
demand (a stale filing assistant, an aborted upload, a late-attaching bridge).
The seam is kept honest by generating the stub's surface from ``GuiApi`` and by
building every pushed event with the REAL constructors in
:mod:`anastomosis.gui.events` — a rename on either side fails this lane.

Console discipline: every load and interaction is recorded, and the ``gui``
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

#: A boot marker planted on the window. It survives a view switch and does NOT
#: survive a document navigation, which is exactly the property under test.
_BOOT_MARKER = "window.__anastBootMarker"


class GuiPage:
    """The opened GUI plus the seam handles a test needs.

    Thin on purpose: ``page`` is the ordinary Playwright handle (locators,
    clicks, keyboard), and the extras are the four things this app's seam adds
    — what the page asked the controller for, what the controller pushes back,
    which view is on screen, and what the console said about it.
    """

    def __init__(self, page: Page) -> None:
        self.page = page
        self.name = "index.html"
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
        assert calls, f"the GUI never called {method}()"
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
        self.page.wait_for_timeout(80)

    def attach_bridge(self) -> None:
        """Install the bridge NOW, replaying pywebview's late attach.

        Used with ``bridge="late"``: the app has already bootstrapped without an
        api and painted its offline notice; this installs
        ``window.pywebview.api`` and fires ``pywebviewready``, which is what the
        real bridge does when it wins the race after DOM ready.
        """
        self.page.evaluate("window.__installAnastBridge()")
        self.page.wait_for_timeout(200)

    # --- the chooser -------------------------------------------------------
    # A one-of-N picker is a button plus a listbox now, not a <select>, so
    # `select_option` no longer applies. These drive it the way a person does —
    # open it, then click a row — which is the point of replacing the native
    # control: its popup was drawn by the OS and invisible to this browser.
    def choices(self, trigger: str) -> list[str]:
        """The labels a person can read in this chooser, in order."""
        return [
            (text or "").strip()
            for text in self.page.locator(
                f"{trigger} + .chooser-list .chooser-name"
            ).all_text_contents()
        ]

    def notes(self, trigger: str) -> list[str]:
        """The mono captions under each row — the ids, for support."""
        return [
            (text or "").strip()
            for text in self.page.locator(
                f"{trigger} + .chooser-list .chooser-note"
            ).all_text_contents()
        ]

    def titles(self, trigger: str) -> list[str]:
        """Each row's tooltip — the machine id, for whoever has to ask."""
        return [
            row.get_attribute("title") or ""
            for row in self.page.locator(f"{trigger} + .chooser-list .chooser-row").all()
        ]

    def choose(self, trigger: str, value: str) -> None:
        """Pick the row carrying ``value``, by pointer, through the popup."""
        self._click_row(trigger, f'.chooser-row[data-value="{value}"]', value)

    def choose_label(self, trigger: str, label: str) -> None:
        """Pick the row a person would recognise by its visible name."""
        labels = self.choices(trigger)
        assert label in labels, f"{trigger} has no option labelled {label!r} (has {labels})"
        self._click_row(trigger, f".chooser-row >> nth={labels.index(label)}", label)

    def chosen(self, trigger: str) -> str:
        """The value the chooser would hand the controller."""
        return str(self.page.locator(trigger).evaluate("node => node.value"))

    def _click_row(self, trigger: str, row: str, wanted: str) -> None:
        self.page.click(trigger)
        self.page.wait_for_timeout(60)
        target = self.page.locator(f"{trigger} + .chooser-list").locator(row)
        assert target.count() == 1, f"{trigger} has no option for {wanted!r}"
        target.click()
        self.page.wait_for_timeout(80)

    # --- views -------------------------------------------------------------
    def show(self, view: str) -> None:
        """Switch views through the nav, and prove it was not a navigation."""
        before = self.page.url
        self.page.click(f'[data-view-target="{view}"]')
        # The crossfade is 240ms; give it that plus the two commit frames.
        self.page.wait_for_timeout(400)
        assert self.page.url == before, "a view switch navigated the document"
        assert self.page.evaluate(f"{_BOOT_MARKER}") == "alive", (
            "the document reloaded on a view switch — the marker planted at boot is gone"
        )
        assert self.visible(view), f"the {view} view did not come up"

    def visible(self, view: str) -> bool:
        return bool(
            self.page.evaluate(
                'name => { const s = document.querySelector(`[data-view="${name}"]`);'
                " return !!s && !s.hidden; }",
                view,
            )
        )

    def view_text(self, view: str) -> str:
        """All text a view carries, hidden panels included."""
        return str(
            self.page.evaluate(
                'name => document.querySelector(`[data-view="${name}"]`).textContent || ""',
                view,
            )
        )

    def text(self, selector: str) -> str:
        node = self.page.locator(selector).first
        if node.count() == 0:
            return ""
        return (node.text_content() or "").strip()

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
    """Open the bundled GUI over ``file://`` with the bridge stub installed.

    ``gui(bridge=..., canned=...)`` returns a :class:`GuiPage`. ``bridge`` is
    one of ``stub.BRIDGE_READY`` (default), ``BRIDGE_LATE`` (install it yourself
    with :meth:`GuiPage.attach_bridge`) or ``BRIDGE_NONE`` (the plain-browser
    preview). ``canned`` overrides individual API returns.

    On teardown every opened page must have a clean console: a normal path that
    logs an error is a finding, so the fixture fails the test rather than
    leaving the test author to remember an assertion.
    """
    opened: list[tuple[Any, GuiPage]] = []

    def _open(*, bridge: str = BRIDGE_READY, canned: dict[str, Any] | None = None) -> GuiPage:
        path = _WEB_DIR / "index.html"
        assert path.is_file(), f"the bundled GUI is missing: {path}"
        context = _browser.new_context(viewport={"width": 1200, "height": 860})
        if bridge != BRIDGE_NONE:
            context.add_init_script(init_script(bridge, canned))
        # Planted before any app script runs, so a reload would wipe it.
        context.add_init_script(f"{_BOOT_MARKER} = 'alive';")
        page = context.new_page()
        gui_page = GuiPage(page)

        def _on_console(message: ConsoleMessage) -> None:
            if message.type == "error":
                gui_page.console.append(f"console.error: {message.text}")

        page.on("console", _on_console)
        page.on("pageerror", lambda exc: gui_page.console.append(f"pageerror: {exc}"))
        page.goto(path.as_uri())
        page.wait_for_load_state("networkidle")
        # The shell bootstraps on DOMContentLoaded and again on pywebviewready;
        # give the resulting promises a beat to resolve before anyone asserts.
        page.wait_for_timeout(200)
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
