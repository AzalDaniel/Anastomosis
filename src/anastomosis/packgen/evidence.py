"""Page provenance: what is native, what was recognized, and where they clash.

Answers "which of :data:`PAGE_CLASSES` is this page?" and, for the raster
parts, collects an OCR observation without overwriting the native stream.
Provenance stays separate (:data:`~.ocr.NATIVE_TEXT`/``NATIVE_OR_SYNTHETIC``
vs. :data:`~.ocr.OCR_OBSERVATION`); a conflict is held, not resolved — a
duplicate is dropped and counted, a disagreement keeps both and marks the
page for review; :class:`EvidenceConflict` carries geometry and
confidence, never the disputed text. :class:`OcrRegionError` fails closed
on non-finite or off-page geometry rather than guessing a transform. PHI:
returns counts, ids and integers only."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .ocr import (
    OcrConfig,
    OcrToken,
    PageImage,
    TesseractWorker,
    above_threshold,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from .extract import Span

__all__ = [
    "AMBIGUOUS",
    "CONFLICT_DISAGREEMENT",
    "CONFLICT_DUPLICATE",
    "EMPTY",
    "IMAGE_ONLY",
    "MIXED",
    "MIXED_EVIDENCE",
    "NATIVE_ONLY",
    "PAGE_CLASSES",
    "EvidenceConflict",
    "LayoutEvidence",
    "OcrRegionError",
    "PageEvidence",
    "PageObservation",
    "normalized_text",
    "observe_page",
    "raster_regions",
]

# --------------------------------------------------------------------------- #
# Page classes — the decision record's four, plus the honest fifth
# --------------------------------------------------------------------------- #

#: Real text objects, no raster.
NATIVE_ONLY = "native_only"
#: Raster content and native text that does NOT sit on top of it — the native
#: header over a scanned body, the small label beside a large clinical image.
MIXED = "mixed"
#: Raster content and nothing selectable. 52 of the 53 measured samples.
IMAGE_ONLY = "image_only"
#: Every native span floats inside a raster that covers the page: a scan with a
#: text layer that may itself be somebody's OCR. Never "native-only".
AMBIGUOUS = "ambiguous"
#: Neither text nor raster — a vector-only or genuinely blank page.
EMPTY = "empty"

PAGE_CLASSES = (NATIVE_ONLY, MIXED, IMAGE_ONLY, AMBIGUOUS, EMPTY)

#: A candidate that both streams contributed to. Not a resolution — a label
#: saying the evidence behind it is not of one kind, so a reviewer looks.
MIXED_EVIDENCE = "mixed_evidence"

#: An OCR token overlapping a native span that says the same thing.
CONFLICT_DUPLICATE = "duplicate"
#: An OCR token overlapping a native span that says something else. Held.
CONFLICT_DISAGREEMENT = "text_disagreement"

# A token and a native span are "in the same place" at this intersection
# fraction of the token's own area. Words are small and lines are long, so the
# test is asymmetric on purpose: how much of the TOKEN the native span covers.
_OVERLAP_FRACTION = 0.5

# Above this fraction of the page covered by raster, a text layer that sits
# entirely inside the raster reads as a scan's text layer rather than as a
# native overlay on an illustration.
_SCAN_COVERAGE = 0.9

# More disjoint raster regions than this and per-region isolation stops being
# meaningful (a tiled scan); the page falls back to one full-page recognition,
# which is recorded as such.
_MAX_REGIONS = 8

# Geometry must be finite and inside the page it claims. A hair of slack
# absorbs the 0.1pt rounding the harvester applies to every box.
_BOUNDS_SLACK_PT = 1.0

_WHITESPACE_RE = re.compile(r"\s+")


class OcrRegionError(ValueError):
    """A raster region cannot be recognized against a known transform:
    raised when a region's box is not finite or falls outside its page —
    a recognition mapped through a transform nobody can name is not
    evidence. The message carries the batch-local page index and
    integers only."""


def combined_provenance(provenances: Sequence[str]) -> str:
    """The one provenance a candidate carries, or :data:`MIXED_EVIDENCE` —
    deliberately not a precedence rule: when native and recognized
    evidence both back the same heading, the answer is "both"."""
    distinct = set(provenances)
    if len(distinct) == 1:
        return distinct.pop()
    return MIXED_EVIDENCE


def normalized_text(text: str) -> str:
    """Collapse whitespace and strip — the canonical form text is compared
    in, shared by the duplicate test here and the recurrence counting in
    :mod:`anastomosis.packgen.infer`, so a recurring string reads the same
    regardless of which stream produced it."""
    return _WHITESPACE_RE.sub(" ", text).strip()


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EvidenceConflict:
    """One place the native and OCR streams describe the same pixels.
    Deliberately NO text field: a disagreement is about a value, which
    may not ride in a review record a summary might echo. Geometry, ids
    and the engine's score are enough to point a reviewer at the page."""

    page_index: int
    region_id: str
    kind: str
    ocr_bbox_pt: tuple[float, float, float, float]
    native_bbox_pt: tuple[float, float, float, float]
    ocr_confidence: float | None


