"""Destination-pack contract: what the browser upload engine drives.

A destination pack teaches Anastomosis how to put one reconstructed chart
into one foreign EHR through its web UI (the route taken when no vendor API
and no C-CDA import exist — the common case for the practices this tool
serves). The engine (later PR) never touches a browser directly; it speaks
only to the small set of :class:`typing.Protocol` interfaces below, so a
vendor rotating their UI is a one-pack event and the engine, the tracking
ledger, and the tests are all driven against a fake destination.

Two identity concepts, kept deliberately distinct:

* ``item_key`` — *our* stable identity for a unit of upload work,
  ``f"{encounter_id}:{sha256[:12]}"``. It survives across runs so the
  crash-resumable ledger can find the same row again.
* ``fingerprint`` — a *destination-comparable* identity used to detect a
  document already filed in the foreign chart (the duplicate defense on
  resume). It defaults to the file name; a pack may override it with
  whatever the destination actually exposes (an uploaded filename, a size,
  a hash echoed back).

PHI rule (non-negotiable, enforced here by shape): nothing in these types
carries a patient name, DOB, or address. ``matched_on`` records the field
*names* used to match a patient, never the values. ``UploadReceipt.extras``
carries destination-generated ids and counts only — documented as never
patient-derived, because receipts are logged and persisted.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from anastomosis.core.model import Patient

__all__ = [
    "BannerCheck",
    "Destination",
    "DestinationPatient",
    "DocumentReader",
    "ExistingDocsScanner",
    "MetadataReader",
    "PatientResolver",
    "Session",
    "UploadDriver",
    "UploadItem",
    "UploadReceipt",
]


@dataclass(frozen=True)
class UploadItem:
    """One unit of upload work: a single reconstructed file for one encounter.

    ``item_key`` is the stable identity used by the tracking ledger; build it
    as ``f"{encounter_id}:{sha256[:12]}"`` so the same source file resolves
    to the same row across runs (the resumability anchor).
    """

    item_key: str
    encounter_id: str
    patient_id: str
    file_path: Path
    sha256: str
    size_bytes: int
    # Destination-comparable identity for the duplicate scan. Defaults to the
    # file name; packs override when the destination exposes something better.
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.fingerprint:
            # frozen dataclass: assign through object.__setattr__ for the default.
            object.__setattr__(self, "fingerprint", self.file_path.name)


@dataclass(frozen=True)
class DestinationPatient:
    """A patient located in the destination system.

    ``matched_on`` lists the field *names* that the resolver matched on
    (e.g. ``("family_name", "birth_date")``) — never the values. The names
    are safe to log and let a reviewer judge match strength without exposing
    PHI.
    """

    destination_patient_id: str
    matched_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class UploadReceipt:
    """What the destination handed back after one upload.

    ``extras`` values must be destination-generated ids or counts (a queue
    position, a document-class id) — never patient-derived. Receipts are
    persisted and logged, so an honest receipt cannot leak PHI.
    """

    destination_doc_id: str | None = None
    echoed_size_bytes: int | None = None
    extras: Mapping[str, str] = field(default_factory=dict)


class Session(Protocol):
    """The destination's authenticated browser session lifecycle.

    ``open`` establishes it, ``close`` tears it down, ``is_alive`` reports
    whether it is still usable (the engine relaunches a dead session rather
    than failing the run).
    """

    def open(self) -> None: ...

    def close(self) -> None: ...

    def is_alive(self) -> bool: ...


class PatientResolver(Protocol):
    """Locate the destination's record for one canonical :class:`Patient`."""

    def resolve(self, patient: Patient) -> DestinationPatient | None:
        """Return the matched destination patient, or ``None`` if not found.

        ``None`` means *not found* — never a best guess. Filing a chart
        against a guessed patient is the wrong-patient failure this whole
        subsystem exists to prevent.
        """
        ...


class BannerCheck(Protocol):
    """The wrong-patient defense: read back who the UI is currently showing."""

    def current_patient_matches(self, expected: Patient) -> bool:
        """Return whether the destination's open chart is ``expected``.

        A readback against the on-screen patient banner. ``False`` is a
        patient-safety event — the engine aborts the entire run rather than
        risk filing into the wrong chart.
        """
        ...


class ExistingDocsScanner(Protocol):
    """List what is already filed in a destination chart (the dupe defense)."""

    def existing_fingerprints(self, patient: DestinationPatient) -> set[str]:
        """Return the fingerprints already present in this patient's chart.

        Compared against :attr:`UploadItem.fingerprint` to skip a document
        that a previous (possibly crashed) run already uploaded — re-filing
        would double a patient's chart.
        """
        ...


class UploadDriver(Protocol):
    """Perform one upload into one resolved destination patient."""

    def upload(self, item: UploadItem, patient: DestinationPatient) -> UploadReceipt:
        """Upload ``item`` into ``patient`` and return the destination receipt."""
        ...


@runtime_checkable
class MetadataReader(Protocol):
    """Optional capability: read a destination's own metadata for a filed doc.

    A destination MAY implement this in addition to the core protocols. The
    L5 verification layer (``deliver/verify``) uses it to cross-check the
    destination's reported size and page count against the local PDF. When a
    destination does NOT implement it, L5 is reported ``skip`` with an explicit
    detail — never silently passed.

    PHI rule: the returned values are destination-*generated* facts (byte
    size, page count, an internal title id) — never patient-derived free
    text. They are persisted and logged, so an honest reader cannot leak PHI.
    """

    def read_metadata(
        self, patient: DestinationPatient, destination_doc_id: str
    ) -> Mapping[str, str | int]:
        """Return the destination's metadata for the uploaded document.

        Keys are reader-defined; the verifier reads the optional ``size_bytes``
        and ``page_count`` keys when present and ignores the rest.
        """
        ...


@runtime_checkable
class DocumentReader(Protocol):
    """Optional capability: read an uploaded document's bytes back.

    A destination MAY implement this. The L6 round-trip verification reads the
    stored bytes back and re-hashes them (with a reprocessed-PDF fallback).
    When a destination does NOT implement it, L6 is reported ``skip`` with an
    explicit detail — never silently passed.
    """

    def read_back(self, patient: DestinationPatient, destination_doc_id: str) -> bytes:
        """Return the uploaded document's bytes as the destination stores them."""
        ...


@runtime_checkable
class Destination(Protocol):
    """A complete destination pack: the engine's whole view of one vendor.

    Aggregates the role protocols so the engine holds a single object. Each
    property returns a long-lived collaborator; ``name`` is a stable,
    log-safe identifier for the destination (e.g. ``"tebra"``).
    """

    @property
    def name(self) -> str: ...

    @property
    def session(self) -> Session: ...

    @property
    def resolver(self) -> PatientResolver: ...

    @property
    def banner(self) -> BannerCheck: ...

    @property
    def scanner(self) -> ExistingDocsScanner: ...

    @property
    def driver(self) -> UploadDriver: ...
