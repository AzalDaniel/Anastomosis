"""The harvest: read every text span and vector drawing out of sample PDFs.

PyMuPDF is the only engine (deterministic, offline); imported lazily inside
:func:`extract_document` so a minimal install still imports this module.
:class:`Span`/:class:`DrawnRect`/:class:`DocumentSample` are the input
contract for :mod:`anastomosis.packgen.infer`. A page that is pixels has no
text objects; passing an OCR worker turns it into observations entering
through the same :class:`Span` shape, carrying ``provenance`` and
``confidence`` so nothing downstream mistakes a recognized word for a read
one — without a worker, the refusal stands. PHI: opaque sample index, never
a path, stored or logged (RULES.md 33)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .evidence import (
    AMBIGUOUS,
    EvidenceConflict,
    LayoutEvidence,
    OcrRegionError,
    PageEvidence,
    normalized_text,
    observe_page,
)
from .ocr import (
    INSTALL_HINT,
    NATIVE_OR_SYNTHETIC,
    NATIVE_TEXT,
    OCR_OBSERVATION,
    OcrEngineError,
    OcrToken,
    TesseractWorker,
)

__all__ = [
    "OCR_SPAN_FONT",
    "DocumentSample",
    "DrawnRect",
    "NoExtractableTextError",
    "OcrRequiredError",
    "Span",
    "extract_document",
    "extract_samples",
]

#: The font name every OCR-derived span carries. Tesseract recovers no face,
#: weight or color — the decision record is explicit that a scan's original
#: rendering system is not recoverable — so recognized spans are deliberately
#: given a name no real PDF font has. It keeps them in their own type-scale
#: cluster (native styles are never averaged together with recognized ones) and
#: it is what the emitter looks for before offering a face to a CSS stack.
OCR_SPAN_FONT = "OcrObservation"

# PyMuPDF text-span flag bits (bit 1 superscript, bit 2 italic, bit 4 serifed,
# bit 16 bold). We only read bold/italic.
_FLAG_ITALIC = 1 << 1
_FLAG_BOLD = 1 << 4

_RENDER_EXTRA_HINT = "layout learning needs the render extra: pip install 'anastomosis[render]'"


class OcrRequiredError(ValueError):
    """A page is raster with no extractable text and no OCR engine present.
    Refuses only when there is nothing to ask — hand :func:`extract_document`
    a worker and the same page becomes a reviewable observation instead. The
    message carries batch-local sample/page indices and
    :data:`~anastomosis.packgen.ocr.INSTALL_HINT`, never source text or a path."""


class NoExtractableTextError(ValueError):
    """A sample contains no extractable text and no page needing OCR —
    distinct from :class:`OcrRequiredError` (an empty/vector-only PDF isn't
    an OCR candidate). Message limited to the opaque batch-local sample
    index."""


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
    """Contract: one styled text run, tagged ``provenance``: :data:`NATIVE_TEXT`
    (real font evidence), :data:`NATIVE_OR_SYNTHETIC` (extracted over a scan,
    not proof it's right), or :data:`OCR_OBSERVATION` (recognized from pixels:
    ``font`` is :data:`OCR_SPAN_FONT`, ``size`` a measured height, style fields
    not evidence; ``confidence`` is the engine's own uncalibrated score)."""

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
    provenance: str = NATIVE_TEXT
    confidence: float | None = None


@dataclass(frozen=True)
class DrawnRect:
    """One vector rectangle or line from ``page.get_drawings()``. Curves are
    dropped on purpose (grids and bands, not bezier art);
    :class:`DocumentSample.dropped_curves` keeps the count so nothing
    vanishes silently."""

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
    # What each page was made of and what recognizing it produced. Empty for a
    # sample harvested without an OCR worker, which is the all-native default.
    evidence: LayoutEvidence = field(default_factory=LayoutEvidence)


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


def _ocr_spans(tokens: Sequence[OcrToken], width: float, height: float) -> list[Span]:
    """Recognized tokens as spans, marked so nothing mistakes them for text.
    ``size`` is a measured text height (enough to rank a heading, not a font
    size); ``bold``/``italic``/``color`` are false/black placeholders, and
    :data:`OCR_SPAN_FONT` says so in the field a type-scale reader checks."""
    return [
        Span(
            text=token.text,
            font=OCR_SPAN_FONT,
            size=token.size_pt,
            bold=False,
            italic=False,
            color=0,
            bbox=token.bbox_page_pt,
            page_index=token.page_index,
            page_width=width,
            page_height=height,
            provenance=OCR_OBSERVATION,
            confidence=token.confidence,
        )
        for token in tokens
    ]


@dataclass(frozen=True)
class _PageHarvest:
    """One page's spans (both streams) and what the ledger records about it."""

    spans: list[Span]
    evidence: PageEvidence
    conflicts: tuple[EvidenceConflict, ...]
    ocr_texts: frozenset[str]


def _harvest_page(
    page: Any,
    *,
    index: int,
    page_index: int,
    width: float,
    height: float,
    worker: TesseractWorker | None,
) -> _PageHarvest:
    """Reads one page's native text, then observes whatever of it is pixels.
    Refuses only when raster content has no text objects AND no engine to
    ask; with an engine, the same page becomes an observation — including
    one that found nothing, recorded as such rather than as never looked at."""
    native = _spans_for_page(page, page_index, width, height)
    observation = observe_page(
        page,
        page_index=page_index,
        page_size=(width, height),
        native_spans=native,
        worker=worker,
    )
    evidence = observation.evidence
    if evidence.classification == AMBIGUOUS:
        # A text layer floating inside a page-covering scan. Extraction worked;
        # that is not evidence the text is right, so it stops being "native".
        native = [replace(span, provenance=NATIVE_OR_SYNTHETIC) for span in native]
    if not native and evidence.raster_region_count and not evidence.ocr_attempted:
        raise OcrRequiredError(f"sample #{index} page #{page_index} requires OCR: {INSTALL_HINT}")
    recognized = _ocr_spans(observation.accepted, width, height)
    return _PageHarvest(
        spans=[*native, *recognized],
        evidence=evidence,
        conflicts=observation.conflicts,
        ocr_texts=frozenset(normalized_text(span.text) for span in recognized),
    )


def extract_document(
    pdf_path: Path, index: int, *, ocr: TesseractWorker | None = None
) -> DocumentSample:
    """Harvests one sample PDF into a :class:`DocumentSample`. ``index`` is
    the opaque identifier stored in place of the path; raises naming the
    path (for the operator) on an unreadable or encrypted PDF. ``ocr`` is
    the offline observation worker; ``None`` means an image-only page
    raises :class:`OcrRequiredError` and nothing is recognized."""
    try:
        import pymupdf  # render extra.
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError(_RENDER_EXTRA_HINT) from exc

    with pymupdf.open(str(pdf_path)) as doc:  # type: ignore[no-untyped-call]
        if doc.needs_pass or doc.is_encrypted:
            raise ValueError(f"sample PDF is encrypted: {pdf_path}")
        spans: list[Span] = []
        rects: list[DrawnRect] = []
        page_evidence: list[PageEvidence] = []
        conflicts: list[EvidenceConflict] = []
        ocr_texts: set[str] = set()
        dropped_curves = 0
        page_sizes: list[tuple[float, float]] = []
        for page_index, page in enumerate(doc):
            width = round(float(page.rect.width), 1)
            height = round(float(page.rect.height), 1)
            page_sizes.append((width, height))
            harvest = _harvest_page(
                page,
                index=index,
                page_index=page_index,
                width=width,
                height=height,
                worker=ocr,
            )
            spans.extend(harvest.spans)
            page_evidence.append(harvest.evidence)
            conflicts.extend(harvest.conflicts)
            ocr_texts |= harvest.ocr_texts
            page_rects, curves = _rects_for_page(page, page_index)
            rects.extend(page_rects)
            dropped_curves += curves
        if not spans:
            raise NoExtractableTextError(f"sample #{index} has no extractable text")
        return DocumentSample(
            index=index,
            pages=len(page_sizes),
            page_sizes=tuple(page_sizes),
            spans=tuple(spans),
            rects=tuple(rects),
            dropped_curves=dropped_curves,
            evidence=LayoutEvidence(
                engine_available=ocr is not None,
                ocr_manifest=_manifest_pairs(ocr),
                pages=tuple(page_evidence),
                conflicts=tuple(conflicts),
                ocr_texts=frozenset(ocr_texts),
            ),
        )


def _manifest_pairs(worker: TesseractWorker | None) -> tuple[tuple[str, str], ...]:
    """The worker's manifest as ordered ``(key, value)`` strings, or empty —
    a tuple, not a dict, because
    :class:`~anastomosis.packgen.evidence.LayoutEvidence` is frozen/hashable
    and gets written verbatim into a pack manifest."""
    if worker is None:
        return ()
    return tuple((key, str(value)) for key, value in worker.manifest().items())


def extract_samples(
    pdf_paths: Sequence[Path], *, ocr: TesseractWorker | None = None
) -> list[DocumentSample]:
    """Harvests a batch of sample PDFs, indexed by position. Loud on the
    first unreadable/encrypted file: the error names both the sample index
    and the path, but callers must log the index and :func:`exc_tag`
    only — never the path or span text. ``ocr`` passes through to every
    sample so one worker (one pinned engine + config hash) covers the batch."""
    samples: list[DocumentSample] = []
    for index, path in enumerate(pdf_paths):
        try:
            samples.append(extract_document(path, index, ocr=ocr))
        except (NoExtractableTextError, OcrEngineError, OcrRegionError, OcrRequiredError):
            # Keep the specific type (already PHI-safe: index + page only) so
            # run_pack_init can surface exc_tag; the generic wrapper below
            # names the PATH, wrong for a refusal that already said enough —
            # and for OcrEngineError, wrong in substance: those four cases are
            # facts about the ENGINE, not the sample.
            raise
        except Exception as exc:
            raise ValueError(f"sample #{index} unreadable ({path}): {exc}") from exc
    return samples
