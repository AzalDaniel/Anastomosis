"""Engine-level QA checks: pack-independent verification of every PDF.

These read the PDF back with PyMuPDF and compare it against the canonical
record it was rendered from — the document either carries the chart or it
doesn't ship.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from anastomosis.core.clock import now as _clock_now
from anastomosis.core.clock import today as _clock_today
from anastomosis.core.identity import (
    date_token_present,
    date_token_spans,
    name_fragment_present,
    token_present,
)
from anastomosis.core.model import (
    CHARTABLE_KINDS,
    Encounter,
    Observation,
    ObservationCategory,
    PatientRecord,
)
from anastomosis.core.timeutil import all_date_spellings, to_local

from .base import CheckResult, QAContext, Verdict, register_check

__all__ = [
    "DataIntegrityCheck",
    "DateStalenessCheck",
    "LayoutPaginationCheck",
    "NoteBodyCheck",
    "RecordCoverageCheck",
    "VitalsLoincCheck",
]

# US Letter in PDF points; A4 for completeness.
_PAGE_SIZES = {"Letter": (612.0, 792.0), "A4": (595.0, 842.0)}

# Where the runner stashes the shared per-document snapshot on the (frozen)
# QAContext. Read via getattr so a bare ctx (a third-party QA pack building its
# own context) simply has no cache and each check opens the file itself.
_CACHE_ATTR = "_qa_page_snapshot"


@dataclass(frozen=True)
class PageInfo:
    """One rendered page captured in a single PDF open: its text and geometry.

    LayoutPagination needs page geometry; the other checks only join the text.
    Capturing both once lets every engine check share one ``pymupdf.open``.
    """

    text: str
    width: float
    height: float


def _open_snapshot(pdf_path: Path) -> list[PageInfo]:
    """Opens the PDF once and captures per-page text + geometry, shared via
    :func:`prime_snapshot_cache` so no check calls ``pymupdf.open`` twice.
    Called only from inside a check, so a corrupt PDF surfaces as that
    check's CHECK CRASHED finding rather than aborting the batch."""
    # Imported here, not at module scope: pymupdf is the `render` extra, and a
    # base install must still be able to import this module (e.g. `anast doctor`).
    import pymupdf

    with pymupdf.open(pdf_path) as doc:  # type: ignore[no-untyped-call]
        return [
            PageInfo(text=page.get_text(), width=page.rect.width, height=page.rect.height)
            for page in doc
        ]


class _SnapshotCache:
    """A lazily-populated page snapshot per PDF path, shared across a run's
    checks. Keyed by path, not a single slot: a context can grade more than
    one document (#392), and unbounded on purpose since a context grades a
    handful, not thousands."""

    __slots__ = ("_pages",)

    def __init__(self) -> None:
        self._pages: dict[Path, list[PageInfo]] = {}

    def get(self, pdf_path: Path) -> list[PageInfo]:
        if pdf_path not in self._pages:
            self._pages[pdf_path] = _open_snapshot(pdf_path)
        return self._pages[pdf_path]


def prime_snapshot_cache(ctx: QAContext) -> None:
    """Attaches a lazy per-document cache to ``ctx`` via
    ``object.__setattr__`` (``QAContext`` is frozen). Lazy, so a corrupt PDF
    still surfaces as the first check's CHECK CRASHED finding, never here."""
    object.__setattr__(ctx, _CACHE_ATTR, _SnapshotCache())


def _snapshot(pdf_path: Path, ctx: QAContext) -> list[PageInfo]:
    """The per-page snapshot for ``pdf_path``, from the shared cache when the
    runner primed one, else opened directly (bare third-party ctx)."""
    cache: _SnapshotCache | None = getattr(ctx, _CACHE_ATTR, None)
    if cache is None:
        return _open_snapshot(pdf_path)
    return cache.get(pdf_path)


def _document_text(pdf_path: Path, ctx: QAContext) -> str:
    """The whole document's text, joined from the shared per-document snapshot."""
    return "\n".join(page.text for page in _snapshot(pdf_path, ctx))


def _present(needle: str, text: str) -> bool:
    """Boundary-anchored presence for a VALUE: rejects a match embedded in
    adjacent word/number characters (a "98" inside "1980", a "4" inside
    "Room 4B"). Names go through :func:`_name_present`, dates through
    :func:`_date_present` — the identity module keeps the three families
    apart on purpose."""
    return token_present(needle, text)


