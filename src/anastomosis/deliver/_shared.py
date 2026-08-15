"""Mechanics shared by the file-writing deliverers (archive + bundle).

Module-private to :mod:`anastomosis.deliver`: the two personas keep their own
layouts — the archive writes ONE cross-patient tree (index + ``unattributed/``
routing), the bundle writes one self-contained directory PER patient (QA slice
+ its own README) — and nothing here merges them. What lives here is only the
mechanics that are byte-for-byte identical in both today:

* :func:`write_fhir_bundle` — the machine-readable rendition every persona
  emits, same filename, same JSON serialization, same PHI-BY-DESIGN guarantee;
* :func:`copy_delivered_file` — the copy-never-move step, returning the
  PHI-safe exception TYPE name so each caller logs the artifact it was copying
  with its own module logger and its own message.

Deliberately NOT shared: PDF *attribution* (the archive routes unclaimed PDFs
to ``unattributed/`` and maps encounter→filename for its per-encounter pages;
the bundle has no unattributed slot because it is per-patient by definition)
and the README texts (three distinct operator-facing documents:
``core.output``'s directory warning, the archive's, and the bundle's).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from anastomosis.core.fhir import to_bundle
from anastomosis.core.logutil import exc_tag
from anastomosis.core.model import PatientRecord

__all__ = ["copy_delivered_file", "write_fhir_bundle"]

#: The per-patient FHIR rendition's filename, identical across deliverers.
BUNDLE_FILENAME = "bundle.json"


def write_fhir_bundle(record: PatientRecord, out_dir: Path) -> Path:
    """Write ``record`` as ``out_dir/bundle.json`` (FHIR R4) and return the path.

    ``indent=2, sort_keys=True`` makes the file diffable and byte-stable across
    runs — delivered archives and bundles are compared by operators, so the
    serialization is part of the contract, not an implementation detail.
    """
    target = out_dir / BUNDLE_FILENAME
    # PHI-BY-DESIGN: writing the patient's FHIR record to disk IS the product.
    # ``out_dir`` sits under a secure_output_dir-hardened tree (0o700 owner-only
    # on POSIX; on Windows NTFS, inheritance stripped and access limited to the
    # current user, SYSTEM, and Administrators) with a PHI-warning README — the
    # deliverers call :func:`anastomosis.core.output.secure_output_dir` before
    # they reach here. See SECURITY.md, "Code scanning & suppression policy
    # (auditable)".
    # codeql[py/clear-text-storage-sensitive-data]
    target.write_text(json.dumps(to_bundle(record), indent=2, sort_keys=True), encoding="utf-8")
    return target


def copy_delivered_file(source: Path, destination: Path) -> str | None:
    """Copy (never move) ``source`` to ``destination``; ``None`` when it landed.

    On an :class:`OSError` the PHI-safe exception TYPE name is returned instead
    of being logged here: each deliverer names the artifact it was copying in
    its own message, under its own module logger. Copying (rather than moving)
    leaves the caller's chart directory intact for a re-run.
    """
    try:
        shutil.copyfile(source, destination)
    except OSError as exc:
        return exc_tag(exc)
    return None
