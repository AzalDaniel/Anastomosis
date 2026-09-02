"""C-CDA deliverer — write one CCD XML per patient for import destinations.

Mirrors the shape of :mod:`anastomosis.deliver.bundle`: it takes canonical
records and an output directory, hardens that directory via
:func:`anastomosis.core.output.secure_output_dir` (0700 + PHI README), and
writes one file per patient. The difference is the artifact: a single
``<patient-id>.xml`` CCD instead of a bundle subdirectory.

Filename discipline (STRICTER than the PDF/bundle directories): files are named
by **patient id only**, never by patient name. PF/Tebra and bundle outputs name
PDFs ``Family_Given_...`` because those land in a per-patient subtree the
operator already controls; C-CDA documents, by contrast, are the
import-into-another-EHR artifact most likely to *travel* (emailed to a vendor,
dropped on a transfer share, imported by a third party). A name in the filename
would put a patient name in the clear at exactly the moment the file is least
under our control. Ids here are pseudonymous, so id-only naming keeps the
filename PHI-free.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from anastomosis.core.atomic import atomic_write_bytes
from anastomosis.core.logutil import exc_tag, safe_log_id
from anastomosis.core.model import PatientRecord
from anastomosis.core.output import secure_output_dir
from anastomosis.core.textutil import budgeted_name
from anastomosis.deliver._shared import claim_delivered_name, record_witness

from .builder import build_ccd, measure_ccd

__all__ = ["CcdaExportResult", "deliver_ccda"]

logger = logging.getLogger(__name__)


#: Report the preservation share above this fraction. Half is the point where
#: most of what a destination receives is not clinical content, which is the
#: operator's business whether or not their endpoint would accept the file.
_PRESERVATION_SHARE_WARN = 0.5


@dataclass(frozen=True)
class CcdaExportResult:
    """What the C-CDA export wrote, and what it could not.

    ``missing_count`` is the point: a build that fails leaves a patient with no
    document, and the archive and bundle deliverers already report that shape
    so ``_print_shortfall`` can say it out loud. This one returned only the
    paths it wrote, so a lost patient read as a smaller batch under a green
    success line.
    """

    paths: list[Path]
    missing_count: int
    #: Bytes across every document written, and how many of them are the
    #: preserved-source-fields section. The C-CDA is the one artifact handed to
    #: somebody else's EHR, and on a real export the preservation block was 97%
    #: of it — a well-formed document doing exactly what the losslessness
    #: guarantee promises, and 33x the size of the clinical payload it travels
    #: with. Nothing measured it, so the operator found out when the
    #: destination refused the file.
    total_bytes: int = 0
    preserved_bytes: int = 0
    #: The single largest document. A destination's size limit applies per
    #: document, not per batch, so the total is the wrong number to compare
    #: against one.
    largest_bytes: int = 0

    @property
    def preserved_share(self) -> float:
        return self.preserved_bytes / self.total_bytes if self.total_bytes else 0.0


def deliver_ccda(records: list[PatientRecord], out_dir: str | Path) -> CcdaExportResult:
    """Write one CCD XML per record into ``out_dir`` and return the paths.

    The directory is created (or hardened) 0700 with a PHI warning README.
    Filenames are ``<patient-id>.xml`` (id only — see the module docstring on
    why this is stricter than the PDF directory). A record that fails to build
    is logged by exception type only (never its values) and skipped, so one bad
    record never sinks a batch.

    A name COLLISION is not one of those survivable failures: ``write_bytes``
    overwrites, so two patient ids that sanitize to one filename — or two
    records arriving under one patient id — would deliver one CCD carrying the
    second over the first. The per-run claimed-name ledger makes that a hard
    stop.
    """
    out = secure_output_dir(out_dir)
    written: list[Path] = []
    claimed: dict[str, str] = {}
    missing = 0
    total_bytes = 0
    preserved_bytes = 0
    largest_bytes = 0
    for index, record in enumerate(records):
        # Budgeted against ``out``: an over-long path would otherwise raise
        # OSError inside the write below, and the batch-continues handler would
        # record that record as merely "failed" — a silently dropped export.
        pid = budgeted_name(record.patient.id, f"patient_{index}", parent=out, suffix=".xml")
        # The record goes in as the claim's witness because a patient id is not
        # guaranteed unique either: an adapter that yields one record per source
        # DOCUMENT hands two records for a patient with two of them, and without
        # the witness the second document's CCD lands on the first while the run
        # reports two patients. The pipeline folds those into one record before
        # delivery; this is what makes a future regression loud.
        claim_delivered_name(
            claimed,
            pid,
            record.patient.id,
            kind="C-CDA document",
            content=record_witness(record),
        )
        target = out / f"{pid}.xml"
        try:
            xml = build_ccd(record)
            atomic_write_bytes(target, xml)
            measured = measure_ccd(xml)
            total_bytes += measured.total_bytes
            preserved_bytes += measured.preserved_bytes
            largest_bytes = max(largest_bytes, measured.total_bytes)
        except Exception as exc:
            # One malformed record must not sink the batch; log the exception
            # TYPE only (its message may embed PHI) and move on. But "move on"
            # used to end there: the count returned was the count WRITTEN, so a
            # patient with no document at all was indistinguishable from a
            # smaller batch and the operator saw an unqualified green line.
            logger.warning("ccda export failed for patient %s (%s)", safe_log_id(pid), exc_tag(exc))
            missing += 1
            continue
        written.append(target)
    # PHI: never log the output path — an operator dir named after a patient
    # would enter the logs (SECURITY.md: never a path). Counts only.
    logger.info("ccda export complete: %d of %d patient(s)", len(written), len(records))
    result = CcdaExportResult(
        paths=written,
        missing_count=missing,
        total_bytes=total_bytes,
        preserved_bytes=preserved_bytes,
        largest_bytes=largest_bytes,
    )
    if result.preserved_share >= _PRESERVATION_SHARE_WARN:
        # Said before the destination says it. This is a line THIS tool draws
        # about its own output, not a vendor limit: what a given EHR will
        # accept is not something this tool can know, and the destination
        # registry's no-hallucination rule forbids inventing one. What is
        # knowable is the shape of the document, and most of it not being
        # clinical content is worth an operator's attention — an importer that
        # renders unrecognised sections will show a physician a wall of
        # preserved key/value narrative beside their actual chart.
        logger.warning(
            "%.0f%% of the exported C-CDA is preserved source fields "
            "(%d of %d bytes; largest document %d bytes) — check your "
            "destination's per-document size limit before importing",
            result.preserved_share * 100,
            preserved_bytes,
            total_bytes,
            largest_bytes,
        )
    return result
