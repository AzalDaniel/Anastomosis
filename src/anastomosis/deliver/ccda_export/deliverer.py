"""C-CDA deliverer — write one CCD XML per record, with its documents beside it.

Mirrors the shape of :mod:`anastomosis.deliver.bundle`: it takes canonical
records and an output directory, hardens that directory via
:func:`anastomosis.core.output.secure_output_dir` (0700 + PHI README), and
writes one file per record. The difference is the artifact: a single
``<patient-id>.xml`` CCD instead of a bundle subdirectory — plus, since #373,
every source document that record carries, written into the same directory and
referenced from the CCD by name, media type and SHA-256.

Filename discipline (STRICTER than the PDF/bundle directories): files are named
by **patient id** and, for a document artifact, by **artifact id** — never by
patient name, and never by the name the source gave the file. PF/Tebra and
bundle outputs name PDFs ``Family_Given_...`` because those land in a
per-patient subtree the operator already controls; C-CDA documents, by
contrast, are the import-into-another-EHR artifact most likely to *travel*
(emailed to a vendor, dropped on a transfer share, imported by a third party).
A name in the filename would put a patient name in the clear at exactly the
moment the file is least under our control — and a C-CDA export names its own
attachments after the patient, so carrying a source filename through would do
it by the back door. Ids here are pseudonymous, so id-only naming keeps the
directory listing PHI-free.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from anastomosis.core.atomic import atomic_copy, atomic_write_bytes
from anastomosis.core.hashutil import hash_and_size
from anastomosis.core.logutil import exc_tag, safe_log_id
from anastomosis.core.model import EXT_INLINE_CONTENT, DocumentArtifact, PatientRecord
from anastomosis.core.output import secure_output_dir
from anastomosis.core.textutil import budgeted_name, media_type_suffix
from anastomosis.deliver._shared import claim_delivered_name

from .builder import DeliveredArtifact, build_ccd, measure_ccd

__all__ = ["ArtifactNotDelivered", "CcdaExportResult", "deliver_ccda"]

logger = logging.getLogger(__name__)


#: Report the preservation share above this fraction. Half is the point where
#: most of what a destination receives is not clinical content, which is the
#: operator's business whether or not their endpoint would accept the file.
_PRESERVATION_SHARE_WARN = 0.5


class ArtifactNotDelivered(Exception):
    """A source document could not be written beside the CCD that names it.

    Not one of the survivable per-record failures below. #373: a C-CDA whose
    whole clinical content was ``nonXMLBody`` artifacts delivered a directory
    with neither the artifacts nor any resolvable reference to them, and the
    run exited 0 — a physician opening that import got a patient with a name
    and nothing on their chart. The rule now is that the deliverable carries
    every artifact or the run stops, so a delivery that cannot conserve one
    fails before anything reports success.

    PHI: raised with counts, ids as run-scoped surrogates and exception TYPE
    names only — never a filename (a source names its attachments after the
    patient) and never a patient value.
    """


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
    #: Source documents written beside the CCDs — scans, faxes, lab reports.
    #: Counted because an operator comparing the delivery against the charts
    #: needs a number, and because zero here beside a chart that HAS artifacts
    #: is what #373 looked like from the outside.
    artifact_count: int = 0

    @property
    def preserved_share(self) -> float:
        return self.preserved_bytes / self.total_bytes if self.total_bytes else 0.0


def deliver_ccda(
    records: list[PatientRecord], out_dir: str | Path, *, artifacts_dir: Path | None = None
) -> CcdaExportResult:
    """Write one CCD per record into ``out_dir``, with its documents, and return
    what landed.

    The directory is created (or hardened) 0700 with a PHI warning README.
    Filenames are ``<patient-id>.xml`` (id only — see the module docstring on
    why this is stricter than the PDF directory). A record that fails to build
    is logged by exception type only (never its values) and skipped, so one bad
    record never sinks a batch.

    ``artifacts_dir`` is where this run already put the source's own document
    files — the pipeline's ``charts/attachments`` directory, or the export
    directory a migration read. An artifact is looked for there under the path
    the source gave it and under that path's filename, because the run flattens
    them into the attachments directory under their basename. An artifact that
    came inline with its record (a C-CDA Unstructured Document's scan lives
    inside the XML) needs no directory at all: its bytes ride on the record.

    Two failures are NOT survivable and stop the run before anything reports
    success (:class:`ArtifactNotDelivered`): an artifact whose file is not
    where the run put it, and one whose delivered bytes do not hash to what the
    record witnesses. Both mean the receiving EHR would get a chart referring
    to a document that is missing or is not the one the source held.

    A name COLLISION is likewise a hard stop: ``write_bytes`` overwrites, so two
    patient ids that sanitize to one filename would deliver one CCD carrying the
    second patient over the first. The per-run claimed-name ledger — shared by
    the documents and the artifacts, since they land in one directory — makes
    that a loud failure instead of a merge.
    """
    out = secure_output_dir(out_dir)
    written: list[Path] = []
    claimed: dict[str, str] = {}
    seen: Counter[str] = Counter()
    missing = 0
    total_bytes = 0
    preserved_bytes = 0
    largest_bytes = 0
    artifacts = 0
    for index, record in enumerate(records):
        seen[record.patient.id] += 1
        name = _document_name(record, index, seen[record.patient.id], out)
        claim_delivered_name(claimed, name, record.patient.id, kind="C-CDA document")
        target = out / name
        # Before the build, and outside the batch-continues handler: the CCD is
        # what NAMES these files, so a build that could not have its artifacts
        # must not be written at all, and this refusal must not be swallowed as
        # one record's bad luck.
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
            # TYPE only (its message may embed PHI) and move on. But "move on"
            # used to end there: the count returned was the count WRITTEN, so a
            # patient with no document at all was indistinguishable from a
            # smaller batch and the operator saw an unqualified green line.
            logger.warning(
                "ccda export failed for patient %s (%s)", safe_log_id(name), exc_tag(exc)
            )
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
    """Say it before the destination says it, when most of the export is not
    clinical content.

    This is a line THIS tool draws about its own output, not a vendor limit:
    what a given EHR will accept is not something this tool can know, and the
    destination registry's no-hallucination rule forbids inventing one. What is
    knowable is the shape of the document, and most of it not being clinical
    content is worth an operator's attention — an importer that renders
    unrecognised sections will show a physician a wall of preserved key/value
    narrative beside their actual chart.
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


