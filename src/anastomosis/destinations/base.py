"""Destination-pack contract: what the browser upload engine drives.

A destination pack files one chart into one foreign EHR via its web UI,
through the :class:`typing.Protocol` interfaces below only — a vendor UI
change is a one-pack event. ``item_key`` is our resumable identity
(``f"{encounter_id}:{sha256[:12]}"``); ``fingerprint`` is
destination-comparable, for the duplicate scan.

PHI: nothing here carries a name, DOB, or address; ``matched_on`` records
field names only, ``UploadReceipt.extras`` destination ids/counts only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
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

    ``item_key`` is the ledger's stable identity
    (``f"{encounter_id}:{sha256[:12]}"``). PHI: ``date_of_service`` is the
    one patient-derived value here — never logged, reaching disk only in
    the upload manifest inside the hardened output directory.
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
    # The encounter's date of service, when the render run knew it. ``None``
    # means it did not — a destination that needs a document date refuses the
    # item rather than filing it under whatever the form defaulted to.
    date_of_service: date | None = None

    def __post_init__(self) -> None:
        if not self.fingerprint:
            # frozen dataclass: assign through object.__setattr__ for the default.
            object.__setattr__(self, "fingerprint", self.file_path.name)


@dataclass(frozen=True)
class DestinationPatient:
    """A patient located in the destination system.

    ``matched_on`` lists field NAMES the resolver matched on (e.g.
    ``("family_name", "birth_date")``), never values — safe to log.
    """

    destination_patient_id: str
    matched_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class UploadReceipt:
    """What the destination handed back after one upload.

    ``extras`` values are destination-generated ids or counts only, never
    patient-derived — receipts are persisted and logged.
    """

    destination_doc_id: str | None = None
    echoed_size_bytes: int | None = None
    extras: Mapping[str, str] = field(default_factory=dict)


class Session(Protocol):
    """The destination's authenticated browser session lifecycle.

    ``is_alive`` lets the engine relaunch a dead session rather than
    failing the run.
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

        ``False`` is a patient-safety event: the engine aborts the whole
        run rather than risk filing into the wrong chart.
        """
        ...


class ExistingDocsScanner(Protocol):
    """List what is already filed in a destination chart (the dupe defense)."""

    def existing_fingerprints(self, patient: DestinationPatient) -> set[str]:
        """Return the fingerprints already present in this patient's chart.

        Compared against :attr:`UploadItem.fingerprint` so a document a
        prior (possibly crashed) run already filed is not filed twice.
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

    ``deliver/verify``'s L5 uses it to cross-check reported size/page count
    against the local PDF; unimplemented, L5 reports ``skip`` explicitly.
    PHI: values are destination-generated facts only (size, page count, an
    internal id), never patient-derived text.
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

    L6 round-trip verification re-hashes them (reprocessed-PDF fallback);
    unimplemented, L6 reports ``skip`` explicitly.
    """

    def read_back(self, patient: DestinationPatient, destination_doc_id: str) -> bytes:
        """Return the uploaded document's bytes as the destination stores them."""
        ...


@runtime_checkable
class Destination(Protocol):
    """A complete destination pack: the engine's whole view of one vendor.

    Aggregates the role protocols into one object; ``name`` is a stable,
    log-safe identifier (e.g. ``"tebra"``).
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
