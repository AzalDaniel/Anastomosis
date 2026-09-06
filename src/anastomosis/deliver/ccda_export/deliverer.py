"""C-CDA deliverer — write one CCD XML per record, with its documents beside it.

Mirrors :mod:`anastomosis.deliver.bundle`'s shape; the artifact is a single
``<patient-id>.xml`` plus, since #373, every source document, referenced
from the CCD by name, media type and SHA-256.

Filenames are patient id / artifact id only, never a patient name or the
source's own filename — stricter than PDF/bundle, because a C-CDA export
travels and names its own attachments after the patient.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from anastomosis.core.atomic import atomic_copy, atomic_write_bytes
from anastomosis.core.hashutil import hash_and_size
from anastomosis.core.logutil import exc_tag, safe_log_id
from anastomosis.core.model import EXT_INLINE_CONTENT, DocumentArtifact, PatientRecord
from anastomosis.core.output import secure_output_dir
from anastomosis.core.textutil import budgeted_name, media_type_suffix
from anastomosis.deliver._shared import claim_delivered_name, record_witness

from .builder import DeliveredArtifact, build_ccd, measure_ccd

__all__ = ["ArtifactNotDelivered", "CcdaExportResult", "deliver_ccda"]

logger = logging.getLogger(__name__)


#: Report the preservation share above this fraction. Half is the point where
#: most of what a destination receives is not clinical content, which is the
#: operator's business whether or not their endpoint would accept the file.
_PRESERVATION_SHARE_WARN = 0.5


class ArtifactNotDelivered(Exception):
    """A source document could not be written beside the CCD that names it
    (#373): the deliverable carries every artifact or the run stops before
    anything reports success. PHI: counts, run-scoped surrogate ids and
    exception TYPE names only — never a filename or a patient value.
    """


@dataclass(frozen=True)
class CcdaExportResult:
    """What the C-CDA export wrote, and what it could not: ``missing_count``
    is the point, so ``_print_shortfall`` can report a lost patient rather
    than a merely smaller batch.
    """

    paths: list[Path]
    missing_count: int
    #: Bytes across every document written, and how many are the
    #: preserved-source-fields section — often the majority of the file.
    total_bytes: int = 0
    preserved_bytes: int = 0
    #: The single largest document: a destination's size limit applies per
    #: document, not per batch.
    largest_bytes: int = 0
    #: Source documents written beside the CCDs. Zero here beside a chart
    #: that has artifacts is the #373 shape.
    artifact_count: int = 0

    @property
    def preserved_share(self) -> float:
        return self.preserved_bytes / self.total_bytes if self.total_bytes else 0.0


def deliver_ccda(
    records: list[PatientRecord], out_dir: str | Path, *, artifacts_dir: Path | None = None
) -> CcdaExportResult:
    """Write one CCD per record into ``out_dir`` with its documents; return
    what landed. A missing or hash-mismatched artifact, or a filename
    collision across documents and artifacts, is an unsurvivable
    :class:`ArtifactNotDelivered`.
    """
    out = secure_output_dir(out_dir)
    written: list[Path] = []
    claimed: dict[str, str] = {}
    missing = 0
    total_bytes = 0
    preserved_bytes = 0
    largest_bytes = 0
    artifacts = 0
    for index, record in enumerate(records):
        # Budgeted against ``out``: an over-long path would otherwise raise
        # OSError below and read as a silently dropped export, not a failure.
        pid = budgeted_name(record.patient.id, f"patient_{index}", parent=out, suffix=".xml")
        # Witnessed because a patient id is not guaranteed unique: an adapter
        # yielding one record per source document hands two for one patient.
        claim_delivered_name(
            claimed,
            pid,
            record.patient.id,
            kind="C-CDA document",
            content=record_witness(record),
        )
        target = out / f"{pid}.xml"
        # Outside the batch-continues handler: a build missing its artifacts
        # must not be written, and that refusal must not read as bad luck.
        delivered = _deliver_artifacts(record, out, artifacts_dir, claimed)
        artifacts += len(delivered)
        try:
            xml = build_ccd(record, delivered=delivered)
            atomic_write_bytes(target, xml)
            measured = measure_ccd(xml)
            total_bytes += measured.total_bytes
            preserved_bytes += measured.preserved_bytes
            largest_bytes = max(largest_bytes, measured.total_bytes)
        except Exception as exc:
            # One malformed record must not sink the batch; log the exception
            # TYPE only, since its message may embed PHI.
            logger.warning("ccda export failed for patient %s (%s)", safe_log_id(pid), exc_tag(exc))
            missing += 1
            continue
        written.append(target)
    # PHI: never log the output path — an operator dir named after a patient
    # would enter the logs (SECURITY.md: never a path). Counts only.
    logger.info(
        "ccda export complete: %d of %d record(s), %d attached document(s)",
        len(written),
        len(records),
        artifacts,
    )
    result = CcdaExportResult(
        paths=written,
        missing_count=missing,
        total_bytes=total_bytes,
        preserved_bytes=preserved_bytes,
        largest_bytes=largest_bytes,
        artifact_count=artifacts,
    )
    _warn_on_preservation_share(result)
    return result


def _warn_on_preservation_share(result: CcdaExportResult) -> None:
    """Warn when most of the export is not clinical content — a line this
    tool draws about its own shape, never a vendor size limit (the registry's
    no-hallucination rule forbids inventing one).
    """
    if result.preserved_share < _PRESERVATION_SHARE_WARN:
        return
    logger.warning(
        "%.0f%% of the exported C-CDA is preserved source fields "
        "(%d of %d bytes; largest document %d bytes) — check your "
        "destination's per-document size limit before importing",
        result.preserved_share * 100,
        result.preserved_bytes,
        result.total_bytes,
        result.largest_bytes,
    )


def _deliver_artifacts(
    record: PatientRecord, out: Path, artifacts_dir: Path | None, claimed: dict[str, str]
) -> dict[str, DeliveredArtifact]:
    """Write this record's source documents into ``out``; what the CCD may
    name. An artifact with no bytes anywhere (an unresolved reference, e.g.
    Oracle EHI's blob rows) is not delivered and not an error; one the run
    DID resolve but this deliverer could not carry raises.
    """
    landed: dict[str, DeliveredArtifact] = {}
    for doc in record.documents:
        content = _artifact_bytes(doc, artifacts_dir)
        if content is None:
            continue
        landed[doc.id] = _write_artifact(doc, content, out, claimed)
    return landed


def _artifact_bytes(doc: DocumentArtifact, artifacts_dir: Path | None) -> bytes | Path | None:
    """The artifact's bytes, or the file holding them, or ``None`` for
    neither: a source-held file is copied, an inline one is written, and
    nothing downstream is told which.
    """
    inline = doc.extensions.get(EXT_INLINE_CONTENT)
    if inline is not None:
        return _decoded(str(inline), doc)
    if doc.path is None or artifacts_dir is None:
        return None
    return _artifact_file(doc.path, artifacts_dir)


def _decoded(content: str, doc: DocumentArtifact) -> bytes:
    """An inline artifact's own bytes. Base64 that will not decode is the
    artifact arriving as nothing, which is a refusal rather than an empty file."""
    try:
        return base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ArtifactNotDelivered(
            f"the bytes carried inline with document {safe_log_id(doc.id)} did not decode "
            f"({exc_tag(exc)}); refusing to deliver a C-CDA that names a document this run "
            "cannot write"
        ) from None


def _artifact_file(path: str, artifacts_dir: Path) -> Path:
    """The file in ``artifacts_dir`` this artifact names, or a refusal. Tries
    the source's own path and its flattened basename, each checked back
    against the directory (no escaping via ``../..``). PHI: names no file.
    """
    root = artifacts_dir.resolve()
    for candidate in (root / path, root / Path(path).name):
        resolved = candidate.resolve()
        if resolved.is_relative_to(root) and resolved.is_file():
            return resolved
    raise ArtifactNotDelivered(
        "a document this run resolved is not in the directory the run put it in, so the "
        "C-CDA delivery would name a file that is not there. Refusing rather than handing "
        "a receiving EHR a chart whose documents do not open. The file is not named here "
        "because a source names its attachments after the patient"
    )


def _write_artifact(
    doc: DocumentArtifact, content: bytes | Path, out: Path, claimed: dict[str, str]
) -> DeliveredArtifact:
    """Land one artifact beside the CCDs, named after its own id (never the
    source's filename), claimed in the same ledger. Witnessed by the digest
    about to be written, not the record's own (possibly unset) ``sha256`` —
    so two scans sharing one id collide loudly, never overwrite.
    """
    suffix = media_type_suffix(doc.mime_type)
    stem = budgeted_name(doc.id, "document", parent=out, suffix=suffix)
    name = f"{stem}{suffix}"
    destination = out / name
    try:
        digest = _landed(content, destination, claimed, doc, name)
    except OSError as exc:
        raise ArtifactNotDelivered(
            f"document {safe_log_id(doc.id)} could not be written beside the C-CDA that "
            f"names it ({exc_tag(exc)}); refusing to report a delivery it is missing from"
        ) from None
    if doc.sha256 is not None and digest != doc.sha256:
        raise ArtifactNotDelivered(
            f"document {safe_log_id(doc.id)} arrived in the delivery directory with a "
            "different SHA-256 than the record witnesses; refusing to hand a receiving EHR "
            "a document that is not the one this chart means"
        )
    return DeliveredArtifact(name=name, sha256=digest)


def _landed(
    content: bytes | Path,
    destination: Path,
    claimed: dict[str, str],
    doc: DocumentArtifact,
    name: str,
) -> str:
    """Claim the slot, write the bytes, and return the digest re-read from
    disk (not the one handed in), so a short write is caught here.
    """
    incoming = (
        hash_and_size(content)[0]
        if isinstance(content, Path)
        else hashlib.sha256(content).hexdigest()
    )
    claim_delivered_name(claimed, name, doc.id, kind="C-CDA document artifact", content=incoming)
    if isinstance(content, Path):
        atomic_copy(content, destination)
    else:
        # PHI-BY-DESIGN: the patient's own scanned document IS the product.
        # ``out`` is hardened by ``secure_output_dir`` above (0o700, PHI
        # README). See SECURITY.md, "Code scanning & suppression policy".
        # codeql[py/clear-text-storage-sensitive-data]
        atomic_write_bytes(destination, content)
    return hash_and_size(destination)[0]
