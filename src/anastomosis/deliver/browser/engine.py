"""The sequential upload engine: drive items through the state machine.

This is the heart of M2 item 10. Given a destination pack, the resumable
ledger, and a manifest of :class:`UploadItem`, the engine walks each item from
``PENDING`` to exactly one terminal state, recording every move in the ledger
so a killed run resumes exactly where it stopped. The batch scheduler, the
parallel workers, the CDP attach, and the reports land in the next PR; this
engine is the single-threaded driver they will build on.

Two safety properties shape the design:

* **No double-filing on resume.** An interrupted upload (``UPLOAD_INTERRUPTED``)
  and a backed-off retry (``RETRY_WAIT``) both re-enter the lifecycle at
  ``RESOLVING_PATIENT`` — never straight back into ``UPLOADING`` — so the
  duplicate scan runs *before* any re-send and catches a document that landed
  just before the crash.
* **Wrong-patient aborts the whole run.** A banner-readback mismatch is the
  failure this subsystem exists to prevent, so it fails the item to
  ``PRE_VERIFY_FAILED`` and stops the loop immediately, recording an abort
  reason on the run. It is reported through :class:`EngineResult`, not raised
  out of :meth:`UploadEngine.run`, so the caller gets a clean, inspectable
  result rather than a crash.

Retry policy: an unexpected exception is treated as transient (the
conservative choice — retrying a real bug wastes a few attempts, but failing
a flaky upload permanently loses a chart). ``TransientDeliveryError`` and
unknown exceptions route to ``RETRY_WAIT`` and back off; once ``attempts``
(read back from the ledger) reaches ``max_attempts`` the item is ``FAILED``.
``PermanentDeliveryError`` skips retries and goes to the step-appropriate
terminal state.

PHI rule: the engine logs item keys, state names, counts, and ``exc_tag``
type names only — never a patient value, never a receipt extra, never a file
path (paths can embed a patient name and exist only inside the hardened
output directory).
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from anastomosis.core.logutil import exc_tag
from anastomosis.core.model import Patient
from anastomosis.destinations.base import Destination, UploadItem

from .errors import PermanentDeliveryError, WrongPatientError
from .manifest import is_skiplisted
from .states import UploadState
from .tracking import TrackingDB
from .verify import NullVerifier, Verifier

# 1 MiB chunks: matches the manifest hasher so preflight re-hashing reads the
# file the same way it was originally measured.
_HASH_CHUNK_BYTES = 1024 * 1024

__all__ = ["EngineResult", "UploadEngine"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EngineResult:
    """The outcome of one :meth:`UploadEngine.run` call.

    ``counts`` is the ledger's per-state tally at the end of the run (keyed by
    :class:`UploadState` value). ``aborted_reason`` is set to a type name when
    the run stopped early for patient safety (a wrong-patient banner), leaving
    later items unprocessed. ``processed`` is how many items this run actually
    drove (skips already-terminal rows from a resumed run).
    """

    counts: Mapping[str, int]
    aborted_reason: str | None = field(default=None)
    processed: int = 0


# An internal signal raised by _process_one to tell run() to abort the whole
# run for patient safety. It never escapes run(); the wrong-patient condition
# is reported through EngineResult.aborted_reason.
class _AbortRun(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class UploadEngine:
    """Drive a manifest of upload items through the resumable state machine."""

    def __init__(
        self,
        destination: Destination,
        tracking: TrackingDB,
        *,
        verifier: Verifier | None = None,
        max_attempts: int = 3,
        backoff_base_s: float = 2.0,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._dest = destination
        self._tracking = tracking
        self._verifier: Verifier = verifier if verifier is not None else NullVerifier()
        self._max_attempts = max_attempts
        self._backoff_base_s = backoff_base_s
        # Injectable so tests never actually sleep through a backoff.
        self._sleeper = sleeper

    def run(
        self,
        items: Sequence[UploadItem],
        patients: Mapping[str, Patient],
        run_id: str,
        *,
        skiplist: frozenset[str] = frozenset(),
    ) -> EngineResult:
        """Enqueue ``items`` (idempotent) and drive each pending item to a terminal state.

        Items already terminal in the ledger are not re-processed (the
        resumability guarantee). A wrong-patient banner stops the loop
        immediately and sets ``aborted_reason`` on the result and the run.
        ``patients`` maps canonical ``patient_id`` to :class:`Patient`; a
        missing entry raises :class:`KeyError` (a manifest/records mismatch is
        a defect, not a skip).
        """
        for item in items:
            self._tracking.enqueue(item)

        aborted_reason: str | None = None
        processed = 0
        for item in self._tracking.pending_items():
            patient = patients[item.patient_id]
            try:
                self._process_one(item, patient, run_id, skiplist)
            except _AbortRun as abort:
                aborted_reason = abort.reason
                self._tracking.finish_run(run_id, aborted_reason=aborted_reason)
                logger.warning(
                    "run aborted for patient safety after %d item(s): %s",
                    processed,
                    aborted_reason,
                )
                break
            processed += 1

        return EngineResult(
            counts=self._tracking.counts(),
            aborted_reason=aborted_reason,
            processed=processed,
        )

    # --- one item ---

    def _process_one(
        self,
        item: UploadItem,
        patient: Patient,
        run_id: str,
        skiplist: frozenset[str],
    ) -> None:
        """Walk one item to a terminal state, recording every transition.

        Entry states handled: PENDING (skiplist + preflight first), and the
        resume re-entries RETRY_WAIT and UPLOAD_INTERRUPTED, both of which join
        the lifecycle at RESOLVING_PATIENT via their legal edges so the
        duplicate scan runs before any re-send.
        """
        state = self._tracking.state_of(item.item_key)

        if state is UploadState.PENDING:
            if is_skiplisted(item, skiplist):
                self._to(item, UploadState.SKIPPED_SKIPLIST, run_id)
                return
            if not self._preflight_ok(item):
                self._to(
                    item,
                    UploadState.PREFLIGHT_FAILED,
                    run_id,
                    error_type="PreflightError",
                )
                return
            self._to(item, UploadState.RESOLVING_PATIENT, run_id)

        # RETRY_WAIT and UPLOAD_INTERRUPTED rejoin here via their legal edge to
        # RESOLVING_PATIENT — the duplicate scan below is the resume defense.
        elif state in (UploadState.RETRY_WAIT, UploadState.UPLOAD_INTERRUPTED):
            self._to(item, UploadState.RESOLVING_PATIENT, run_id)
        else:  # pragma: no cover - pending_items only yields the three above.
            logger.warning(
                "skipping item %s in unexpected pickup state %s", item.item_key, state.name
            )
            return

        self._drive_active(item, patient, run_id)

    def _drive_active(self, item: UploadItem, patient: Patient, run_id: str) -> None:
        """Drive the active lifecycle from RESOLVING_PATIENT to a terminal state.

        Re-entrant on retry: a transient failure routes to RETRY_WAIT, backs
        off, then loops back here through the legal RETRY_WAIT->RESOLVING_PATIENT
        edge and starts the lifecycle over (so the duplicate scan re-runs).
        """
        while True:
            try:
                if self._lifecycle(item, patient, run_id):
                    return
                # Transient path: lifecycle routed to RETRY_WAIT and wants a
                # retry. Decide retry-vs-give-up, then re-enter at the top.
                if not self._after_retry_wait(item, run_id):
                    return
            except WrongPatientError as exc:
                # Patient-safety: fail the item, then signal run() to abort.
                self._to(
                    item,
                    UploadState.PRE_VERIFY_FAILED,
                    run_id,
                    error_type="WrongPatientError",
                )
                logger.error("wrong-patient banner for item %s (%s)", item.item_key, exc_tag(exc))
                raise _AbortRun("WrongPatientError") from exc

    def _lifecycle(self, item: UploadItem, patient: Patient, run_id: str) -> bool:
        """One pass of resolve → dup-scan → pre-verify → upload → post-verify.

        Returns ``True`` when the item reached a terminal state (done — no
        retry), ``False`` when it routed to RETRY_WAIT (the caller decides
        whether to retry). A :class:`WrongPatientError` propagates out to the
        abort handler; any other unexpected exception is treated as transient.
        """
        try:
            # (b) RESOLVING_PATIENT — already entered by the caller.
            dest_patient = self._dest.resolver.resolve(patient)
            if dest_patient is None:
                self._to(item, UploadState.PATIENT_NOT_FOUND, run_id)
                return True

            # (c) duplicate scan — the resume double-file defense.
            if item.fingerprint in self._dest.scanner.existing_fingerprints(dest_patient):
                self._to(item, UploadState.DUPLICATE_AT_DESTINATION, run_id)
                return True

            # (d) VERIFYING_PRE — banner readback FIRST, then the verifier.
            self._to(item, UploadState.VERIFYING_PRE, run_id)
            if not self._dest.banner.current_patient_matches(patient):
                raise WrongPatientError
            self._verifier.verify_pre(item, patient)

            # (e) UPLOADING.
            self._to(item, UploadState.UPLOADING, run_id)
            receipt = self._dest.driver.upload(item, dest_patient)

            # (f) VERIFYING_POST — size echo check, then the verifier.
            self._to(item, UploadState.VERIFYING_POST, run_id)
            if (
                receipt.echoed_size_bytes is not None
                and receipt.echoed_size_bytes != item.size_bytes
            ):
                self._to(item, UploadState.POST_VERIFY_FAILED, run_id, error_type="SizeMismatch")
                return True
            self._verifier.verify_post(item, receipt)
            self._to(
                item,
                UploadState.COMPLETED,
                run_id,
                destination_doc_id=receipt.destination_doc_id,
            )
            return True
        except WrongPatientError:
            # Patient-safety event — let the abort handler own it.
            raise
        except PermanentDeliveryError as exc:
            self._fail_permanent(item, run_id, exc)
            return True
        except Exception as exc:
            # Unknown failures are treated as transient — failing a flaky
            # upload permanently loses a chart, the worse outcome.
            self._to(item, UploadState.RETRY_WAIT, run_id, error_type=exc_tag(exc))
            logger.warning("transient failure for item %s (%s)", item.item_key, exc_tag(exc))
            return False

    def _after_retry_wait(self, item: UploadItem, run_id: str) -> bool:
        """In RETRY_WAIT: give up to FAILED if exhausted, else back off and retry.

        Returns ``True`` to retry (after sleeping the backoff), ``False`` when
        attempts are exhausted and the item was failed. ``attempts`` is read
        back from the ledger, which bumps it on every RETRY_WAIT write.
        """
        attempts = self._attempts(item.item_key)
        if attempts >= self._max_attempts:
            self._to(item, UploadState.FAILED, run_id, error_type="RetriesExhausted")
            logger.warning("item %s failed after %d attempt(s)", item.item_key, attempts)
            return False
        # Exponential backoff on the attempt just recorded (1-based).
        self._sleeper(self._backoff_base_s * 2 ** (attempts - 1))
        self._to(item, UploadState.RESOLVING_PATIENT, run_id)
        return True

    def _fail_permanent(self, item: UploadItem, run_id: str, exc: Exception) -> None:
        """Route a permanent failure to the terminal state for the current step.

        PRE_VERIFY_FAILED if verify_pre failed, POST_VERIFY_FAILED if
        verify_post failed, otherwise FAILED — read from the item's current
        ledger state so the terminal matches where the failure actually struck.
        """
        current = self._tracking.state_of(item.item_key)
        target = {
            UploadState.VERIFYING_PRE: UploadState.PRE_VERIFY_FAILED,
            UploadState.VERIFYING_POST: UploadState.POST_VERIFY_FAILED,
        }.get(current, UploadState.FAILED)
        self._to(item, target, run_id, error_type=exc_tag(exc))
        logger.warning(
            "permanent failure for item %s -> %s (%s)",
            item.item_key,
            target.name,
            exc_tag(exc),
        )

    # --- helpers ---

    def _preflight_ok(self, item: UploadItem) -> bool:
        """File exists and its content/size still match the manifest.

        Re-hashes the file streamed in chunks; a mismatch means the render was
        corrupted or swapped after the manifest was built — a hard preflight
        fail, never an upload. Logs the item key only, never the path.
        """
        path = item.file_path
        if not path.exists():
            return False
        digest = hashlib.sha256()
        size = 0
        try:
            with path.open("rb") as handle:
                while chunk := handle.read(_HASH_CHUNK_BYTES):
                    digest.update(chunk)
                    size += len(chunk)
        except OSError as exc:
            logger.warning("preflight read failed for item %s (%s)", item.item_key, exc_tag(exc))
            return False
        return digest.hexdigest() == item.sha256 and size == item.size_bytes

    def _attempts(self, item_key: str) -> int:
        return self._tracking.attempts_of(item_key)

    def _to(
        self,
        item: UploadItem,
        new_state: UploadState,
        run_id: str,
        *,
        error_type: str | None = None,
        destination_doc_id: str | None = None,
    ) -> None:
        """Record one transition on the ledger (the only place state is written)."""
        self._tracking.transition(
            item.item_key,
            new_state,
            run_id=run_id,
            error_type=error_type,
            destination_doc_id=destination_doc_id,
        )
        logger.debug("item %s -> %s", item.item_key, new_state.name)
