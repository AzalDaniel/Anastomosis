"""The QA check contract and registry.

Preserved verbatim from the battle-tested predecessor: a check is a named
object whose ``run(pdf_path, ctx)`` returns a verdict plus human-readable
findings. Checks never raise for document problems — a problem is a
finding; an exception is a bug in the check.

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


@dataclass(frozen=True)
class QAContext:
    """Everything a check may compare the PDF against."""

    encounter: Encounter
    record: PatientRecord
    section_flags: dict[str, bool] = field(default_factory=dict)
    page_size: str = "Letter"


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
