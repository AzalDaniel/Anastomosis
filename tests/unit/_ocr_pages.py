"""Synthetic pages for the OCR tests, and the two metrics they are scored on.

Every page here is DRAWN by this module with PyMuPDF and then rasterized —
rendered to a pixmap and re-embedded as an image — so the PDF the OCR worker
sees genuinely has no text objects at all. Nothing is checked in: no sample
PDF exists in this repository, and none may.

Everything on these pages is invented. The names are placeholders
(``Synthia Example``), the identifiers are ``feedface-`` GUIDs, the phone
numbers use the 555 exchange, and the clinical content is generic. That is the
PHI rule, and it is also what makes the goldens publishable.

The two metrics are deliberately separate, and the goldens report them
separately, because they answer different questions and neither can excuse the
other:

* :func:`semantic_ratio` — did the engine read the same WORDS the page was
  drawn with? Sequence-aware, so a dropped or transposed word costs.
* :func:`median_iou` and :func:`median_center_offset` — did it put them in the
  same PLACE? The median box overlap, and the median distance between box
  centres in PDF points. Both are reported because the first has a ceiling well
  under 1.0 by construction (a native word box spans the font's full
  ascender-to-descender metric; a recognized box hugs the ink) and the second
  does not — an offset of a fraction of a point is the same-place answer that
  an overlap of 0.55 is too easily misread as failing.

A layout learner can be right about one and wrong about the other, and the
failure that matters most — text in the wrong place — is invisible to a text
comparison alone.
"""

from __future__ import annotations

import difflib
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any

import pymupdf

__all__ = [
    "CASES",
    "OcrCase",
    "case_digest",
    "draw_pdf",
    "median_center_offset",
    "median_iou",
    "native_words",
    "rasterize",
    "semantic_ratio",
]

PAGE_W, PAGE_H = 612.0, 792.0

#: The DPI the page is rasterized AT, before the worker rasterizes it again to
#: recognize it. Deliberately not the worker's own 216: a real scan was not
#: produced by this codebase, and a fixture that shares the worker's exact
#: sampling grid would flatter it.
SCAN_DPI = 200


@dataclass(frozen=True)
class OcrCase:
    """One golden case: how to draw it, and what it is scored against."""

    key: str
    draw: Callable[[Any], None]
    #: Extra pages drawn after the first, for the pagination case.
    extra: tuple[Callable[[Any], None], ...] = ()
    note: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------- #
# The pages
# --------------------------------------------------------------------------- #


def _short(page: Any) -> None:
    page.insert_text((60, 90), "PROGRESS NOTE", fontsize=13, fontname="hebo")


def _long(page: Any) -> None:
    page.insert_text((60, 80), "SUBJECTIVE", fontsize=13, fontname="hebo")
    body = (
        "Patient reports steady improvement since the last visit and denies "
        "chest pain, shortness of breath, or swelling of the ankles. Home "
        "readings have been consistent and the medication schedule has been "
        "followed without missed doses. Diet and activity were reviewed in "
        "detail and the patient agreed to continue the current plan through "
        "the next quarter with a follow up appointment already scheduled."
    )
    page.insert_textbox(
        pymupdf.Rect(60, 100, 552, 320), body, fontsize=11, fontname="helv", lineheight=1.4
    )


def _empty(page: Any) -> None:
    """Nothing at all. A rasterized blank page must recognize as zero words."""


def _multiline(page: Any) -> None:
    page.insert_text((60, 90), "ASSESSMENT AND PLAN", fontsize=13, fontname="hebo")
    for index, line in enumerate(
        (
            "Hypertension, stable on current therapy.",
            "Continue lisinopril ten milligrams daily.",
            "Recheck blood pressure in three months.",
            "Discussed sodium restriction and daily walking.",
        )
    ):
        page.insert_text((60, 120 + index * 24), line, fontsize=11, fontname="helv")