def _name_present(name: str, text: str) -> bool:
    """Name-boundary presence, matching the delivery verifier's own
    predicate — the wrong-match defense (RULES.md 6)."""
    return name_fragment_present(name, text)


def _date_present(spelling: str, text: str) -> bool:
    """Date-boundary presence, matching the delivery verifier's own
    predicate (RULES.md 6)."""
    return date_token_present(spelling, text)


def _date_spellings(value: date) -> set[str]:
    """Delegates to the single canonical enumerator so this check and the
    delivery verifier can never diverge on which spellings count."""
    return all_date_spellings(value)


#: Words per matched passage. Small enough that a page break costs one passage
#: rather than the section; large enough that a passage is a phrase, not a word
#: that any chart might happen to contain.
_CHUNK_WORDS = 8

_WHITESPACE_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Whitespace-collapsed, case-folded text, for comparing prose to prose.

    The renderer re-wraps and the PDF extractor re-breaks, so line structure
    says nothing about whether the words arrived.
    """
    return _WHITESPACE_RE.sub(" ", text).strip().casefold()


def _chunks(body: str) -> list[str]:
    """``body`` as consecutive runs of :data:`_CHUNK_WORDS` words.

    A short body is one chunk, so it is all-or-nothing — which is right: there
    is no page break to forgive inside eight words.
    """
    words = _normalize(body).split()
    return [
        " ".join(words[start : start + _CHUNK_WORDS])
        for start in range(0, len(words), _CHUNK_WORDS)
    ]


def _note_bodies(encounter: Encounter, flags: dict[str, bool]) -> list[tuple[str, str]]:
    """Every piece of narrative the chart should carry, labelled by SECTION
    name only — never the text, since findings travel into logs and reports.
    A section switched off via ``flags`` is skipped, not asked about."""
    bodies = [
        (f"the {section.kind.value} section", section.text)
        for section in encounter.sections
        if (section.text or "").strip() and flags.get(section.kind.value, True)
    ]
    if flags.get("addenda", True):
        bodies += [
            (f"addendum {nth}", addendum.text)
            for nth, addendum in enumerate(encounter.addenda, start=1)
            if (addendum.text or "").strip()
        ]
    return [(label, body) for label, body in bodies if body]


class DataIntegrityCheck:
    """The wrong-chart defense: name, DOB, and DOS must be on the document."""

    name = "data_integrity"

    def run(self, pdf_path: Path, ctx: QAContext) -> CheckResult:
        text = _document_text(pdf_path, ctx)
        findings: list[str] = []
        warnings: list[str] = []

        patient = ctx.record.patient
        if patient.display_name:
            if not _name_present(patient.display_name, text):
                findings.append(f"patient name {patient.display_name!r} not found on document")
        if patient.birth_date:
            if not any(_date_present(s, text) for s in _date_spellings(patient.birth_date)):
                findings.append("date of birth not found on document")
        if not patient.display_name and not patient.birth_date:
            warnings.append("record carries no identity anchors (name/DOB) to verify")
        dos = ctx.encounter.date_of_service
        if dos and not any(_date_present(s, text) for s in _date_spellings(dos)):
            findings.append("date of service not found on document")

        if findings:
            return CheckResult(self.name, Verdict.FAIL, findings + warnings)
        return CheckResult(self.name, Verdict.WARN if warnings else Verdict.PASS, warnings)


class LayoutPaginationCheck:
    """No empty documents, no blank pages, page geometry as declared."""

    name = "layout_pagination"

    def run(self, pdf_path: Path, ctx: QAContext) -> CheckResult:
        findings: list[str] = []
        warn_only = True
        snapshot = _snapshot(pdf_path, ctx)
        if not snapshot:
            return CheckResult(self.name, Verdict.FAIL, ["document has no pages"])
        expected = _PAGE_SIZES.get(ctx.page_size)
        if expected is None:
            findings.append(f"unrecognized page size {ctx.page_size!r}: geometry not verified")
        for index, page in enumerate(snapshot, start=1):
            if not page.text.strip():
                findings.append(f"page {index} is blank")
                warn_only = False
            if expected is not None:
                width, height = page.width, page.height
                if abs(width - expected[0]) > 2 or abs(height - expected[1]) > 2:
                    findings.append(
                        f"page {index} is {width:.0f}x{height:.0f}pt, expected {ctx.page_size}"
                    )
        if not findings:
            return CheckResult(self.name, Verdict.PASS, [])
        return CheckResult(self.name, Verdict.WARN if warn_only else Verdict.FAIL, findings)


def _dangling_encounter_findings(record: PatientRecord, known: set[str]) -> list[str]:
    """The second, worse arm of :class:`UnattributedVitalsCheck`: an
    observation naming an encounter this record does not contain — always a
    defect, so it is not this module's concern whether one rendered."""
    return [
        f"{obs.display or obs.code or 'observation'} names an encounter this record does not have"
        for obs in record.observations
        if obs.encounter_id is not None and obs.encounter_id not in known
    ]


