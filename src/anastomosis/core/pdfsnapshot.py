"""One PDF read once — the pages QA grades and the delivery ladder verifies.
Imports nothing of the project's, so neither side imports the other (75, 76)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "PageInfo",
    "PdfSnapshot",
    "PdfSnapshotCache",
    "first_page_text",
    "import_pymupdf",
    "pages_of",
]


def import_pymupdf() -> Any:
    """PyMuPDF, lazily, raising :exc:`RuntimeError` naming the ``render`` extra (75)."""
    try:
        import pymupdf
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError(
            "PDF verification needs the render extra: pip install 'anastomosis[render]'"
        ) from exc
    return pymupdf


@dataclass(frozen=True)
class PageInfo:
    """One rendered page: the text a check reads and the geometry it measures."""

    text: str
    width: float
    height: float


def pages_of(doc: Any) -> list[PageInfo]:
    """Every page of an OPEN document, so all of it describes one read of one file."""
    return [
        PageInfo(text=page.get_text(), width=page.rect.width, height=page.rect.height)
        for page in doc
    ]


def first_page_text(pages: list[PageInfo]) -> str:
    """Page one's text, or "" for a document that has no pages."""
    return pages[0].text if pages else ""


class PdfSnapshot:
    """One PDF read on the first ask and shared from there. Lazy: L1's size
    floor still rejects unopened, and a corrupt file fails the check that asked."""

    __slots__ = ("_page_count", "_page_one_text", "_pages", "path")

    def __init__(self, path: Path) -> None:
        self.path = path
        self._pages: list[PageInfo] | None = None
        self._page_count: int | None = None
        self._page_one_text: str | None = None

    def _head(self) -> tuple[int, str]:
        """Count and page one in ONE open, reading no page past the first: the
        pair the ladder wants, where extracting all 300 cost 51x on one chart."""
        if self._pages is not None:
            return len(self._pages), first_page_text(self._pages)
        count, text = self._page_count, self._page_one_text
        if count is None or text is None:
            with import_pymupdf().open(self.path) as doc:
                count = int(doc.page_count)
                text = str(doc[0].get_text()) if count else ""
            self._page_count, self._page_one_text = count, text
        return count, text

    @property
    def pages(self) -> list[PageInfo]:
        """Every page's text and geometry: the only ask that reads the whole file."""
        if self._pages is None:
            with import_pymupdf().open(self.path) as doc:
                self._pages = pages_of(doc)
        return self._pages

    @property
    def page_count(self) -> int:
        return self._head()[0]

    @property
    def page_one_text(self) -> str:
        return self._head()[1]

    @property
    def text(self) -> str:
        return "\n".join(page.text for page in self.pages)


class PdfSnapshotCache:
    """A snapshot per path: one context grades more than one document (#392)."""

    __slots__ = ("_by_path",)

    def __init__(self) -> None:
        self._by_path: dict[Path, PdfSnapshot] = {}

    def get(self, path: Path) -> PdfSnapshot:
        if path not in self._by_path:
            self._by_path[path] = PdfSnapshot(path)
        return self._by_path[path]
