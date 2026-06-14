"""Per-patient bundle deliverer — Responder persona.

When a practice gets a record request, the deliverable is a packet for ONE
patient: the FHIR R4 Bundle (machine-readable lossless export), the rendered
chart PDFs, and the QA report sliced down to that patient's documents. No
search index, no cross-patient navigation — one patient per directory, ready
to hand over.

Layout::

    out_dir/<patient_id>/
      bundle.json        — FHIR R4 Bundle (collection)
      pdfs/*.pdf         — only this patient's rendered charts
      qa_report.json     — sliced QA report (only this patient's docs)
      README.txt         — what this bundle is, when, PHI applies

PHI hygiene: the directory is created via
:func:`anastomosis.core.output.secure_output_dir` (0700 + PHI warning README).
Logging emits counts and ids only.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from anastomosis.core.fhir import to_bundle
from anastomosis.core.logutil import exc_tag
from anastomosis.core.model import Patient, PatientRecord
from anastomosis.core.output import secure_output_dir
from anastomosis.deliver.pdfindex import patient_prefix
from anastomosis.qa import QAReport, Verdict

__all__ = ["BundleDeliverer", "BundleResult"]

logger = logging.getLogger(__name__)


_README_TEMPLATE = """\
Anastomosis per-patient bundle
================================

Patient id : {patient_id}
Generated  : {generated_at}
Generator  : {generator}

Contents:
  bundle.json   — FHIR R4 Bundle (collection) for this one patient.
                  Machine-readable; round-trips back to the canonical model.
  pdfs/         — Rendered chart PDFs for this patient's encounters.
  qa_report.json (optional)
                — Per-document QA results, only for this patient's charts.

PHI WARNING
-----------
This folder contains Protected Health Information about a single patient.
Handle accordingly:
  * Do not upload to consumer cloud storage.
  * Do not share by unencrypted email.
  * Store on encrypted media; destroy securely when no longer needed.
