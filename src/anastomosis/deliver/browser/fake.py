"""The reference in-memory destination: test double and future --dry-run seed.

:class:`FakeDestination` implements the whole :class:`Destination` protocol
with no I/O and full determinism, so the engine's failure, retry,
wrong-patient and kill-and-resume paths can be driven end to end. Sharing
``existing`` across two instances simulates a destination's own persistence
across a crash. ``readable=True`` adds the reader protocols so L5/L6 can
run. PHI: only opaque ids, fingerprints and synthetic bytes; never logs (2).
"""

from __future__ import annotations

from collections.abc import Mapping

from anastomosis.core.model import Patient
from anastomosis.destinations.base import (
    DestinationPatient,
    Session,
    UploadItem,
    UploadReceipt,
)

from .errors import PermanentDeliveryError, TransientDeliveryError

__all__ = ["FakeCrash", "FakeDestination"]


class FakeCrash(KeyboardInterrupt):
    """Simulated process death after ``crash_after`` uploads: a
    :class:`BaseException`, not a :class:`DeliveryError`, so the engine's
    exception-retry handler (which catches only ``Exception``) lets it
    through like a real kill, leaving the item mid-``UPLOADING``.
    """


class _FakeSession:
    """A no-op session that is always alive (this double never opens a browser)."""

    def open(self) -> None:
        return None

    def close(self) -> None:
        return None

    def is_alive(self) -> bool:
        return True


class FakeDestination:
    """An in-memory destination implementing the aggregate Destination protocol."""

    def __init__(
        self,
        known_patients: Mapping[str, str],
        *,
        existing: dict[str, set[str]] | None = None,
        transient_failures: Mapping[str, int] | None = None,
        permanent_failures: set[str] | None = None,
        wrong_patient_ids: set[str] | None = None,
        crash_after: int | None = None,
        crash_before: int | None = None,
        echo_wrong_size_keys: set[str] | None = None,
        readable: bool = False,
        page_counts: Mapping[str, int] | None = None,
        corrupt_readback: set[str] | None = None,
    ) -> None:
        # canonical patient_id -> destination_patient_id
        self._known_patients = dict(known_patients)
        # destination_patient_id -> fingerprints filed; held by REFERENCE
        # (not copied) so a resumed destination sees fingerprints filed pre-crash.
        self._existing: dict[str, set[str]] = existing if existing is not None else {}
        # item_key -> remaining transient failures before upload succeeds.
        self._transient_remaining: dict[str, int] = (
            dict(transient_failures) if transient_failures else {}
        )
        self._permanent_failures = set(permanent_failures or set())
        self._wrong_patient_ids = set(wrong_patient_ids or set())
        self._crash_after = crash_after
        # Crashes BEFORE the destination commits: the not-landed kill variant,
        # whose resume must RE-UPLOAD (crash_after's resume must not).
        self._crash_before = crash_before
        self._echo_wrong_size_keys = set(echo_wrong_size_keys or set())
        # readable=True additionally stores upload() bytes so L5/L6 read-back
        # can resolve them; page counts come from the page_counts knob.
        self._readable = readable
        self._page_counts = dict(page_counts) if page_counts else {}
        self._corrupt_readback = set(corrupt_readback or set())
        # destination_doc_id -> (item_key, stored bytes), populated on upload.
        self._stored: dict[str, tuple[str, bytes]] = {}
        self._successful_uploads = 0
        self._session = _FakeSession()
        # (item_key, destination_patient_id) for every successful upload.
        self.uploads: list[tuple[str, str]] = []
        if readable:
            self._enable_reader()

    # --- Destination protocol ---

    @property
    def name(self) -> str:
        return "fake"

    @property
    def session(self) -> Session:
        return self._session

    @property
    def resolver(self) -> FakeDestination:
        return self

    @property
    def banner(self) -> FakeDestination:
        return self

    @property
    def scanner(self) -> FakeDestination:
        return self

    @property
    def driver(self) -> FakeDestination:
        return self

    # --- PatientResolver ---

    def resolve(self, patient: Patient) -> DestinationPatient | None:
        dest_id = self._known_patients.get(patient.id)
        if dest_id is None:
            return None
        return DestinationPatient(destination_patient_id=dest_id, matched_on=("id",))

    # --- BannerCheck ---

    def current_patient_matches(self, expected: Patient) -> bool:
        return expected.id not in self._wrong_patient_ids

    # --- ExistingDocsScanner ---

    def existing_fingerprints(self, patient: DestinationPatient) -> set[str]:
        # Copy so a caller can't mutate the destination's store by accident.
        return set(self._existing.get(patient.destination_patient_id, set()))

    # --- UploadDriver ---

    def upload(self, item: UploadItem, patient: DestinationPatient) -> UploadReceipt:
        if item.item_key in self._permanent_failures:
            raise PermanentDeliveryError
        remaining = self._transient_remaining.get(item.item_key, 0)
        if remaining > 0:
            self._transient_remaining[item.item_key] = remaining - 1
            raise TransientDeliveryError

        if self._crash_before is not None and self._successful_uploads + 1 >= self._crash_before:
            # Nothing recorded yet, so nothing is visible to the scanner:
            # resume must re-upload this document.
            self._crash_before = None
            raise FakeCrash

        # Success: the document is now filed at the destination, so it becomes
        # visible to the scanner (the resume duplicate-defense property).
        self._existing.setdefault(patient.destination_patient_id, set()).add(item.fingerprint)
        self.uploads.append((item.item_key, patient.destination_patient_id))
        self._successful_uploads += 1
        if self._readable:
            # Store the bytes as the destination's copy, keyed by the doc id we
            # are about to hand back, so the verifier's read-back resolves them.
            self._stored[f"doc-{item.item_key}"] = (item.item_key, item.file_path.read_bytes())
        if self._crash_after is not None and self._successful_uploads >= self._crash_after:
            # Landed already: resume must rely on the duplicate scan, not
            # re-upload.
            self._crash_after = None
            raise FakeCrash

        echoed = (
            item.size_bytes + 1 if item.item_key in self._echo_wrong_size_keys else item.size_bytes
        )
        return UploadReceipt(
            destination_doc_id=f"doc-{item.item_key}",
            echoed_size_bytes=echoed,
        )

    # --- MetadataReader / DocumentReader (only when readable=True) ---
    #
    # Bound onto the instance only when readable=True, so isinstance() against
    # the runtime_checkable reader protocols reflects real capability — L5/L6
    # must SKIP a plain fake, not crash on it.

    def _enable_reader(self) -> None:
        self.read_metadata = self._read_metadata
        self.read_back = self._read_back

    def _read_metadata(
        self, patient: DestinationPatient, destination_doc_id: str
    ) -> Mapping[str, str | int]:
        """Reports the stored byte size and, when supplied via
        ``page_counts``, the page count (the fake never parses a PDF).
        """
        _item_key, data = self._stored[destination_doc_id]
        meta: dict[str, str | int] = {"size_bytes": len(data)}
        if destination_doc_id in self._page_counts:
            meta["page_count"] = self._page_counts[destination_doc_id]
        return meta

    def _read_back(self, patient: DestinationPatient, destination_doc_id: str) -> bytes:
        """Return the stored bytes — altered when this item is in ``corrupt_readback``."""
        item_key, data = self._stored[destination_doc_id]
        if item_key in self._corrupt_readback:
            # Mangled bytes: the round-trip hash differs and the bytes fail
            # to parse as a PDF, so L6 fails both tiers.
            return b"corrupted-not-a-pdf"
        return data