def _table(page: Any) -> None:
    page.insert_text((60, 84), "VITALS", fontsize=13, fontname="hebo")
    rows = (
        ("Measure", "Value", "Units"),
        ("Systolic", "128", "mmHg"),
        ("Diastolic", "82", "mmHg"),
        ("Pulse", "68", "bpm"),
    )
    top, height, widths = 100.0, 26.0, (160.0, 120.0, 120.0)
    for row_index, row in enumerate(rows):
        y = top + row_index * height
        x = 60.0
        for cell, width in zip(row, widths, strict=True):
            page.draw_rect(pymupdf.Rect(x, y, x + width, y + height), color=(0, 0, 0), width=0.6)
            page.insert_text((x + 8, y + 18), cell, fontsize=11, fontname="helv")
            x += width


def _attachment(page: Any) -> None:
    page.insert_text((60, 84), "ATTACHMENT", fontsize=13, fontname="hebo")
    page.insert_text((60, 110), "Outside imaging report, one page", fontsize=11, fontname="helv")
    page.draw_rect(pymupdf.Rect(60, 130, 552, 430), color=(0.4, 0.4, 0.4), width=1.0)
    page.insert_text((72, 160), "SCANNED DOCUMENT", fontsize=12, fontname="hebo")
    page.insert_text((72, 186), "Radiology of Example County", fontsize=11, fontname="helv")
    page.insert_text((72, 210), "Telephone 555 0142", fontsize=11, fontname="helv")


def _paginated_first(page: Any) -> None:
    page.insert_text((60, 60), "Synthia Example", fontsize=11, fontname="helv")
    page.insert_text((60, 90), "HISTORY OF PRESENT ILLNESS", fontsize=13, fontname="hebo")
    page.insert_text((60, 120), "Continued on the following page.", fontsize=11, fontname="helv")
    page.insert_text((60, 740), "Page 1 of 2", fontsize=9, fontname="helv")


def _paginated_second(page: Any) -> None:
    page.insert_text((60, 60), "Synthia Example", fontsize=11, fontname="helv")
    page.insert_text((60, 90), "HISTORY OF PRESENT ILLNESS", fontsize=13, fontname="hebo")
    page.insert_text(
        (60, 120), "Symptoms resolved without treatment.", fontsize=11, fontname="helv"
    )
    page.insert_text((60, 740), "Page 2 of 2", fontsize=9, fontname="helv")


def _font_fallback(page: Any) -> None:
    """Three faces the destination host may not have, drawn on one page.

    Recognition recovers no face at all, which is the point: the golden proves
    the WORDS and the BOXES survive a face change, and by construction proves
    nothing about the typography, because there is nothing to recover.
    """
    page.insert_text((60, 90), "SERIF HEADING", fontsize=13, fontname="tibo")
    page.insert_text((60, 130), "Monospaced body text sample", fontsize=11, fontname="cobo")
    page.insert_text((60, 170), "Italic annotation line", fontsize=11, fontname="tiit")


CASES: tuple[OcrCase, ...] = (
    OcrCase("short", _short, note="one short heading", tags=("short",)),
    OcrCase("long", _long, note="a wrapped paragraph", tags=("long",)),
    OcrCase("empty", _empty, note="a rasterized blank page", tags=("empty",)),
    OcrCase("multiline", _multiline, note="a heading over four lines", tags=("multiline",)),
    OcrCase("table", _table, note="a ruled three-column table", tags=("table",)),
    OcrCase("attachment", _attachment, note="a framed scanned attachment", tags=("attachment",)),
    OcrCase(
        "pagination",
        _paginated_first,
        extra=(_paginated_second,),
        note="two pages with a repeated header and page-number footer",
        tags=("pagination",),
    ),
    OcrCase(
        "font_fallback",
        _font_fallback,
        note="serif, monospaced and italic faces on one page",
        tags=("font-fallback",),
    ),
)


# --------------------------------------------------------------------------- #
# Drawing and rasterizing
# --------------------------------------------------------------------------- #


def draw_pdf(case: OcrCase, path: Path) -> Path:
    """Write the case's NATIVE (text-object) PDF and return the path."""
    doc = pymupdf.open()
    for draw in (case.draw, *case.extra):
        draw(doc.new_page(width=PAGE_W, height=PAGE_H))
    doc.save(str(path))
    doc.close()
    return path


