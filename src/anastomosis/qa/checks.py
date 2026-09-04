"""Engine-level QA checks: pack-independent verification of every PDF.

These read the PDF back with PyMuPDF and compare it against the canonical
record it was rendered from — the document either carries the chart or it
doesn't ship.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

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
    """Open the PDF once and capture per-page text + geometry.

    Text extraction is the expensive part and every engine check reads the same
    rendered document, so the runner primes a shared cache
    (:func:`prime_snapshot_cache`) and the checks read from it instead of each
    calling ``pymupdf.open``. A corrupt/unreadable PDF raises here — but this is
    always called from inside a check, so it surfaces as that check's CHECK
    CRASHED finding rather than aborting the batch.
    """
    # Imported here, not at module scope. `pymupdf` ships in the `render` extra,
    # and this module is reached from `anastomosis.deliver.archive` — so a base
    # install could not import the archive deliverer at all, and `anast doctor`
    # reported its own bundled assets as missing on a correct install.
    import pymupdf

    with pymupdf.open(pdf_path) as doc:  # type: ignore[no-untyped-call]
        return [
            PageInfo(text=page.get_text(), width=page.rect.width, height=page.rect.height)
            for page in doc
        ]


class _SnapshotCache:
    """A lazily-populated page snapshot PER PDF PATH, shared across a run's
    checks so each document is opened and text-extracted at most once.

    This is one extraction per PDF per context — not a single-document store.
    A context is created once per QA run and today's checks only ever ask it
    about the one document they are grading, so keying by path costs one dict
    and changes nothing observable yet. But a context is not contractually
    one-document: the first version of this cache kept a single slot and
    silently served it for every path asked after the first, so a check that
    later opens a SECOND document against the same context (#392 asks the
    per-encounter vitals against the whole-record summary page, not the
    per-encounter chart) would have graded the first document's text under
    the second document's name and reported a pass. Unbounded on purpose: a
    context grades a handful of documents, not thousands, so eviction would
    trade a real bug (the one above) for a fabricated ceiling.
    """

    __slots__ = ("_pages",)

    def __init__(self) -> None:
        self._pages: dict[Path, list[PageInfo]] = {}

    def get(self, pdf_path: Path) -> list[PageInfo]:
        if pdf_path not in self._pages:
            self._pages[pdf_path] = _open_snapshot(pdf_path)
        return self._pages[pdf_path]


def prime_snapshot_cache(ctx: QAContext) -> None:
    """Attach a lazy per-document snapshot cache to ``ctx`` so the run's checks
    share one ``pymupdf.open`` instead of opening the PDF once per check.

    ``QAContext`` is a frozen dataclass, so the cache slot is set through
    ``object.__setattr__`` — the same escape hatch frozen dataclasses use
    internally. The cache is lazy: a corrupt PDF still raises inside the first
    check that touches it (surfacing as that check's CHECK CRASHED finding),
    never here, so batch behavior is unchanged. Idempotent per document.
    """
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
    """Boundary-anchored presence: the value must stand alone.

    Raw substring matching is a proven false-PASS factory — a missing heart
    rate of "98" hides inside a DOB "…1980", "4" inside "Room 4B", a name
    inside a longer name, an unpadded date inside a different date. The
    lookarounds reject matches embedded in adjacent word characters or
    number runs.

    For a VALUE. A name goes through :func:`_name_present` and a date through
    :func:`_date_present` — see those. The identity module keeps three families
    apart on purpose, and this check has to pick the right one per field rather
    than treat every field as a value.
    """
    return token_present(needle, text)


def _name_present(name: str, text: str) -> bool:
    """Is this PATIENT'S name on the document, as their name?

    The name-boundary predicate, which is what the L2/L3/L6 delivery verifier
    and the browser pack both use — because intra-name joiners have to count as
    embedding. This check used the VALUE predicate, so a chart for
    "Mary-Ann Li-Wong" passed verification against a record for "Ann Li": a
    different patient's chart, marked verified, by the one check whose entire
    job is to catch that.
    """
    return name_fragment_present(name, text)


def _date_present(spelling: str, text: str) -> bool:
    """Is this date on the document?

    The date-boundary predicate, again matching the delivery verifier: a date
    has its own run-of-digits rules, and an unpadded spelling must not match
    inside a longer number.
    """
    return date_token_present(spelling, text)


def _date_spellings(value: date) -> set[str]:
    """The chart spellings the QA integrity check accepts for a date.

    Delegates to the single canonical enumerator so the QA check and the L2/L3
    delivery verifier can never diverge (they once did — the verifier accepted an
    unpadded ``M-D-YYYY`` DOB the QA check rejected, blocking a correct chart).
    """
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
    """Every piece of narrative the chart is supposed to carry, labelled.

    Labels name the SECTION, never quote it: a finding travels into logs and
    run reports, and the body is the patient's chart.

    A section the operator switched off is absent on purpose and is not asked
    about — ``addenda`` is a declared flag in the bundled packs, and a pack may
    declare one named for any section kind. Only what the chart was asked to
    carry is verified.
    """
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
    observation naming an encounter this record does not contain. No summary
    can rescue this one — the defect is the dangling reference, not where the
    value landed — so it is not this module's concern whether one rendered.
    """
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
    """Split ``unattributed`` into (labels on no chart at all, count the
    record summary carries) by reading the page, never inferring from the
    record. Read once per call, only when there is something to check against
    — a record with no unattributed vitals never pays for opening a second
    PDF (the caller skips this entirely in that case).
    """
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
    """A measurement that survived ingest and may appear on no chart at all.

    :class:`VitalsLoincCheck` asks whether the vitals FOR THIS ENCOUNTER
    reached the page. When nothing in the record names the encounter, that
    list is empty and the question answers itself — five checks pass over a
    record holding eight vitals and a chart showing none of them. That vacuous
    pass is the gap this fills, and it is the one shape the other checks
    cannot see, because they all start from what the encounter claims.

    Two failures, and they are not the same one:

    * A vital sign attached to no encounter. A blood pressure is taken at a
      visit by definition, so one that names no visit has lost the link on
      the way in, and no per-encounter section can render it — but the bundle
      also carries the whole-patient record summary (#239), and THAT page has
      no encounter to key on at all, so it is the one place an orphaned value
      can still land. Read from ``ctx.record_summary_path`` (never re-derived,
      never inferred from the record) and graded by what is actually ON it:

        - no summary was rendered for this patient (``record_summary_path`` is
          ``None``) → FAIL. Nothing verified the value landed anywhere.
        - a summary was rendered and the value is on it → WARN. The visit link
          is genuinely missing and an operator should see that, but the fact
          itself reached the bundle, so the run does not refuse it.
        - a summary was rendered and the value is NOT on it → FAIL. Rendering
          a page is not the same as the value being on it.

    * An observation of any kind pointing at an encounter this record does not
      contain. Worse than the first, because it looks attributed — the value
      names a visit, and the visit is not there. No summary can rescue this
      one: the defect is the dangling reference itself, not where the value
      landed, so it stays FAIL unconditionally.

    Deliberately silent about the third case: a NON-vital observation with no
    encounter. A smoking status is a fact about the patient rather than
    something measured at an appointment, and the C-CDA linker declines to
    place it on a visit on purpose. Flagging it would put a finding on every
    chart of every patient who has ever been asked about tobacco, and a check
    that cries on the normal case is a check operators learn to skip.

    The finding belongs to the record, not to any one document, so it is
    repeated on each chart. That is the honest reporting: every chart really
    is missing the visit link, and there is no one document to pin it to.
    """

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


#: Observation categories that are RESULTS — something measured and reported
#: back. Vital signs are excluded because they are encounter-scoped and have
#: two checks of their own; social history and screening are excluded because
#: each is somebody else's section on the chart, with its own empty state.
#: "Other" is included deliberately: an uncategorised observation is a value a
#: source handed us and we could not classify, which is the last thing that
#: should quietly go unlooked-for.
_RESULT_CATEGORIES = frozenset({ObservationCategory.LABORATORY, ObservationCategory.OTHER})


def _first(*candidates: str | None) -> str | None:
    """The first field with something in it, or None if a record item has no
    name at all. The fallback order goes from what a person reads down to what
    a machine reads — a chart printing the ICD-10 instead of the description
    has still carried the diagnosis."""
    return next((value for value in candidates if value), None)


#: Where each chartable kind lives in the record, and how one of its items
#: would be named on a page. One entry per item INCLUDING the unnamed ones: a
#: medication with no name is still a medication the record holds, and counting
#: only the nameable ones would let a source that lost every label report full
#: coverage.
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
    """How each item of ``kind`` would be named on a chart, in record order.

    ``KeyError`` for an unknown kind rather than an empty list: callers iterate
    :data:`CHARTABLE_KINDS`, so a miss means the vocabulary grew and this table
    did not, and returning nothing would read as a record holding nothing.
    """
    return _COVERAGE_LABELS[kind](record)


def _label_present(label: str, normalized_text: str) -> bool:
    """Is this clinical label on the page as its own phrase?

    Both sides are already whitespace-collapsed and case-folded by
    :func:`_normalize` — the renderer re-wraps and the extractor re-breaks, and
    packs disagree about capitalisation. The word boundaries are what stop
    "Fever" from being satisfied by "Fever blister": the same reasoning as the
    identity predicates, one step down in stakes. Not those predicates
    themselves, because a diagnosis is not a name and does not want the
    hyphen-and-apostrophe rules that keep "Ann" out of "Mary-Ann".
    """
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
    """The some/none boundary, which is the whole signal.

    A chart printing eight of eleven results is abbreviating; one printing zero
    has lost the section. Partial misses are deliberately not reported — that
    is what keeps this quiet on ordinary charts.
    """
    if not cov.named or cov.found:
        return []
    return [f"none of the {cov.named} {cov.kind} in the record are on this chart"]


class RecordCoverageCheck:
    """Did the chart carry the record, or only the parts the layout is good at?

    Every other check reads the page and asks whether what is there is
    well-formed. None of them asks whether what was in the record got there, so
    a chart could drop almost all of it and still be graded clean: a real
    Synthea patient rendered down to a header line and the words UNSIGNED NOTE,
    with five green checks under it, because each check found nothing of its
    kind to object to and nothing objects to finding nothing.

    So this one compares the two sides QA has been holding all along. For each
    kind of clinical fact the record carries, it asks whether ANY of them
    reached the page. The interesting signal is entirely at the boundary
    between some and none — a chart that prints eight of eleven results is
    abbreviating, a chart that prints zero has lost the section — so a partial
    miss is deliberately not a finding. That keeps the check quiet on the
    normal case, which is the difference between a check operators read and a
    check they learn to skip.

    Absence alone does not say whether something broke, though. A SOAP visit
    note has no problem list by design and a forensic chart replica very much
    does, and the same empty page means opposite things in the two. So the pack
    says which it is (``coverage`` in pack.yaml) and this check grades
    accordingly:

    * a kind the layout claims to carry, wholly absent — FAIL;
    * a kind the layout says it has no place for — the count is reported and
      carried up to the run summary, never graded green-with-nothing-said;
    * a pack that declares neither — every kind is checked and an absence
      WARNs, with the finding naming the field that would sharpen it. The
      conservative direction on purpose: you opt into an exemption in a
      reviewed file, you do not get one by leaving something out.

    Findings name kinds and counts, never a diagnosis or a drug — a coverage
    line travels into run reports, and the label it would quote is the chart.
    """

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
            # Say so. A check that finds nothing to check and returns a bare
            # pass lets absence of data read as presence of quality, and that
            # is exactly how a chart with no vitals on it and eight in the
            # record came back green.
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
    """The day it was where the chart was rendered.

    Goes through the same ``to_local`` the packs stamp their dates with, so the
    check and the page can never disagree about what "today" means — the
    argument already made for ``_date_spellings`` above, applied to the clock
    instead of the spelling. It was ``date.today()``, the HOST's day, and once
    the packs moved off host-local a byte-identical chart bearing its own
    render-day stamp warned on a UTC-12 machine and passed on an Eastern one.
    Silently passing is the bad half of that: the check's sensitivity moved with
    the operator.

    No pack timezone means nobody rendered in a declared zone (the C-CDA path
    builds its documents without a pack), and the host's day is all there is.
    """
    if ctx.render_tz is None:
        return date.today()  # noqa: DTZ011 — no pack clock to borrow; the host's day it is
    return to_local(datetime.now(UTC), ctx.render_tz).date()


def _render_day_occurrences(today: date, text: str) -> int:
    """How many times today's date stands alone on this page.

    Counted across every accepted spelling, unioned by POSITION rather than
    summed, so one printed date is one stamp no matter how many spellings
    reach it. With today's spelling set none of them overlap — the boundary
    rule keeps "8/29/2026" out of "08/29/2026" — so the union is defensive
    rather than load-bearing; it is what stops a spelling added later from
    silently turning a pack that stamps once into a pack that appears to stamp
    twice, which would be a false warning on the one layout this exists for.
    """
    spans: set[tuple[int, int]] = set()
    for spelling in _date_spellings(today):
        spans.update(date_token_spans(spelling, text))
    return len(spans)


class DateStalenessCheck:
    """A render-day date on the chart usually means a template used now().

    Usually, not always. The Practice Fusion replica stamps the render day into
    the medication list's "as of" heading on purpose — a forensic rule carried
    from the gold standard, which prints the day the chart was produced rather
    than the day of the visit. So that pack warned on every document it ever
    rendered, and a warning that is always on is one operators learn to skip:
    the state the check flags became indistinguishable from the state the pack
    is permanently in.

    The layout says how many stamps it places (``render_day_stamps`` in
    pack.yaml) and this counts against that number. A pack that declares one
    and prints one is doing what it said; a pack that declares one and prints
    four has a template calling now() somewhere it should not, and still warns.
    Declaring an exemption would have traded a useless warning for a blind
    check, which is the worse of the two.
    """

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
    """The clinical note reached the page.

    Four checks verified the header, the geometry, the vitals and the date
    stamp, and none of them read the note. A chart with its Subjective,
    Objective, Assessment and Plan bodies removed — headings and vitals table
    intact — passed all four. The note is the field family the README calls the
    one that routinely fails to survive a migration, and it was the one nothing
    looked at.

    Matched in word chunks rather than whole, because a long section legitimately
    straddles a page break and picks up a footer in the extracted text. A body
    that is wholly absent is a FAIL; one that is partly absent is a WARN, since
    that is the shape a page-boundary artifact takes and the shape truncation
    takes, and telling those apart is a person's job.

    ``NoteSection.text`` is the plain-text shadow the model already carries
    "for search and QA" — this is the QA half finally reading it.

    Known limit, stated rather than papered over: a body of only a few words
    ("Deferred.", "Stable.") is one short passage, and a short passage can be
    somewhere else on the chart by coincidence — so for those the check can
    pass without the section having rendered. Ruling that out needs to know
    where on the page the pack put the section, which a pack-independent check
    does not. The absent-body case this exists for is caught either way.
    """

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
