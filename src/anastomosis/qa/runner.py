"""Run QA checks over a batch of rendered documents and report.

The JSON report lands inside the (hardened) output directory next to the
charts it describes — findings may quote chart values there. Anything that
goes to a *logger* is verdict counts only.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from anastomosis.core.model import Encounter, PatientRecord

from . import checks as _checks  # noqa: F401 — registers the engine checks
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
    def ok(self) -> bool:
        return self.count(Verdict.FAIL) == 0


def run_qa(
    documents: Iterable[tuple[Path, Encounter, PatientRecord]],
    *,
    section_flags: dict[str, bool] | None = None,
    page_size: str = "Letter",
    checks: list[QACheck] | None = None,
) -> QAReport:
    """Apply every check to every document; check crashes are check bugs and
    surface as CRASH findings rather than aborting the batch."""
    active = checks if checks is not None else engine_checks()
    report = QAReport()
    for pdf_path, encounter, record in documents:
        ctx = QAContext(
            encounter=encounter,
            record=record,
            section_flags=section_flags or {},
            page_size=page_size,
        )
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
        "generated_at": datetime.now(UTC).isoformat(),
        "summary": {v.value: report.count(v) for v in Verdict},
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
    target = out_dir / REPORT_NAME
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return target
