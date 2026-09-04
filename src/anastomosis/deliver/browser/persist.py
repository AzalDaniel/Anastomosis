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
TYPE), and is NEVER committed. ``file_path`` is stored relative to ``out_dir``
— a basename for a chart, ``attachments/<name>`` for a source document — so the
manifest is relocatable and never embeds an absolute path; it is re-absolutized
against ``out_dir`` on read, and a stored path that would climb out of the
bundle is refused there rather than followed.

From v3 the file also carries the run's REVIEWED context: the destination route
plan it was prepared for and the gate outcomes it passed
(:mod:`anastomosis.deliver.browser.gates`). Those are what
:func:`~anastomosis.deliver.browser.gates.assert_deliverable` refuses on, so the
bundle an executor moves is the bundle somebody checked.

From v4 it also carries the bundle's SOURCE DOCUMENTS — the scans and reports
in ``charts/attachments`` — and, per item, the
:class:`~anastomosis.deliver.verify.types.VerifyPolicy` that says which of the
L0-L6 levels can honestly be run over those bytes. Before that, a patient whose
whole chart is a scanned Unstructured Document produced a manifest with zero
items and zero patients, and the run exited 0.

See ``docs/UPLOAD_MANIFEST.md`` for what each schema version carries and why a
v1 file still loads.
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
#: The version that introduced the reviewed route plan and the run's gate
#: outcomes is defined by :mod:`anastomosis.deliver.browser.gates` — the
#: delivery decision needs it too, and that module cannot import this one back
#: — and re-exported here, where every caller already looks for it.

#: The version that introduced the bundle's source documents as items, and with
#: them the per-item ``verify_policy`` they need: nothing before v4 could carry
#: a file the ladder must not read as a rendered chart.
POLICY_VERSION = 4

# Versions :func:`load_upload_manifest` accepts; anything else is a defect and
# raises. Each field group is gated on the version that introduced it, not on
# ``MANIFEST_VERSION`` — the reader used to do the latter, which was correct
# only while 2 was the newest and would have silently dropped v2's ladder
# fields out of a v2 file the moment 3 existed.
SUPPORTED_MANIFEST_VERSIONS: frozenset[int] = frozenset(
    {1, LADDER_VERSION, GATE_VERSION, POLICY_VERSION}
)


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

    :attr:`route` and :attr:`gates` are the v3 additions and are ``None`` for
    anything older — and for a v3 file whose writer had nothing to record. They
    are what :func:`~anastomosis.deliver.browser.gates.assert_deliverable`
    decides on; ``None`` there means "this bundle never recorded its gates",
    which is a warning rather than a refusal (see that module).

    :attr:`verify_policies` is the v4 addition, keyed by ``item_key`` like
    :attr:`expected_pages`. Every item of an older file is a rendered chart, so
    a missing key reads as :attr:`~.VerifyPolicy.RENDERED_CHART` and nothing
    about an existing tree changes.
    """

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
        """Whether this file predates the ladder fields (a v1 manifest).

        True means an upload over these items verifies LESS than a current one:
        L3 has no pack and no dates of service, and L1 has no exact page count.
        """
        return self.version < LADDER_VERSION


@dataclass(frozen=True)
class WrittenManifest:
    """What one :func:`write_upload_manifest` call put on disk.

    The counts are here because the run's own rail reads them. The MANIFEST
    stage event used to count the RENDERED documents it had been handed, which
    was the same number as the items only while charts were the only kind of
    item — an attachment-only bundle then announced ``0 item(s)`` over a file
    that had two. A writer that reports what it wrote cannot drift from it.
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
    """The item's file as the manifest records it: relative to the bundle.

    A relative path (not an absolute one) is stored so the manifest is
    relocatable and never embeds the host directory layout; it is re-absolutized
    against ``out_dir`` on read. A chart sits in the bundle's root and so keeps
    the basename this has always written; a source document sits in
    ``attachments/``, and storing ITS basename alone would re-absolutize to a
    file that is not there — the item would resolve to nothing on a machine that
    only has the manifest.

    A file outside ``out_dir`` altogether has no relative form, so it keeps its
    basename: the same answer, and the same limitation, as before.
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
    """One item as a deterministic JSON object.

    ``expected_pages`` and ``date_of_service`` are ``null`` when the render run
    could not know them (a PDF that would not parse; an item with no encounter,
    as the whole-patient ccda-standard view has). A ``null`` makes the level that
    wants it skip or fail loudly on upload — it never lets a level pass on an
    assumed value.

    ``verify_policy`` appears only in a v4 file. A v2/v3 file's items are all
    rendered charts by construction, so writing the field into one would state
    at a version its reader does not know about the one thing every item there
    already is.
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
    """Measure each pageable PDF's page count: L1's ``expected_pages`` on upload.

    The page count is a WRITE-TIME fact — what this run actually produced, or
    carried — and recording it is what lets L1 assert "exactly N pages" hours
    later, on another machine, against a file it re-opens itself. Deriving it at
    upload time instead would prove only that the file agrees with itself.

    Takes the items worth opening (:func:`_pageable`), not every item: a source
    document under a media type nothing here pages has no count to take, and
    asking for one anyway would report it below as unreadable.

    Never silent: an item whose count cannot be read (no PyMuPDF, or a file that
    will not parse) is simply absent from the result, and the miss is logged as a
    COUNT plus the exception TYPE — never a path, a name, or a patient value.
    Absent means L1 falls back to its page floor for that item; it never means an
    invented count.
    """
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
    """The items whose page count is worth measuring.

    A rendered chart always is. A source document is only when the SOURCE said
    it was a media type this toolkit pages: opening a TIFF scan (or a body that
    declared no type) to ask how many pages it has would report every one of
    them as an unreadable count, which is a warning about the toolkit dressed up
    as a warning about the bundle.
    """
    opaque = VerifyPolicy.SOURCE_OPAQUE
    return [
        item
        for item in items
        if policies.get(item.item_key, VerifyPolicy.RENDERED_CHART) is not opaque
    ]