def _unattributed_vitals(record: PatientRecord) -> list[Observation]:
    """Vital signs with no encounter at all — the first arm's candidates."""
    return [
        obs
        for obs in record.observations
        if obs.encounter_id is None and obs.category == ObservationCategory.VITAL_SIGNS
    ]


def _graded_against_summary(
    unattributed: list[Observation], ctx: QAContext
) -> tuple[list[str], int]:
    """Splits ``unattributed`` into (labels on no chart, count carried by
    the summary) by reading the page, never inferring from the record; the
    caller skips this entirely when there's nothing to check."""
    summary_text = (
        _document_text(ctx.record_summary_path, ctx)
        if ctx.record_summary_path is not None
        else None
    )
    no_chart: list[str] = []
    carried = 0
    for obs in unattributed:
        label = obs.display or obs.code or "observation"
        if summary_text is not None and _present(obs.value or "", summary_text):
            carried += 1
        else:
            no_chart.append(label)
    return no_chart, carried


def _no_chart_findings(no_chart: list[str], *, summary_rendered: bool) -> list[str]:
    """One finding per vital that reached no chart, worded for WHY: no summary
    was rendered at all, or one was rendered and still does not carry it —
    rendering a page is not the same as the value being on it.
    """
    if not no_chart:
        return []
    reason = (
        "is on no encounter and not on the record summary either, so it is on no chart"
        if summary_rendered
        else "is on no encounter, so it is on no chart"
    )
    return [f"vital {label} {reason}" for label in no_chart]


class UnattributedVitalsCheck:
    """Contract: a vital with no ``encounter_id`` is graded against
    ``ctx.record_summary_path``: no summary → FAIL; on the summary → WARN;
    rendered but absent → FAIL. An observation naming an encounter the
    record lacks is always FAIL — no summary can rescue that one
    (`RULES_CANDIDATES.md` #4)."""

    name = "unattributed_vitals"

    def run(self, pdf_path: Path, ctx: QAContext) -> CheckResult:
        known = {enc.id for enc in ctx.record.encounters}
        findings = _dangling_encounter_findings(ctx.record, known)

        unattributed = _unattributed_vitals(ctx.record)
        no_chart, carried_by_summary = (
            _graded_against_summary(unattributed, ctx) if unattributed else ([], 0)
        )
        findings.extend(
            _no_chart_findings(no_chart, summary_rendered=ctx.record_summary_path is not None)
        )

        verdict = Verdict.FAIL if findings else Verdict.PASS
        if carried_by_summary:
            findings.append(
                f"{carried_by_summary} vital(s) are on no encounter, so no chart carries the "
                "visit link, but the value is on the record summary"
            )
            if verdict is Verdict.PASS:
                verdict = Verdict.WARN
        return CheckResult(self.name, verdict, findings)


#: RESULTS categories: something measured and reported back. Vitals are
#: excluded (encounter-scoped, checked elsewhere); social history and
#: screening are excluded (their own sections). OTHER is included on
#: purpose: an unclassified value is the last thing that should go unlooked.
_RESULT_CATEGORIES = frozenset({ObservationCategory.LABORATORY, ObservationCategory.OTHER})


def _first(*candidates: str | None) -> str | None:
    """The first field with something in it: what a person reads before
    what a machine reads, so a diagnosis printed as its ICD-10 still counts."""
    return next((value for value in candidates if value), None)