@dataclass(frozen=True)
class PageEvidence:
    """What one page is made of, and what recognizing it cost and produced."""

    page_index: int
    classification: str
    native_span_count: int
    raster_region_count: int
    ocr_token_count: int = 0
    ocr_accepted_count: int = 0
    ocr_below_confidence: int = 0
    duplicate_count: int = 0
    disagreement_count: int = 0
    full_page_fallback: bool = False
    ocr_attempted: bool = False
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PageObservation:
    """A page's evidence record plus the OCR tokens promoted to layout
    signal. ``accepted`` excludes duplicates of native text but keeps
    disagreements ("preserve both and hold the page"); the caller turns
    these into spans carrying :data:`~.ocr.OCR_OBSERVATION`."""

    evidence: PageEvidence
    accepted: tuple[OcrToken, ...]
    conflicts: tuple[EvidenceConflict, ...]


@dataclass(frozen=True)
class LayoutEvidence:
    """The whole batch's provenance ledger — what the pack may claim. The
    default is what an all-native batch produces: nothing to review.
    ``ocr_texts`` lets the emitter mark exactly which lines are
    OCR-derived; ``review_required`` must never be inferred from the
    other numbers."""

    engine_available: bool = False
    ocr_manifest: tuple[tuple[str, str], ...] = ()
    pages: tuple[PageEvidence, ...] = ()
    conflicts: tuple[EvidenceConflict, ...] = ()
    ocr_texts: frozenset[str] = field(default_factory=frozenset)

    @property
    def ocr_attempted(self) -> bool:
        """Whether any page in this batch was actually put to an engine —
        asked before the emitter says a word about evidence, since naming
        it on a batch that never called OCR would change every pack
        produced before OCR existed."""
        return any(page.ocr_attempted for page in self.pages)

    @property
    def review_required(self) -> bool:
        """Whether a human must review this pack before it is trusted."""
        return bool(self.ocr_texts) or self.ocr_attempted

    @property
    def class_counts(self) -> dict[str, int]:
        """Page counts by classification, in :data:`PAGE_CLASSES` order."""
        counts = dict.fromkeys(PAGE_CLASSES, 0)
        for page in self.pages:
            counts[page.classification] += 1
        return counts

    @property
    def ocr_token_count(self) -> int:
        return sum(page.ocr_token_count for page in self.pages)

    @property
    def ocr_accepted_count(self) -> int:
        return sum(page.ocr_accepted_count for page in self.pages)

    @property
    def duplicate_count(self) -> int:
        return sum(page.duplicate_count for page in self.pages)

    @property
    def disagreement_count(self) -> int:
        return sum(page.disagreement_count for page in self.pages)

    @property
    def below_confidence_count(self) -> int:
        return sum(page.ocr_below_confidence for page in self.pages)

    def is_ocr_derived(self, text: str) -> bool:
        """Whether this exact string reached the pack through recognition."""
        return normalized_text(text) in self.ocr_texts


