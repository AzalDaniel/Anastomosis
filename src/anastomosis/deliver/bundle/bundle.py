"""Per-patient bundle deliverer.

One patient per directory: the FHIR R4 Bundle, that patient's rendered
chart PDFs, and the QA report sliced to their documents. No search index,
no cross-patient navigation.

Layout: ``out_dir/<patient_id>/{bundle.json, pdfs/*.pdf, qa_report.json,
README.txt}``. Directory hardened via ``secure_output_dir`` (RULES.md 18);
logging emits counts and ids only (RULES.md 2)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from anastomosis.core.atomic import atomic_write_text
from anastomosis.core.clock import now as _clock_now
from anastomosis.core.fhir import DeliveredAttachment
from anastomosis.core.logutil import safe_log_id
from anastomosis.core.model import Patient, PatientRecord
from anastomosis.core.output import secure_output_dir
from anastomosis.core.textutil import HASH_TAG_CHARS, budgeted_name
from anastomosis.deliver._shared import (
    claim_delivered_name,
    copy_claimed_chart,
    measured_attachment,
    record_witness,
    write_fhir_bundle,
)
from anastomosis.deliver.render_index import RenderIndex
from anastomosis.pipeline import ATTACHMENTS_DIRNAME
from anastomosis.qa import DocumentQA, QAReport, Verdict

__all__ = ["BundleDeliverer", "BundleResult"]

logger = logging.getLogger(__name__)

# Reserve = the fixed wrapper (``/pdfs/`` + ``.pdf``) plus a budgeted name's
# shortest distinct form (its hash tag) — not a guess at a plausible chart
# filename, which can run to ~617 characters.
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
    #: Source documents (scans, lab reports) the charts reference — separate
    #: from ``pdf_paths`` so either going missing is never hidden by the other.
    attachment_paths: list[Path] = field(default_factory=list)
    qa_report_path: Path | None = None
    readme_path: Path | None = None
    #: Charts the render index named for this patient but were not on disk
    #: when built — travels with the result rather than being filtered away.
    missing_count: int = 0


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
        """Deliver a bundle per record.

        Charts attribute strictly by ``patient_id`` (RULES.md 11), never by
        name prefix; a missing index means an empty, loudly-logged PDF list.
        The per-run claimed-name ledger lives here, where patients can collide."""
        render_index = RenderIndex.load(pdfs_dir)
        missing: dict[str, int] = {}
        if pdfs_dir is None or not pdfs_dir.is_dir():
            pdfs_lookup: dict[str, list[Path]] = {}
        elif render_index is None:
            logger.warning("no render index; bundle will deliver without chart PDFs")
            pdfs_lookup = {}
        else:
            # Tracked separately (not filtered away) so a missing chart is
            # counted, not silently absorbed into the per-patient PDF count.
            pdfs_lookup = {}
            for record in records:
                found: list[Path] = []
                absent = 0
                for name in render_index.for_patient(record.patient.id):
                    path = pdfs_dir / name
                    if path.is_file():
                        found.append(path)
                    else:
                        absent += 1
                if absent:
                    # By the run-scoped surrogate and a count: a chart filename
                    # carries the patient's name and their date of service.
                    logger.warning(
                        "%d indexed chart(s) missing on disk for patient %s",
                        absent,
                        safe_log_id(record.patient.id),
                    )
                pdfs_lookup[record.patient.id] = found
                missing[record.patient.id] = absent
        # No index needed here, unlike the charts above: a document is named
        # BY the record that owns it, not by a filename with no patient id.
        landing = (pdfs_dir / ATTACHMENTS_DIRNAME) if pdfs_dir is not None else None
        attachments_lookup = {
            record.patient.id: _attachments_for(record, landing) for record in records
        }
        claimed_dirs: dict[str, str] = {}
        return [
            self.deliver(
                record,
                pdfs_lookup.get(record.patient.id, []),
                out_dir,
                qa_report=qa_report,
                claimed_dirs=claimed_dirs,
                attachments=attachments_lookup.get(record.patient.id, []),
                missing_count=missing.get(record.patient.id, 0),
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
        attachments: list[tuple[str, Path]] | None = None,
        missing_count: int = 0,
    ) -> BundleResult:
        """Deliver ONE patient's bundle into ``out_dir``.

        ``attachments`` is ``(artifact id, source file)`` pairs (see
        :func:`_attachments_for`). ``claimed_dirs`` is the per-run name ledger
        from :meth:`deliver_records`; omitted, a fresh ledger cannot collide."""
        out = secure_output_dir(out_dir)
        # Budgeted so a long source id cannot produce a directory the
        # filesystem refuses; room reserved for the deepest child (pdfs/*.pdf).
        pid = budgeted_name(record.patient.id, "unknown", parent=out, reserve=_PDF_CHILD_RESERVE)
        # The record is the witness: a patient id is not guaranteed unique,
        # so two records under one id would otherwise merge silently here.
        claim_delivered_name(
            claimed_dirs if claimed_dirs is not None else {},
            pid,
            record.patient.id,
            kind="patient directory",
            content=record_witness(record),
        )
        patient_dir = out / pid
        patient_dir.mkdir(parents=True, exist_ok=True)

        # Copied before the FHIR bundle: the record alone can't say what name
        # a document lands under, so the bundle needs what this copy measured.
        attachment_paths, landed_attachments = self._copy_attachments(
            attachments or [], patient_dir
        )

        # Carries what was just measured above, so every DocumentReference
        # resolves to a real file beside it.
        bundle_path = write_fhir_bundle(record, patient_dir, landed_attachments)

        # PDFs — copied (never moved) so the caller's working tree is intact.
        pdf_paths = self._copy_pdfs(record.patient, pdfs or [], patient_dir)

        qa_path = self._write_qa_slice(record, patient_dir, qa_report)

        readme_path = self._write_readme(record.patient.id, patient_dir)

        logger.info(
            "bundle delivered for patient %s: %d pdfs, %d attachments, %d missing, qa=%s",
            safe_log_id(pid),
            len(pdf_paths),
            len(attachment_paths),
            missing_count,
            "yes" if qa_path else "no",
        )
        return BundleResult(
            patient_id=pid,
            out_dir=patient_dir,
            bundle_path=bundle_path,
            pdf_paths=pdf_paths,
            attachment_paths=attachment_paths,
            qa_report_path=qa_path,
            readme_path=readme_path,
            missing_count=missing_count,
        )

    # --- internals ----------------------------------------------------------

    def _copy_pdfs(self, patient: Patient, pdfs: list[Path], patient_dir: Path) -> list[Path]:
        # ``pdfs`` is already filtered by patient_id (index or caller). Naming
        # is deliberately outside the warn-and-continue path: an unnameable
        # destination raises rather than silently missing from the bundle.
        if not pdfs:
            return []
        target_dir = patient_dir / "pdfs"
        target_dir.mkdir(parents=True, exist_ok=True)
        copied: list[Path] = []
        claimed: dict[str, str] = {}
        for pdf in pdfs:
            delivered, failure = copy_claimed_chart(
                target_dir, claimed, pdf, pdf.name, kind="chart"
            )
            if failure is not None:
                logger.warning("pdf copy failed (%s)", failure)
                continue
            assert delivered is not None  # copy_claimed_chart: failure is None => delivered isn't
            copied.append(target_dir / delivered)
        return copied

    def _copy_attachments(
        self, attachments: list[tuple[str, Path]], patient_dir: Path
    ) -> tuple[list[Path], dict[str, DeliveredAttachment]]:
        """Copy this patient's source documents; measure each for the FHIR
        rendition beside it (same budget-claim-copy sequence as the charts).

        Two artifact ids naming ONE file each get a list entry, but the copy
        and hash are not doubled (:func:`measured_attachment` reuses the first)."""
        if not attachments:
            return [], {}
        target_dir = patient_dir / ATTACHMENTS_DIRNAME
        target_dir.mkdir(parents=True, exist_ok=True)
        copied: list[Path] = []
        claimed: dict[str, str] = {}
        landed: dict[str, DeliveredAttachment] = {}  # source filename -> what was measured
        by_doc: dict[str, DeliveredAttachment] = {}
        for doc_id, attachment in attachments:
            delivered, failure = copy_claimed_chart(
                target_dir, claimed, attachment, attachment.name, kind="attachment"
            )
            if failure is not None:
                logger.warning("attachment copy failed (%s)", failure)
                continue
            assert delivered is not None  # copy_claimed_chart: no failure => a name
            destination = target_dir / delivered
            copied.append(destination)
            by_doc[doc_id] = measured_attachment(
                landed, destination, f"{ATTACHMENTS_DIRNAME}/{delivered}"
            )
        return copied, by_doc

    @staticmethod
    def _is_this_patients(doc: DocumentQA, record: PatientRecord) -> bool:
        """Whether a graded row belongs in ``record``'s bundle.

        A whole-patient page has no visit id; it carries the PATIENT id in
        that slot instead. Encounter ids alone dropped that row, turning a
        missing verdict into a false "nothing to say" (#399)."""
        return doc.encounter_id == record.patient.id or doc.encounter_id in {
            encounter.id for encounter in record.encounters
        }

    def _write_qa_slice(
        self,
        record: PatientRecord,
        patient_dir: Path,
        qa_report: QAReport | None,
    ) -> Path | None:
        if qa_report is None:
            return None
        slice_docs = [doc for doc in qa_report.documents if self._is_this_patients(doc, record)]
        payload = {
            "generated_at": _clock_now().isoformat(),
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
        atomic_write_text(target, json.dumps(payload, indent=2))
        return target

    def _write_readme(self, patient_id: str, patient_dir: Path) -> Path:
        target = patient_dir / "README.txt"
        # PHI-BY-DESIGN: the README names its patient id; caller already
        # hardened the directory (RULES.md 18). See SECURITY.md.
        # codeql[py/clear-text-storage-sensitive-data]
        atomic_write_text(
            target,
            _README_TEMPLATE.format(
                patient_id=patient_id,
                generated_at=_clock_now().isoformat(),
                generator=self.generator,
            ),
        )
        return target


def _attachments_for(record: PatientRecord, landing: Path | None) -> list[tuple[str, Path]]:
    """``(artifact id, file)`` pairs ``landing`` holds for this record.

    The id travels with the file so the copy step can tell the FHIR bundle
    which ``DocumentArtifact`` each name belongs to. A name the record gives
    but ``landing`` does not hold is left out, never guessed at."""
    if landing is None or not landing.is_dir():
        return []
    found: list[tuple[str, Path]] = []
    for doc in record.documents:
        if not doc.path:
            continue
        candidate = landing / Path(doc.path).name
        if candidate.is_file():
            found.append((doc.id, candidate))
        else:
            logger.warning(
                "record names an attachment the charts directory does not hold for patient %s",
                safe_log_id(record.patient.id),
            )
    return found
