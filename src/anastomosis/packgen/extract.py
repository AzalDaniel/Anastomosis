"""The harvest: read every text span and vector drawing out of sample PDFs.

PyMuPDF (``fitz``) is the only engine — deterministic, fully offline, no
torch. It is an optional ``render``-extra dependency, imported lazily inside
:func:`extract_document` so this module imports on a minimal install (the same
error style the Chromium renderer uses when the extra is absent).

The frozen :class:`Span` / :class:`DrawnRect` / :class:`DocumentSample`
dataclasses are the input contract for :mod:`anastomosis.packgen.infer`.

PHI rule: a sample PDF may be *named after a patient* and its body is
per-patient data. :class:`DocumentSample` therefore stores an opaque integer
``index`` — never the file path. :func:`extract_samples` fails loudly on an
unreadable or encrypted file; the raised exception names the offending
**path** (the operator needs to know which file), but that path must never be
*logged* — the distinction is enforced by callers logging :func:`exc_tag`
plus the sample index only.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "DocumentSample",
    "DrawnRect",
    "Span",
    "extract_document",
    "extract_samples",
]

# PyMuPDF text-span flag bits (bit 1 superscript, bit 2 italic, bit 4 serifed,
# bit 16 bold). We only read bold/italic.
_FLAG_ITALIC = 1 << 1
_FLAG_BOLD = 1 << 4

_RENDER_EXTRA_HINT = "layout learning needs the render extra: pip install 'anastomosis[render]'"


def _bbox4(rect: Any) -> tuple[float, float, float, float]:
    """Round a PyMuPDF rect (or 4-tuple) to a 0.1pt 4-tuple."""
    x0, y0, x1, y1 = rect[0], rect[1], rect[2], rect[3]
    return (round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1))


def _rgb_from_float(triple: Sequence[float] | None) -> int | None:
    """Pack a PyMuPDF 0..1 RGB float triple into a 24-bit ``0xRRGGBB`` int.

    Span colors arrive as ints already; drawing fills/strokes arrive as float
    triples (or ``None`` when the path has no fill/stroke).
    """
    if triple is None:
        return None
    r, g, b = (max(0, min(255, round(c * 255))) for c in triple[:3])
    return (r << 16) | (g << 8) | b


@dataclass(frozen=True)
class Span:
    """One contiguous run of identically-styled text on a page."""

    text: str
    font: str
    size: float  # rounded to 0.1pt
    bold: bool
    italic: bool
    color: int  # 24-bit sRGB, 0xRRGGBB
    bbox: tuple[float, float, float, float]  # x0,y0,x1,y1 rounded to 0.1pt
    page_index: int
    page_width: float
    page_height: float


@dataclass(frozen=True)
class DrawnRect:
    """One vector rectangle or line from ``page.get_drawings()``.

    Curves are intentionally dropped (a layout learner reads grids and bands,
    not bezier art); :class:`DocumentSample.dropped_curves` keeps the count so
    nothing vanishes *silently*.
    """

    bbox: tuple[float, float, float, float]
    fill_color: int | None  # 0xRRGGBB or None (no fill)
    stroke_color: int | None  # 0xRRGGBB or None (no stroke)
    stroke_width: float
    page_index: int


@dataclass(frozen=True)
class DocumentSample:
    """Everything harvested from one sample PDF.

    ``index`` is an opaque per-batch identifier; the file path is deliberately
    NOT stored (PHI: a sample may be named after a patient).
    """

    index: int
    pages: int
    # Per-page (width, height) in points, in page order.
    page_sizes: tuple[tuple[float, float], ...]
    spans: tuple[Span, ...]
    rects: tuple[DrawnRect, ...]
    dropped_curves: int


def _spans_for_page(page: Any, page_index: int, width: float, height: float) -> list[Span]:
    spans: list[Span] = []
    blocks = page.get_text("dict").get("blocks", [])
    for block in blocks:
        for line in block.get("lines", []):
            for raw in line.get("spans", []):
                text = raw.get("text", "")
                if not text.strip():
                    continue  # whitespace-only spans carry no layout signal
                flags = int(raw.get("flags", 0))
                spans.append(
                    Span(
                        text=text,
                        font=str(raw.get("font", "")),
                        size=round(float(raw.get("size", 0.0)), 1),
                        bold=bool(flags & _FLAG_BOLD),
                        italic=bool(flags & _FLAG_ITALIC),
                        color=int(raw.get("color", 0)),
                        bbox=_bbox4(raw.get("bbox", (0.0, 0.0, 0.0, 0.0))),
                        page_index=page_index,
                        page_width=width,
                        page_height=height,
                    )
                )
    return spans


def _rects_for_page(page: Any, page_index: int) -> tuple[list[DrawnRect], int]:
    rects: list[DrawnRect] = []
    dropped_curves = 0
    for drawing in page.get_drawings():
        # A drawing may mix primitives; keep it only if every item is a
        # rectangle ("re") or line ("l"). Curves ("c") and quads ("qu") are
        # counted and skipped — see DrawnRect docstring.
        ops = [item[0] for item in drawing.get("items", [])]
        if not ops:
            continue
        if any(op in ("c", "qu") for op in ops):
            dropped_curves += 1
            continue
        if any(op not in ("re", "l") for op in ops):
            dropped_curves += 1
            continue
        fill = _rgb_from_float(drawing.get("fill"))
        stroke = _rgb_from_float(drawing.get("color"))
        width = drawing.get("width")
        rects.append(
            DrawnRect(
                bbox=_bbox4(drawing["rect"]),
                fill_color=fill,
                stroke_color=stroke,
                stroke_width=round(float(width), 2) if width is not None else 0.0,
                page_index=page_index,
            )
        )
    return rects, dropped_curves


def extract_document(pdf_path: Path, index: int) -> DocumentSample:
    """Harvest one sample PDF into a :class:`DocumentSample`.

    ``index`` is the opaque identifier stored in place of the path. Raises a
    descriptive error (naming the path, for the operator) on an unreadable or
    encrypted PDF — losslessness/loud-failure invariant.
    """
    try:
        import fitz  # PyMuPDF — render extra.
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError(_RENDER_EXTRA_HINT) from exc

    with fitz.open(str(pdf_path)) as doc:
        if doc.needs_pass or doc.is_encrypted:
            raise ValueError(f"sample PDF is encrypted: {pdf_path}")
        spans: list[Span] = []
        rects: list[DrawnRect] = []
        dropped_curves = 0
        page_sizes: list[tuple[float, float]] = []
        for page_index, page in enumerate(doc):
            width = round(float(page.rect.width), 1)
            height = round(float(page.rect.height), 1)
            page_sizes.append((width, height))
            spans.extend(_spans_for_page(page, page_index, width, height))
            page_rects, curves = _rects_for_page(page, page_index)
            rects.extend(page_rects)
            dropped_curves += curves
        return DocumentSample(
            index=index,
            pages=len(page_sizes),
            page_sizes=tuple(page_sizes),
            spans=tuple(spans),
            rects=tuple(rects),
            dropped_curves=dropped_curves,
        )


def extract_samples(pdf_paths: Sequence[Path]) -> list[DocumentSample]:
    """Harvest a batch of sample PDFs, indexed by position.

    Loud on the first unreadable/encrypted file: the raised error names BOTH
    the sample index (which file in the batch) and the path (so the operator
    can find it). Callers must log the index and :func:`exc_tag` only — never
    the path or any span text.
    """
    samples: list[DocumentSample] = []
    for index, path in enumerate(pdf_paths):
        try:
            samples.append(extract_document(path, index))
        except Exception as exc:
            raise ValueError(f"sample #{index} unreadable ({path}): {exc}") from exc
    return samples
