"""Manifest persistence: the bridge from a render run to a later ``anast upload``.

A browser upload is a separate, operator-driven step that happens AFTER the
charts have been reconstructed — often on a different machine, against a live
EHR session the operator logs into by hand. So the upload driver cannot re-run
the pipeline; it needs the render run's :class:`UploadItem` manifest and the
:class:`Patient` demographics (the resolver searches the destination by name +
DOB) written to disk, ready to read back.

:func:`write_upload_manifest` is that writer; :func:`read_upload_manifest` reads
it back. Two invariants shape the file:

* **Deterministic.** ``sort_keys=True``, items sorted by ``item_key``, patients
  keyed by ``patient_id`` — two writes over the same inputs are byte-identical
  (the same golden-style discipline the run report uses). No clock, no random.
* **Loud on malformed.** A missing file, a version mismatch, or a missing key
  raises :class:`ManifestError` — a corrupt manifest is a defect to surface, not
  a run to start with half the data (the loud-failure invariant).

PHI rule (load-bearing): this file carries patient demographics (the resolver
needs name + DOB), so it lives ONLY inside the hardened ``0o700`` output
directory (:func:`anastomosis.core.output.secure_output_dir`), is NEVER logged
(the writer logs an item COUNT only — never a name, a DOB, or a path), and is
NEVER committed. ``file_path`` is stored as a basename (relative) so the
manifest is relocatable and never embeds an absolute path; it is re-absolutized
against ``out_dir`` on read.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from anastomosis.core.model import Patient
from anastomosis.core.output import secure_output_dir
from anastomosis.destinations.base import UploadItem

from .manifest import build_manifest

if TYPE_CHECKING:
    from anastomosis.core.model import PatientRecord
    from anastomosis.reconstruct.engine import RenderedDoc

__all__ = [
    "MANIFEST_NAME",
    "MANIFEST_VERSION",
    "ManifestError",
    "read_upload_manifest",
    "write_upload_manifest",
]

logger = logging.getLogger(__name__)

MANIFEST_NAME = "upload_manifest.json"
MANIFEST_VERSION = 1


class ManifestError(Exception):
    """The upload manifest is missing or malformed — loud, never a silent skip.

    Raised by :func:`read_upload_manifest` for an absent file, a version
    mismatch, or a missing/wrong-shaped key. The message names the file and the
    fault (both PHI-safe — a path to the manifest and a structural reason, never
    a patient value) so the caller can surface a clean error instead of a
    ``KeyError``/``JSONDecodeError`` traceback.
    """


def _item_to_json(item: UploadItem) -> dict[str, Any]:
    """One item as a deterministic JSON object — ``file_path`` as a basename.

    The basename (not the absolute path) is stored so the manifest is
    relocatable and never embeds the host directory layout; it is re-absolutized
    against ``out_dir`` on read.
    """
    return {
        "item_key": item.item_key,
        "encounter_id": item.encounter_id,
        "patient_id": item.patient_id,
        "file_path": item.file_path.name,
        "sha256": item.sha256,
        "size_bytes": item.size_bytes,
        "fingerprint": item.fingerprint,
    }


def write_upload_manifest(
    documents: Iterable[RenderedDoc],
    records: Iterable[PatientRecord],
    out_dir: Path,
) -> Path:
    """Write ``<out_dir>/upload_manifest.json`` for a later ``anast upload``.

    Builds the items via :func:`build_manifest` (so the same content hashing and
    ``item_key`` rule the engine relies on is reused, not re-implemented), then
    selects the :class:`Patient` each item refers to from ``records``. Only the
    patients an item actually references are written. The file lands inside
    :func:`secure_output_dir` (``0o700``).

    Deterministic: items are sorted by ``item_key``, patients keyed by
    ``patient_id``, and the JSON is written with ``sort_keys=True`` — two writes
    over the same inputs are byte-identical.

    PHI rule: this file carries demographics, so it stays inside the hardened
    output dir and is never logged. The only log line is the item COUNT.
    """
    items = build_manifest(documents)
    # canonical patient_id -> Patient, for the items' patient_id lookups.
    patients_by_id: dict[str, Patient] = {record.patient.id: record.patient for record in records}

    items_json = [_item_to_json(item) for item in sorted(items, key=lambda it: it.item_key)]
    # Only patients referenced by an item are written (the upload step needs no
    # demographics for a patient that produced no chart). A missing referenced
    # patient is a defect — surface it loudly rather than write a half manifest.
    referenced = {item.patient_id for item in items}
    patients_json: dict[str, Any] = {}
    for patient_id in sorted(referenced):
        patient = patients_by_id.get(patient_id)
        if patient is None:
            raise ManifestError(
                f"manifest references patient_id {patient_id!r} with no matching record"
            )
        patients_json[patient_id] = patient.model_dump(mode="json")

    payload = {
        "version": MANIFEST_VERSION,
        "items": items_json,
        "patients": patients_json,
    }
    out = secure_output_dir(out_dir)
    path = out / MANIFEST_NAME
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    # PHI: log the COUNT only — never a name, a DOB, or a path under out_dir.
    logger.info("wrote upload manifest: %d item(s)", len(items_json))
    return path


def _require(data: dict[str, Any], key: str, path: Path) -> Any:
    if key not in data:
        raise ManifestError(f"upload manifest {path} missing required key {key!r}")
    return data[key]


def read_upload_manifest(out_dir: Path) -> tuple[list[UploadItem], dict[str, Patient]]:
    """Read the manifest back as ``(items, patients_by_id)``.

    Re-absolutizes each item's basename ``file_path`` against ``out_dir`` and
    validates each patient via :meth:`Patient.model_validate`. Loud on malformed:
    a missing file, a version mismatch, or a missing/wrong-shaped key raises
    :class:`ManifestError` rather than starting a run with partial data.
    """
    path = out_dir / MANIFEST_NAME
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"upload manifest {path} is missing or unreadable") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ManifestError(f"upload manifest {path} is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ManifestError(f"upload manifest {path} must be a JSON object")

    version = _require(data, "version", path)
    if version != MANIFEST_VERSION:
        raise ManifestError(
            f"upload manifest {path} version {version!r} != supported {MANIFEST_VERSION}"
        )

    raw_items = _require(data, "items", path)
    raw_patients = _require(data, "patients", path)
    if not isinstance(raw_items, list) or not isinstance(raw_patients, dict):
        raise ManifestError(f"upload manifest {path} has a malformed items/patients shape")

    items: list[UploadItem] = []
    for entry in raw_items:
        if not isinstance(entry, dict):
            raise ManifestError(f"upload manifest {path} item entry must be an object")
        try:
            items.append(
                UploadItem(
                    item_key=entry["item_key"],
                    encounter_id=entry["encounter_id"],
                    patient_id=entry["patient_id"],
                    # Re-absolutize the stored basename against out_dir.
                    file_path=out_dir / str(entry["file_path"]),
                    sha256=entry["sha256"],
                    size_bytes=int(entry["size_bytes"]),
                    fingerprint=entry["fingerprint"],
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ManifestError(
                f"upload manifest {path} item entry is malformed ({type(exc).__name__})"
            ) from exc

    patients: dict[str, Patient] = {}
    for patient_id, raw_patient in raw_patients.items():
        try:
            patients[str(patient_id)] = Patient.model_validate(raw_patient)
        except (ValueError, TypeError) as exc:
            raise ManifestError(
                f"upload manifest {path} patient {patient_id!r} failed validation "
                f"({type(exc).__name__})"
            ) from exc

    return items, patients
