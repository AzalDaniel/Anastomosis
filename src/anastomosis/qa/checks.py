"""Engine-level QA checks: pack-independent verification of every PDF.

These read the PDF back with PyMuPDF and compare it against the canonical
record it was rendered from — the document either carries the chart or it
doesn't ship.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import fitz  # PyMuPDF

from anastomosis.core.model import ObservationCategory

from .base import CheckResult, QAContext, Verdict, register_check

__all__ = [
    "DataIntegrityCheck",
    "DateStalenessCheck",
    "LayoutPaginationCheck",
    "VitalsLoincCheck",
]

# US Letter in PDF points; A4 for completeness.
_PAGE_SIZES = {"Letter": (612.0, 792.0), "A4": (595.0, 842.0)}


def _pages_text(pdf_path: Path) -> list[str]:
    with fitz.open(pdf_path) as doc:
        return [page.get_text() for page in doc]


def _present(needle: str, text: str) -> bool:
    """Boundary-anchored presence: the value must stand alone.

    Raw substring matching is a proven false-PASS factory — a missing heart
    rate of "98" hides inside a DOB "…1980", "4" inside "Room 4B", a name
    inside a longer name, an unpadded date inside a different date. The
    lookarounds reject matches embedded in adjacent word characters or
    number runs.
    """
    return re.search(rf"(?<![\w.]){re.escape(needle)}(?![\w.])", text) is not None


def _date_spellings(value: date) -> set[str]:
    """Padded and unpadded chart spellings; %-d is glibc-only, build by hand."""
    return {
        value.strftime("%B %d, %Y"),
        value.strftime("%b %d, %Y"),
        f"{value.strftime('%B')} {value.day}, {value.year}",
        f"{value.strftime('%b')} {value.day}, {value.year}",
        value.strftime("%m/%d/%Y"),
        value.strftime("%m-%d-%Y"),
        f"{value.month}/{value.day}/{value.year}",
    }


class DataIntegrityCheck:
    """The wrong-chart defense: name, DOB, and DOS must be on the document."""

    name = "data_integrity"

    def run(self, pdf_path: Path, ctx: QAContext) -> CheckResult:
        text = "\n".join(_pages_text(pdf_path))
        findings: list[str] = []
        warnings: list[str] = []

        patient = ctx.record.patient
        if patient.display_name:
            if not _present(patient.display_name, text):
                findings.append(f"patient name {patient.display_name!r} not found on document")
        if patient.birth_date:
            if not any(_present(s, text) for s in _date_spellings(patient.birth_date)):
                findings.append("date of birth not found on document")
        if not patient.display_name and not patient.birth_date:
            warnings.append("record carries no identity anchors (name/DOB) to verify")
        dos = ctx.encounter.date_of_service
        if dos and not any(_present(s, text) for s in _date_spellings(dos)):
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
        with fitz.open(pdf_path) as doc:
            if doc.page_count == 0:
                return CheckResult(self.name, Verdict.FAIL, ["document has no pages"])
            expected = _PAGE_SIZES.get(ctx.page_size)
            if expected is None:
                findings.append(f"unrecognized page size {ctx.page_size!r}: geometry not verified")
            for index, page in enumerate(doc, start=1):
                if not page.get_text().strip():
                    findings.append(f"page {index} is blank")
                    warn_only = False
                if expected is not None:
                    width, height = page.rect.width, page.rect.height
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
        text = "\n".join(_pages_text(pdf_path))
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
        text = "\n".join(_pages_text(pdf_path))
        findings = [
            f"today's date ({spelling}) appears on a chart dated {ctx.encounter.date_of_service}"
            for spelling in sorted(_date_spellings(today))
            if _present(spelling, text)
        ]
        return CheckResult(self.name, Verdict.WARN if findings else Verdict.PASS, findings)


for _check in (
    DataIntegrityCheck(),
    LayoutPaginationCheck(),
    VitalsLoincCheck(),
    DateStalenessCheck(),
):
    register_check(_check)