def rasterize(source: Path, target: Path, dpi: int = SCAN_DPI) -> Path:
    """Render every page of ``source`` to an image and re-embed it in ``target``.

    The result is what the 53-sample set was: a PDF whose pages are pictures.
    ``extract_document`` finds no text objects on it, which is exactly the
    condition the OCR worker exists for — the test is against a real raster,
    not a mock of one.
    """
    src = pymupdf.open(str(source))
    out = pymupdf.open()
    for page in src:
        pixmap = page.get_pixmap(dpi=dpi)
        new_page = out.new_page(width=page.rect.width, height=page.rect.height)
        new_page.insert_image(pymupdf.Rect(0, 0, page.rect.width, page.rect.height), pixmap=pixmap)
    out.save(str(target))
    out.close()
    src.close()
    return target


def native_words(path: Path) -> list[tuple[str, tuple[float, float, float, float]]]:
    """``(word, page-point box)`` for every word of the NATIVE source PDF.

    This is the ground truth both metrics score against: the same page before
    it was flattened to pixels.
    """
    doc = pymupdf.open(str(path))
    words: list[tuple[str, tuple[float, float, float, float]]] = []
    for page in doc:
        for x0, y0, x1, y1, text, *_ in page.get_text("words"):
            words.append((text, (round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1))))
    doc.close()
    return words


# --------------------------------------------------------------------------- #
# The two metrics
# --------------------------------------------------------------------------- #


def semantic_ratio(expected: list[str], observed: list[str]) -> float:
    """Sequence similarity of the two word lists, 0..1.

    ``difflib`` rather than a set comparison: reading the right words in the
    wrong order is a reading-order failure, and a set would score it perfect.
    Two empty lists agree completely, which is the blank-page case.
    """
    if not expected and not observed:
        return 1.0
    return difflib.SequenceMatcher(a=expected, b=observed, autojunk=False).ratio()


def _iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    overlap = max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(
        0.0, min(a[3], b[3]) - max(a[1], b[1])
    )
    if overlap <= 0.0:
        return 0.0
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return overlap / (area_a + area_b - overlap)


def median_iou(
    truth: list[tuple[str, tuple[float, float, float, float]]],
    observed: list[tuple[str, tuple[float, float, float, float]]],
) -> float | None:
    """Median best-match box overlap between recognized and native words.

    ``None`` when there is nothing to measure (the blank page), which the
    goldens report as ``null`` rather than as a passing score — an absent
    measurement is not a good one.

    The ceiling is below 1.0 by construction and not by defect: a native word
    box spans the font's full ascender-to-descender metric while a recognized
    box hugs the ink. The threshold is calibrated against what that geometry
    can actually reach, and the goldens record the measured value beside it.
    """
    if not observed or not truth:
        return None
    scores = [max((_iou(box, other) for _, other in truth), default=0.0) for _, box in observed]
    return round(median(scores), 3)


def median_center_offset(
    truth: list[tuple[str, tuple[float, float, float, float]]],
    observed: list[tuple[str, tuple[float, float, float, float]]],
) -> float | None:
    """Median distance, in points, from a recognized box centre to the nearest
    native word box centre. ``None`` when there is nothing to measure.

    The overlap metric's ceiling is a box-convention artefact; this one is not.
    A word recognized a whole line away from where it was drawn shows up here
    as points of error no matter how the boxes are cropped.
    """
    if not observed or not truth:
        return None
    offsets = [
        min(
            ((box[0] + box[2]) / 2 - (other[0] + other[2]) / 2) ** 2
            + ((box[1] + box[3]) / 2 - (other[1] + other[3]) / 2) ** 2
            for _, other in truth
        )
        ** 0.5
        for _, box in observed
    ]
    return round(median(offsets), 3)


def case_digest(case: OcrCase) -> str:
    """A digest of the drawn page, so a changed fixture cannot reuse a golden."""
    doc = pymupdf.open()
    for draw in (case.draw, *case.extra):
        draw(doc.new_page(width=PAGE_W, height=PAGE_H))
    payload = "|".join(
        f"{text}@{box}" for page in doc for *box, text, _a, _b, _c in page.get_text("words")
    )
    doc.close()
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
