"""The statistics: infer a design system from harvested sample spans.

Stdlib only, no numpy/torch. Every function takes the
:class:`~anastomosis.packgen.extract.DocumentSample` list and returns a
frozen, deterministic structure; :func:`analyze` aggregates them into
:class:`PackAnalysis`, the emitter's contract. Clustering is simple greedy
bucketing, not DBSCAN, so an operator can read why a level was inferred.

PHI: the only span text reaching a summary is *static* text — on every
sample AND owning an exclusive page slot (see :func:`infer_static_text`,
#200); neither test alone is a proof, so the summary stays captioned for
a person to read. CAVEAT: assumes samples are DISTINCT patients/
encounters — :data:`anastomosis.packgen.emit.SAME_PATIENT_CAVEAT` says
why that alone still isn't enough."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from statistics import median

from .evidence import LayoutEvidence, combined_provenance, merge_evidence, normalized_text
from .extract import DocumentSample, Span
from .ocr import NATIVE_TEXT

__all__ = [
    "OCR_EVIDENCE_CAVEAT",
    "ColorUsage",
    "ColumnGrid",
    "ColumnStart",
    "DesignTokens",
    "PackAnalysis",
    "PageBreakStats",
    "PageGeometry",
    "SectionCandidate",
    "TypeScale",
    "TypeScaleLevel",
    "analyze",
    "infer_column_grid",
    "infer_design_tokens",
    "infer_page_breaks",
    "infer_page_geometry",
    "infer_section_taxonomy",
    "infer_static_text",
    "infer_type_scale",
]

# Clustering tolerances (points).
_SIZE_TOLERANCE = 0.25  # type-scale font-size cluster width
_COLUMN_TOLERANCE = 1.0  # x0 column-start cluster width

# A section HEADING is recurring when it appears in at least this fraction of
# samples — a supermajority, not all of them, since an empty section's
# heading may not print at all. `infer_static_text` needs a stricter test
# than frequency (see there) and does not use this constant.
_STATIC_FRACTION = 0.6

#: The sentence every OCR-touched artifact carries, verbatim, in every place a
#: person might read only one of them. It is the decision record's rule stated
#: as a claim about THIS pack: recognized text is geometry, and a high-risk
#: field needs an independent structured source or a person.
OCR_EVIDENCE_CAVEAT = (
    "OCR EVIDENCE: some of this layout was recognized from page images, not read "
    "from the document. Recognized text is layout evidence only — it may suggest "
    "lines, columns, bands and page breaks, and it may NOT be treated as clinical "
    "truth. Any high-risk field (identity, dates, author, medications, allergies, "
    "results, status) needs an independent structured source or human review "
    "against the original image before it is trusted."
)

_HEADING_ROLES = ("h1", "h2", "h3")
_MAX_COLUMNS = 6


def _normalize(text: str) -> str:
    """Collapse whitespace and strip — matches the golden tooling's
    normalization and the native/OCR duplicate test in
    :mod:`anastomosis.packgen.evidence`, so a string recurs identically
    regardless of source or intra-span whitespace."""
    return normalized_text(text)


# --------------------------------------------------------------------------- #
# Type scale
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TypeScaleLevel:
    """One (font, size, bold) cluster with its usage count and role guess."""

    font: str
    size: float
    bold: bool
    count: int
    role: str  # "body" | "h1" | "h2" | "h3" | "small"


@dataclass(frozen=True)
class TypeScale:
    """The inferred type scale: distinct style clusters with role guesses."""

    levels: tuple[TypeScaleLevel, ...]

    @property
    def body_size(self) -> float | None:
        for level in self.levels:
            if level.role == "body":
                return level.size
        return None

    @property
    def body_font(self) -> str | None:
        for level in self.levels:
            if level.role == "body":
                return level.font
        return None


def _cluster_styles(spans: Sequence[Span]) -> list[tuple[str, float, bool, int]]:
    """Groups spans into (font, size, bold) clusters with weighted counts.
    Two spans share a cluster when font+bold match and sizes are within
    :data:`_SIZE_TOLERANCE`; each is weighted by character count, so a
    one-word title doesn't out-vote a paragraph of body text when picking
    the most-used (body) cluster. Reported size is the weighted mean."""
    # key -> [total_weight, weighted_size_sum] grouped first by exact size, then
    # merged across the tolerance below.
    exact: dict[tuple[str, float, bool], int] = Counter()
    for span in spans:
        exact[(span.font, span.size, span.bold)] += max(1, len(span.text.strip()))

    # Merge adjacent sizes within tolerance, per (font, bold), smallest first.
    # The reported size is the char-weight-weighted mean across the bucket.
    merged: list[tuple[str, float, bool, int]] = []
    by_style: dict[tuple[str, bool], list[tuple[float, int]]] = {}
    for (font, size, bold), weight in exact.items():
        by_style.setdefault((font, bold), []).append((size, weight))
    for (font, bold), sizes in by_style.items():
        sizes.sort()
        bucket: list[tuple[float, int]] = []
        for size, weight in sizes:
            if bucket and size - bucket[0][0] > _SIZE_TOLERANCE:
                mean, total = _collapse_bucket(bucket)
                merged.append((font, mean, bold, total))
                bucket = []
            bucket.append((size, weight))
        if bucket:
            mean, total = _collapse_bucket(bucket)
            merged.append((font, mean, bold, total))
    return merged


def _collapse_bucket(bucket: list[tuple[float, int]]) -> tuple[float, int]:
    """Char-weight-weighted mean size (rounded 0.1pt) and total weight."""
    total = sum(w for _, w in bucket)
    mean = sum(size * w for size, w in bucket) / total
    return round(mean, 1), total


def infer_type_scale(samples: Sequence[DocumentSample]) -> TypeScale:
    """Clusters spans into (font, size, bold) levels and guesses roles:
    the highest-weight cluster is ``body``; larger ones are h1/h2/h3 by
    descending size (capped at three); smaller ones are ``small``. A bold
    cluster at or below body size, not body itself, is also a heading
    (10.5pt bold under 11pt body) and takes the next h-role."""
    all_spans = [span for sample in samples for span in sample.spans]
    clusters = _cluster_styles(all_spans)
    if not clusters:
        return TypeScale(levels=())

    # Body = the highest-weight cluster.
    body_idx = max(range(len(clusters)), key=lambda i: clusters[i][3])
    body_font, body_size, _body_bold, _ = clusters[body_idx]

    levels: list[TypeScaleLevel] = []

    # Heading candidates: larger than body, OR bold and not the body cluster.
    def is_heading(font: str, size: float, bold: bool) -> bool:
        if size > body_size + _SIZE_TOLERANCE:
            return True
        return bold and not (font == body_font and abs(size - body_size) <= _SIZE_TOLERANCE)

    heading_clusters = sorted(
        (c for i, c in enumerate(clusters) if i != body_idx and is_heading(*c[:3])),
        key=lambda c: (-c[1], c[0]),  # largest first, then font name
    )
    role_for_cluster: dict[tuple[str, float, bool], str] = {}
    for rank, cluster in enumerate(heading_clusters):
        role_for_cluster[cluster[:3]] = _HEADING_ROLES[min(rank, len(_HEADING_ROLES) - 1)]

    for i, (font, size, bold, count) in enumerate(clusters):
        if i == body_idx:
            role = "body"
        elif (font, size, bold) in role_for_cluster:
            role = role_for_cluster[(font, size, bold)]
        else:
            role = "small"
        levels.append(TypeScaleLevel(font=font, size=size, bold=bold, count=count, role=role))

    # Deterministic order: by size descending, then bold, then font.
    levels.sort(key=lambda lvl: (-lvl.size, not lvl.bold, lvl.font))
    return TypeScale(levels=tuple(levels))


# --------------------------------------------------------------------------- #
# Section taxonomy + static text
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SectionCandidate:
    """A recurring heading-level string — a likely section heading.
    ``provenance`` is where its spans came from (real text, a floating
    layer, this run's OCR, or MIXED_EVIDENCE for more than one stream) —
    a recognized heading is a hypothesis, and the pack says so per
    section."""

    text: str  # normalized
    role: str  # the type-scale role the span carried ("h1"/"h2"/"h3")
    count: int  # how many distinct samples contain it
    median_y_fraction: float  # vertical position (0 top, 1 bottom of page)
    all_pages_first: bool  # always on page 0?
    provenance: str = NATIVE_TEXT


def _static_threshold(n_samples: int) -> int:
    """Minimum sample count for "recurring" — ceil(0.6 * N), floor 1."""
    return max(1, math.ceil(_STATIC_FRACTION * n_samples))


def _heading_roles(scale: TypeScale) -> set[str]:
    return {lvl.role for lvl in scale.levels if lvl.role in _HEADING_ROLES}


def _style_role(scale: TypeScale) -> dict[tuple[str, float, bool], str]:
    return {(lvl.font, lvl.size, lvl.bold): lvl.role for lvl in scale.levels}


def infer_section_taxonomy(samples: Sequence[DocumentSample]) -> list[SectionCandidate]:
    """Recurring heading-level texts → the section-heading taxonomy, kept
    when a text recurs across >= ``ceil(0.6 * N)`` samples — a smaller
    claim than dropping every per-patient heading (:func:`infer_static_text`).
    A SINGLE sample makes everything recur trivially (threshold 1),
    flagged via ``count == 1``."""
    scale = infer_type_scale(samples)
    style_role = _style_role(scale)
    heading_roles = _heading_roles(scale)
    threshold = _static_threshold(len(samples))

    # text -> set(sample index), list of (y_fraction), list of page_index, role
    seen_samples: dict[str, set[int]] = {}
    y_fractions: dict[str, list[float]] = {}
    pages: dict[str, list[int]] = {}
    roles: dict[str, str] = {}
    provenances: dict[str, list[str]] = {}
    for sample in samples:
        for span in sample.spans:
            role = style_role.get((span.font, span.size, span.bold))
            if role not in heading_roles:
                continue
            text = _normalize(span.text)
            if not text:
                continue
            seen_samples.setdefault(text, set()).add(sample.index)
            height = span.page_height or 1.0
            y_fractions.setdefault(text, []).append(round(span.bbox[1] / height, 3))
            pages.setdefault(text, []).append(span.page_index)
            roles.setdefault(text, role or "")
            provenances.setdefault(text, []).append(span.provenance)

    candidates: list[SectionCandidate] = []
    for text, sample_set in seen_samples.items():
        if len(sample_set) < threshold:
            continue
        candidates.append(
            SectionCandidate(
                text=text,
                role=roles[text],
                count=len(sample_set),
                median_y_fraction=round(median(y_fractions[text]), 3),
                all_pages_first=all(p == 0 for p in pages[text]),
                provenance=combined_provenance(provenances[text]),
            )
        )
    # Top of page first, then most-recurring, then alphabetical — deterministic.
    candidates.sort(key=lambda c: (c.median_y_fraction, -c.count, c.text))
    return candidates


#: How finely a span's origin is bucketed when asking "is this the same place
#: on the page". Two points — about the jitter a font metric introduces between
#: two renders of one form, and well under the height of a table row, so two
#: patients' values in the same cell land in the same bucket and are seen to be
#: competing for it.
_SLOT_TOLERANCE = 2.0


def _slot(span: Span) -> tuple[int, int, int]:
    """Where on the page this span sits, bucketed — a printed form's slot."""
    x0, y0 = span.bbox[0], span.bbox[1]
    return (span.page_index, round(x0 / _SLOT_TOLERANCE), round(y0 / _SLOT_TOLERANCE))


def _exclusive_slots(samples: Sequence[DocumentSample]) -> set[str]:
    """Texts that OWN a page slot nothing else was ever printed in — the
    test frequency alone cannot do. A label is printed BY the form and
    holds its slot on every chart; a value is printed INTO the form and
    shares its slot with the next patient's. Not a proof: a shared value
    in a fixed cell has no competitor and still gets through."""
    occupants: dict[tuple[int, int, int], set[str]] = {}
    for sample in samples:
        for span in sample.spans:
            text = _normalize(span.text)
            if text:
                occupants.setdefault(_slot(span), set()).add(text)
    return {next(iter(texts)) for texts in occupants.values() if len(texts) == 1}


def infer_static_text(samples: Sequence[DocumentSample]) -> list[str]:
    """The label vocabulary (``"DOB:"``, ``"Provider:"``), minus section
    headings. Two tests, both required: on EVERY sample, not a
    supermajority (the 0.6 bar let a two-patient value through, #200);
    and it OWNS an exclusive page slot (:func:`_exclusive_slots`).
    Neither is a proof; output stays quarantined and captioned."""
    seen: dict[str, set[int]] = {}
    for sample in samples:
        for span in sample.spans:
            text = _normalize(span.text)
            if not text:
                continue
            seen.setdefault(text, set()).add(sample.index)
    everywhere = {sample.index for sample in samples}
    owns_a_slot = _exclusive_slots(samples)
    headings = {c.text for c in infer_section_taxonomy(samples)}
    return sorted(
        text
        for text, samps in seen.items()
        if samps >= everywhere and text in owns_a_slot and text not in headings
    )


# --------------------------------------------------------------------------- #
# Column grid
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ColumnStart:
    """A clustered left edge (x0) shared by many spans — a column origin."""

    x0: float
    count: int


@dataclass(frozen=True)
class ColumnGrid:
    """Inferred left-edge columns and (if drawings exist) fill-rect gutters."""

    columns: tuple[ColumnStart, ...]
    # x positions where persistent fill-rect edges sit (table/band borders).
    gutters: tuple[float, ...]


def _cluster_scalars(values: Sequence[float], tolerance: float) -> list[tuple[float, int]]:
    """Greedy 1-D clustering: sort, then bucket within ``tolerance`` of the
    bucket's first member. Returns (mean, count) per bucket, sorted by mean."""
    clusters: list[tuple[float, int]] = []
    if not values:
        return clusters
    ordered = sorted(values)
    bucket: list[float] = [ordered[0]]
    for value in ordered[1:]:
        if value - bucket[0] > tolerance:
            clusters.append((round(sum(bucket) / len(bucket), 1), len(bucket)))
            bucket = [value]
        else:
            bucket.append(value)
    clusters.append((round(sum(bucket) / len(bucket), 1), len(bucket)))
    return clusters


def infer_column_grid(samples: Sequence[DocumentSample]) -> ColumnGrid:
    """Cluster span left edges (x0, 1pt tolerance) → column starts with usage
    counts; report the top <= 6 by count. Gutters are the clustered vertical
    edges of persistent fill rects, if any drawings were harvested.
    """
    x0s = [span.bbox[0] for sample in samples for span in sample.spans]
    clusters = _cluster_scalars(x0s, _COLUMN_TOLERANCE)
    # Most-used first, then left-to-right for ties — then re-sort the kept top
    # columns left-to-right for a readable grid.
    by_count = sorted(clusters, key=lambda c: (-c[1], c[0]))[:_MAX_COLUMNS]
    columns = tuple(
        ColumnStart(x0=x0, count=count) for x0, count in sorted(by_count, key=lambda c: c[0])
    )

    fill_edges = [
        edge
        for sample in samples
        for rect in sample.rects
        if rect.fill_color is not None
        for edge in (rect.bbox[0], rect.bbox[2])
    ]
    gutters = tuple(x0 for x0, _ in _cluster_scalars(fill_edges, _COLUMN_TOLERANCE))
    return ColumnGrid(columns=columns, gutters=gutters)


# --------------------------------------------------------------------------- #
# Page breaks
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PageBreakStats:
    """Per-sample page-count distribution and content-extent statistics."""

    # page-count -> number of samples with that many pages.
    page_count_distribution: tuple[tuple[int, int], ...]
    # Largest content y-fraction seen on any page (bottom-margin estimate).
    max_content_y_fraction: float
    # Texts recurring at the top of pages across samples — running headers.
    running_headers: tuple[str, ...]


def infer_page_breaks(samples: Sequence[DocumentSample]) -> PageBreakStats:
    """Page-count distribution, max content y-fraction (bottom-margin
    estimate), and repeated top-of-page text (running headers)."""
    counts = Counter(sample.pages for sample in samples)
    distribution = tuple(sorted(counts.items()))

    max_y_fraction = 0.0
    # text seen near the top (<=15%) of a page -> set(sample index)
    top_texts: dict[str, set[int]] = {}
    for sample in samples:
        for span in sample.spans:
            height = span.page_height or 1.0
            y_bottom_fraction = span.bbox[3] / height
            max_y_fraction = max(max_y_fraction, y_bottom_fraction)
            if span.bbox[1] / height <= 0.15:
                text = _normalize(span.text)
                if text:
                    top_texts.setdefault(text, set()).add(sample.index)
    threshold = _static_threshold(len(samples))
    running_headers = tuple(
        sorted(text for text, samps in top_texts.items() if len(samps) >= threshold)
    )
    return PageBreakStats(
        page_count_distribution=distribution,
        max_content_y_fraction=round(max_y_fraction, 3),
        running_headers=running_headers,
    )


# --------------------------------------------------------------------------- #
# Page geometry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PageGeometry:
    """Modal page size with content-bbox-derived margin estimates (points)."""

    width: float
    height: float
    margin_left: float
    margin_right: float
    margin_top: float
    margin_bottom: float


def _quantile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolated quantile of an already-sorted list."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    low = math.floor(pos)
    high = math.ceil(pos)
    if low == high:
        return sorted_values[low]
    frac = pos - low
    return sorted_values[low] * (1 - frac) + sorted_values[high] * frac


def infer_page_geometry(samples: Sequence[DocumentSample]) -> PageGeometry:
    """Modal page width/height; margins from content-bbox quantiles: left
    = 5th-pct span x0, right = width - 95th-pct x1, top = 5th-pct y0,
    bottom = height - 95th-pct y1 (quantiles, not min/max, shrug off a
    stray glyph). Mixed page sizes: the MODAL geometry wins; minority
    sizes are ignored."""
    sizes = Counter(size for sample in samples for size in sample.page_sizes)
    if not sizes:
        return PageGeometry(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    # Most common size; ties broken by larger area then larger width for
    # determinism.
    (width, height), _ = max(
        sizes.items(), key=lambda item: (item[1], item[0][0] * item[0][1], item[0][0])
    )

    x0s = sorted(s.bbox[0] for sample in samples for s in sample.spans)
    x1s = sorted(s.bbox[2] for sample in samples for s in sample.spans)
    y0s = sorted(s.bbox[1] for sample in samples for s in sample.spans)
    y1s = sorted(s.bbox[3] for sample in samples for s in sample.spans)
    if not x0s:
        return PageGeometry(width, height, 0.0, 0.0, 0.0, 0.0)

    margin_left = round(_quantile(x0s, 0.05), 1)
    margin_right = round(width - _quantile(x1s, 0.95), 1)
    margin_top = round(_quantile(y0s, 0.05), 1)
    margin_bottom = round(height - _quantile(y1s, 0.95), 1)
    return PageGeometry(
        width=width,
        height=height,
        margin_left=max(0.0, margin_left),
        margin_right=max(0.0, margin_right),
        margin_top=max(0.0, margin_top),
        margin_bottom=max(0.0, margin_bottom),
    )


# --------------------------------------------------------------------------- #
# Design tokens
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ColorUsage:
    """A fill color (0xRRGGBB) and how many rects carry it."""

    rgb: int
    count: int

    @property
    def hex(self) -> str:
        return f"#{self.rgb:06x}"


@dataclass(frozen=True)
class DesignTokens:
    """The drawing/typography palette: fills, stroke widths, body font."""

    fill_colors: tuple[ColorUsage, ...]
    stroke_widths: tuple[float, ...]
    body_font: str | None


def infer_design_tokens(samples: Sequence[DocumentSample]) -> DesignTokens:
    """Distinct fill colors of drawn rects with counts (the banding/header
    palette), distinct stroke widths, and the inferred body font family."""
    fill_counts: Counter[int] = Counter()
    widths: set[float] = set()
    for sample in samples:
        for rect in sample.rects:
            if rect.fill_color is not None:
                fill_counts[rect.fill_color] += 1
            if rect.stroke_color is not None and rect.stroke_width > 0:
                widths.add(rect.stroke_width)
    fill_colors = tuple(
        ColorUsage(rgb=rgb, count=count)
        for rgb, count in sorted(fill_counts.items(), key=lambda item: (-item[1], item[0]))
    )
    body_font = infer_type_scale(samples).body_font
    return DesignTokens(
        fill_colors=fill_colors, stroke_widths=tuple(sorted(widths)), body_font=body_font
    )


# --------------------------------------------------------------------------- #
# Aggregate
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PackAnalysis:
    """Frozen aggregate of the whole analysis — the item-15 emit contract."""

    sample_count: int
    type_scale: TypeScale
    sections: tuple[SectionCandidate, ...]
    static_text: tuple[str, ...]
    column_grid: ColumnGrid
    page_breaks: PageBreakStats
    page_geometry: PageGeometry
    design_tokens: DesignTokens
    dropped_curves: int = 0
    low_confidence: bool = False  # set when only one sample was analyzed
    # The provenance ledger: which pages were pixels, what was recognized, and
    # every place the two streams described the same spot. The default is an
    # all-native batch — no engine asked, nothing to review.
    evidence: LayoutEvidence = field(default_factory=LayoutEvidence)

    def summary_lines(self) -> list[str]:
        """One-screen digest: counts, roles, geometry, and the STATIC
        text. Section headings use the supermajority test, so a shared
        value can still surface here — hence the caption. A SINGLE
        sample breaks even that (threshold 1): nothing is printed,
        counts and geometry only."""
        geom = self.page_geometry
        lines = [
            f"samples analyzed: {self.sample_count}"
            + (" (single sample — low confidence)" if self.low_confidence else ""),
            f"page geometry: {geom.width:.0f}x{geom.height:.0f}pt"
            f" margins L{geom.margin_left:.0f} R{geom.margin_right:.0f}"
            f" T{geom.margin_top:.0f} B{geom.margin_bottom:.0f}pt",
            "type scale:",
        ]
        for level in self.type_scale.levels:
            weight = "bold" if level.bold else "regular"
            lines.append(
                f"  {level.role}: {level.size:.1f}pt {level.font} ({weight}, {level.count} chars)"
            )
        if self.low_confidence:
            # One sample: "static" is indistinguishable from per-patient data,
            # so no span text may be echoed (PHI rule). Counts only.
            lines.append(
                f"section headings: {len(self.sections)} candidate(s) — text suppressed"
                " (single sample: static vs per-patient indistinguishable)"
            )
            lines.append(f"static labels: {len(self.static_text)} — text suppressed")
        else:
            lines.append(f"section headings ({len(self.sections)}):")
            lines.extend(
                f"  [{c.role}] {c.text} (in {c.count}/{self.sample_count})" for c in self.sections
            )
            lines.append(f"static labels ({len(self.static_text)}): " + ", ".join(self.static_text))
        lines.append(
            f"columns: {len(self.column_grid.columns)}"
            f" at {[c.x0 for c in self.column_grid.columns]}"
        )
        lines.append(
            "fill colors: "
            + ", ".join(f"{c.hex}x{c.count}" for c in self.design_tokens.fill_colors)
        )
        lines.append(
            f"pages per sample: {dict(self.page_breaks.page_count_distribution)};"
            f" content bottom <= {self.page_breaks.max_content_y_fraction:.2f}"
        )
        lines.extend(self.evidence_lines())
        return lines

    def evidence_lines(self) -> list[str]:
        """The provenance block: page classes, OCR counts, held
        conflicts. Empty when no page was recognized; otherwise opens
        with the sentence governing what the recognized half of this
        pack may be used for. Counts and integers only — a conflict is
        about a VALUE, so its text is never quoted here."""
        evidence = self.evidence
        if not evidence.review_required:
            return []
        classes = ", ".join(f"{name} {count}" for name, count in evidence.class_counts.items())
        lines = [
            OCR_EVIDENCE_CAVEAT,
            f"page provenance: {classes}",
            f"OCR observations: {evidence.ocr_token_count} token(s),"
            f" {evidence.ocr_accepted_count} used as layout evidence,"
            f" {evidence.below_confidence_count} below the confidence threshold",
            f"native/OCR overlaps held for review: {evidence.duplicate_count} duplicate(s),"
            f" {evidence.disagreement_count} disagreement(s) — neither was resolved",
        ]
        if evidence.ocr_manifest:
            lines.append(
                "OCR engine: " + ", ".join(f"{key}={value}" for key, value in evidence.ocr_manifest)
            )
        return lines


def analyze(samples: Sequence[DocumentSample]) -> PackAnalysis:
    """Run every inference over the samples and freeze the aggregate."""
    return PackAnalysis(
        sample_count=len(samples),
        type_scale=infer_type_scale(samples),
        sections=tuple(infer_section_taxonomy(samples)),
        static_text=tuple(infer_static_text(samples)),
        column_grid=infer_column_grid(samples),
        page_breaks=infer_page_breaks(samples),
        page_geometry=infer_page_geometry(samples),
        design_tokens=infer_design_tokens(samples),
        dropped_curves=sum(sample.dropped_curves for sample in samples),
        low_confidence=len(samples) <= 1,
        evidence=merge_evidence([sample.evidence for sample in samples]),
    )