"""


def _safe_id(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", (value or "").strip()).strip("_")
    return cleaned or fallback


@dataclass(frozen=True)
class BundleResult:
    """What landed on disk for one patient."""

    patient_id: str
    out_dir: Path
    bundle_path: Path
    pdf_paths: list[Path] = field(default_factory=list)
    qa_report_path: Path | None = None
    readme_path: Path | None = None


class BundleDeliverer:
    """Render canonical records as per-patient bundles for record requests."""

    def __init__(self, generator: str | None = None) -> None:
        import anastomosis

        self.generator = generator or f"anastomosis {anastomosis.__version__}"

    def deliver_records(
        self,
        records: list[PatientRecord],
        pdfs_dir: Path | None,
        out_dir: str | Path,
        *,
        qa_report: QAReport | None = None,
    ) -> list[BundleResult]:
        """Deliver a bundle per record, attributing each patient's charts in a
        single pass over the rendered PDFs.

        Instead of re-filtering every rendered PDF for every patient (the old
        O(patients x pdfs) loop), bucket each chart under the LONGEST patient
        prefix it starts with, once, then hand each record its own bucket. The
        longest-first, single-assignment rule is exactly equivalent to the old
        per-patient ``startswith`` filter — patient prefixes end in ``_`` so a
        chart matches at most one — and stays correct for multi-token names
        (``Van_Buren_John_…``) that a naive ``{token0}_{token1}_`` index would
        mis-bucket.
        """
        all_pdfs = sorted(pdfs_dir.glob("*.pdf")) if pdfs_dir and pdfs_dir.is_dir() else []
        prefixes = sorted(
            {p for record in records if (p := patient_prefix(record.patient))},
            key=len,
            reverse=True,
        )
        buckets: dict[str, list[Path]] = {p: [] for p in prefixes}
        for pdf in all_pdfs:
            for prefix in prefixes:
                if pdf.name.startswith(prefix):
                    buckets[prefix].append(pdf)
                    break
        return [
            self.deliver(
                record,
                buckets.get(patient_prefix(record.patient), []),
                out_dir,
                qa_report=qa_report,
            )
            for record in records
        ]

    def deliver(
        self,
        record: PatientRecord,
        pdfs: list[Path] | None,
        out_dir: str | Path,
        *,
        qa_report: QAReport | None = None,
    ) -> BundleResult:
        out = secure_output_dir(out_dir)
        pid = _safe_id(record.patient.id, "unknown")
        patient_dir = out / pid
        patient_dir.mkdir(parents=True, exist_ok=True)

        # FHIR R4 Bundle — the machine-readable rendition.
        bundle_path = patient_dir / "bundle.json"
        bundle_path.write_text(
            json.dumps(to_bundle(record), indent=2, sort_keys=True), encoding="utf-8"
        )

        # PDFs — copied (never moved) so the caller's working tree is intact.
        pdf_paths = self._copy_pdfs(record.patient, pdfs or [], patient_dir)

        # QA slice — only this patient's documents.
        qa_path = self._write_qa_slice(record, patient_dir, qa_report)

        # README — what/why/PHI.
        readme_path = self._write_readme(record.patient.id, patient_dir)

        logger.info(
            "bundle delivered for patient %s: %d pdfs, qa=%s",
            pid,
            len(pdf_paths),
            "yes" if qa_path else "no",
        )
        return BundleResult(
            patient_id=pid,
            out_dir=patient_dir,
            bundle_path=bundle_path,
            pdf_paths=pdf_paths,
            qa_report_path=qa_path,
            readme_path=readme_path,
        )

    # --- internals ----------------------------------------------------------

    def _copy_pdfs(self, patient: Patient, pdfs: list[Path], patient_dir: Path) -> list[Path]:
        if not pdfs:
            return []
        prefix = patient_prefix(patient)
        if not prefix:
            return []
        target_dir = patient_dir / "pdfs"
        target_dir.mkdir(parents=True, exist_ok=True)
        copied: list[Path] = []
        for pdf in pdfs:
            if not pdf.name.startswith(prefix):
                continue
            try:
                destination = target_dir / pdf.name
                shutil.copyfile(pdf, destination)
                copied.append(destination)
            except OSError as exc:
                logger.warning("pdf copy failed (%s)", exc_tag(exc))
        return copied

    def _write_qa_slice(
        self,
        record: PatientRecord,
        patient_dir: Path,
        qa_report: QAReport | None,
    ) -> Path | None:
        if qa_report is None:
            return None
        encounter_ids = {encounter.id for encounter in record.encounters}
        slice_docs = [doc for doc in qa_report.documents if doc.encounter_id in encounter_ids]
        if not slice_docs:
            # Still emit an empty slice so the bundle structure is uniform —
            # downstream consumers can count on the file existing whenever a
            # report was passed in.
            slice_docs = []
        payload = {
            "generated_at": datetime.now(UTC).isoformat(),
            "patient_id": record.patient.id,
            "summary": {v.value: sum(1 for d in slice_docs if d.verdict is v) for v in Verdict},
            "documents": [
                {
                    "file": doc.path.name,
                    "encounter_id": doc.encounter_id,
                    "verdict": doc.verdict.value,
                    "checks": [
                        {
                            "check": result.check,
                            "verdict": result.verdict.value,
                            "findings": result.findings,
                        }
                        for result in doc.results
                    ],
                }
                for doc in slice_docs
            ],
        }
        target = patient_dir / "qa_report.json"
        target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return target

    def _write_readme(self, patient_id: str, patient_dir: Path) -> Path:
        target = patient_dir / "README.txt"
        target.write_text(
            _README_TEMPLATE.format(
                patient_id=patient_id,
                generated_at=datetime.now(UTC).isoformat(),
                generator=self.generator,
            ),
            encoding="utf-8",
        )
        return target
