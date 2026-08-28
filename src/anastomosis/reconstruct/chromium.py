"""Chromium-backed PDF renderer (Playwright).

Requires the ``render`` extra and a fetched browser
(``playwright install chromium``). Imported lazily so the rest of the
toolkit works without a browser on the machine.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["ChromiumRenderer", "RendererUnavailable"]

#: What to do about it, in one sentence this repository owns.
#:
#: The remedy is OURS, never the underlying library's message: Playwright's own
#: text is not under our control, and forwarding an uncontrolled string is how
#: something unexpected reaches a console. The exception TYPE goes with it, for
#: a support request.
INSTALL_HINT = (
    "Install the render extra and fetch the browser: "
    "pip install 'anastomosis[render]' && playwright install chromium"
)


class RendererUnavailable(RuntimeError):
    """Charts cannot be rendered on this computer at all.

    A property of the MACHINE, not of any one chart — so it is raised once and
    stops the run, rather than being tagged onto every encounter in turn. It
    subclasses RuntimeError so anything that already caught the old error keeps
    working.

    The message is safe to print verbatim: a fixed string plus an exception type
    name, with nothing interpolated from the input.
    """

    def __init__(self, kind: str) -> None:
        super().__init__(f"Charts cannot be rendered on this computer ({kind}). {INSTALL_HINT}")


class ChromiumRenderer:
    """One Chromium instance, one page, print-quality PDFs."""

    def __init__(self, *, page_size: str = "Letter", margins: dict[str, str] | None = None) -> None:
        from anastomosis.core.logutil import exc_tag

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - environment-dependent
            raise RendererUnavailable(exc_tag(exc)) from exc
        self._page_size = page_size
        self._margins = margins or {}
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch()
        except Exception as exc:  # pragma: no cover - environment-dependent
            # The extra is installed but the browser was never fetched, or a
            # `pip install -U` moved it: `render = ["playwright>=1.46"]` is
            # unbounded above, so an upgrade routinely invalidates the browser
            # that was downloaded for the previous version. Same remedy, same
            # once-per-run treatment.
            raise RendererUnavailable(exc_tag(exc)) from exc
        self._page: Any = self._browser.new_page()

    def render(self, html: str, pdf_path: Path) -> None:
        self._page.set_content(html, wait_until="load")
        self._page.pdf(
            path=str(pdf_path),
            format=self._page_size,
            margin=self._margins,
            print_background=True,  # design tokens live in backgrounds
        )

    def close(self) -> None:
        try:
            self._browser.close()
        finally:
            self._playwright.stop()
