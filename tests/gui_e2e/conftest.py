"""The GUI behaviour lane: the shipped app, driven the way an operator
does, over ``file://`` in headless Chromium with pywebview replaced by
a stub generated from :class:`~anastomosis.gui.controller.GuiApi`
(``stub.py``). Assertions are behavioural, never pixel-diffs.

One document, four views: :meth:`GuiPage.show` asserts nothing
reloaded on every switch. The ``gui`` fixture FAILS the test on
teardown if the page logged a console error or threw.
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
    """The opened GUI plus the seam handles a test needs: ``page`` is
    the ordinary Playwright handle, and the extras are this app's own
    seam — what it asked the controller for, what the controller
    pushed back, which view is on screen, and what the console said."""

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
        """Push a controller event into the page, as the shell's sink
        does: a single ``window.anastEvent(<json>)`` call, the payload
        crossing as JSON so the page sees nothing the real sink would
        not serialize. Build ``event`` with :mod:`anastomosis.gui.events`."""
        self.page.evaluate("event => window.anastEvent(event)", event)
        # Let the handler's own async work (a last_run_summary fetch) settle.
        self.page.wait_for_timeout(80)

    def attach_bridge(self) -> None:
        """Install the bridge NOW, replaying pywebview's late attach —
        used with ``bridge="late"`` after the app has bootstrapped
        without an api. Installs ``window.pywebview.api`` and fires
        ``pywebviewready``, as the real bridge does after DOM ready."""
        self.page.evaluate("window.__installAnastBridge()")
        self.page.wait_for_timeout(200)

    # --- the chooser -------------------------------------------------------
    # A one-of-N picker is a button plus a listbox, not a <select>: these
    # drive it the way a person does — open it, then click a row — since its
    # popup would otherwise be drawn by the OS and invisible to this browser.
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
    """Open the bundled GUI over ``file://`` with the bridge stub
    installed. ``gui(bridge=..., canned=..., reduced_motion=...)``
    returns a :class:`GuiPage`; ``bridge`` is ``BRIDGE_READY``
    (default), ``BRIDGE_LATE``, or ``BRIDGE_NONE``. Fails the test on
    teardown if any opened page logged a console error."""
    opened: list[tuple[Any, GuiPage]] = []

    def _open(
        *,
        bridge: str = BRIDGE_READY,
        canned: dict[str, Any] | None = None,
        reduced_motion: bool = False,
    ) -> GuiPage:
        path = _WEB_DIR / "index.html"
        assert path.is_file(), f"the bundled GUI is missing: {path}"
        context = _browser.new_context(
            viewport={"width": 1200, "height": 860},
            reduced_motion="reduce" if reduced_motion else "no-preference",
        )
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