def merge_evidence(parts: Sequence[LayoutEvidence]) -> LayoutEvidence:
    """Folds every sample's ledger into the batch ledger the analysis
    carries. The manifest is taken from the first part that has one: every
    sample is recognized by the same worker with the same config, so a
    differing manifest cannot arise here."""
    manifest: tuple[tuple[str, str], ...] = ()
    for part in parts:
        if part.ocr_manifest:
            manifest = part.ocr_manifest
            break
    return LayoutEvidence(
        engine_available=any(part.engine_available for part in parts),
        ocr_manifest=manifest,
        pages=tuple(page for part in parts for page in part.pages),
        conflicts=tuple(conflict for part in parts for conflict in part.conflicts),
        ocr_texts=frozenset().union(*(part.ocr_texts for part in parts)) if parts else frozenset(),
    )


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #


def _finite(box: Sequence[float]) -> bool:
    """Four real, bounded numbers — ``NaN`` and runaway coordinates fail here."""
    return len(box) == 4 and all(
        isinstance(v, int | float) and v == v and abs(v) < 1e6 for v in box
    )


def _area(box: tuple[float, float, float, float]) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _intersection(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> float:
    """Area of ``a ∩ b`` in whatever single frame both are expressed in."""
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))


def _union_box(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> tuple[float, float, float, float]:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _require_unrotated(page: Any, page_index: int) -> None:
    """Refuse a rotated page: its image boxes and rendered pixmap don't
    share one obvious frame, and guessing it would be a silently wrong
    geometry rather than a refusal an operator can act on."""
    if int(getattr(page, "rotation", 0) or 0) % 360 != 0:
        raise OcrRegionError(
            f"page #{page_index} is rotated; its region transform is outside declared bounds"
        )


def raster_regions(
    page: Any, page_index: int, page_size: tuple[float, float]
) -> list[tuple[float, float, float, float]]:
    """Disjoint raster boxes on ``page``, in page points, top-to-bottom.
    Overlapping images merge into one region so a split scan's two
    halves aren't recognized (and duplicated) twice. Raises
    :class:`OcrRegionError` on a rotated page or a box that is
    non-finite or off-page; a page with no raster is exempt."""
    infos = page.get_image_info()
    if not infos:
        return []
    _require_unrotated(page, page_index)
    width, height = page_size
    boxes: list[tuple[float, float, float, float]] = []
    for info in infos:
        raw = info.get("bbox")
        if raw is None or not _finite(raw):
            raise OcrRegionError(f"page #{page_index} has a raster region with no finite box")
        box = (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))
        if (
            box[0] < -_BOUNDS_SLACK_PT
            or box[1] < -_BOUNDS_SLACK_PT
            or box[2] > width + _BOUNDS_SLACK_PT
            or box[3] > height + _BOUNDS_SLACK_PT
            or _area(box) <= 0.0
        ):
            raise OcrRegionError(
                f"page #{page_index} has a raster region outside its "
                f"{width:.0f}x{height:.0f}pt page"
            )
        boxes.append((max(box[0], 0.0), max(box[1], 0.0), min(box[2], width), min(box[3], height)))
    return _merge_overlapping(boxes)


def _absorb(
    merged: list[tuple[float, float, float, float]], box: tuple[float, float, float, float]
) -> list[tuple[float, float, float, float]]:
    """Grows ``box`` by every region it touches, repeating until it
    touches none — the repeat matters, since the enlarged box can reach a
    region an earlier pass had already cleared."""
    current = box
    changed = True
    while changed:
        changed = False
        rest: list[tuple[float, float, float, float]] = []
        for existing in merged:
            if _intersection(existing, current) > 0.0:
                current = _union_box(existing, current)
                changed = True
            else:
                rest.append(existing)
        merged = rest
    return [*merged, current]


def _merge_overlapping(
    boxes: list[tuple[float, float, float, float]],
) -> list[tuple[float, float, float, float]]:
    """Union every pair of overlapping boxes until none overlap, then sort."""
    merged: list[tuple[float, float, float, float]] = []
    for box in sorted(boxes):
        merged = _absorb(merged, box)
    return sorted(merged, key=lambda b: (b[1], b[0]))


