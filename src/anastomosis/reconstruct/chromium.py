"""Chromium-backed PDF renderer (Playwright).

Requires the ``render`` extra and a fetched browser
(``playwright install chromium``). Imported lazily so the rest of the
toolkit works without a browser on the machine.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["ChromiumRenderer"]


class ChromiumRenderer:
    """One Chromium instance, one page, print-quality PDFs."""

    def __init__(self, *, page_size: str = "Letter", margins: dict[str, str] | None = None) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - environment-dependent
            raise RuntimeError(
                "PDF rendering needs the render extra: pip install 'anastomosis[render]' "
                "&& playwright install chromium"
            ) from exc
        self._page_size = page_size
        self._margins = margins or {}
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch()
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
