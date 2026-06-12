"""The statistics: infer a design system from harvested sample spans.

Stdlib only — no numpy, no torch. Every function takes the
:class:`~anastomosis.packgen.extract.DocumentSample` list produced by
:mod:`anastomosis.packgen.extract` and returns a frozen, deterministically
ordered structure. :func:`analyze` aggregates them into :class:`PackAnalysis`,
the contract the draft-pack emitter (item 15) builds on.

Clustering is intentionally simple and explainable (sorted greedy bucketing
within a tolerance), not DBSCAN — a practice operator must be able to read why
a column or type level was inferred.

PHI rule: the only span text that ever reaches a human-readable summary is the
*static* text — strings recurring across a supermajority of samples, which are
by construction template labels/headings, not per-patient values. A string
seen in only one sample is per-patient by definition and never appears in
:meth:`PackAnalysis.summary_lines`. This is asserted in the tests.

CAVEAT: the split assumes samples are DISTINCT patients/encounters — copies
of one patient's chart make that patient's values recur like template text
and surface as "static". Operator guidance, restated by the wizard.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import median

from .extract import DocumentSample, Span

__all__ = [
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

# A candidate is "static" (template, not per-patient) when it recurs across at
# least this fraction of samples.
_STATIC_FRACTION = 0.6

_HEADING_ROLES = ("h1", "h2", "h3")
_MAX_COLUMNS = 6


def _normalize(text: str) -> str:
    """Collapse whitespace and strip — the canonical form text is compared in.

    Matches the golden tooling's normalization so a heading recurs identically
    regardless of intra-span whitespace.
    """
    return re.sub(r"\s+", " ", text).strip()


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
    """Group spans into (font, size, bold) clusters with weighted counts.

    Two spans share a cluster when font + bold match and their sizes are within
    :data:`_SIZE_TOLERANCE`. Each span is weighted by its character count, so a
    one-word 13pt title does not out-vote a paragraph of 11pt body when picking
    the most-used (body) cluster. The reported size is the count-weighted mean,
    rounded to 0.1pt.
    """
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
    """Cluster every span into (font, size, bold) levels and guess roles.

    Roles: the most-used cluster is ``body``; clusters *larger* than body are
    ``h1``/``h2``/``h3`` by descending size (capped at three, the rest fall to
    the nearest); clusters smaller than body are ``small``. Bold clusters at or
    below body size that are not the body itself are also treated as headings
    (the section-band case: bold 10.5pt headings under 11pt body) and take the
    next available h-role — the PLAN's "repeated bold spans → section heading
    taxonomy" intent, see module/decisions note.
    """
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
    """A recurring heading-level string — a likely pack section heading."""

    text: str  # normalized
    role: str  # the type-scale role the span carried ("h1"/"h2"/"h3")
    count: int  # how many distinct samples contain it
    median_y_fraction: float  # vertical position (0 top, 1 bottom of page)
    all_pages_first: bool  # always on page 0?


def _static_threshold(n_samples: int) -> int:
    """Minimum sample count for "recurring" — ceil(0.6 * N), floor 1."""
    return max(1, math.ceil(_STATIC_FRACTION * n_samples))


def _heading_roles(scale: TypeScale) -> set[str]:
    return {lvl.role for lvl in scale.levels if lvl.role in _HEADING_ROLES}


def _style_role(scale: TypeScale) -> dict[tuple[str, float, bool], str]:
    return {(lvl.font, lvl.size, lvl.bold): lvl.role for lvl in scale.levels}


def infer_section_taxonomy(samples: Sequence[DocumentSample]) -> list[SectionCandidate]:
    """Recurring heading-level texts → the pack's section-heading taxonomy.

    A heading-level span is one whose (font, size, bold) cluster carries an
    h-role in the inferred type scale. A candidate is STATIC (a template
    heading, not per-patient data) when its normalized text recurs across at
    least ``ceil(0.6 * N)`` samples; per-patient data, with N >= 2 samples,
    does not recur and is dropped here.

    With a SINGLE sample everything recurs trivially (threshold 1), so every
    heading is returned flagged low-confidence (``count == 1``). More samples
    sharpen the static/per-patient split — this is documented and is why the
    e2e validation renders several encounters.
    """
    scale = infer_type_scale(samples)
    style_role = _style_role(scale)
    heading_roles = _heading_roles(scale)
    threshold = _static_threshold(len(samples))

    # text -> set(sample index), list of (y_fraction), list of page_index, role
    seen_samples: dict[str, set[int]] = {}
    y_fractions: dict[str, list[float]] = {}
    pages: dict[str, list[int]] = {}
    roles: dict[str, str] = {}
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
            )
        )
    # Top of page first, then most-recurring, then alphabetical — deterministic.
    candidates.sort(key=lambda c: (c.median_y_fraction, -c.count, c.text))
    return candidates


def infer_static_text(samples: Sequence[DocumentSample]) -> list[str]:
    """All normalized span texts recurring in >= 60% of samples — the label
    vocabulary (e.g. ``"DOB:"``, ``"Provider:"``) — minus section headings.

    These are the strings that are the SAME on every chart: field labels,
    running headers, empty-state text. Per-patient values, recurring in fewer
    samples, are excluded. Section headings (from
    :func:`infer_section_taxonomy`) are subtracted so the two outputs do not
    double-count.
    """
    threshold = _static_threshold(len(samples))
    seen: dict[str, set[int]] = {}
    for sample in samples:
        for span in sample.spans:
            text = _normalize(span.text)
            if not text:
                continue
            seen.setdefault(text, set()).add(sample.index)
    headings = {c.text for c in infer_section_taxonomy(samples)}
    static = sorted(
        text for text, samps in seen.items() if len(samps) >= threshold and text not in headings
    )
    return static


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
    """Modal page width/height; margins from content-bbox quantiles.

    Margins estimate the whitespace frame: left = 5th-percentile span x0, right
    = page width - 95th-percentile span x1, top = 5th-percentile span y0,
    bottom = page height - 95th-percentile span y1. Quantiles (not min/max)
    shrug off the occasional full-bleed rule or stray glyph.

    Mixed page sizes across samples: the MODAL geometry wins and minority
    sizes are ignored (an operator mixing Letter and A4 samples gets the
    majority's page; the wizard surfaces the inferred geometry for review).
    """
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

    def summary_lines(self) -> list[str]:
        """PHI-safe one-screen digest: counts, roles, geometry, and — only —
        the STATIC text (by construction template strings, never per-patient).

        A value appearing in just one sample is per-patient by definition and
        is excluded from ``sections``/``static_text`` upstream, so it can never
        surface here. The tests assert exactly that property.
        """
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
    )
