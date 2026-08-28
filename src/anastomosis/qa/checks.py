"""Engine-level QA checks: pack-independent verification of every PDF.

These read the PDF back with PyMuPDF and compare it against the canonical
record it was rendered from — the document either carries the chart or it
doesn't ship.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pymupdf

from anastomosis.core.identity import date_token_present, name_fragment_present, token_present
from anastomosis.core.model import Encounter, ObservationCategory
from anastomosis.core.timeutil import all_date_spellings

from .base import CheckResult, QAContext, Verdict, register_check

__all__ = [
    "DataIntegrityCheck",
    "DateStalenessCheck",
    "LayoutPaginationCheck",
    "NoteBodyCheck",
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
    with pymupdf.open(pdf_path) as doc:  # type: ignore[no-untyped-call]
        return [
            PageInfo(text=page.get_text(), width=page.rect.width, height=page.rect.height)
            for page in doc
        ]


class _SnapshotCache:
    """A lazily-populated page snapshot for one document, shared across a run's
    checks so the PDF is opened and text-extracted exactly once."""

    __slots__ = ("_pages",)

    def __init__(self) -> None:
        self._pages: list[PageInfo] | None = None

    def get(self, pdf_path: Path) -> list[PageInfo]:
        if self._pages is None:
            self._pages = _open_snapshot(pdf_path)
        return self._pages


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


class VitalsLoincCheck:
    """Every charted vital value for the encounter appears on the document."""

    name = "vitals_loinc"

    def run(self, pdf_path: Path, ctx: QAContext) -> CheckResult:
        if not ctx.section_flags.get("vitals", True):
            return CheckResult(self.name, Verdict.PASS, ["vitals section disabled by flags"])
        text = _document_text(pdf_path, ctx)
        findings = [
            f"vital {obs.display or obs.code} value {obs.value!r} not found"
            for obs in ctx.record.observations_for(ctx.encounter.id)
            if obs.category == ObservationCategory.VITAL_SIGNS
            and obs.value
            and not _present(obs.value, text)
        ]
        return CheckResult(self.name, Verdict.FAIL if findings else Verdict.PASS, findings)


class DateStalenessCheck:
    """A render-day date on the chart usually means a template used now()."""

    name = "date_staleness"

    def run(self, pdf_path: Path, ctx: QAContext) -> CheckResult:
        today = date.today()  # noqa: DTZ011 — local render day is exactly the point
        if ctx.encounter.date_of_service == today:
            return CheckResult(self.name, Verdict.PASS, [])
        text = _document_text(pdf_path, ctx)
        findings = [
            f"today's date ({spelling}) appears on a chart dated {ctx.encounter.date_of_service}"
            for spelling in sorted(_date_spellings(today))
            if _date_present(spelling, text)
        ]
        return CheckResult(self.name, Verdict.WARN if findings else Verdict.PASS, findings)


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
        text = _normalize(_document_text(pdf_path, ctx))
        findings: list[str] = []
        warnings: list[str] = []

        for label, body in _note_bodies(ctx.encounter, ctx.section_flags):
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
    VitalsLoincCheck(),
    DateStalenessCheck(),
):
    register_check(_check)