def classify_page(
    native_boxes: Sequence[tuple[float, float, float, float]],
    regions: Sequence[tuple[float, float, float, float]],
    page_size: tuple[float, float],
) -> str:
    """Which of :data:`PAGE_CLASSES` this page is: the distinction that
    matters is a native overlay ON a scan versus a native part BESIDE
    one. When raster covers nearly the whole page and every native box
    sits inside it, the text layer is the scan's own — ``ambiguous``;
    otherwise text plus raster is ``mixed``."""
    if not regions:
        return NATIVE_ONLY if native_boxes else EMPTY
    if not native_boxes:
        return IMAGE_ONLY
    coverage = sum(_area(region) for region in regions) / max(page_size[0] * page_size[1], 1e-9)
    inside = all(
        any(_intersection(box, region) >= _area(box) * _OVERLAP_FRACTION for region in regions)
        for box in native_boxes
    )
    return AMBIGUOUS if inside and coverage >= _SCAN_COVERAGE else MIXED


# --------------------------------------------------------------------------- #
# Observation
# --------------------------------------------------------------------------- #


def _region_image(
    page: Any,
    page_index: int,
    region_index: int,
    region: tuple[float, float, float, float],
    page_size: tuple[float, float],
    config: OcrConfig,
) -> tuple[bytes, PageImage]:
    """Renders one region to PNG bytes and describes the transform exactly.
    DPI is reduced by :meth:`~.ocr.OcrConfig.dpi_for` when the region
    would exceed the pixel cap. PyMuPDF's clipped pixmap covers exactly
    the clip box at ``dpi/72`` scale with its origin at the clip's own —
    the whole of the pixel-to-point transform."""
    dpi = config.dpi_for(region[2] - region[0], region[3] - region[1])
    pixmap = page.get_pixmap(clip=region, dpi=dpi)
    png = bytes(pixmap.tobytes("png"))
    return png, PageImage(
        page_index=page_index,
        region_id=f"p{page_index}r{region_index}",
        bytes_sha256=hashlib.sha256(png).hexdigest(),
        width_px=int(pixmap.width),
        height_px=int(pixmap.height),
        dpi_x=float(dpi),
        dpi_y=float(dpi),
        rotation_deg=0.0,
        page_width_pt=page_size[0],
        page_height_pt=page_size[1],
        clip_pt=region,
    )


def _adjudicate(
    tokens: Sequence[OcrToken], native_spans: Sequence[Span]
) -> tuple[list[OcrToken], list[EvidenceConflict]]:
    """Splits recognized tokens against the native stream; never picks a
    winner. A token whose native span already SAYS it: not promoted, but
    recorded, never a silent drop. A token whose native span says
    something else is a disagreement: both survive and the page is held."""
    accepted: list[OcrToken] = []
    conflicts: list[EvidenceConflict] = []
    for token in tokens:
        overlap = _overlapping_native(token, native_spans)
        if overlap is None:
            accepted.append(token)
            continue
        # Word sets, not substring containment: a substring test would call
        # '100' a duplicate of 'Glucose 1000 mg/dL' and '8.6' of '98.6' — a
        # genuine disagreement misreported as agreement. A recognition is
        # the same reading only when every word in it is a word the native
        # span has.
        recognized = set(normalized_text(token.text).casefold().split())
        native = set(normalized_text(overlap.text).casefold().split())
        duplicate = bool(recognized) and recognized <= native
        kind = CONFLICT_DUPLICATE if duplicate else CONFLICT_DISAGREEMENT
        conflicts.append(
            EvidenceConflict(
                page_index=token.page_index,
                region_id=token.region_id,
                kind=kind,
                ocr_bbox_pt=token.bbox_page_pt,
                native_bbox_pt=overlap.bbox,
                ocr_confidence=token.confidence,
            )
        )
        if not duplicate:
            accepted.append(token)
    return accepted, conflicts


def _overlapping_native(token: OcrToken, native_spans: Sequence[Span]) -> Span | None:
    """The native span covering most of ``token``, or ``None`` if none does."""
    token_area = _area(token.bbox_page_pt)
    if token_area <= 0.0:
        return None
    best: Span | None = None
    best_overlap = token_area * _OVERLAP_FRACTION
    for span in native_spans:
        overlap = _intersection(token.bbox_page_pt, span.bbox)
        if overlap >= best_overlap:
            best, best_overlap = span, overlap
    return best


