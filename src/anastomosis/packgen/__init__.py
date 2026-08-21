"""Pack-from-samples layout learner — the analysis half (M3a).

A practice hands us N sample PDFs of their EHR's note format. ``packgen``
*sees* the layout deterministically — no torch, fully offline — and infers
the design system that produced it:

* :mod:`anastomosis.packgen.extract` — the harvest: every text span and
  vector drawing read out of the sample PDFs with PyMuPDF.
* :mod:`anastomosis.packgen.infer` — the statistics: type scale, heading
  taxonomy, column grid, page geometry, page-break rules, design tokens, and
  the static/per-patient text split, aggregated into a :class:`PackAnalysis`.

:class:`PackAnalysis` is the contract the draft-pack emitter (item 15) builds
on; it is a frozen aggregate of everything inferred.

PHI rule (non-negotiable): sample PDFs may be named after patients and carry
per-patient values, so this package stores an opaque sample *index* — never a
file path — and never logs sample-derived text. The only span text that ever
escapes into a human-readable summary is the *static* text recurring across
samples (template labels/headings, which are by construction not patient
data); a value seen in only one sample never appears in a summary.

CAVEAT (operator guidance, not enforceable by math): the static/per-patient
split assumes samples come from DIFFERENT patients/encounters. Hand this
tool three copies of ONE patient's chart and that patient's values recur in
100% of samples — indistinguishable from template text — and WILL surface as
"static". Sample sets must be distinct patients; the pack-init wizard (item
15) repeats this warning interactively.

PyMuPDF is an optional (``render`` extra) dependency and is imported lazily
inside :func:`~anastomosis.packgen.extract.extract_document`, so this package
imports cleanly on a minimal install.
"""

from __future__ import annotations

from .extract import (
    DocumentSample,
    DrawnRect,
    Span,
    extract_document,
    extract_samples,
)
from .infer import (
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

__all__ = [
    "ColorUsage",
    "ColumnGrid",
    "ColumnStart",
    "DesignTokens",
    "DocumentSample",
    "DrawnRect",
    "PackAnalysis",
    "PageBreakStats",
    "PageGeometry",
    "SectionCandidate",
    "Span",
    "TypeScale",
    "TypeScaleLevel",
    "analyze",
    "extract_document",
    "extract_samples",
    "infer_column_grid",
    "infer_design_tokens",
    "infer_page_breaks",
    "infer_page_geometry",
    "infer_section_taxonomy",
    "infer_static_text",
    "infer_type_scale",
]
