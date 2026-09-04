"""Build the upload manifest from rendered documents, and the operator skiplist.

The manifest is the bridge from reconstruction (:class:`RenderedDoc`) to the
upload engine (:class:`UploadItem`): for each rendered chart it computes the
content hash and size that anchor the item's stable identity and its
preflight integrity check. Hashing is streamed in fixed chunks so an
arbitrarily large PDF never has to fit in memory.

A chart is not the only thing a bundle delivers. A patient whose record IS a
scan — a C-CDA Unstructured Document — has no encounter to render and every
byte of their chart sits in ``charts/attachments`` as a
:class:`~anastomosis.core.model.DocumentArtifact`.
:func:`build_attachment_manifest` turns those into upload work too, so the
upload route carries the same record the archive and the bundle already do.

Loud by design (the losslessness/loud-failure invariant): a manifest built
over a missing render is a *defect*, not something to skip — so a missing
file raises :class:`FileNotFoundError` rather than dropping the row. A source
document that changed under the bundle, or one file two patients both claim,
raises :class:`AttachmentNotDeliverable` for the same reason. Likewise a
skiplist path that does not exist is operator error and raises; only blank
lines and ``#`` comments inside an existing skiplist are ignored.

PHI rule: nothing here logs or returns a patient-derived value. ``item_key``
embeds an encounter id and a hash prefix; the skiplist is matched on those
opaque keys only; a refusal names counts and
:func:`~anastomosis.core.logutil.safe_log_id` surrogates.
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

#: Media types this toolkit can open and count pages in. Read from what the
#: source DECLARED, never sniffed from the bytes — the same rule the pf_tebra
#: mapper's page counter follows, and the same rule the C-CDA parser follows
#: when it records a ``<nonXMLBody>``'s ``@mediaType`` verbatim. A scan whose
#: document declared nothing is opaque here even if its first bytes say ``%PDF``:
#: telling a receiving system what a clinical artifact is remains a claim only
#: the document holding it may make.
PAGED_MEDIA_TYPES = frozenset({"application/pdf"})


class AttachmentNotDeliverable(Exception):
    """A source document the records name cannot become an upload item.

    Raised for the two shapes that would put the wrong bytes in somebody's
    chart: a file whose digest no longer matches what the record recorded (so
    what would be filed is not what was read), and one delivered file two
    different patients both claim (so filing it for one files it for the other).
    Both are refusals, never warnings — the message carries counts and
    :func:`~anastomosis.core.logutil.safe_log_id` surrogates only.
    """


@dataclass(frozen=True)
class SourceDocuments:
    """The source documents a bundle carries, as upload work.

    :attr:`policies` is keyed by ``item_key`` — the same shape the manifest
    already uses for ``expected_pages`` — because the policy is a fact about
    the item, and the upload ledger the engine re-reads its items from stores
    six columns and would drop a seventh field silently.

    :attr:`not_carried` counts the artifacts a record names that have no file in
    this bundle. They are NOT items, and the count is what says so: an
    attachment-only patient whose scan never landed must not read as a patient
    with nothing to deliver.
    """

    items: list[UploadItem] = field(default_factory=list)
    policies: dict[str, VerifyPolicy] = field(default_factory=dict)
    not_carried: int = 0


def build_manifest(documents: Iterable[RenderedDoc]) -> list[UploadItem]:
    """Turn rendered documents into upload items, one per document.

    For each :class:`RenderedDoc` the file's streaming sha256 and size are
    computed (via the shared :func:`anastomosis.core.hashutil.hash_and_size`, so
    the manifest measures a file exactly the way preflight and L0 re-measure
    it); ``item_key`` is ``f"{encounter_id}:{sha256[:12]}"`` (the resumability
    anchor) and ``fingerprint`` defaults via :class:`UploadItem`. A missing
    render file raises :class:`FileNotFoundError` — a defect the manifest must
    surface, never silently skip.
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
    """``(patient_id, delivered name, artifact)`` for each artifact naming a file.

    The delivered name is the artifact's own ``path`` basename — the same rule
    ``pipeline._delivered_name`` writes the file under and the archive reads it
    back by, so the three cannot disagree about where a document landed.

    Attribution is the RECORD's patient, not the artifact's ``patient_id``: a
    document is named by the record that owns it (the archive's rule, for the
    same reason — a chart's filename carries no owner, a document's record
    does).

    An artifact with no ``path`` names no file and is not yielded: an Oracle EHI
    remote blob that was never fetched, a pf_tebra row whose file is absent from
    the export. Nothing carried them, so nothing can deliver them; the caller
    counts what it left out.
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
    """The single patient this delivered file belongs to, or refuse.

    One file, one chart. Two patients whose records both name ``report.pdf``
    already collide in ``pipeline._carry_attachments``, which claims each
    delivered name against the file's digest — but only while the two artifacts
    differ. Two records handed the SAME artifact id and the SAME bytes pass that
    ledger as one owner re-claiming its own slot, and would then arrive here as
    one file with two patients: uploading it files one patient's document into
    the other's chart. That is the wrong-patient failure the whole subsystem
    exists to prevent, so it is refused rather than attributed to whichever
    record sorted first.
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
    """One carried file as ``(item, policy)``, measured rather than believed.

    The digest and size are read off the delivered file — the bytes an upload
    would actually send — and then checked against what the record recorded, so
    a document that changed under the bundle refuses instead of shipping under a
    hash that describes the old one.

    ``encounter_id`` is the visit the document belongs to when its artifacts
    agree on one, and the patient id when they do not or when there is no visit
    at all — a scanned chart has none. That is the same stand-in the
    whole-patient ccda-standard view uses for its own encounter-less items, and
    it keeps ``item_key`` (``f"{encounter_id}:{sha256[:12]}"``) built by the one
    rule the tracking ledger resumes on.
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
    """Turn the source documents a bundle carries into upload items.

    The defect this closes: an upload manifest built from rendered charts alone
    reported ``0 item(s)`` for a patient whose whole chart is a scan — a C-CDA
    Unstructured Document renders no encounter, so its clinical content sat in
    ``charts/attachments`` while the upload route was told there was nothing to
    deliver, and exited 0 saying so.

    One delivered FILE is one item, not one artifact: two artifacts naming the
    same carried file (a document referenced twice) are one thing on disk and
    must be one thing to upload. Items come back sorted by ``item_key`` so a
    manifest written twice over one bundle is byte-identical.

    Nothing here scans the directory. The records say which files are theirs and
    each is looked up by name, so the directory's own README — and anything else
    an operator dropped in it — is never mistaken for a patient's document.
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
    """Read an operator skiplist: one ``item_key`` OR ``encounter_id`` per line.

    Blank lines and ``#`` comments are ignored; surrounding whitespace is
    stripped. A path that does not exist raises :class:`FileNotFoundError` —
    an explicitly supplied skiplist that is missing is operator error, not an
    empty skiplist.
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