def _assert_one_file_per_item_key(items: list[UploadItem]) -> None:
    """Two items may not share an ``item_key``, because the ledger dedupes them.

    ``item_key`` is the tracking ledger's PRIMARY KEY — that is what makes a
    killed run resumable — so two items arriving under one key are enqueued as
    one row and exactly one file is ever uploaded. The other is not refused and
    not reported: it is simply never sent.

    Reachable now that source documents are items: one patient's scan carried
    twice under two names is two files, and both take the same key (no encounter
    to tell them apart, so the patient id stands in, and identical bytes give an
    identical digest). Refused rather than half-delivered.

    PHI: counts only. An ``item_key`` embeds an encounter id, so it is not
    printed even though those ids are pseudonymous.
    """
    keys = {item.item_key for item in items}
    if len(keys) != len(items):
        raise ManifestError(
            f"{len(items) - len(keys)} of {len(items)} manifest item(s) share an item_key with "
            "another; the upload ledger keys on it, so one file per collision would never be "
            "sent. Refusing to write a manifest that cannot deliver what it lists"
        )


def _file_version(carried: SourceDocuments, *, gates: RunGates | None) -> int:
    """The schema version this file's CONTENT is, not the build that wrote it.

    A writer given no gate record produces a manifest that carries none, which
    is exactly a version-2 file — and stamping it 3 anyway was the ambiguity
    underneath the whole grandfather clause: a reader could not tell "written
    before gates existed" from "written now and edited since", so the branch
    meant for old trees was reachable by deleting one value from a current one.

    A file carrying source documents is a v4 file by the same rule, because a
    v3 reader would take its ``attachments/`` items for rendered charts and run
    the chart ladder over a scan. Nothing else moves: a bundle of charts with
    gates is the same v3 file it was.
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
    """Write ``<out_dir>/upload_manifest.json`` for a later ``anast upload``.

    Builds the items via :func:`build_manifest` (so the same content hashing and
    ``item_key`` rule the engine relies on is reused, not re-implemented), then
    selects the :class:`Patient` each item refers to from ``records``. Only the
    patients an item actually references are written. The file lands inside
    :func:`secure_output_dir` (``0o700``).

    The bundle's SOURCE DOCUMENTS are items too
    (:func:`~anastomosis.deliver.browser.manifest.build_attachment_manifest`).
    Serializing the rendered charts alone was the whole of issue #374: a C-CDA
    Unstructured Document renders no encounter, so a patient whose entire chart
    is a scan produced ``0 item(s)``, ``0 patients`` and exit 0 while both of
    their documents sat in ``<out_dir>/attachments``. A patient reaches
    ``patients`` because an ITEM names them, so an attachment-only patient now
    arrives there by the same rule every other patient always has.

    ``pack`` is the template pack that rendered ``documents`` — the name the
    upload side reloads L3's ``verify_header_fields`` from. ``None`` says no
    Jinja pack rendered these charts (the ccda-standard whole-patient view), and
    the upload side reports L3 as skipped for that reason rather than checking
    against a pack that never ran.

    ``route`` is the destination route plan this bundle was PREPARED for and
    ``gates`` is what the run checked before writing it — the reviewed context
    an executor refuses on hours later
    (:func:`~anastomosis.deliver.browser.gates.assert_deliverable`). A caller
    that has neither writes ``null`` for both, which is honest: the bundle
    records that nothing was checked rather than implying something was.

    Deterministic: items are sorted by ``item_key``, patients keyed by
    ``patient_id``, and the JSON is written with ``sort_keys=True`` — two writes
    over the same inputs are byte-identical.

    PHI rule: this file carries demographics and dates of service, so it stays
    inside the hardened output dir and is never logged. The only log lines are
    counts.
    """
    held = list(records)  # walked twice below; ``records`` may be a one-shot iterable
    charts = build_manifest(documents)
    carried = build_attachment_manifest(held, out_dir / ATTACHMENTS_DIRNAME)
    items = [*charts, *carried.items]
    _assert_one_file_per_item_key(items)
    # One pass over ``records``: the canonical patient_id -> Patient for the
    # items' lookups, and the encounter_id -> DOS that L3's ``dos`` field is
    # verified against on upload.
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
        "version": version,
        "pack": pack,
        "route": None if route is None else route.as_json(),
        "gates": None if gates is None else gates.as_json(),
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
    """Say out loud that this manifest leaves out documents the records name.

    Not every caller assembles the bundle it writes a manifest for: the pack
    pipeline carries every named attachment before it gets here (and refuses the
    run when one did not land), while a ``migrate --render ccda-standard`` writes
    its manifest beside charts it rendered and never carried an attachment to at
    all. So an absent file cannot be a refusal here without stranding that mode
    — but it must never be a silence either, because "this patient's scan is not
    in the delivery" is precisely the fact #374 was about.

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
    """Re-absolutize a stored item path against ``out_dir``, or refuse it.

    The stored path is relative BY CONTRACT (a basename for a chart,
    ``attachments/<name>`` for a source document), and it is read off a file. An
    absolute path, or one that climbs out with ``..``, would make the manifest a
    way to point an upload at any file the process can read — a bundle is copied
    between machines, so what it names has to stay inside the bundle. Refused
    loudly, never quietly re-based.
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
    """One item entry as ``(item, expected_pages, date_of_service)``.

    The ladder fields are REQUIRED keys in a current-version file: a ``null``
    value is the render run saying it did not know the value, while an ABSENT
    key means the file does not match the version it declares — a defect, so it
    raises. A v1 entry carries neither and yields ``None`` for both.

    The date of service is read ONCE and handed out twice: returned beside the
    item for the encounter map L3 verifies against, and set on the item itself
    for a destination whose filing dialog asks for a document date. No new
    field is written, so this stays a v2 file — the value was already there.
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
    """One item's verification policy: required at v4, a chart before it.

    A pre-v4 file has only rendered charts in it, so the absent key is not a
    missing value — it is the answer. At v4 the key is required and its value
    must be one this build knows: a policy nothing here recognizes would
    otherwise fall back to the chart ladder, which is exactly the wrong
    direction to fail in for a scan.
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
    """The v3 route plan and gate outcomes, or ``(None, None)`` for an older file.

    Both keys are REQUIRED in a v3 file and both may be ``null``: absent means
    the file does not match the version it declares, which is a defect, while
    ``null`` is the writer saying it had nothing to record. A malformed value
    raises rather than degrading — a half-read gate record is exactly the thing
    that must not quietly become "no gate record", because that is the case an
    executor is allowed to proceed past.
    """
    if version < GATE_VERSION:
        return None, None
    raw_route = _require(data, "route", path)
    raw_gates = _require(data, "gates", path)
    # Both keys must be PRESENT at this version (``_require`` above), and both
    # may legitimately be null: a render that chose no destination has no route
    # to record, and saying so is the honest answer. What a null gate record
    # may not do is buy delivery — that decision belongs to
    # :func:`~anastomosis.deliver.browser.gates.assert_deliverable`, which
    # refuses it at this version rather than warning, and warns only for a
    # manifest old enough to predate the record entirely.
    try:
        route = None if raw_route is None else RoutePlan.from_json(raw_route)
        gates = None if raw_gates is None else RunGates.from_json(raw_gates)
    except (KeyError, TypeError, ValueError) as exc:
        raise ManifestError(
            f"upload manifest {path} route/gates entry is malformed ({type(exc).__name__})"
        ) from exc
    return route, gates


def load_upload_manifest(out_dir: Path) -> UploadManifest:
    """Read the manifest back in full, as an :class:`UploadManifest`.

    Re-absolutizes each item's relative ``file_path`` against ``out_dir`` (a
    stored path that would climb out of the bundle is refused) and validates each
    patient via :meth:`Patient.model_validate`. Loud on malformed: a missing
    file, an unsupported version, or a missing/wrong-shaped key raises
    :class:`ManifestError` rather than starting a run with partial data.

    A v1 file (no pack, no expected pages, no dates of service) is accepted —
    refusing it would strand every already-rendered tree — and logs ONE warning
    naming the degraded coverage. The line carries a version and a count only.
    A pre-v3 file likewise carries no route plan and no gate outcomes; what an
    executor does about that is
    :func:`~anastomosis.deliver.browser.gates.assert_deliverable`'s decision,
    not this reader's.

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
        # Recorded for every item at every version: a pre-v4 file's items are
        # rendered charts, which is an answer, not a gap.
        policies[item.item_key] = _policy_from_json(entry, version=version, path=path)
        if pages is not None:
            expected_pages[item.item_key] = pages
        if version >= LADDER_VERSION:
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
        route=route,
        gates=gates,
        verify_policies=policies,
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
