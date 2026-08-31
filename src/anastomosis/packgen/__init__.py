"""Pack-from-samples layout learner — the analysis half.

A practice hands us N sample PDFs of their EHR's note format. ``packgen``
*sees* the layout deterministically — no torch, fully offline — and infers
the design system that produced it:

* :mod:`anastomosis.packgen.ocr` — the offline OCR observation worker
  (pinned Tesseract CLI, TSV + hOCR, no network ever), and the provenance
  vocabulary every span carries.
* :mod:`anastomosis.packgen.evidence` — page classification (native-only,
  mixed, image-only, ambiguous, empty), the native/OCR adjudication that holds
  conflicts instead of resolving them, and the :class:`LayoutEvidence` ledger.
* :mod:`anastomosis.packgen.extract` — the harvest: every text span and
  vector drawing read out of the sample PDFs with PyMuPDF, plus — when an
  engine is available — the recognized spans from pages that are pixels.
* :mod:`anastomosis.packgen.infer` — the statistics: type scale, heading
  taxonomy, column grid, page geometry, page-break rules, design tokens, and
  the static/per-patient text split, aggregated into a :class:`PackAnalysis`.

:class:`PackAnalysis` is the contract the draft-pack emitter builds on; it is
a frozen aggregate of everything inferred.

PHI rule (non-negotiable): sample PDFs may be named after patients and carry
per-patient values, so this package stores an opaque sample *index* — never a
file path — and never logs sample-derived text. The only span text that ever
escapes into a human-readable summary is the *static* text recurring across
samples. "(Template labels/headings, which are by construction not patient
data)" is what this said; recurrence is a count, and a value two patients
happen to share recurs and escapes alongside the labels (#200). The half it
does settle: a value seen in only one sample never appears in a summary.

CAVEAT (operator guidance, not enforceable by math): the static/per-patient
split assumes samples come from DIFFERENT patients/encounters. Hand this
tool three copies of ONE patient's chart and that patient's values recur in
100% of samples — indistinguishable from template text — and WILL surface as
"static". Sample sets must be distinct patients; the pack-init wizard (item
15) repeats this warning interactively.

OCR is layout evidence and never clinical truth. A recognized span carries
:data:`~anastomosis.packgen.ocr.OCR_OBSERVATION` and a confidence, a heading
built from one carries its provenance, and the emitted draft says so in its
manifest, its ``DRAFT.md``, its quarantine file and its ``OCR_EVIDENCE.md``. A
high-risk field still needs an independent structured source or a person.

PyMuPDF is an optional (``render`` extra) dependency and is imported lazily
inside :func:`~anastomosis.packgen.extract.extract_document`, so this package
imports cleanly on a minimal install.
"""

from __future__ import annotations

from .evidence import (
    AMBIGUOUS,
    EMPTY,
    IMAGE_ONLY,
    MIXED,
    MIXED_EVIDENCE,
    NATIVE_ONLY,
    PAGE_CLASSES,
    EvidenceConflict,
    LayoutEvidence,
    OcrRegionError,
    PageEvidence,
    observe_page,
)
from .extract import (
    OCR_SPAN_FONT,
    DocumentSample,
    DrawnRect,
    NoExtractableTextError,
    OcrRequiredError,
    Span,
    extract_document,
    extract_samples,
)
from .infer import (
    OCR_EVIDENCE_CAVEAT,
    ColorUsage,
    ColumnGrid,
    ColumnStart,
    DesignTokens,
    PackAnalysis,
    PageBreakStats,
    PageGeometry,
    SectionCandidate,
    TypeScale,
    TypeScaleLevel,
    analyze,
    infer_column_grid,
    infer_design_tokens,
    infer_page_breaks,
    infer_page_geometry,
    infer_section_taxonomy,
    infer_static_text,
    infer_type_scale,
)
from .ocr import (
    NATIVE_OR_SYNTHETIC,
    NATIVE_TEXT,
    OCR_OBSERVATION,
    PROVENANCES,
    TESSERACT_BACKEND_ID,
    OcrConfig,
    OcrEngineError,
    OcrPageResult,
    OcrToken,
    OcrUnavailableError,
    PageImage,
    TesseractWorker,
    discover_worker,
    find_tesseract,
)

__all__ = [
    "AMBIGUOUS",
    "EMPTY",
    "IMAGE_ONLY",
    "MIXED",
    "MIXED_EVIDENCE",
    "NATIVE_ONLY",
    "NATIVE_OR_SYNTHETIC",
    "NATIVE_TEXT",
    "OCR_EVIDENCE_CAVEAT",
    "OCR_OBSERVATION",
    "OCR_SPAN_FONT",
    "PAGE_CLASSES",
    "PROVENANCES",
    "TESSERACT_BACKEND_ID",
    "ColorUsage",
    "ColumnGrid",
    "ColumnStart",
    "DesignTokens",
    "DocumentSample",
    "DrawnRect",
    "EvidenceConflict",
    "LayoutEvidence",
    "NoExtractableTextError",
    "OcrConfig",
    "OcrEngineError",
    "OcrPageResult",
    "OcrRegionError",
    "OcrRequiredError",
    "OcrToken",
    "OcrUnavailableError",
    "PackAnalysis",
    "PageBreakStats",
    "PageEvidence",
    "PageGeometry",
    "PageImage",
    "SectionCandidate",
    "Span",
    "TesseractWorker",
    "TypeScale",
    "TypeScaleLevel",
    "analyze",
    "discover_worker",
    "extract_document",
    "extract_samples",
    "find_tesseract",
    "infer_column_grid",
    "infer_design_tokens",
    "infer_page_breaks",
    "infer_page_geometry",
    "infer_section_taxonomy",
    "infer_static_text",
    "infer_type_scale",
    "observe_page",
]