def _isolation_regions(
    regions: list[tuple[float, float, float, float]],
    page_size: tuple[float, float],
    warnings: list[str],
) -> tuple[list[tuple[float, float, float, float]], bool]:
    """Per-region recognition, or the recorded full-page fallback.
    Region-level is preferred (keeps a native overlay out of the
    recognized stream); past :data:`_MAX_REGIONS` disjoint rasters
    isolation stops being meaningful (a tiled scan), so the page is
    recognized whole and the fallback is written down."""
    if len(regions) <= _MAX_REGIONS:
        return regions, False
    warnings.append(
        f"{len(regions)} raster regions exceeded the {_MAX_REGIONS}-region isolation "
        "limit; recognized as one full-page image (full_page_fallback)"
    )
    return [(0.0, 0.0, page_size[0], page_size[1])], True


def _recognized_evidence(
    base: PageEvidence,
    *,
    regions: Sequence[tuple[float, float, float, float]],
    token_count: int,
    accepted: Sequence[OcrToken],
    conflicts: Sequence[EvidenceConflict],
    below: int,
    full_page_fallback: bool,
    warnings: Sequence[str],
) -> PageEvidence:
    """``base`` plus everything the recognition pass learned about the page."""
    duplicates = sum(1 for conflict in conflicts if conflict.kind == CONFLICT_DUPLICATE)
    return PageEvidence(
        page_index=base.page_index,
        classification=base.classification,
        native_span_count=base.native_span_count,
        raster_region_count=len(regions),
        ocr_token_count=token_count,
        ocr_accepted_count=len(accepted),
        ocr_below_confidence=below,
        duplicate_count=duplicates,
        disagreement_count=len(conflicts) - duplicates,
        full_page_fallback=full_page_fallback,
        ocr_attempted=True,
        warnings=tuple(warnings),
    )


def observe_page(
    page: Any,
    *,
    page_index: int,
    page_size: tuple[float, float],
    native_spans: Sequence[Span],
    worker: TesseractWorker | None,
) -> PageObservation:
    """Classifies one page and, where it is pixels, observes it. With no
    ``worker`` the page is still classified and recorded, so the ledger
    says an image-only page went un-recognized — letting the caller
    refuse rather than quietly emit a pack with a hole in it."""
    regions = raster_regions(page, page_index, page_size)
    base = PageEvidence(
        page_index=page_index,
        classification=classify_page([span.bbox for span in native_spans], regions, page_size),
        native_span_count=len(native_spans),
        raster_region_count=len(regions),
    )
    if not regions or worker is None:
        return PageObservation(evidence=base, accepted=(), conflicts=())

    warnings: list[str] = []
    regions, full_page = _isolation_regions(regions, page_size, warnings)
    tokens, below = _recognize_regions(page, page_index, regions, page_size, worker, warnings)
    accepted, conflicts = _adjudicate(above_threshold(tokens, worker.config), native_spans)
    return PageObservation(
        evidence=_recognized_evidence(
            base,
            regions=regions,
            token_count=len(tokens),
            accepted=accepted,
            conflicts=conflicts,
            below=below,
            full_page_fallback=full_page,
            warnings=warnings,
        ),
        accepted=tuple(accepted),
        conflicts=tuple(conflicts),
    )


def _recognize_regions(
    page: Any,
    page_index: int,
    regions: Sequence[tuple[float, float, float, float]],
    page_size: tuple[float, float],
    worker: TesseractWorker,
    warnings: list[str],
) -> tuple[list[OcrToken], int]:
    """Recognizes each region in turn, accumulating tokens and PHI-free
    warnings. Every token comes back here, low-scoring ones included,
    with how many fall under the selection threshold — a filtered token
    always has a stated reason and a number, never a silent deletion.
    Sub-threshold token text itself is not carried further."""
    tokens: list[OcrToken] = []
    below = 0
    for region_index, region in enumerate(regions):
        png, page_image = _region_image(
            page, page_index, region_index, region, page_size, worker.config
        )
        result = worker.recognize(png, page_image)
        tokens.extend(result.tokens)
        below += result.below_confidence
        warnings.extend(result.warnings)
    return tokens, below
