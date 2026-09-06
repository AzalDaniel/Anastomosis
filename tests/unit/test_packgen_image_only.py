"""Safety gates for image-only samples passed to pack generation.

The refusal these cover is the fail-closed half, and it is now conditional on
there being no engine to ask: ``extract_document`` with no OCR worker behaves
exactly as it always did. What OCR turns those pages into is covered by
``test_packgen_ocr_evidence.py``; what stays true here is that nothing is
learned from pixels nobody read, and that a failed run writes no pack.

The PDFs here are synthetic and contain no patient-derived text or imagery.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf", reason="packgen needs the render extra")

from anastomosis.core.packinit import PackInitCommand, run_pack_init  # noqa: E402
from anastomosis.packgen.extract import (  # noqa: E402
    NoExtractableTextError,
    OcrRequiredError,
    extract_document,
    extract_samples,
)


def _insert_raster(page: pymupdf.Page) -> None:
    """Place a small synthetic raster image onto ``page``."""
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 16, 16), False)
    pixmap.clear_with(0x2A6F97)
    page.insert_image(pymupdf.Rect(72, 72, 216, 216), pixmap=pixmap)


def _write_pdf(path: Path, page_kinds: list[str]) -> None:
    doc = pymupdf.open()
    for kind in page_kinds:
        page = doc.new_page(width=612, height=792)
        if kind == "image":
            _insert_raster(page)
        elif kind == "text":
            page.insert_text((72, 72), "SYNTHETIC TEMPLATE", fontsize=12)
        elif kind != "blank":
            raise AssertionError(f"unknown test page kind: {kind}")
    doc.save(str(path))
    doc.close()


def test_all_image_document_requires_ocr(tmp_path: Path) -> None:
    """No worker, no reading: the harvester refuses and names what to install."""
    path = tmp_path / "image-only.pdf"
    _write_pdf(path, ["image"])

    with pytest.raises(OcrRequiredError, match=r"sample #7 page #0") as excinfo:
        extract_document(path, index=7)

    assert path.name not in str(excinfo.value)
    assert "Nothing is downloaded" in str(excinfo.value)


def test_mixed_document_rejects_every_image_only_page(tmp_path: Path) -> None:
    path = tmp_path / "mixed.pdf"
    _write_pdf(path, ["text", "image"])

    with pytest.raises(OcrRequiredError, match=r"sample #0 page #1"):
        extract_document(path, index=0)


def test_textless_document_without_raster_has_distinct_failure(tmp_path: Path) -> None:
    path = tmp_path / "blank.pdf"
    _write_pdf(path, ["blank"])

    with pytest.raises(NoExtractableTextError, match=r"sample #3"):
        extract_document(path, index=3)


def test_valid_text_pdf_still_extracts(tmp_path: Path) -> None:
    path = tmp_path / "text.pdf"
    _write_pdf(path, ["text"])

    sample = extract_document(path, index=2)

    assert sample.index == 2
    assert [span.text for span in sample.spans] == ["SYNTHETIC TEMPLATE"]


def test_extract_samples_preserves_ocr_exception_type(tmp_path: Path) -> None:
    path = tmp_path / "image-only.pdf"
    _write_pdf(path, ["image"])

    with pytest.raises(OcrRequiredError, match=r"sample #0"):
        extract_samples([path])


def test_pack_init_refuses_a_textless_sample_without_writing_a_pack(
    tmp_path: Path,
) -> None:
    """A flat raster carrying no text at all cannot become a pack either
    way: with an offline engine installed the page IS observed empty;
    with no engine it is refused unread. Both roads end in a refusal and
    an empty output directory — only the exception TYPE says which
    happened, and the frontend surfaces that type, not a message."""
    samples = tmp_path / "samples"
    samples.mkdir()
    _write_pdf(samples / "image-only.pdf", ["image"])
    output = tmp_path / "packs"

    result = run_pack_init(
        PackInitCommand(samples=[str(samples)], name="synthetic", out_dir=output, confirmed=True)
    )

    assert result.ok is False
    assert result.error in {"OcrRequiredError", "NoExtractableTextError"}
    assert result.pack_dir is None
    assert result.draft_md is None
    assert not (output / "synthetic").exists()


def test_pack_init_without_an_engine_names_the_ocr_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the binary made genuinely unfindable, the refusal is the OCR one.

    PATH is pointed at a directory that does not exist and the operator
    override is cleared, so discovery really fails rather than being faked.
    """
    monkeypatch.setenv("PATH", str(tmp_path / "no-binaries-here"))
    monkeypatch.delenv("ANAST_OCR_TESSERACT", raising=False)
    samples = tmp_path / "samples"
    samples.mkdir()
    _write_pdf(samples / "image-only.pdf", ["image"])
    output = tmp_path / "packs"

    result = run_pack_init(
        PackInitCommand(samples=[str(samples)], name="synthetic", out_dir=output, confirmed=True)
    )

    assert result.ok is False
    assert result.error == "OcrRequiredError"
    assert not (output / "synthetic").exists()
