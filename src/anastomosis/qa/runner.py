"""Run QA checks over a batch of rendered documents and report.

The JSON report lands inside the (hardened) output directory next to the
charts it describes — findings may quote chart values there. Anything that
goes to a *logger* is verdict counts only.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from anastomosis.core.atomic import atomic_write_text
from anastomosis.core.clock import now as _clock_now
from anastomosis.core.model import Encounter, PatientRecord
from anastomosis.core.output import secure_output_dir

from . import checks as _checks  # registers the engine checks; also primes the shared snapshot
from .base import CheckResult, QACheck, QAContext, Verdict, engine_checks

__all__ = ["DocumentQA", "QAReport", "run_qa", "write_report"]

REPORT_NAME = "qa_report.json"


@dataclass
class DocumentQA:
    path: Path
    encounter_id: str
    results: list[CheckResult] = field(default_factory=list)

    @property
    def verdict(self) -> Verdict:
        return max(
            (result.verdict for result in self.results),
            key=lambda v: v.rank,
            default=Verdict.PASS,
        )


@dataclass
class QAReport:
    documents: list[DocumentQA] = field(default_factory=list)

    def count(self, verdict: Verdict) -> int:
        return sum(1 for doc in self.documents if doc.verdict is verdict)

    @property
    def not_carried(self) -> int:
        """Record items this run's layout had no place for, across every chart.

        Three green counts over a batch that dropped the problem list is an
        accurate statement about the checks and a false one about the run, so
        the number travels up to the stage rail beside them.
        """
        return sum(r.not_carried for doc in self.documents for r in doc.results)

    @property
    def ok(self) -> bool:
        return self.count(Verdict.FAIL) == 0


def run_qa(
    documents: Iterable[tuple[Path, Encounter, PatientRecord]],
    *,
    section_flags: dict[str, bool] | None = None,
    page_size: str = "Letter",
    render_tz: str | None = None,
    render_day_stamps: int = 0,
    carries: frozenset[str] | None = None,
    omits: dict[str, str] | None = None,
    checks: list[QACheck] | None = None,
    record_summary_paths: dict[str, Path] | None = None,
) -> QAReport:
    """Apply every check to every document; check crashes are check bugs and
    surface as CRASH findings rather than aborting the batch.

    ``render_tz`` is the timezone the pack rendered these documents in. Pass it
    whenever one is known: a check that reasons about the render day has to read
    the same clock the pack stamped the page with, or its verdict depends on
    where the operator happens to be sitting.

    ``render_day_stamps`` is how many render-day dates the layout prints on
    purpose (see the manifest field of that name); the staleness check counts
    against it rather than treating the first one as a defect.

    ``carries``/``omits`` are the layout's own statement about which record
    kinds it renders (a pack's ``coverage`` block). Passing neither is allowed
    and means undeclared, which the coverage check treats as conservatively as
    it can — it verifies every kind and softens its verdict, because with no
    statement it cannot tell a lost section from a layout that never had one.

    ``record_summary_paths`` keys the rendered whole-patient record summary by
    ``patient.id``, so each document's ``QAContext.record_summary_path`` is the
    SAME patient's summary rather than whichever one happened to render last. A
    patient absent from the mapping (or no mapping at all) gets ``None`` —
    nothing was rendered for that whole record, not that this run declined to
    check.
    """
    active = checks if checks is not None else engine_checks()
    report = QAReport()
    for pdf_path, encounter, record in documents:
        ctx = QAContext(
            encounter=encounter,
            record=record,
            section_flags=section_flags or {},
            page_size=page_size,
            render_tz=render_tz,
            render_day_stamps=render_day_stamps,
            carries=carries or frozenset(),
            omits=omits or {},
            record_summary_path=(
                record_summary_paths.get(record.patient.id) if record_summary_paths else None
            ),
        )
        # Extract the PDF's text + geometry once for this document; the engine
        # checks share it instead of each re-opening the file (up to 4x per run).
        # Lazy, so a corrupt PDF still fails per-check below, never here.
        _checks.prime_snapshot_cache(ctx)
        doc_qa = DocumentQA(path=pdf_path, encounter_id=encounter.id)
        for check in active:
            try:
                doc_qa.results.append(check.run(pdf_path, ctx))
            except Exception as exc:
                doc_qa.results.append(
                    CheckResult(check.name, Verdict.FAIL, [f"CHECK CRASHED: {type(exc).__name__}"])
                )
        report.documents.append(doc_qa)
    return report


def write_report(report: QAReport, out_dir: Path) -> Path:
    payload = {
        "generated_at": _clock_now().isoformat(),
        "summary": {
            **{v.value: report.count(v) for v in Verdict},
            "not_carried": report.not_carried,
        },
        "documents": [
            {
                "file": doc.path.name,
                "encounter_id": doc.encounter_id,
                "verdict": doc.verdict.value,
                "checks": [
                    {"check": r.check, "verdict": r.verdict.value, "findings": r.findings}
                    for r in doc.results
                ],
            }
            for doc in report.documents
        ],
    }
    # The report embeds DataIntegrityCheck findings, which can contain literal
    # patient names ("patient name ... not found"). Harden the directory to
    # 0o700 HERE rather than trusting every caller to have done so — the function
    # that writes the PHI owns its boundary. Idempotent: in the normal pipeline
    # the dir is already secured, so this is a no-op.
    out_dir = secure_output_dir(out_dir)
    target = out_dir / REPORT_NAME
    # Atomic write: a reader (or a concurrent run) never sees a half-written report.
    atomic_write_text(target, json.dumps(payload, indent=2))
    return target
