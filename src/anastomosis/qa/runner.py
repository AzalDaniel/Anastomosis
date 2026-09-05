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
        """Record items this run's layout had no place for, across every
        chart — travels to the stage rail because an all-green check count
        would otherwise read a dropped section as a pass."""
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
    """Contract: applies every check to every document; a check that raises
    surfaces as a CRASH finding rather than aborting the batch. ``render_tz``
    is the clock a render-day check must use instead of the operator's own.
    ``render_day_stamps`` is how many render-day dates the layout prints on
    purpose. ``carries``/``omits`` are the pack's own coverage declaration;
    neither given means undeclared, and the coverage check verifies every
    kind rather than assume a defect. ``record_summary_paths`` keys the
    rendered whole-patient summary by ``patient.id`` so a document never gets
    another patient's summary; a patient absent gets ``None`` (nothing
    rendered, not "declined to check")."""
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
        # Primes the shared per-document snapshot cache so checks don't each
        # reopen the file; lazy, so a corrupt PDF fails per-check, never here.
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
    # Findings can embed literal patient text; the function that writes it
    # owns hardening the directory, rather than trusting every caller.
    out_dir = secure_output_dir(out_dir)
    target = out_dir / REPORT_NAME
    atomic_write_text(target, json.dumps(payload, indent=2))
    return target