#: Where each chartable kind lives in the record, and how one of its items
#: would be named on a page. One entry per item, including unnamed ones: a
#: source that lost every label must not read as full coverage.
_COVERAGE_LABELS: dict[str, Callable[[PatientRecord], list[str | None]]] = {
    "conditions": lambda r: [_first(c.display, c.icd10, c.snomed) for c in r.conditions],
    "allergies": lambda r: [a.substance for a in r.allergies],
    "medications": lambda r: [
        _first(m.display_name, m.generic_name, m.brand_name) for m in r.medications
    ],
    "immunizations": lambda r: [i.vaccine for i in r.immunizations],
    "results": lambda r: [
        _first(o.display, o.code) for o in r.observations if o.category in _RESULT_CATEGORIES
    ],
}


def _coverage_labels(record: PatientRecord, kind: str) -> list[str | None]:
    """KeyError, not an empty list, for an unknown ``kind``: callers iterate
    CHARTABLE_KINDS, so a miss means this table fell behind the vocabulary."""
    return _COVERAGE_LABELS[kind](record)


def _label_present(label: str, normalized_text: str) -> bool:
    """Word-boundary match on already-normalized text (the renderer
    re-wraps, packs disagree on case). Not the identity predicates: a
    diagnosis isn't a name and skips their hyphen/apostrophe rules."""
    needle = _normalize(label)
    if not needle:
        return False
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", normalized_text) is not None


@dataclass(frozen=True)
class _KindCoverage:
    """What one record kind did on one page."""

    kind: str
    held: int  # items in the record
    named: int  # of those, ones with a label to look for
    found: int  # of those, ones on the page


def _kind_coverage(record: PatientRecord, kind: str, text: str) -> _KindCoverage:
    labels = _coverage_labels(record, kind)
    named = [label for label in labels if label]
    found = sum(1 for label in named if _label_present(label, text))
    return _KindCoverage(kind=kind, held=len(labels), named=len(named), found=found)


def _unlookable(cov: _KindCoverage) -> list[str]:
    """Items of this kind that carry nothing to search the page for."""
    if cov.named == cov.held:
        return []
    return [
        f"{cov.held - cov.named} {cov.kind} carry no description or code to look for on the page"
    ]


def _absent_from_page(cov: _KindCoverage) -> list[str]:
    """The some/none boundary is the whole signal: a partial miss is not
    reported, only a total one — that's what keeps this quiet on ordinary
    charts."""
    if not cov.named or cov.found:
        return []
    return [f"none of the {cov.named} {cov.kind} in the record are on this chart"]


class RecordCoverageCheck:
    """Contract: per :data:`CHARTABLE_KINDS` kind the record holds, FAILs
    only a total absence — a partial miss is not reported. A kind the pack
    claims but is wholly absent is FAIL; one excused via ``omits`` counts
    toward ``not_carried`` instead; an undeclared pack WARNs. Findings name
    kinds and counts only, never a value (`RULES_CANDIDATES.md` #5)."""

    name = "record_coverage"

    def run(self, pdf_path: Path, ctx: QAContext) -> CheckResult:
        text = _normalize(_document_text(pdf_path, ctx))
        seen = [_kind_coverage(ctx.record, kind, text) for kind in CHARTABLE_KINDS]
        held = [cov for cov in seen if cov.held]
        if not held:
            return CheckResult(
                self.name,
                Verdict.PASS,
                ["record carries none of " + ", ".join(CHARTABLE_KINDS) + ": nothing to compare"],
            )

        findings: list[str] = []
        absent: list[str] = []
        not_carried = 0
        for cov in held:
            reason = ctx.omits.get(cov.kind)
            if reason is not None:
                not_carried += cov.held
                findings.append(f"{cov.held} {cov.kind} not carried by this layout: {reason}")
                continue
            findings.extend(_unlookable(cov))
            absent.extend(_absent_from_page(cov))

        if not ctx.coverage_declared:
            findings.append(
                "this pack does not say what its layout carries "
                "(pack.yaml: coverage.carries / coverage.omits), so an absence "
                "below is a warning rather than a failure"
            )
        verdict = Verdict.PASS
        if absent:
            verdict = Verdict.FAIL if ctx.coverage_declared else Verdict.WARN
        return CheckResult(self.name, verdict, absent + findings, not_carried=not_carried)


def _charted_vitals(ctx: QAContext) -> list[Observation]:
    """This encounter's vital signs that carry a value to look for."""
    return [
        obs
        for obs in ctx.record.observations_for(ctx.encounter.id)
        if obs.category == ObservationCategory.VITAL_SIGNS and obs.value
    ]


