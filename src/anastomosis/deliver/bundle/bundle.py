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

from anastomosis.core.atomic import atomic_write_text
from anastomosis.core.logutil import safe_log_id
from anastomosis.core.model import Patient, PatientRecord
from anastomosis.core.output import secure_output_dir
from anastomosis.core.textutil import HASH_TAG_CHARS, budgeted_name
from anastomosis.deliver._shared import (
    claim_delivered_name,
    copy_claimed_chart,
    record_witness,
    write_fhir_bundle,
)
from anastomosis.deliver.render_index import RenderIndex
from anastomosis.pipeline import ATTACHMENTS_DIRNAME
from anastomosis.qa import DocumentQA, QAReport, Verdict

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
    #: The source's own documents for this patient — the scans and lab reports
    #: their charts reference. Separate from ``pdf_paths``, which is charts this
    #: run rendered: a request for a patient's record is answered with both, and
    #: one list covering them would hide either going missing.
    attachment_paths: list[Path] = field(default_factory=list)
    qa_report_path: Path | None = None
    readme_path: Path | None = None
    #: Charts the render index named for this patient that were not on disk
    #: when the bundle was built. A record request answered without a chart the
    #: run says it rendered is exactly the silent loss this tool exists to end,
    #: so the number travels with the result instead of being filtered away.
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
        """Deliver a bundle per record, attributing each patient's charts
        strictly by ``patient_id`` via the engine's persisted render index
        (never by name prefix — two patients sharing both names must never
        cross-attribute). ``pdfs_dir`` is the engine's ``render_index.json``
        sidecar; when it is missing every patient's PDF list is empty —
        bundles render without charts and the absence is logged loudly.
        Bundle has no per-patient ``unattributed`` slot (it's per-patient by
        definition), so it never guesses.

        The per-run claimed-name ledger lives here, at the scope that has more
        than one patient in it: two ids that sanitize to one directory name
        would otherwise merge two patients into a single bundle.
        """
        render_index = RenderIndex.load(pdfs_dir)
        missing: dict[str, int] = {}
        if pdfs_dir is None or not pdfs_dir.is_dir():
            pdfs_lookup: dict[str, list[Path]] = {}
        elif render_index is None:
            logger.warning("no render index; bundle will deliver without chart PDFs")
            pdfs_lookup = {}
        else:
            # Split rather than filtered. This was one comprehension with an
            # `if ... .is_file()` on the end, so a chart the index named and
            # that was not there vanished with no branch, no log line, and no
            # count — and the per-patient INFO line below then reported the
            # post-filter number as though it were the whole truth.
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
        # The attachments the run carried, looked up per record. No index is
        # needed, unlike the charts above: a chart's filename carries no patient
        # id, but a document is named BY the record that owns it.
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
        attachments: list[Path] | None = None,
        missing_count: int = 0,
    ) -> BundleResult:
        """Deliver ONE patient's bundle into ``out_dir``.

        ``missing_count`` is how many charts the caller's index named for this
        patient and could not find; it rides the result so the run summary can
        report it. A standalone call has nothing to reconcile against and gets
        the default 0.

        ``claimed_dirs`` is the per-run ledger of delivered directory name ->
        the record that claimed it, threaded in by :meth:`deliver_records`; a
        second, DIFFERENT id claiming a name raises rather than merging two
        patients into one bundle, and so does a second, different RECORD under
        the same id. A standalone call gets a fresh ledger — a single record
        cannot collide with itself.
        """
        out = secure_output_dir(out_dir)
        # Budgeted against the directory this bundle is written into, so a long
        # source id cannot produce a patient directory the filesystem refuses,
        # with room reserved for the deepest child (``pdfs/<chart>.pdf``).
        pid = budgeted_name(record.patient.id, "unknown", parent=out, reserve=_PDF_CHILD_RESERVE)
        # The record is the claim's witness because a patient id is not
        # guaranteed unique: an adapter that yields one record per source
        # DOCUMENT hands two records for a patient with two of them, and the
        # exist_ok directory below would then hold the second document's bundle
        # over the first while the run reported two patients. The pipeline folds
        # those into one record before delivery; this is what makes a regression
        # loud rather than silent.
        claim_delivered_name(
            claimed_dirs if claimed_dirs is not None else {},
            pid,
            record.patient.id,
            kind="patient directory",
            content=record_witness(record),
        )
        patient_dir = out / pid
        patient_dir.mkdir(parents=True, exist_ok=True)

        # FHIR R4 Bundle — the machine-readable rendition (the shared deliverer
        # mechanic; its PHI-BY-DESIGN rationale lives there).
        bundle_path = write_fhir_bundle(record, patient_dir)

        # PDFs — copied (never moved) so the caller's working tree is intact.
        pdf_paths = self._copy_pdfs(record.patient, pdfs or [], patient_dir)

        # The source's own documents — what the charts reference. A record
        # request answered without them hands over notes that cite scans the
        # bundle does not contain.
        attachment_paths = self._copy_attachments(attachments or [], patient_dir)

        # QA slice — only this patient's documents.
        qa_path = self._write_qa_slice(record, patient_dir, qa_report)

        # README — what/why/PHI.
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
        # ``pdfs`` is the index-attributed list assembled by
        # :meth:`deliver_records` (already filtered by patient_id via the
        # render index) or supplied directly by a caller that did the same.
        #
        # The DESTINATION name is budgeted (copy_claimed_chart): renderer
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
            delivered, failure = copy_claimed_chart(
                target_dir, claimed, pdf, pdf.name, kind="chart"
            )
            if failure is not None:
                logger.warning("pdf copy failed (%s)", failure)
                continue
            assert delivered is not None  # copy_claimed_chart: failure is None => delivered isn't
            copied.append(target_dir / delivered)
        return copied

    def _copy_attachments(self, attachments: list[Path], patient_dir: Path) -> list[Path]:
        """Copy this patient's source documents into the bundle's own slot.

        Same budget-claim-copy sequence as the charts above, and the same
        reason for claiming: two documents resolving to one delivered name
        would file one patient's scan over another's rather than fail.
        """
        if not attachments:
            return []
        target_dir = patient_dir / ATTACHMENTS_DIRNAME
        target_dir.mkdir(parents=True, exist_ok=True)
        copied: list[Path] = []
        claimed: dict[str, str] = {}
        for attachment in attachments:
            delivered, failure = copy_claimed_chart(
                target_dir, claimed, attachment, attachment.name, kind="attachment"
            )
            if failure is not None:
                logger.warning("attachment copy failed (%s)", failure)
                continue
            assert delivered is not None  # copy_claimed_chart: no failure => a name
            copied.append(target_dir / delivered)
        return copied

    @staticmethod
    def _is_this_patients(doc: DocumentQA, record: PatientRecord) -> bool:
        """Whether a graded row belongs in ``record``'s bundle.

        The row names the visit it graded, and a whole-patient page has none:
        the record summary — the one page in a scan-only patient's bundle —
        carries the PATIENT id in that slot, the same stand-in the upload
        manifest and the export's own encounter check use where a document
        covers a chart rather than a visit. Selecting on the record's encounter
        ids alone therefore dropped exactly the row a reader most needs, and an
        empty report reads as "nothing to say" rather than "the verdict for
        this page is missing" (#399).
        """
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
        atomic_write_text(target, json.dumps(payload, indent=2))
        return target

    def _write_readme(self, patient_id: str, patient_dir: Path) -> Path:
        target = patient_dir / "README.txt"
        # PHI-BY-DESIGN: the per-bundle README names its patient id; same
        # secure_output_dir hardening as write_fhir_bundle (_shared.py); see
        # SECURITY.md.
        # codeql[py/clear-text-storage-sensitive-data]
        atomic_write_text(
            target,
            _README_TEMPLATE.format(
                patient_id=patient_id,
                generated_at=datetime.now(UTC).isoformat(),
                generator=self.generator,
            ),
        )
        return target


def _attachments_for(record: PatientRecord, landing: Path | None) -> list[Path]:
    """The files in ``landing`` this record names, in the order it names them.

    A document the record names but the charts directory does not hold is left
    out rather than guessed at. Conservation belongs to the run —
    ``pipeline._carry_attachments`` knows the export and stops a run whose
    attachments did not all arrive — so reaching here means the directory was
    assembled without that step or edited after it.
    """
    if landing is None or not landing.is_dir():
        return []
    found: list[Path] = []
    for doc in record.documents:
        if not doc.path:
            continue
        candidate = landing / Path(doc.path).name
        if candidate.is_file():
            found.append(candidate)
        else:
            logger.warning(
                "record names an attachment the charts directory does not hold for patient %s",
                safe_log_id(record.patient.id),
            )
    return found
