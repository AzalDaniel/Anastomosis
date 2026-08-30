"""QA for a document that stands for the whole record rather than one visit.

Two paths now render such a document: the ccda-standard migration renders HL7's
own view per patient as its primary artifact, and every pack-mode run renders
the same view beside the charts as the record summary. They are the SAME
document, so they have to be graded the same way — and the way is not the
per-encounter way:

* only the DOCUMENT-GENERIC engine checks can be answered by a whole-patient
  page (:data:`DOC_GENERIC_CHECKS`);
* the encounter-scoped ones are recorded as skipped WITH A REASON
  (:data:`ENCOUNTER_SCOPED_SKIPS`), never silently omitted — a report that
  quietly drops a check reads exactly like a report where the check passed;
* ``carries`` is every :data:`~anastomosis.core.model.CHARTABLE_KINDS` kind,
  because this view is HL7's stylesheet over the whole record: a fact family
  the record holds and this page does not show is a defect, not a layout
  choice. That is what makes ``record_coverage`` able to FAIL here, which is
  the whole reason the summary is in the bundle.

The two tables between them must name EVERY registered check. Nothing enforced
that when they lived in the migration module, and ``note_body`` was registered
later and landed in neither — so the one path that promised never to omit a
check omitted it. ``test_the_whole_patient_report_names_every_check_the_neutral_path_does``
is what keeps the promise honest now, and it guards both paths at once because
both read these tables.

PHI rule: this module builds contexts and returns a report. Findings may quote
chart values (the report lands inside the hardened output directory); nothing
here logs.
"""

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
#: ``unattributed_vitals`` belongs here rather than below because it never reads
#: ``ctx.encounter`` at all: it asks whether the RECORD's observations name
#: encounters the record contains, which is as fair a question of a whole-patient
#: document as of a per-encounter one.
#:
#: ``record_coverage`` is the one that earns this document its place in the
#: bundle. Paired with the ``carries`` below, it cannot pass while a fact family
#: the record holds has zero representation on the page.
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

#: Every chartable kind, because this view is the record.
#:
#: Named rather than inlined because a kind dropping out of it is INVISIBLE in
#: the verdicts: ``RecordCoverageCheck`` asks whether coverage was declared at
#: all and whether a kind is excused by ``omits``, so a kind quietly missing
#: from this set still fails when it is absent — right up until someone excuses
#: it. The set is the promise, so the set is what a test pins.
WHOLE_PATIENT_CARRIES: frozenset[str] = frozenset(CHARTABLE_KINDS)


def _anchor_record(record: PatientRecord) -> PatientRecord:
    """The record ``data_integrity`` can actually anchor on for this document.

    The HL7 stylesheet canonicalizes the patient NAME (uppercase family,
    non-breaking-space separators), so the shared name matcher cannot assert the
    mixed-case spelling the record holds. Blanking the name lets that check's OWN
    conditional skip take over (a falsy ``display_name`` skips the name
    sub-check) and leaves the DOB — which this view renders in a matchable
    spelling — as the identity anchor. Nothing is weakened silently: the check
    still has to find the DOB on the page.
    """
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
    """Turn ``(pdf, record)`` pairs into the ``(pdf, encounter, record)`` triples
    :func:`~anastomosis.qa.runner.run_qa` takes.

    A whole-patient document has no encounter, and QA's context requires one. A
    synthetic encounter carrying the patient's own id (and therefore no date of
    service) is the honest stand-in: the DOS sub-checks self-skip rather than
    grading the document against a visit it does not represent.
    """
    return [
        (
            path,
            Encounter(id=record.patient.id, patient_id=record.patient.id),
            _anchor_record(record),
        )
        for path, record in documents
    ]


def whole_patient_report(documents: Iterable[tuple[Path, PatientRecord]]) -> QAReport:
    """Grade whole-patient documents and return their report (never written here).

    The caller decides what to do with it: the migration settles it on its own,
    the pack pipeline merges it into the per-encounter report so ONE report
    describes the whole bundle. Both get the same check set, the same skips, and
    the same ``carries``.
    """
    from .base import CheckResult, Verdict, engine_checks
    from .runner import run_qa

    by_name = {check.name: check for check in engine_checks()}
    report = run_qa(
        whole_patient_batch(documents),
        section_flags={},
        page_size=WHOLE_PATIENT_PAGE_SIZE,
        # HL7's own stylesheet over the whole record, so every chartable kind IS
        # on the page and an absence is a defect rather than a layout choice.
        carries=WHOLE_PATIENT_CARRIES,
        checks=[by_name[name] for name in DOC_GENERIC_CHECKS],
    )
    for doc_qa in report.documents:
        for name, reason in ENCOUNTER_SCOPED_SKIPS.items():
            doc_qa.results.append(CheckResult(name, Verdict.PASS, [reason]))
    return report
