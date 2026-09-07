"""QA for a document that stands for the whole record, not one visit.

The C-CDA migration's HL7 stylesheet page and the pack-mode record summary
are the same kind of document and are graded the same way: only
:data:`DOC_GENERIC_CHECKS` apply, and the rest are recorded as skipped with
a reason (:data:`ENCOUNTER_SCOPED_SKIPS`), never silently omitted.
``carries`` is every :data:`~anastomosis.core.model.CHARTABLE_KINDS` kind, so
a fact family missing from the page is a defect, not a layout choice. The
two tables together must name every registered check
(``test_the_whole_patient_report_names_every_check_the_neutral_path_does``).
Findings may quote chart values; nothing here logs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from anastomosis.core.model import CHARTABLE_KINDS, Encounter, PatientRecord

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from .runner import QAReport

__all__ = [
    "DOC_GENERIC_CHECKS",
    "ENCOUNTER_SCOPED_SKIPS",
    "WHOLE_PATIENT_CARRIES",
    "WHOLE_PATIENT_PAGE_SIZE",
    "whole_patient_batch",
    "whole_patient_report",
]

#: The engine checks a whole-patient document can actually answer.
#:
#: ``unattributed_vitals`` never reads ``ctx.encounter``, only the record's
#: own observations, so it applies here too — and for this population
#: ``ctx.record_summary_path`` IS the document being graded. ``record_coverage``,
#: paired with ``carries`` below, is what can FAIL this document.
DOC_GENERIC_CHECKS: tuple[str, ...] = (
    "data_integrity",
    "layout_pagination",
    "record_coverage",
    "unattributed_vitals",
)

#: The encounter-scoped engine checks, recorded as skipped WITH A REASON (not
#: omitted). A skip is ``Verdict.PASS`` + a ``skipped: ...`` finding — the same
#: idiom ``VitalsLoincCheck`` uses when its section is disabled.
ENCOUNTER_SCOPED_SKIPS: dict[str, str] = {
    "vitals_loinc": (
        "skipped: vitals are encounter-scoped; this is a whole-patient document "
        "with no single-encounter vitals context"
    ),
    "date_staleness": (
        "skipped: date-staleness compares the chart against one encounter's date "
        "of service; this is a whole-patient document (no single DOS)"
    ),
    "note_body": (
        "skipped: note-body verifies one encounter's Subjective/Objective/"
        "Assessment/Plan bodies; this is a whole-patient document and carries no "
        "single encounter's note"
    ),
}

#: The whole-patient view always renders on Letter geometry (see
#: ``reconstruct.ccda_standard.renderer._default_renderer``).
WHOLE_PATIENT_PAGE_SIZE = "Letter"

#: Every chartable kind, because this view is the record. Named rather than
#: inlined so ``RecordCoverageCheck`` still catches a kind dropped from this
#: set (rather than never checking it); the set itself is what a test pins.
WHOLE_PATIENT_CARRIES: frozenset[str] = frozenset(CHARTABLE_KINDS)


def _anchor_record(record: PatientRecord) -> PatientRecord:
    """Blanks the patient name so ``data_integrity``'s own conditional skip
    takes over and anchors on DOB instead: the HL7 stylesheet canonicalizes
    the name (uppercase, non-breaking-space separators), which the shared
    name matcher cannot assert against the record's mixed-case spelling."""
    patient = record.patient.model_copy(
        update={
            "given_name": None,
            "middle_name": None,
            "family_name": None,
            "suffix": None,
        }
    )
    return record.model_copy(update={"patient": patient})


def whole_patient_batch(
    documents: Iterable[tuple[Path, PatientRecord]],
) -> list[tuple[Path, Encounter, PatientRecord]]:
    """``(pdf, record)`` pairs to the ``(pdf, encounter, record)`` triples
    :func:`~anastomosis.qa.runner.run_qa` takes, standing in a synthetic
    encounter (the patient's own id, no date of service) so the DOS
    sub-checks self-skip rather than grade against a visit that isn't one."""
    return [
        (
            path,
            Encounter(id=record.patient.id, patient_id=record.patient.id),
            _anchor_record(record),
        )
        for path, record in documents
    ]


def whole_patient_report(documents: Iterable[tuple[Path, PatientRecord]]) -> QAReport:
    """Grade whole-patient documents and return the report, never written
    here — the caller decides (settle it alone, or merge it into the
    per-encounter report). ``documents`` is materialized because it also
    builds the patient-id -> path map ``unattributed_vitals`` reads: for
    this population the graded document IS the record summary."""
    from .base import CheckResult, Verdict, engine_checks
    from .runner import run_qa

    docs = list(documents)
    by_name = {check.name: check for check in engine_checks()}
    # Keyed by patient id, not the record object, so it still agrees with
    # `run_qa`'s lookup after `_anchor_record` copies the record.
    summary_paths = {record.patient.id: path for path, record in docs}
    report = run_qa(
        whole_patient_batch(docs),
        section_flags={},
        page_size=WHOLE_PATIENT_PAGE_SIZE,
        carries=WHOLE_PATIENT_CARRIES,
        checks=[by_name[name] for name in DOC_GENERIC_CHECKS],
        record_summary_paths=summary_paths,
    )
    for doc_qa in report.documents:
        for name, reason in ENCOUNTER_SCOPED_SKIPS.items():
            doc_qa.results.append(CheckResult(name, Verdict.PASS, [reason]))
    return report
