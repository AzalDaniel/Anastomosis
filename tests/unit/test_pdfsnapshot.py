"""Direct tests for the one PDF page reader both graders share (core/pdfsnapshot.py)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pymupdf = pytest.importorskip("pymupdf", reason="the page reader needs PyMuPDF (render extra)")

from anastomosis.core import pdfsnapshot  # noqa: E402
from anastomosis.core.pdfsnapshot import PageInfo, PdfSnapshot, PdfSnapshotCache  # noqa: E402


def _make_pdf(path: Path, pages: list[str]) -> Path:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page(width=612, height=792)
        page.insert_textbox(pymupdf.Rect(36, 36, 576, 756), text)
    doc.save(str(path))
    doc.close()
    return path


def test_a_snapshot_opens_the_file_once_and_only_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One open per PDF is the whole point of the shared snapshot: the QA
    checks and the L0-L6 ladder each ask several times per document."""
    path = _make_pdf(tmp_path / "chart.pdf", ["page one text", "page two text"])
    real = pdfsnapshot.import_pymupdf()
    opens = 0

    class _CountingPymupdf:
        def open(self, *args: object, **kwargs: object) -> object:
            nonlocal opens
            opens += 1
            return real.open(*args, **kwargs)

    monkeypatch.setattr(pdfsnapshot, "import_pymupdf", _CountingPymupdf)
    snapshot = PdfSnapshot(path)
    assert opens == 0, "constructing a snapshot must not open the file"
    assert snapshot.page_count == 2
    assert "page one text" in snapshot.page_one_text
    assert "page two text" not in snapshot.page_one_text
    assert "page two text" in snapshot.text
    assert opens == 1, f"the snapshot opened the PDF {opens} times, expected 1"


def test_a_snapshot_carries_each_pages_geometry(tmp_path: Path) -> None:
    path = _make_pdf(tmp_path / "chart.pdf", ["only page"])
    (page,) = PdfSnapshot(path).pages
    assert (round(page.width), round(page.height)) == (612, 792)


def test_a_document_with_no_pages_has_no_page_one_text() -> None:
    """PyMuPDF refuses to write a zero-page PDF, so the empty case is driven
    through :func:`first_page_text` — the answer is "", never a raise."""
    assert pdfsnapshot.pages_of([]) == []
    assert pdfsnapshot.first_page_text([]) == ""
    snapshot = PdfSnapshot(Path("/nonexistent.pdf"))
    snapshot._pages = []
    assert snapshot.page_count == 0
    assert snapshot.page_one_text == ""
    assert snapshot.text == ""


def test_pages_of_reads_text_and_geometry_from_an_open_document(tmp_path: Path) -> None:
    path = _make_pdf(tmp_path / "chart.pdf", ["alpha"])
    with pymupdf.open(path) as doc:
        pages: list[PageInfo] = pdfsnapshot.pages_of(doc)
    assert len(pages) == 1
    assert "alpha" in pages[0].text
    assert pdfsnapshot.first_page_text(pages) == pages[0].text


def test_the_cache_hands_back_one_snapshot_per_path(tmp_path: Path) -> None:
    first = _make_pdf(tmp_path / "a.pdf", ["alpha"])
    second = _make_pdf(tmp_path / "b.pdf", ["beta"])
    cache = PdfSnapshotCache()
    assert cache.get(first) is cache.get(first)
    assert cache.get(first) is not cache.get(second)
    assert "beta" in cache.get(second).text


def test_a_missing_render_extra_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def _refuse(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "pymupdf":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _refuse)
    with pytest.raises(RuntimeError, match="render extra"):
        pdfsnapshot.import_pymupdf()
