"""Build the upload manifest from rendered documents, and the operator skiplist.

Bridges reconstruction (:class:`RenderedDoc`) to the upload engine
(:class:`UploadItem`), streamed so a large PDF never fills memory.
:func:`build_attachment_manifest` does the same for attachment-only
patients (a scan has no encounter to render). Raises rather than drops a
row: a missing render, a changed or two-patient-claimed source document,
or a missing skiplist path (44). PHI: counts and
:func:`~anastomosis.core.logutil.safe_log_id` surrogates only (3).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from anastomosis.core.hashutil import hash_and_size
from anastomosis.core.logutil import safe_log_id
from anastomosis.deliver.verify.types import VerifyPolicy
from anastomosis.destinations.base import UploadItem
from anastomosis.reconstruct.engine import RenderedDoc

if TYPE_CHECKING:
    from anastomosis.core.model import DocumentArtifact, PatientRecord

__all__ = [
    "AttachmentNotDeliverable",
    "SourceDocuments",
    "build_attachment_manifest",
    "build_manifest",
    "is_skiplisted",
    "load_skiplist",
]

#: Media types this toolkit can page-count; read from what the source
#: declared, never sniffed from bytes.
PAGED_MEDIA_TYPES = frozenset({"application/pdf"})


class AttachmentNotDeliverable(Exception):
    """A source document cannot become an upload item: its digest fails to
    match what the record recorded, or two different patients both claim
    the same delivered file. Refusals only, never warnings — counts and
    :func:`~anastomosis.core.logutil.safe_log_id` surrogates only.
    """


@dataclass(frozen=True)
class SourceDocuments:
    """The source documents a bundle carries, as upload work. ``policies``
    is keyed by ``item_key`` (the ledger's six columns have no room for a
    seventh). ``not_carried`` counts named artifacts with no file here —
    not items, so a scan-only patient never reads as nothing to deliver.
    """

    items: list[UploadItem] = field(default_factory=list)
    policies: dict[str, VerifyPolicy] = field(default_factory=dict)
    not_carried: int = 0


def build_manifest(documents: Iterable[RenderedDoc]) -> list[UploadItem]:
    """One :class:`UploadItem` per :class:`RenderedDoc`: ``item_key`` is
    ``f"{encounter_id}:{sha256[:12]}"``, hashed via the same
    :func:`hash_and_size` preflight and L0 re-measure with. Raises
    :class:`FileNotFoundError` on a missing render file.
    """
    items: list[UploadItem] = []
    for doc in documents:
        sha256, size_bytes = hash_and_size(doc.path)
        items.append(
            UploadItem(
                item_key=f"{doc.encounter_id}:{sha256[:12]}",
                encounter_id=doc.encounter_id,
                patient_id=doc.patient_id,
                file_path=doc.path,
                sha256=sha256,
                size_bytes=size_bytes,
            )
        )
    return items


def _named_documents(
    records: Iterable[PatientRecord],
) -> Iterator[tuple[str, str, DocumentArtifact]]:
    """``(patient_id, delivered name, artifact)`` per named artifact. The
    name is the same ``path`` basename ``pipeline._delivered_name`` and the
    archive resolve. Attribution is the RECORD's patient; an artifact with
    no ``path`` is skipped (the caller counts what was left out).
    """
    for record in records:
        for doc in record.documents:
            if doc.path:
                yield record.patient.id, Path(str(doc.path)).name, doc


def _sole(values: set[str | None]) -> str | None:
    """The one non-empty value a group agrees on, ``None`` if it does not."""
    stated = {value for value in values if value}
    return stated.pop() if len(stated) == 1 else None


def _one_patient_only(name: str, owners: list[tuple[str, DocumentArtifact]]) -> str:
    """The single patient this delivered file belongs to, or refuse — two
    records naming the SAME artifact id and bytes pass
    ``pipeline._carry_attachments``'s dedup as one owner, so they can
    still arrive here as one file claimed by two patients.
    """
    patients = {patient_id for patient_id, _doc in owners}
    if len(patients) > 1:
        raise AttachmentNotDeliverable(
            f"{len(patients)} patients' records name one delivered source document "
            f"({safe_log_id(name)}); refusing to file one patient's document into "
            "another patient's chart"
        )
    return patients.pop()


def _document_item(
    path: Path, patient_id: str, owners: list[tuple[str, DocumentArtifact]]
) -> tuple[UploadItem, VerifyPolicy]:
    """One carried file as ``(item, policy)``: digest/size are read off the
    delivered bytes and checked against what the record recorded, refusing
    on a mismatch. ``encounter_id`` is the visit when artifacts agree on
    one, else ``patient_id`` (a scanned chart has no visit).
    """
    sha256, size_bytes = hash_and_size(path)
    recorded = {doc.sha256 for _patient_id, doc in owners if doc.sha256}
    if recorded and recorded != {sha256}:
        raise AttachmentNotDeliverable(
            f"a source document in this bundle ({safe_log_id(path.name)}) no longer hashes to "
            "what the record recorded for it; refusing to deliver bytes nobody read"
        )
    encounter_id = _sole({doc.encounter_id for _patient_id, doc in owners}) or patient_id
    media_type = _sole({doc.mime_type for _patient_id, doc in owners})
    policy = (
        VerifyPolicy.SOURCE_PAGED if media_type in PAGED_MEDIA_TYPES else VerifyPolicy.SOURCE_OPAQUE
    )
    item = UploadItem(
        item_key=f"{encounter_id}:{sha256[:12]}",
        encounter_id=encounter_id,
        patient_id=patient_id,
        file_path=path,
        sha256=sha256,
        size_bytes=size_bytes,
    )
    return item, policy


def build_attachment_manifest(
    records: Iterable[PatientRecord], attachments_dir: Path
) -> SourceDocuments:
    """One delivered FILE is one item (two artifacts naming the same file
    dedup to one); items sort by ``item_key`` for a byte-identical rewrite.
    Never scans the directory — only files the records name are read.
    """
    groups: dict[str, list[tuple[str, DocumentArtifact]]] = {}
    for patient_id, name, doc in _named_documents(records):
        groups.setdefault(name, []).append((patient_id, doc))

    items: list[UploadItem] = []
    policies: dict[str, VerifyPolicy] = {}
    not_carried = 0
    for name in sorted(groups):
        owners = groups[name]
        patient_id = _one_patient_only(name, owners)
        path = attachments_dir / name
        if not path.is_file():
            not_carried += 1
            continue
        item, policy = _document_item(path, patient_id, owners)
        items.append(item)
        policies[item.item_key] = policy
    items.sort(key=lambda item: item.item_key)
    return SourceDocuments(items, policies, not_carried)


def load_skiplist(path: Path) -> frozenset[str]:
    """One ``item_key`` OR ``encounter_id`` per line; blank lines and ``#``
    comments ignored. Raises :class:`FileNotFoundError` on a missing path
    — an explicitly supplied skiplist is operator error, not empty (44).
    """
    entries: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        entries.add(line)
    return frozenset(entries)


def is_skiplisted(item: UploadItem, skiplist: frozenset[str]) -> bool:
    """Whether ``item`` is excluded — matched by ``item_key`` or ``encounter_id``."""
    return item.item_key in skiplist or item.encounter_id in skiplist
