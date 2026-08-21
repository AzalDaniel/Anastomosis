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
from pathlib import Path

from anastomosis.core.logutil import exc_tag, safe_log_id
from anastomosis.core.model import PatientRecord
from anastomosis.core.output import secure_output_dir
from anastomosis.core.textutil import budgeted_name

from .builder import build_ccd

__all__ = ["deliver_ccda"]

logger = logging.getLogger(__name__)


def deliver_ccda(records: list[PatientRecord], out_dir: str | Path) -> list[Path]:
    """Write one CCD XML per record into ``out_dir`` and return the paths.

    The directory is created (or hardened) 0700 with a PHI warning README.
    Filenames are ``<patient-id>.xml`` (id only — see the module docstring on
    why this is stricter than the PDF directory). A record that fails to build
    is logged by exception type only (never its values) and skipped, so one bad
    record never sinks a batch.
    """
    out = secure_output_dir(out_dir)
    written: list[Path] = []
    for index, record in enumerate(records):
        # Budgeted against ``out``: an over-long path would otherwise raise
        # OSError inside the write below, and the batch-continues handler would
        # record that record as merely "failed" — a silently dropped export.
        pid = budgeted_name(record.patient.id, f"patient_{index}", parent=out, suffix=".xml")
        target = out / f"{pid}.xml"
        try:
            target.write_bytes(build_ccd(record))
        except Exception as exc:
            # One malformed record must not sink the batch; log the exception
            # TYPE only (its message may embed PHI) and move on.
            logger.warning("ccda export failed for patient %s (%s)", safe_log_id(pid), exc_tag(exc))
            continue
        written.append(target)
    # PHI: never log the output path — an operator dir named after a patient
    # would enter the logs (SECURITY.md: never a path). Counts only.
    logger.info("ccda export complete: %d of %d patient(s)", len(written), len(records))
    return written
