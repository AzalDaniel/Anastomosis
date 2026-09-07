"""The QA check contract and registry.

A check is a named object whose ``run(pdf_path, ctx)`` returns a verdict
plus human-readable findings. Checks never raise for document problems — a
problem is a finding; an exception is a bug in the check.

Findings may quote chart values (they live next to the charts, inside the
hardened output directory) but must never be logged — loggers get verdict
counts only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from anastomosis.core.model import Encounter, PatientRecord

__all__ = ["CheckResult", "QACheck", "QAContext", "Verdict", "engine_checks", "register_check"]


class Verdict(StrEnum):
    PASS = "pass"  # noqa: S105 — verdict label, not a password
    WARN = "warn"
    FAIL = "fail"

    @property
    def rank(self) -> int:
        return {"pass": 0, "warn": 1, "fail": 2}[self.value]


@dataclass(frozen=True)
class CheckResult:
    check: str
    verdict: Verdict
    findings: list[str] = field(default_factory=list)
    #: Record items the layout has no place for; only the coverage check sets
    #: it, so the run-level summary can be accurate even when every check passes.
    not_carried: int = 0


@dataclass(frozen=True)
class QAContext:
    """Everything a check may compare the PDF against."""

    encounter: Encounter
    record: PatientRecord
    section_flags: dict[str, bool] = field(default_factory=dict)
    page_size: str = "Letter"
    #: The pack's timezone; ``None`` (the C-CDA path, a third-party context)
    #: falls back to the host's day.
    render_tz: str | None = None
    #: How many render-day date stamps this layout places on purpose. More
    #: than this is the accidental-now() defect the staleness check exists for.
    render_day_stamps: int = 0
    #: The record kinds this layout renders (see ``PackCoverage``). A kind here
    #: that reaches no page is a defect.
    carries: frozenset[str] = frozenset()
    #: The record kinds this layout has no place for, each with the reason —
    #: never graded as a pass with nothing said.
    omits: dict[str, str] = field(default_factory=dict)
    #: This patient's rendered whole-record summary, if any. ``None`` means
    #: nothing was rendered, never that a check declined to look — a fact no
    #: per-encounter chart can place may still be on this page
    #: (:class:`~anastomosis.qa.checks.UnattributedVitalsCheck`). Read through
    #: the shared ``_document_text(path, ctx)`` cache, keyed by path (#398).
    record_summary_path: Path | None = None

    @property
    def coverage_declared(self) -> bool:
        """Did whoever built this context say what the layout carries?

        No is not a pass. It means the coverage check has to verify every kind
        and soften its verdict, because it cannot tell a defect from a design.
        """
        return bool(self.carries or self.omits)


class QACheck(Protocol):
    name: str

    def run(self, pdf_path: Path, ctx: QAContext) -> CheckResult: ...


_REGISTRY: dict[str, QACheck] = {}


def register_check(check: QACheck) -> QACheck:
    if check.name in _REGISTRY:
        raise ValueError(f"QA check {check.name!r} is already registered")
    _REGISTRY[check.name] = check
    return check


def engine_checks() -> list[QACheck]:
    """All registered checks, stable order."""
    return [_REGISTRY[name] for name in sorted(_REGISTRY)]
