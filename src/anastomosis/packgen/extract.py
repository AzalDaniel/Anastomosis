"""The harvest: read every text span and vector drawing out of sample PDFs.

PyMuPDF (``pymupdf``) is the only engine — deterministic, fully offline, no
torch. It is an optional ``render``-extra dependency, imported lazily inside
:func:`extract_document` so this module imports on a minimal install (the same
error style the Chromium renderer uses when the extra is absent).

The frozen :class:`Span` / :class:`DrawnRect` / :class:`DocumentSample`
dataclasses are the input contract for :mod:`anastomosis.packgen.infer`.

A page that is pixels has no text objects to read, and the one real sample set
this product has been shown was 802 such pages. Passing an OCR worker
(:func:`anastomosis.packgen.ocr.discover_worker`) turns those pages into
observations that enter through the SAME :class:`Span` shape, carrying
``provenance`` and a ``confidence`` so nothing downstream can mistake a
recognized word for a read one. Without a worker the old refusal stands, and
its message now says which half is missing.

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
    """A page is raster with no extractable text, and no OCR engine is present.

    The refusal that used to be unconditional. Learning a layout from an
    image-only page without reading it would make a generic-looking draft while
    silently missing that page — so the harvester still refuses, but only when
    there is nothing to ask. Hand :func:`extract_document` a worker and the
    same page becomes a reviewable observation instead.

    The message contains the batch-local sample/page indices and
    :data:`anastomosis.packgen.ocr.INSTALL_HINT` — never source text, never a
    path.
    """


class NoExtractableTextError(ValueError):
    """A sample contains no extractable text and no page needing OCR.

    This is distinct from :class:`OcrRequiredError`: an empty/vector-only PDF
    cannot be learned from, but it is not an OCR candidate.  Its message is
    likewise limited to the opaque batch-local sample index.
    """


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
    """One contiguous run of identically-styled text on a page.

    ``provenance`` is the field that keeps the two evidence streams apart, and
    it travels with the span everywhere the learner takes it — a reader of a
    span never has to ask a page or a docstring where its text came from:

    * :data:`~anastomosis.packgen.ocr.NATIVE_TEXT` — a real PDF text object.
      ``font``/``size``/``bold``/``italic``/``color`` are source evidence.
    * :data:`~anastomosis.packgen.ocr.NATIVE_OR_SYNTHETIC` — a selectable layer
      floating over a scan. Extraction succeeded; that is not the same as the
      text being right, and it may itself be somebody else's OCR.
    * :data:`~anastomosis.packgen.ocr.OCR_OBSERVATION` — recognized from pixels
      by this run. ``font`` is :data:`OCR_SPAN_FONT`, ``size`` is a measured
      text HEIGHT rather than a font size, and ``bold``/``italic``/``color``
      are not evidence at all: the engine recovers none of them. Layout
      evidence only — never a clinical value.

    ``confidence`` is the engine's own score for a recognized span (``None``
    for native text, which is not scored). It is a triage signal, not a
    calibrated probability, and two engines' scores are not comparable.
    """

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
    """Recognized tokens as spans, marked so nothing can mistake them for text.

    ``size`` is the token's measured text height in points, which is enough to
    rank a heading above body copy and is NOT a font size; ``bold``/``italic``
    are ``False`` and ``color`` is black because the engine recovers none of
    them, and :data:`OCR_SPAN_FONT` says so in the one field a reader of the
    type scale actually looks at.
    """
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
    """Read one page's native text, then observe whatever of it is pixels.

    The refusal lives here and is now conditional: a page with raster content,
    no text objects, and NO engine to ask is the case pack generation cannot
    honestly proceed through. With an engine, the same page becomes an
    observation — including an observation that found nothing, which is
    recorded as such rather than being mistaken for a page that was never
    looked at.
    """
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
    """Harvest one sample PDF into a :class:`DocumentSample`.

    ``index`` is the opaque identifier stored in place of the path. Raises a
    descriptive error (naming the path, for the operator) on an unreadable or
    encrypted PDF — losslessness/loud-failure invariant.

    ``ocr`` is the offline observation worker
    (:func:`anastomosis.packgen.ocr.discover_worker`). ``None`` — the default —
    keeps the historical behaviour exactly: an image-only page raises
    :class:`OcrRequiredError` and nothing is recognized.
    """
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
    """The worker's manifest as ordered ``(key, value)`` strings, or empty.

    Strings, and a tuple rather than a dict, because a
    :class:`~anastomosis.packgen.evidence.LayoutEvidence` is frozen and hashable
    and gets written verbatim into a pack manifest.
    """
    if worker is None:
        return ()
    return tuple((key, str(value)) for key, value in worker.manifest().items())


def extract_samples(
    pdf_paths: Sequence[Path], *, ocr: TesseractWorker | None = None
) -> list[DocumentSample]:
    """Harvest a batch of sample PDFs, indexed by position.

    Loud on the first unreadable/encrypted file: the raised error names BOTH
    the sample index (which file in the batch) and the path (so the operator
    can find it). Callers must log the index and :func:`exc_tag` only — never
    the path or any span text.

    ``ocr`` is passed through to every sample, so one discovered worker (and
    therefore one pinned engine version and config hash) covers the batch.
    """
    samples: list[DocumentSample] = []
    for index, path in enumerate(pdf_paths):
        try:
            samples.append(extract_document(path, index, ocr=ocr))
        except (NoExtractableTextError, OcrRegionError, OcrRequiredError):
            # These failures already carry a PHI-safe, opaque sample index (and
            # a page index). Keep their specific type so run_pack_init can
            # surface exc_tag — the generic wrapper below names the PATH, which
            # is right for an unreadable file and wrong for a refusal that has
            # already said everything an operator needs.
            raise
        except Exception as exc:
            raise ValueError(f"sample #{index} unreadable ({path}): {exc}") from exc
    return samples