class VitalsLoincCheck:
    """Every charted vital value for the encounter appears on the document."""

    name = "vitals_loinc"

    def run(self, pdf_path: Path, ctx: QAContext) -> CheckResult:
        if not ctx.section_flags.get("vitals", True):
            return CheckResult(self.name, Verdict.PASS, ["vitals section disabled by flags"])
        charted = _charted_vitals(ctx)
        if not charted:
            # State it explicitly: a bare pass on "nothing to check" reads as
            # quality, not absence of data.
            return CheckResult(
                self.name, Verdict.PASS, ["no vital signs on this encounter to verify"]
            )
        text = _document_text(pdf_path, ctx)
        findings = [
            f"vital {obs.display or obs.code} value {obs.value!r} not found"
            for obs in charted
            if not _present(obs.value or "", text)
        ]
        return CheckResult(self.name, Verdict.FAIL if findings else Verdict.PASS, findings)


def _render_day(ctx: QAContext) -> date:
    """The day it was where the chart was rendered, via the same
    ``to_local`` the packs stamp dates with, so the check and the page can't
    disagree about "today". No pack timezone (the C-CDA path) falls back to
    the host's day."""
    if ctx.render_tz is None:
        return _clock_today()  # no pack clock to borrow; the host's day it is
    return to_local(_clock_now(), ctx.render_tz).date()


def _render_day_occurrences(today: date, text: str) -> int:
    """Counts today's date across every accepted spelling, unioned by
    position so one printed date is never counted twice under two
    spellings."""
    spans: set[tuple[int, int]] = set()
    for spelling in _date_spellings(today):
        spans.update(date_token_spans(spelling, text))
    return len(spans)


class DateStalenessCheck:
    """Contract: a render-day date usually means a template called now(),
    but a layout may declare how many it prints on purpose
    (``render_day_stamps``). WARNs only when the page shows more than
    declared; under-declaring still warns rather than being exempted."""

    name = "date_staleness"

    def run(self, pdf_path: Path, ctx: QAContext) -> CheckResult:
        today = _render_day(ctx)
        if ctx.encounter.date_of_service == today:
            return CheckResult(self.name, Verdict.PASS, [])
        found = _render_day_occurrences(today, _document_text(pdf_path, ctx))
        declared = ctx.render_day_stamps
        if found <= declared:
            if declared:
                return CheckResult(
                    self.name,
                    Verdict.PASS,
                    [f"{found} of {declared} declared render-day stamp(s) on the page"],
                )
            return CheckResult(self.name, Verdict.PASS, [])
        return CheckResult(
            self.name,
            Verdict.WARN,
            [
                f"today's date appears {found} time(s) on a chart dated "
                f"{ctx.encounter.date_of_service}; this layout declares {declared}"
            ],
        )


class NoteBodyCheck:
    """Contract: matches the note body in word chunks (a section can
    straddle a page break). Wholly absent → FAIL; partly absent → WARN,
    since that also matches a page-boundary artifact. A body of only a
    few words can pass unrendered: one short passage may appear elsewhere
    on the chart by coincidence."""

    name = "note_body"

    def run(self, pdf_path: Path, ctx: QAContext) -> CheckResult:
        bodies = _note_bodies(ctx.encounter, ctx.section_flags)
        if not bodies:
            return CheckResult(
                self.name, Verdict.PASS, ["this encounter carries no narrative to verify"]
            )
        text = _normalize(_document_text(pdf_path, ctx))
        findings: list[str] = []
        warnings: list[str] = []

        for label, body in bodies:
            chunks = _chunks(body)
            if not chunks:
                continue
            missing = sum(1 for chunk in chunks if chunk not in text)
            if missing == len(chunks):
                findings.append(f"{label} is not on the document")
            elif missing:
                warnings.append(
                    f"{label} is only partly on the document "
                    f"({missing} of {len(chunks)} passages missing)"
                )

        if findings:
            return CheckResult(self.name, Verdict.FAIL, findings + warnings)
        return CheckResult(self.name, Verdict.WARN if warnings else Verdict.PASS, warnings)


for _check in (
    DataIntegrityCheck(),
    LayoutPaginationCheck(),
    NoteBodyCheck(),
    RecordCoverageCheck(),
    UnattributedVitalsCheck(),
    VitalsLoincCheck(),
    DateStalenessCheck(),
):
    register_check(_check)
