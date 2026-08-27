"""QA: every reconstructed document is verified before it ships — nothing
leaves the pipeline unchecked.

    check.run(pdf_path, ctx) -> CheckResult(verdict=pass|warn|fail, findings)

Engine checks (this package) apply to every pack; packs add their own
layout-specific checks through the same registry.
"""

from .base import CheckResult, QACheck, QAContext, Verdict, engine_checks, register_check
from .runner import DocumentQA, QAReport, run_qa, write_report

__all__ = [
    "CheckResult",
    "DocumentQA",
    "QACheck",
    "QAContext",
    "QAReport",
    "Verdict",
    "engine_checks",
    "register_check",
    "run_qa",
    "write_report",
]
