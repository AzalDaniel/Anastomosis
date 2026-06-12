"""The reference in-memory destination: test double and future --dry-run target.

:class:`FakeDestination` implements the whole :class:`Destination` protocol
without performing any I/O, so the upload engine can be driven end to end —
including failure, retry, wrong-patient, and kill-and-resume paths — against a
fully deterministic destination. It is also the seed of the eventual
``--dry-run`` destination: a real run that resolves patients and scans for
duplicates but never actually files anything.

Two behaviors model the properties the engine's safety guarantees rely on:

* **Uploaded fingerprints become visible to the scanner.** After a successful
  upload the item's fingerprint is added to that destination patient's
  existing-docs set, so a *resumed* run's duplicate scan finds a document that
  landed just before a crash — exactly the double-file defense kill-and-resume
  tests exercise. Sharing the ``existing`` mapping across two FakeDestination
  instances simulates the destination's own persistence across the crash.
* **``crash_after`` raises mid-run.** After N successful uploads the driver
  raises :class:`FakeCrash` to stand in for process death, so a test can kill a
  run partway and then resume it on a fresh ledger.

Optional read-back (``readable=True``): the double additionally implements the
:class:`~anastomosis.destinations.base.MetadataReader` and
:class:`~anastomosis.destinations.base.DocumentReader` capability protocols, so
the L0-L6 verifier's post-upload checks (L5 metadata, L6 round-trip) can run
against it. A successful upload stores the item's bytes keyed by the
destination doc id; ``read_back`` returns them, ``read_metadata`` reports their
size and (via the ``page_counts`` knob, since the fake never parses a PDF) page
count. The ``corrupt_readback`` knob makes ``read_back`` return altered bytes
for chosen item keys — the L6 fail path.

PHI rule: this double stores only opaque ids, fingerprints, item keys, and the
synthetic document bytes a test wrote — no patient-derived values — and never
logs.
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
    """Simulated process death raised by the driver after ``crash_after`` uploads.

    Deliberately a :class:`BaseException` (via :class:`KeyboardInterrupt`), NOT
    a :class:`DeliveryError` or other ``Exception``. A real process kill is not
    a catchable delivery failure: the engine's "unknown exception => retry"
    handler only catches :class:`Exception`, so a ``BaseException`` sails
    straight through it and out of the run — exactly as a SIGKILL/Ctrl-C would,
    leaving the in-flight item mid-``UPLOADING`` for :meth:`TrackingDB.recover`
    to rewind. (The plan text wrote ``FakeCrash(RuntimeError)``; a RuntimeError
    would be swallowed by the transient-retry catch and could never reproduce a
    kill, so it is modeled as a BaseException instead — see report.)
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
        # destination_patient_id -> fingerprints already filed. Held by
        # REFERENCE (not copied) so passing the SAME dict to a resumed run's
        # destination makes uploads that landed pre-crash visible to the
        # resumed scanner — the property the kill-and-resume defense relies on.
        self._existing: dict[str, set[str]] = existing if existing is not None else {}
        # item_key -> remaining transient failures before upload succeeds.
        self._transient_remaining: dict[str, int] = (
            dict(transient_failures) if transient_failures else {}
        )
        self._permanent_failures = set(permanent_failures or set())
        self._wrong_patient_ids = set(wrong_patient_ids or set())
        self._crash_after = crash_after
        # Crash on what would have been the Nth successful upload, BEFORE the
        # destination commits anything — the not-landed kill variant, whose
        # resume must RE-UPLOAD (vs. crash_after, whose resume must NOT).
        self._crash_before = crash_before
        self._echo_wrong_size_keys = set(echo_wrong_size_keys or set())
        # When readable, the double also implements MetadataReader +
        # DocumentReader: upload() stores the item's bytes so a verifier's
        # L5/L6 post-checks can read them back. The fake never parses a PDF, so
        # the page count it reports is supplied per destination_doc_id.
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
            # Stand in for process death BEFORE the destination commits the
            # bytes: nothing recorded, nothing visible to the scanner — the
            # resumed run must re-upload this document.
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
            # Stand in for process death AFTER the upload has landed, so the
            # resumed run must rely on the duplicate scan to avoid re-filing.
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
    # The two reader methods are bound onto the instance in __init__ ONLY when
    # readable=True (see _enable_reader), so the optional-capability protocols
    # detect the fake honestly: an isinstance() check against
    # MetadataReader/DocumentReader (both runtime_checkable, hasattr-based) is
    # True for a readable fake and False for a plain one — exactly as a real
    # reader vs. non-reader pack would behave. The verifier's L5/L6 must SKIP a
    # plain fake, not crash on it, so the methods must be genuinely absent.

    def _enable_reader(self) -> None:
        # Bind the public reader names onto the instance, so hasattr (and thus
        # isinstance against the runtime_checkable reader protocols) is True
        # only for a readable fake.
        self.read_metadata = self._read_metadata
        self.read_back = self._read_back

    def _read_metadata(
        self, patient: DestinationPatient, destination_doc_id: str
    ) -> Mapping[str, str | int]:
        """Destination-reported metadata for a stored document.

        Reports the stored byte size and, when the test supplied one via
        ``page_counts`` (keyed by destination_doc_id), the page count — the
        fake never parses a PDF, so an unsupplied count is simply omitted.
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
            # A destination that mangled the document into unreadable garbage:
            # the round-trip hash differs AND the bytes no longer parse as a
            # PDF, so L6 fails (neither byte-identity nor the reprocessed tier).
            return b"corrupted-not-a-pdf"
        return data
