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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from anastomosis.core.logutil import safe_log_id
from anastomosis.core.model import Patient, PatientRecord
from anastomosis.core.output import secure_output_dir
from anastomosis.core.textutil import HASH_TAG_CHARS, budgeted_name
from anastomosis.deliver._shared import (
    budgeted_copy_name,
    claim_delivered_name,
    copy_delivered_file,
    write_fhir_bundle,
)
from anastomosis.deliver.render_index import RenderIndex
from anastomosis.qa import QAReport, Verdict

__all__ = ["BundleDeliverer", "BundleResult"]

logger = logging.getLogger(__name__)

# Room kept free under each patient directory for its deepest child, a copied
# chart at ``pdfs/<chart>.pdf``. That name is itself budgeted now
# (``budgeted_copy_name``), so the reserve holds the room a budgeted child
# needs to stay DISTINCT — the fixed wrapper plus the shortest distinct name
# ``budgeted_name`` can return, its hash tag — not a guess at a plausible
# chart filename (the renderer's can run to ~617 characters, which no reserve
# could have covered anyway).
_PDF_CHILD_RESERVE = len("/pdfs/") + HASH_TAG_CHARS + len(".pdf")


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
        """Deliver a bundle per record, attributing each patient's charts via
        the engine's persisted render index.

        Previously this bucketed PDFs by the leading ``{family}_{given}_``
        filename prefix; two patients sharing both names cross-attributed
        without warning. Attribution is now strictly by ``patient_id``:
        the render index (``render_index.json`` written by the engine into
        ``pdfs_dir``) tells the deliverer exactly which PDFs the engine
        wrote for which patient. When the index is missing every patient's
        PDF list is empty — bundles render without charts and the index
        absence is logged loudly. Bundle has no per-patient ``unattributed``
        slot (it's per-patient by definition), so it never guesses.

        The per-run claimed-name ledger lives here, at the scope that has more
        than one patient in it: two ids that sanitize to one directory name
        would otherwise merge two patients into a single bundle.
        """
        render_index = RenderIndex.load(pdfs_dir)
        if pdfs_dir is None or not pdfs_dir.is_dir():
            pdfs_lookup: dict[str, list[Path]] = {}
        elif render_index is None:
            logger.warning("no render index; bundle will deliver without chart PDFs")
            pdfs_lookup = {}
        else:
            pdfs_lookup = {
                record.patient.id: [
                    pdfs_dir / name
                    for name in render_index.for_patient(record.patient.id)
                    if (pdfs_dir / name).is_file()
                ]
                for record in records
            }
        claimed_dirs: dict[str, str] = {}
        return [
            self.deliver(
                record,
                pdfs_lookup.get(record.patient.id, []),
                out_dir,
                qa_report=qa_report,
                claimed_dirs=claimed_dirs,
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
        claimed_dirs: dict[str, str] | None = None,
    ) -> BundleResult:
        """Deliver ONE patient's bundle into ``out_dir``.

        ``claimed_dirs`` is the per-run ledger of delivered directory name ->
        the patient id that claimed it, threaded in by :meth:`deliver_records`;
        a second, DIFFERENT id claiming a name raises rather than merging two
        patients into one bundle. A standalone call gets a fresh ledger — a
        single record cannot collide with itself.
        """
        out = secure_output_dir(out_dir)
        # Budgeted against the directory this bundle is written into, so a long
        # source id cannot produce a patient directory the filesystem refuses,
        # with room reserved for the deepest child (``pdfs/<chart>.pdf``).
        pid = budgeted_name(record.patient.id, "unknown", parent=out, reserve=_PDF_CHILD_RESERVE)
        claim_delivered_name(
            claimed_dirs if claimed_dirs is not None else {},
            pid,
            record.patient.id,
            kind="patient directory",
        )
        patient_dir = out / pid
        patient_dir.mkdir(parents=True, exist_ok=True)

        # FHIR R4 Bundle — the machine-readable rendition (the shared deliverer
        # mechanic; its PHI-BY-DESIGN rationale lives there).
        bundle_path = write_fhir_bundle(record, patient_dir)

        # PDFs — copied (never moved) so the caller's working tree is intact.
        pdf_paths = self._copy_pdfs(record.patient, pdfs or [], patient_dir)

        # QA slice — only this patient's documents.
        qa_path = self._write_qa_slice(record, patient_dir, qa_report)

        # README — what/why/PHI.
        readme_path = self._write_readme(record.patient.id, patient_dir)

        logger.info(
            "bundle delivered for patient %s: %d pdfs, qa=%s",
            safe_log_id(pid),
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
        # ``pdfs`` is the index-attributed list assembled by
        # :meth:`deliver_records` (or supplied directly by a caller that
        # already filtered by ``patient.id``). The deliverer trusts that
        # filter and copies the lot verbatim; the old startswith re-check
        # is gone because filename prefixes are no longer the source of
        # truth for attribution — the render index is.
        #
        # The DESTINATION name is budgeted (``budgeted_copy_name``): renderer
        # chart names run to ~617 characters, and an over-MAX_PATH copy used to
        # fail into the warn-and-continue below — a chart silently missing from
        # a delivered bundle. Naming is deliberately outside that warn path, so
        # a destination that cannot be named distinctly raises instead.
        if not pdfs:
            return []
        target_dir = patient_dir / "pdfs"
        target_dir.mkdir(parents=True, exist_ok=True)
        copied: list[Path] = []
        claimed: dict[str, str] = {}
        for pdf in pdfs:
            delivered = budgeted_copy_name(target_dir, pdf.name)
            claim_delivered_name(claimed, delivered, pdf.name, kind="chart")
            destination = target_dir / delivered
            failure = copy_delivered_file(pdf, destination)
            if failure is not None:
                logger.warning("pdf copy failed (%s)", failure)
                continue
            copied.append(destination)
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
        # PHI-BY-DESIGN: the per-bundle README names its patient id and the PHI
        # handling rules; it lands in the same secure_output_dir-hardened tree
        # (0o700 owner-only on POSIX; on Windows NTFS, inheritance stripped and
        # access limited to the current user, SYSTEM, and Administrators) as the
        # record it describes. See SECURITY.md, "Code scanning & suppression
        # policy (auditable)".
        # codeql[py/clear-text-storage-sensitive-data]
        target.write_text(
            _README_TEMPLATE.format(
                patient_id=patient_id,
                generated_at=datetime.now(UTC).isoformat(),
                generator=self.generator,
            ),
            encoding="utf-8",
        )
        return target
