"""QA: every reconstructed document is verified before it ships — nothing
leaves the pipeline unchecked.

    check.run(pdf_path, ctx) -> CheckResult(verdict=pass|warn|fail, findings)

Engine checks (this package) apply to every pack; a pack adds its own
layout-specific checks through ``run_qa(checks=...)``.
"""

from .base import CheckResult, QACheck, QAContext, Verdict
from .checks import ENGINE_CHECKS
from .runner import DocumentQA, QAReport, run_qa, write_report
from .wholepatient import whole_patient_report

__all__ = [
    "ENGINE_CHECKS",
    "CheckResult",
    "DocumentQA",
    "QACheck",
    "QAContext",
    "QAReport",
    "Verdict",
    "run_qa",
    "whole_patient_report",
    "write_report",
]
