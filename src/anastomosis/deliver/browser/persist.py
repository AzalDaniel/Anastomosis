"""Manifest persistence: the bridge from a render run to a later ``anast upload``.

:func:`write_upload_manifest` writes it; :func:`load_upload_manifest` reads
it back. Deterministic (``sort_keys=True``, sorted keys, no clock or
random); loud on malformed (44). Carries demographics and, from v2, dates
of service — hardened-dir only, never logged, never committed (45). From
v3 it also carries the reviewed route/gates (46); from v4, source
documents and per-item :class:`~anastomosis.deliver.verify.types.VerifyPolicy`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from anastomosis.core.atomic import atomic_write_text
from anastomosis.core.logutil import exc_tag
from anastomosis.core.model import Encounter, Patient
from anastomosis.core.output import secure_output_dir
from anastomosis.deliver.verify.types import VerifyPolicy
from anastomosis.destinations.base import UploadItem
from anastomosis.pipeline import ATTACHMENTS_DIRNAME

from .gates import GATE_VERSION, RoutePlan, RunGates
from .manifest import SourceDocuments, build_attachment_manifest, build_manifest

if TYPE_CHECKING:
    from anastomosis.core.model import PatientRecord
    from anastomosis.reconstruct.engine import RenderedDoc

__all__ = [
    "GATE_VERSION",
    "LADDER_VERSION",
    "MANIFEST_NAME",
    "MANIFEST_VERSION",
    "POLICY_VERSION",
    "SUPPORTED_MANIFEST_VERSIONS",
    "ManifestError",
    "UploadManifest",
    "WrittenManifest",
    "load_upload_manifest",
    "read_upload_manifest",
    "write_upload_manifest",
]

logger = logging.getLogger(__name__)

MANIFEST_NAME = "upload_manifest.json"
MANIFEST_VERSION = 4

#: The version that introduced the L0-L6 ladder fields (``pack``,
#: ``expected_pages``, ``date_of_service``).
LADDER_VERSION = 2
#: The version that introduced the route plan/gate outcomes; defined in
#: :mod:`.gates` (which this module cannot import back) and re-exported here.

#: The version that introduced source documents as items and their
#: per-item ``verify_policy`` — nothing before v4 could carry a non-chart file.
POLICY_VERSION = 4

# Versions load_upload_manifest accepts; anything else raises. Each field
# group is gated on the version that introduced it, not on
# MANIFEST_VERSION, so a v2 file never loses its ladder fields once v3+ exists.
SUPPORTED_MANIFEST_VERSIONS: frozenset[int] = frozenset(
    {1, LADDER_VERSION, GATE_VERSION, POLICY_VERSION}
)


class ManifestError(Exception):
    """The upload manifest is missing or malformed (44); the message names
    the file and a PHI-safe structural reason, never a patient value.
    """


@dataclass(frozen=True)
class UploadManifest:
    """One manifest file read back. ``pack``, ``expected_pages`` and
    ``encounters`` are v2 (empty + :attr:`degraded` for v1); ``route``
    and ``gates`` are v3 (``None`` for older or an unrecorded v3+);
    ``verify_policies`` is v4 (a missing key reads as
    :attr:`~.VerifyPolicy.RENDERED_CHART`)."""

    version: int
    items: list[UploadItem]
    patients: dict[str, Patient]
    pack: str | None
    expected_pages: dict[str, int]
    encounters: dict[str, Encounter]
    route: RoutePlan | None = None
    gates: RunGates | None = None
    verify_policies: dict[str, VerifyPolicy] = field(default_factory=dict)

    @property
    def degraded(self) -> bool:
        """Whether this is a pre-ladder (v1) file: L3 has no pack or dates
        of service, and L1 has no exact page count.
        """
        return self.version < LADDER_VERSION


@dataclass(frozen=True)
class WrittenManifest:
    """What one :func:`write_upload_manifest` call put on disk: the run's
    own rail reads these counts, so a writer cannot drift from what it wrote.
    """

    path: Path
    charts: int
    documents: int
    not_carried: int

    @property
    def items(self) -> int:
        """Every item in the file: the rendered charts and the source documents."""
        return self.charts + self.documents


def _stored_path(item: UploadItem, out_dir: Path) -> str:
    """The item's file as the manifest records it: relative to ``out_dir``
    so the manifest is relocatable — a chart's basename, or
    ``attachments/<name>`` for a source document. Falls back to the
    basename for a file outside ``out_dir`` altogether.
    """
    try:
        return item.file_path.relative_to(out_dir).as_posix()
    except ValueError:
        return item.file_path.name


def _item_to_json(
    item: UploadItem,
    *,
    stored_path: str,
    policy: VerifyPolicy,
    expected_pages: int | None,
    date_of_service: date | None,
    version: int,
) -> dict[str, Any]:
    """One item as a deterministic JSON object. ``expected_pages``/
    ``date_of_service`` are ``null`` when the render run could not know
    them, so a level fails loudly rather than assuming a value.
    ``verify_policy`` is written only from v4 (46).
    """
    entry: dict[str, Any] = {
        "item_key": item.item_key,
        "encounter_id": item.encounter_id,
        "patient_id": item.patient_id,
        "file_path": stored_path,
        "sha256": item.sha256,
        "size_bytes": item.size_bytes,
        "fingerprint": item.fingerprint,
        "expected_pages": expected_pages,
        "date_of_service": date_of_service.isoformat() if date_of_service is not None else None,
    }
    if version >= POLICY_VERSION:
        entry["verify_policy"] = policy.value
    return entry


def _pymupdf_or_none() -> Any:
    """PyMuPDF if the ``render`` extra is installed, else ``None`` — a
    missing optional dependency costs page counts, never the manifest (75).
    """
    try:
        import pymupdf
    except ImportError:
        return None
    return pymupdf


def _page_counts(items: list[UploadItem]) -> dict[str, int]:
    """Measure each pageable PDF's page count (L1's ``expected_pages``). An
    unreadable item is simply absent from the result (a count and
    exception type only are logged); absent means L1 falls back to its
    page floor, never an invented count."""
    if not items:  # nothing to page: no counts to take, and nothing to warn about
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


def _pageable(items: list[UploadItem], policies: dict[str, VerifyPolicy]) -> list[UploadItem]:
    """Items worth page-counting: a rendered chart always; a source
    document only when it declared a pageable media type — opening an
    undeclared scan would report an unreadable count, not a bundle fact.
    """
    opaque = VerifyPolicy.SOURCE_OPAQUE
    return [
        item
        for item in items
        if policies.get(item.item_key, VerifyPolicy.RENDERED_CHART) is not opaque
    ]


def _assert_one_file_per_item_key(items: list[UploadItem]) -> None:
    """Two items may not share an ``item_key``: it is the tracking
    ledger's primary key, so a collision enqueues one row and the other
    item is silently never sent. Refused rather than half-delivered (44).
    PHI: counts only.
    """
    keys = {item.item_key for item in items}
    if len(keys) != len(items):
        raise ManifestError(
            f"{len(items) - len(keys)} of {len(items)} manifest item(s) share an item_key with "
            "another; the upload ledger keys on it, so one file per collision would never be "
            "sent. Refusing to write a manifest that cannot deliver what it lists"
        )


def _file_version(carried: SourceDocuments, *, gates: RunGates | None) -> int:
    """The schema version this file's CONTENT is, not the build that
    wrote it: no gate record is a v2 file regardless of build version
    (46); any source document makes it v4, else v3/v2 by ``gates``.
    """
    if carried.items:
        return POLICY_VERSION
    return GATE_VERSION if gates is not None else LADDER_VERSION


def write_upload_manifest(
    documents: Iterable[RenderedDoc],
    records: Iterable[PatientRecord],
    out_dir: Path,
    *,
    pack: str | None = None,
    route: RoutePlan | None = None,
    gates: RunGates | None = None,
) -> WrittenManifest:
    """Contract: writes ``<out_dir>/upload_manifest.json`` (0o700). Items
    are rendered charts plus source documents (#374); only patients an
    item references are written. ``route``/``gates`` are the reviewed
    context, ``null`` when neither was checked. Deterministic; PHI stays
    in-file, only counts are logged."""
    held = list(records)  # walked twice below; ``records`` may be a one-shot iterable
    charts = build_manifest(documents)
    carried = build_attachment_manifest(held, out_dir / ATTACHMENTS_DIRNAME)
    items = [*charts, *carried.items]
    _assert_one_file_per_item_key(items)
    # One pass: canonical patient_id -> Patient for item lookups, and
    # encounter_id -> DOS for L3's verification.
    patients_by_id: dict[str, Patient] = {}
    dos_by_encounter: dict[str, date | None] = {}
    for record in held:
        patients_by_id[record.patient.id] = record.patient
        for encounter in record.encounters:
            dos_by_encounter[encounter.id] = encounter.date_of_service

    version = _file_version(carried, gates=gates)
    page_counts = _page_counts(_pageable(items, carried.policies))
    items_json = [
        _item_to_json(
            item,
            stored_path=_stored_path(item, out_dir),
            policy=carried.policies.get(item.item_key, VerifyPolicy.RENDERED_CHART),
            expected_pages=page_counts.get(item.item_key),
            date_of_service=dos_by_encounter.get(item.encounter_id),
            version=version,
        )
        for item in sorted(items, key=lambda it: it.item_key)
    ]
    # Only patients referenced by an item are written; a missing
    # referenced patient is a defect, surfaced loudly.
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
        "version": version,
        "pack": pack,
        "route": None if route is None else route.as_json(),
        "gates": None if gates is None else gates.as_json(),
        "items": items_json,
        "patients": patients_json,
    }
    out = secure_output_dir(out_dir)
    path = out / MANIFEST_NAME
    # PHI-BY-DESIGN: demographics + DOS, written only into this hardened
    # dir (45); see SECURITY.md "Code scanning & suppression policy".
    # codeql[py/clear-text-storage-sensitive-data]
    atomic_write_text(path, json.dumps(payload, sort_keys=True, indent=2) + "\n")
    # PHI: counts only — never a name, DOB, date of service, or path.
    written = WrittenManifest(
        path=path,
        charts=len(charts),
        documents=len(carried.items),
        not_carried=carried.not_carried,
    )
    logger.info(
        "wrote upload manifest v%d: %d item(s) (%d source document(s)), "
        "%d with an expected page count",
        version,
        written.items,
        written.documents,
        len(page_counts),
    )
    _report_not_carried(written)
    return written


def _report_not_carried(written: WrittenManifest) -> None:
    """Say out loud that this manifest leaves out documents the records
    name — never silent, because that gap is what #374 was about.
    PHI: counts only.
    """
    if written.not_carried:
        logger.warning(
            "%d source document(s) named by these records are not in this bundle and are "
            "NOT in its upload manifest (%d item(s) written): an upload over it delivers "
            "the charts and the documents that were carried, and nothing else",
            written.not_carried,
            written.items,
        )


def _require(data: dict[str, Any], key: str, path: Path) -> Any:
    if key not in data:
        raise ManifestError(f"upload manifest {path} missing required key {key!r}")
    return data[key]


def _resolved_file(stored: str, out_dir: Path, path: Path) -> Path:
    """Re-absolutize a stored relative path against ``out_dir``, refusing
    one that climbs outside it — a bundle moves between machines, so
    what it names must stay inside it (44).
    """
    resolved = (out_dir / stored).resolve()
    if not resolved.is_relative_to(out_dir.resolve()):
        raise ManifestError(
            f"upload manifest {path} names a file outside its own directory; "
            "refusing to deliver bytes from outside the bundle"
        )
    return out_dir / stored


def _item_from_json(
    entry: dict[str, Any], out_dir: Path, *, version: int, path: Path
) -> tuple[UploadItem, int | None, date | None]:
    """One item entry as ``(item, expected_pages, date_of_service)``. The
    ladder fields are required keys from v2; an absent key at that
    version is a defect, ``null`` is an honest "did not know". A v1
    entry yields ``None`` for both.
    """
    pages: int | None = None
    dos: date | None = None
    try:
        if version >= LADDER_VERSION:
            raw_pages = entry["expected_pages"]
            raw_dos = entry["date_of_service"]
            pages = None if raw_pages is None else int(raw_pages)
            dos = None if raw_dos is None else date.fromisoformat(str(raw_dos))
        item = UploadItem(
            item_key=entry["item_key"],
            encounter_id=entry["encounter_id"],
            patient_id=entry["patient_id"],
            # Re-absolutize the stored relative path against out_dir.
            file_path=_resolved_file(str(entry["file_path"]), out_dir, path),
            sha256=entry["sha256"],
            size_bytes=int(entry["size_bytes"]),
            fingerprint=entry["fingerprint"],
            # None on a v1 entry, which had no such field: a pack that needs a
            # document date then refuses the item rather than inventing one.
            date_of_service=dos,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ManifestError(
            f"upload manifest {path} item entry is malformed ({type(exc).__name__})"
        ) from exc
    return item, pages, dos


def _policy_from_json(entry: dict[str, Any], *, version: int, path: Path) -> VerifyPolicy:
    """One item's verification policy: absent before v4 reads as
    :attr:`VerifyPolicy.RENDERED_CHART`; required and validated at v4 —
    an unrecognised value must not fall back to the chart ladder (46).
    """
    if version < POLICY_VERSION:
        return VerifyPolicy.RENDERED_CHART
    raw = _require(entry, "verify_policy", path)
    try:
        return VerifyPolicy(str(raw))
    except ValueError as exc:
        raise ManifestError(
            f"upload manifest {path} item declares an unknown verify_policy"
        ) from exc


def _reviewed_context(
    data: dict[str, Any], path: Path, *, version: int
) -> tuple[RoutePlan | None, RunGates | None]:
    """The v3 route plan and gate outcomes, or ``(None, None)`` before v3.
    Both keys are required from v3 and may be ``null`` (nothing to
    record); a malformed value raises rather than degrading to "no
    record", which :func:`assert_deliverable` treats as passable (46).
    """
    if version < GATE_VERSION:
        return None, None
    raw_route = _require(data, "route", path)
    raw_gates = _require(data, "gates", path)
    # Both keys must be present (_require above); null is a legitimate
    # "nothing to record" — assert_deliverable, not this reader, decides.
    try:
        route = None if raw_route is None else RoutePlan.from_json(raw_route)
        gates = None if raw_gates is None else RunGates.from_json(raw_gates)
    except (KeyError, TypeError, ValueError) as exc:
        raise ManifestError(
            f"upload manifest {path} route/gates entry is malformed ({type(exc).__name__})"
        ) from exc
    return route, gates


def load_upload_manifest(out_dir: Path) -> UploadManifest:
    """Contract: reads the manifest, re-absolutizing each ``file_path``
    and validating patients; raises :class:`ManifestError` on anything
    missing or malformed (44). A v1 file is accepted, degraded, and
    logged once; pre-v3 carries no route/gates (46). Returned
    :class:`Encounter` objects carry only the DOS L3 needs."""
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
    pack = _require(data, "pack", path) if version >= LADDER_VERSION else None
    if pack is not None and not isinstance(pack, str):
        raise ManifestError(f"upload manifest {path} pack must be a string or null")
    route, gates = _reviewed_context(data, path, version=version)

    items: list[UploadItem] = []
    expected_pages: dict[str, int] = {}
    encounters: dict[str, Encounter] = {}
    policies: dict[str, VerifyPolicy] = {}
    for entry in raw_items:
        if not isinstance(entry, dict):
            raise ManifestError(f"upload manifest {path} item entry must be an object")
        item, pages, dos = _item_from_json(entry, out_dir, version=version, path=path)
        items.append(item)
        policies[item.item_key] = _policy_from_json(entry, version=version, path=path)
        if pages is not None:
            expected_pages[item.item_key] = pages
        if version >= LADDER_VERSION:
            # Recorded for every v2 item, DOS or not: "no date of service"
            # is itself the answer L3 needs.
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
        route=route,
        gates=gates,
        verify_policies=policies,
    )
    if manifest.degraded:
        # PHI-safe: versions and a count. Never silent — the log names
        # what verification this version skips.
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
    """The ``(items, patients)`` projection of :func:`load_upload_manifest`
    — cheap pre-attach validation and the engine's two positional inputs.
    """
    manifest = load_upload_manifest(out_dir)
    return manifest.items, manifest.patients
