"""Manifest persistence: the bridge from a render run to a later ``anast upload``.

An upload happens after the charts are built, often on another machine, so the
upload driver cannot re-run the pipeline — it reads this file instead.
:func:`write_upload_manifest` writes it; :func:`load_upload_manifest` reads it
back (:func:`read_upload_manifest` is the ``(items, patients)`` projection).

Two invariants shape the file:

* **Deterministic.** ``sort_keys=True``, items sorted by ``item_key``, patients
  keyed by ``patient_id`` — two writes over the same inputs are byte-identical.
  No clock, no random.
* **Loud on malformed.** A missing file, an unsupported version, or a missing
  key raises :class:`ManifestError`. A corrupt manifest is a defect to surface,
  not a run to start with half the data.

PHI rule, load-bearing: this file carries patient demographics (the resolver
needs name + DOB) and, from v2, dates of service. It lives ONLY inside the
hardened ``0o700`` output directory
(:func:`anastomosis.core.output.secure_output_dir`) beside the chart PDFs those
values are rendered into — so v2 opens no new exposure surface — is NEVER
logged (every log line here is a count, a version number, or an exception
TYPE), and is NEVER committed. ``file_path`` is stored as a basename so the
manifest is relocatable and never embeds an absolute path; it is re-absolutized
against ``out_dir`` on read.

See ``docs/UPLOAD_MANIFEST.md`` for what each schema version carries and why a
v1 file still loads.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from anastomosis.core.atomic import atomic_write_text
from anastomosis.core.logutil import exc_tag
from anastomosis.core.model import Encounter, Patient
from anastomosis.core.output import secure_output_dir
from anastomosis.destinations.base import UploadItem

from .manifest import build_manifest

if TYPE_CHECKING:
    from anastomosis.core.model import PatientRecord
    from anastomosis.reconstruct.engine import RenderedDoc

__all__ = [
    "MANIFEST_NAME",
    "MANIFEST_VERSION",
    "SUPPORTED_MANIFEST_VERSIONS",
    "ManifestError",
    "UploadManifest",
    "load_upload_manifest",
    "read_upload_manifest",
    "write_upload_manifest",
]

logger = logging.getLogger(__name__)

MANIFEST_NAME = "upload_manifest.json"
MANIFEST_VERSION = 2

# Versions :func:`load_upload_manifest` accepts. v1 predates the ladder fields
# and loads degraded; anything else is a defect and raises. The reader tells the
# two apart with ``version < MANIFEST_VERSION``, which holds only while 1 is the
# single older version — a future v3 must gate each field on its own version.
SUPPORTED_MANIFEST_VERSIONS: frozenset[int] = frozenset({1, MANIFEST_VERSION})


class ManifestError(Exception):
    """The upload manifest is missing or malformed — loud, never a silent skip.

    Raised by :func:`load_upload_manifest` for an absent file, an unsupported
    version, or a missing/wrong-shaped key. The message names the file and the
    fault (both PHI-safe — a path to the manifest and a structural reason, never
    a patient value) so the caller can surface a clean error instead of a
    ``KeyError``/``JSONDecodeError`` traceback.
    """


@dataclass(frozen=True)
class UploadManifest:
    """One manifest file read back: the items plus what the ladder checks against.

    :attr:`pack`, :attr:`expected_pages` and :attr:`encounters` are the v2
    additions and are empty for a v1 file — :attr:`degraded` says so, and the
    reader has already logged it. Each map is keyed the way
    :class:`~anastomosis.deliver.verify.LayeredVerifier` looks it up:
    ``expected_pages`` by ``item_key``, ``encounters`` by ``encounter_id``.
    """

    version: int
    items: list[UploadItem]
    patients: dict[str, Patient]
    pack: str | None
    expected_pages: dict[str, int]
    encounters: dict[str, Encounter]

    @property
    def degraded(self) -> bool:
        """Whether this file predates the ladder fields (a v1 manifest).

        True means an upload over these items verifies LESS than a current one:
        L3 has no pack and no dates of service, and L1 has no exact page count.
        """
        return self.version < MANIFEST_VERSION


def _item_to_json(
    item: UploadItem, *, expected_pages: int | None, date_of_service: date | None
) -> dict[str, Any]:
    """One item as a deterministic JSON object — ``file_path`` as a basename.

    The basename (not the absolute path) is stored so the manifest is
    relocatable and never embeds the host directory layout; it is re-absolutized
    against ``out_dir`` on read.

    ``expected_pages`` and ``date_of_service`` are ``null`` when the render run
    could not know them (a PDF that would not parse; an item with no encounter,
    as the whole-patient ccda-standard view has). A ``null`` makes the level that
    wants it skip or fail loudly on upload — it never lets a level pass on an
    assumed value.
    """
    return {
        "item_key": item.item_key,
        "encounter_id": item.encounter_id,
        "patient_id": item.patient_id,
        "file_path": item.file_path.name,
        "sha256": item.sha256,
        "size_bytes": item.size_bytes,
        "fingerprint": item.fingerprint,
        "expected_pages": expected_pages,
        "date_of_service": date_of_service.isoformat() if date_of_service is not None else None,
    }


def _pymupdf_or_none() -> Any:
    """PyMuPDF if the ``render`` extra is installed, else ``None``.

    Imported here rather than at module scope so this module keeps loading on a
    machine without the extra (the whole ``deliver.browser`` package holds that
    line for Playwright too). Returning ``None`` instead of raising is the point:
    a missing optional dependency costs the manifest its page counts — announced
    loudly by the caller — it does not cost the operator their manifest.
    """
    try:
        import pymupdf
    except ImportError:
        return None
    return pymupdf


def _page_counts(items: list[UploadItem]) -> dict[str, int]:
    """Measure each rendered PDF's page count: L1's ``expected_pages`` on upload.

    The page count is a RENDER-TIME fact — what this run actually produced — and
    recording it is what lets L1 assert "exactly N pages" hours later, on another
    machine, against a file it re-opens itself. Deriving it at upload time
    instead would prove only that the file agrees with itself.

    Never silent: an item whose count cannot be read (no PyMuPDF, or a file that
    will not parse) is simply absent from the result, and the miss is logged as a
    COUNT plus the exception TYPE — never a path, a name, or a patient value.
    Absent means L1 falls back to its page floor for that item; it never means an
    invented count.
    """
    if not items:  # nothing rendered: no counts to take, and nothing to warn about
        return {}
    pymupdf = _pymupdf_or_none()
    if pymupdf is None:
        logger.warning(
            "page counts unmeasured for %d item(s): PyMuPDF is not installed "
            "(pip install 'anastomosis[render]') — an upload over this manifest "
            "will check the page floor only",
            len(items),
        )
        return {}
    pages: dict[str, int] = {}
    failures: dict[str, int] = {}
    for item in items:
        try:
            with pymupdf.open(item.file_path) as doc:
                pages[item.item_key] = int(doc.page_count)
        except Exception as exc:  # any unparseable render: record the miss, keep going
            tag = exc_tag(exc)
            failures[tag] = failures.get(tag, 0) + 1
    if failures:
        logger.warning(
            "page count unreadable for %d of %d item(s) (%s) — an upload over "
            "those items will check the page floor only",
            sum(failures.values()),
            len(items),
            ", ".join(f"{tag}={count}" for tag, count in sorted(failures.items())),
        )
    return pages


def write_upload_manifest(
    documents: Iterable[RenderedDoc],
    records: Iterable[PatientRecord],
    out_dir: Path,
    *,
    pack: str | None = None,
) -> Path:
    """Write ``<out_dir>/upload_manifest.json`` for a later ``anast upload``.

    Builds the items via :func:`build_manifest` (so the same content hashing and
    ``item_key`` rule the engine relies on is reused, not re-implemented), then
    selects the :class:`Patient` each item refers to from ``records``. Only the
    patients an item actually references are written. The file lands inside
    :func:`secure_output_dir` (``0o700``).

    ``pack`` is the template pack that rendered ``documents`` — the name the
    upload side reloads L3's ``verify_header_fields`` from. ``None`` says no
    Jinja pack rendered these charts (the ccda-standard whole-patient view), and
    the upload side reports L3 as skipped for that reason rather than checking
    against a pack that never ran.

    Deterministic: items are sorted by ``item_key``, patients keyed by
    ``patient_id``, and the JSON is written with ``sort_keys=True`` — two writes
    over the same inputs are byte-identical.

    PHI rule: this file carries demographics and dates of service, so it stays
    inside the hardened output dir and is never logged. The only log lines are
    counts.
    """
    items = build_manifest(documents)
    # One pass over ``records`` (it may be a one-shot iterable): the canonical
    # patient_id -> Patient for the items' lookups, and the encounter_id -> DOS
    # that L3's ``dos`` field is verified against on upload.
    patients_by_id: dict[str, Patient] = {}
    dos_by_encounter: dict[str, date | None] = {}
    for record in records:
        patients_by_id[record.patient.id] = record.patient
        for encounter in record.encounters:
            dos_by_encounter[encounter.id] = encounter.date_of_service

    page_counts = _page_counts(items)
    items_json = [
        _item_to_json(
            item,
            expected_pages=page_counts.get(item.item_key),
            date_of_service=dos_by_encounter.get(item.encounter_id),
        )
        for item in sorted(items, key=lambda it: it.item_key)
    ]
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
        "pack": pack,
        "items": items_json,
        "patients": patients_json,
    }
    out = secure_output_dir(out_dir)
    path = out / MANIFEST_NAME
    # PHI-BY-DESIGN: the upload manifest carries the demographics the later
    # ``anast upload`` resolver needs (name + DOB) and the dates of service L3
    # verifies, so it is written ONLY into this secure_output_dir-hardened
    # directory (0700 owner-only on POSIX; on Windows NTFS, inheritance stripped
    # and access limited to the current user, SYSTEM, and Administrators) with a
    # PHI-warning README, never logged and never committed. See SECURITY.md,
    # "Code scanning & suppression policy (auditable)".
    # codeql[py/clear-text-storage-sensitive-data]
    atomic_write_text(path, json.dumps(payload, sort_keys=True, indent=2) + "\n")
    # PHI: log COUNTS only — never a name, a DOB, a date of service, or a path
    # under out_dir. The page-count tally is the honest measure of how much of
    # L1 an upload over this manifest can actually run.
    logger.info(
        "wrote upload manifest v%d: %d item(s), %d with an expected page count",
        MANIFEST_VERSION,
        len(items_json),
        len(page_counts),
    )
    return path


def _require(data: dict[str, Any], key: str, path: Path) -> Any:
    if key not in data:
        raise ManifestError(f"upload manifest {path} missing required key {key!r}")
    return data[key]


def _item_from_json(
    entry: dict[str, Any], out_dir: Path, *, version: int, path: Path
) -> tuple[UploadItem, int | None, date | None]:
    """One item entry as ``(item, expected_pages, date_of_service)``.

    The ladder fields are REQUIRED keys in a current-version file: a ``null``
    value is the render run saying it did not know the value, while an ABSENT
    key means the file does not match the version it declares — a defect, so it
    raises. A v1 entry carries neither and yields ``None`` for both.
    """
    try:
        item = UploadItem(
            item_key=entry["item_key"],
            encounter_id=entry["encounter_id"],
            patient_id=entry["patient_id"],
            # Re-absolutize the stored basename against out_dir.
            file_path=out_dir / str(entry["file_path"]),
            sha256=entry["sha256"],
            size_bytes=int(entry["size_bytes"]),
            fingerprint=entry["fingerprint"],
        )
        if version < MANIFEST_VERSION:  # v1: no ladder fields to read
            return item, None, None
        raw_pages = entry["expected_pages"]
        raw_dos = entry["date_of_service"]
        pages = None if raw_pages is None else int(raw_pages)
        dos = None if raw_dos is None else date.fromisoformat(str(raw_dos))
    except (KeyError, TypeError, ValueError) as exc:
        raise ManifestError(
            f"upload manifest {path} item entry is malformed ({type(exc).__name__})"
        ) from exc
    return item, pages, dos


def load_upload_manifest(out_dir: Path) -> UploadManifest:
    """Read the manifest back in full, as an :class:`UploadManifest`.

    Re-absolutizes each item's basename ``file_path`` against ``out_dir`` and
    validates each patient via :meth:`Patient.model_validate`. Loud on malformed:
    a missing file, an unsupported version, or a missing/wrong-shaped key raises
    :class:`ManifestError` rather than starting a run with partial data.

    A v1 file (no pack, no expected pages, no dates of service) is accepted —
    refusing it would strand every already-rendered tree — and logs ONE warning
    naming the degraded coverage. The line carries a version and a count only.

    The DOS-only :class:`Encounter` objects returned in ``encounters`` are built
    for L3, which reads ``date_of_service`` and nothing else: they deliberately
    carry no sections and no clinical content, because the manifest deliberately
    stores none.
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
    if version not in SUPPORTED_MANIFEST_VERSIONS:
        raise ManifestError(
            f"upload manifest {path} version {version!r} is not one of the supported "
            f"{sorted(SUPPORTED_MANIFEST_VERSIONS)}"
        )

    raw_items = _require(data, "items", path)
    raw_patients = _require(data, "patients", path)
    if not isinstance(raw_items, list) or not isinstance(raw_patients, dict):
        raise ManifestError(f"upload manifest {path} has a malformed items/patients shape")
    pack = _require(data, "pack", path) if version >= MANIFEST_VERSION else None
    if pack is not None and not isinstance(pack, str):
        raise ManifestError(f"upload manifest {path} pack must be a string or null")

    items: list[UploadItem] = []
    expected_pages: dict[str, int] = {}
    encounters: dict[str, Encounter] = {}
    for entry in raw_items:
        if not isinstance(entry, dict):
            raise ManifestError(f"upload manifest {path} item entry must be an object")
        item, pages, dos = _item_from_json(entry, out_dir, version=version, path=path)
        items.append(item)
        if pages is not None:
            expected_pages[item.item_key] = pages
        if version >= MANIFEST_VERSION:
            # Recorded for every v2 item, DOS or not: "this encounter has no date
            # of service" is itself the answer L3 needs, and it fails the ``dos``
            # field loudly instead of looking like a missing record.
            encounters[item.encounter_id] = Encounter(
                id=item.encounter_id, patient_id=item.patient_id, date_of_service=dos
            )

    patients: dict[str, Patient] = {}
    for patient_id, raw_patient in raw_patients.items():
        try:
            patients[str(patient_id)] = Patient.model_validate(raw_patient)
        except (ValueError, TypeError) as exc:
            raise ManifestError(
                f"upload manifest {path} patient {patient_id!r} failed validation "
                f"({type(exc).__name__})"
            ) from exc

    manifest = UploadManifest(
        version=int(version),
        items=items,
        patients=patients,
        pack=pack,
        expected_pages=expected_pages,
        encounters=encounters,
    )
    if manifest.degraded:
        # PHI-safe: versions and a count. Never silent — an operator uploading an
        # older tree is told, in the run's own log, what is no longer checked.
        logger.warning(
            "upload manifest is version %d (current is %d): verification is DEGRADED for "
            "all %d item(s) — L3 (pack header/DOS fields) SKIPS and L1 checks the page "
            "floor instead of an exact page count. Re-render these charts to write a "
            "version-%d manifest and get the full ladder.",
            manifest.version,
            MANIFEST_VERSION,
            len(items),
            MANIFEST_VERSION,
        )
    return manifest


def read_upload_manifest(out_dir: Path) -> tuple[list[UploadItem], dict[str, Patient]]:
    """The ``(items, patients)`` projection of :func:`load_upload_manifest`.

    What a caller needs when it only has to answer "does this manifest parse,
    and over what?" — both frontends' cheap pre-attach validation, and the
    engine's two positional inputs. The upload drive itself takes the full
    :class:`UploadManifest`, because the ladder verifies against the rest of it.
    """
    manifest = load_upload_manifest(out_dir)
    return manifest.items, manifest.patients