def _document_name(record: PatientRecord, index: int, ordinal: int, out: Path) -> str:
    """This record's delivered filename, ``<patient-id>.xml`` where it can be.

    One file per RECORD, not per patient. A C-CDA export gives a patient one
    document per encounter — a referral, a discharge summary, four scans — and
    this adapter reads each as its own record, so writing them all to
    ``<patient-id>.xml`` handed the receiving EHR the LAST one and silently
    dropped the rest, artifacts and all. The second and later records for one
    patient are therefore ``<patient-id>-2.xml``, ``-3.xml``, in the source's
    own stable order, and a patient with a single record keeps exactly the name
    they have always had.

    The ordinal rather than the record's source document id: that id is
    optional and free-form (a record from a hand-made FHIR bundle has none),
    while the position always exists and is as deterministic as the load order
    every other delivered name already depends on.

    Budgeted against ``out``: an over-long path would otherwise raise OSError
    inside the write, and the batch-continues handler would record that record
    as merely "failed" — a silently dropped export.
    """
    tail = ".xml" if ordinal == 1 else f"-{ordinal}.xml"
    return budgeted_name(record.patient.id, f"patient_{index}", parent=out, suffix=tail) + tail


def _deliver_artifacts(
    record: PatientRecord, out: Path, artifacts_dir: Path | None, claimed: dict[str, str]
) -> dict[str, DeliveredArtifact]:
    """Write this record's source documents into ``out``; what the CCD may name.

    An artifact with neither bytes on the record nor a file this run put in
    ``artifacts_dir`` is not delivered and is not an error: a source that
    recorded a document REFERENCE it never resolved (Oracle EHI's blob rows are
    the case) has no bytes for anyone to carry, and its fields narrate in the
    loss ledger exactly as they did before. What is an error is an artifact the
    run DID resolve and this deliverer then could not carry — that one raises.
    """
    landed: dict[str, DeliveredArtifact] = {}
    for doc in record.documents:
        content = _artifact_bytes(doc, artifacts_dir)
        if content is None:
            continue
        landed[doc.id] = _write_artifact(doc, content, out, claimed)
    return landed


def _artifact_bytes(doc: DocumentArtifact, artifacts_dir: Path | None) -> bytes | Path | None:
    """The artifact's bytes, or the file holding them, or ``None`` for neither.

    Two kinds arrive and both leave this directory as one file. A source whose
    export holds the file names it and it is COPIED; a source whose artifact
    came inside the record it was read from carries its own bytes and they are
    WRITTEN. Nothing downstream is told which — the delivered directory holds
    one thing, a document beside the CCD that references it.
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
    """The file in ``artifacts_dir`` this artifact names, or a refusal.

    Two spellings, one rule: the run puts a source's documents either under the
    path the source gave them (a migration reading them out of the export) or
    under that path's filename (the pipeline, which flattens them into
    ``charts/attachments``), so both are looked for here.

    The path is the source adapter's word, and a record can also arrive from a
    FHIR bundle someone else wrote, so each candidate is resolved and checked
    back against the directory: a ``../..`` in a hand-made bundle must not make
    this deliverer read a file from anywhere the process can reach into a
    directory that then gets handed to another EHR.

    PHI: the refusal names no filename. A C-CDA export names its attachments
    after the patient, so the path is a patient-derived string and the message
    says the SHAPE of what is missing instead.
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
    """Land one artifact beside the CCDs and witness what landed.

    Named after the artifact's own id, never after the source's filename: this
    directory travels (see the module docstring), and a source's attachment
    filename is patient-derived. The name is claimed in the same per-run ledger
    the CCDs use, so an artifact and a document that would land on one name is a
    loud collision rather than one written over the other.

    The claim's witness is the digest of the bytes about to be written, not the
    one the record happens to carry: an artifact whose ``sha256`` is unset would
    otherwise be owned by its id alone, and a source that gave two different
    scans one id would pass the ledger and file the second over the first —
    inside one patient, which is the wrong-document shape the ledger exists to
    stop. Measured first, claimed second, written third, then read back and
    compared against what the record witnesses: a truncated copy or a swapped
    source file is a refusal here rather than a scan that opens in the
    destination and is not the one the chart means.
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
    """Claim the slot on the incoming bytes, write them, and return their digest.

    The digest returned is re-read from the file, so it answers what is on disk
    rather than what was handed in — a short write is then caught here and not
    by the destination.
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
