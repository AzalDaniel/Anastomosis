"""Unit tests for the packgen harvest (extract.py).

No Chromium: tiny PDFs are built directly with PyMuPDF (``insert_text`` /
``draw_rect`` at controlled positions, fonts, sizes), so these run in the unit
lane. The fixtures are wholly synthetic — ``feedface`` ids, 555 phones,
example-style names — never patient-derived.
"""

from __future__ import annotations

from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz", reason="packgen extract needs the render extra (PyMuPDF)")

from anastomosis.packgen.extract import (  # noqa: E402
    DocumentSample,
    DrawnRect,
    Span,
    extract_document,
    extract_samples,
)

# US Letter in PDF points.
_W, _H = 612.0, 792.0
_GREY_F1 = (0.9451, 0.9451, 0.9451)  # #f1f1f1


def _build_pdf(path: Path, *, encrypt: bool = False, with_curve: bool = False) -> None:
    """A single-page synthetic note: a bold heading band, body prose, two
    aligned label columns, and a grey fill rect."""
    doc = fitz.open()
    page = doc.new_page(width=_W, height=_H)
    page.draw_rect(fitz.Rect(43, 95, 569, 110), fill=_GREY_F1, color=None)
    page.insert_text((43, 90), "SUBJECTIVE", fontsize=10.5, fontname="hebo")
    page.insert_text((43, 130), "Patient reports feeling well.", fontsize=11, fontname="helv")
    # Two label columns at known x0s (left labels, indented values).
    page.insert_text((43, 160), "DOB:", fontsize=9.5, fontname="helv")
    page.insert_text((150, 160), "03/14/1985", fontsize=9.5, fontname="helv")
    if with_curve:
        # A bezier curve must be dropped (and counted), never harvested.
        page.draw_bezier((60, 400), (80, 420), (100, 380), (120, 400))
    if encrypt:
        doc.save(
            str(path),
            encryption=fitz.PDF_ENCRYPT_AES_256,
            owner_pw="owner",  # synthetic test password (S ignored in tests)
            user_pw="user",
        )
    else:
        doc.save(str(path))
    doc.close()


@pytest.fixture
def sample_pdf(tmp_path: Path) -> Path:
    path = tmp_path / "note.pdf"
    _build_pdf(path)
    return path


def _spans_by_text(sample: DocumentSample) -> dict[str, Span]:
    return {s.text.strip(): s for s in sample.spans}


def test_extract_document_reads_spans_and_geometry(sample_pdf: Path) -> None:
    sample = extract_document(sample_pdf, index=7)
    assert sample.index == 7
    assert sample.pages == 1
    assert sample.page_sizes == ((_W, _H),)
    spans = _spans_by_text(sample)
    assert "SUBJECTIVE" in spans
    assert "Patient reports feeling well." in spans
    assert "DOB:" in spans


def test_span_size_font_and_flags(sample_pdf: Path) -> None:
    spans = _spans_by_text(extract_document(sample_pdf, index=0))
    heading = spans["SUBJECTIVE"]
    assert heading.size == pytest.approx(10.5, abs=0.05)
    assert heading.bold is True
    assert heading.italic is False
    assert "Bold" in heading.font

    body = spans["Patient reports feeling well."]
    assert body.size == pytest.approx(11.0, abs=0.05)
    assert body.bold is False


def test_span_bbox_rounded_and_positioned(sample_pdf: Path) -> None:
    spans = _spans_by_text(extract_document(sample_pdf, index=0))
    body = spans["Patient reports feeling well."]
    x0, y0, x1, y1 = body.bbox
    # bbox rounded to 0.1pt and reported in page coordinates.
    assert all(round(v, 1) == v for v in body.bbox)
    assert x0 == pytest.approx(43.0, abs=1.0)
    assert x1 > x0
    assert y1 > y0
    assert body.page_width == _W
    assert body.page_height == _H


def test_span_color_is_int_rgb(sample_pdf: Path) -> None:
    sample = extract_document(sample_pdf, index=0)
    # Default text color is black (0). Color is an int, never a float triple.
    assert all(isinstance(s.color, int) for s in sample.spans)


def test_drawn_rect_fill_color_packed_to_rgb(sample_pdf: Path) -> None:
    sample = extract_document(sample_pdf, index=0)
    fills = [r for r in sample.rects if r.fill_color is not None]
    assert fills, "the grey heading band rect should be harvested"
    assert any(r.fill_color == 0xF1F1F1 for r in fills), [hex(r.fill_color or 0) for r in fills]
    band = next(r for r in fills if r.fill_color == 0xF1F1F1)
    assert band.bbox[0] == pytest.approx(43.0, abs=1.0)
    assert band.page_index == 0


def test_curves_dropped_and_counted(tmp_path: Path) -> None:
    path = tmp_path / "curvy.pdf"
    _build_pdf(path, with_curve=True)
    sample = extract_document(path, index=0)
    assert sample.dropped_curves >= 1
    # The rect harvest still includes the legit grey band (curve-only drawing
    # is the thing dropped, not the whole page).
    assert any(r.fill_color == 0xF1F1F1 for r in sample.rects)


def test_extract_samples_indexes_by_position(tmp_path: Path) -> None:
    paths = []
    for i in range(3):
        p = tmp_path / f"s{i}.pdf"
        _build_pdf(p)
        paths.append(p)
    samples = extract_samples(paths)
    assert [s.index for s in samples] == [0, 1, 2]
    assert all(s.pages == 1 for s in samples)


def test_encrypted_pdf_fails_loudly_naming_index_and_path(tmp_path: Path) -> None:
    good = tmp_path / "good.pdf"
    bad = tmp_path / "locked.pdf"
    _build_pdf(good)
    _build_pdf(bad, encrypt=True)
    with pytest.raises(ValueError, match=r"sample #1") as excinfo:
        extract_samples([good, bad])
    # The exception (operator-facing) names the path; never logged though.
    assert "locked.pdf" in str(excinfo.value)


def test_garbage_pdf_fails_loudly(tmp_path: Path) -> None:
    junk = tmp_path / "junk.pdf"
    junk.write_bytes(b"%PDF-1.4 this is not a real pdf at all \x00\x01\x02")
    with pytest.raises(ValueError, match=r"sample #0"):
        extract_samples([junk])


def test_frozen_dataclasses_are_immutable() -> None:
    import dataclasses

    span = Span(
        text="x",
        font="f",
        size=11.0,
        bold=False,
        italic=False,
        color=0,
        bbox=(0.0, 0.0, 1.0, 1.0),
        page_index=0,
        page_width=_W,
        page_height=_H,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        span.size = 12.0  # type: ignore[misc]
    rect = DrawnRect(
        bbox=(0.0, 0.0, 1.0, 1.0),
        fill_color=None,
        stroke_color=None,
        stroke_width=0.0,
        page_index=0,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        rect.stroke_width = 1.0  # type: ignore[misc]
