"""The sequential upload engine: drive items through the state machine.

Walks each item from ``PENDING`` to exactly one terminal state, recording
every move so a killed run resumes exactly where it stopped (51). An
unrecognised exception retries as transient up to ``max_attempts`` (50); a
wrong-patient mismatch aborts the whole run via
:class:`EngineResult.aborted_reason`, never a raise out of :meth:`run` (48).
PHI: item keys, state names, counts and ``exc_tag`` only (2).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from anastomosis.core.conservation import Conservation
from anastomosis.core.hashutil import hash_and_size
from anastomosis.core.logutil import exc_tag, safe_log_id
from anastomosis.core.model import Patient
from anastomosis.destinations.base import Destination, UploadItem

from .errors import PermanentDeliveryError, WrongPatientError
from .manifest import is_skiplisted
from .states import UploadState
from .tracking import TrackingDB
from .verify import NullVerifier, Verifier

__all__ = ["EngineResult", "UploadEngine"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EngineResult:
    """The outcome of one :meth:`UploadEngine.run` call. ``counts`` is the
    ledger's final per-state tally; ``aborted_reason`` is a type name when
    a wrong-patient banner stopped the run early; ``processed`` is how
    many items this call actually drove.
    """

    counts: Mapping[str, int]
    aborted_reason: str | None = field(default=None)
    processed: int = 0


def _ledger_conservation(offered_keys: Sequence[str], known: int) -> Conservation:
    """Every offered item must get a ledger row, or it vanishes from every
    later count; checked right after enqueue so the run stops before
    touching a destination, not after.
    """
    distinct = len(set(offered_keys))
    return Conservation(
        stage="offered -> ledger",
        unit="item",
        offered=distinct,
        dispositions={"in the ledger": known},
    )


# Internal signal from _process_one telling run() to abort for patient
# safety; never escapes run() (reported via EngineResult.aborted_reason).
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
        stop: threading.Event | None = None,
        manage_run: bool = True,
        restrict_to_items: bool = False,
    ) -> EngineResult:
        """Contract: enqueues ``items``, drives each pending one to a
        terminal state, skipping terminal ones (resumable). ``patients``
        maps id to :class:`Patient`, raising :class:`KeyError` on a miss.
        ``stop``/``manage_run``/``restrict_to_items`` are the parallel-runner
        seam, defaulted to single-threaded behavior."""
        for item in items:
            self._tracking.enqueue(item)
        offered_keys = [item.item_key for item in items]
        _ledger_conservation(offered_keys, self._tracking.count_known(offered_keys)).check()

        scope: frozenset[str] | None = (
            frozenset(item.item_key for item in items) if restrict_to_items else None
        )

        aborted_reason: str | None = None
        processed = 0
        for item in self._tracking.pending_items():
            if scope is not None and item.item_key not in scope:
                continue
            # Cooperative cancel: a sibling worker aborted for patient safety.
            # Checked at the item boundary so nothing in flight is abandoned.
            if stop is not None and stop.is_set():
                logger.info("run stopped by external signal after %d item(s)", processed)
                break
            patient = patients[item.patient_id]
            try:
                self._process_one(item, patient, run_id, skiplist)
            except _AbortRun as abort:
                aborted_reason = abort.reason
                if manage_run:
                    self._tracking.finish_run(run_id, aborted_reason=aborted_reason)
                if stop is not None:
                    # Tell sibling workers to stop at their next item boundary.
                    stop.set()
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
        PENDING runs skiplist + preflight first; RETRY_WAIT and
        UPLOAD_INTERRUPTED resume-rejoin at RESOLVING_PATIENT so the
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
                "skipping item %s in unexpected pickup state %s",
                safe_log_id(item.item_key),
                state.name,
            )
            return

        self._drive_active(item, patient, run_id)

    def _drive_active(self, item: UploadItem, patient: Patient, run_id: str) -> None:
        """Loop RESOLVING_PATIENT to a terminal state; a transient failure
        re-enters via RETRY_WAIT->RESOLVING_PATIENT so the duplicate scan
        re-runs on each retry.
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
                logger.error(
                    "wrong-patient banner for item %s (%s)",
                    safe_log_id(item.item_key),
                    exc_tag(exc),
                )
                raise _AbortRun("WrongPatientError") from exc

    def _lifecycle(self, item: UploadItem, patient: Patient, run_id: str) -> bool:
        """One resolve -> dup-scan -> pre-verify -> upload -> post-verify
        pass. Returns ``True`` at a terminal state, ``False`` when routed
        to RETRY_WAIT. :class:`WrongPatientError` propagates to the abort
        handler; any other exception is treated as transient (50).
        """
        try:
            # RESOLVING_PATIENT — already entered by the caller.
            dest_patient = self._dest.resolver.resolve(patient)
            if dest_patient is None:
                self._to(item, UploadState.PATIENT_NOT_FOUND, run_id)
                return True

            # Banner readback first, before the duplicate scan: an
            # unconfirmed chart's fingerprint list is never trusted.
            self._to(item, UploadState.VERIFYING_PRE, run_id)
            if not self._dest.banner.current_patient_matches(patient):
                raise WrongPatientError

            # Duplicate scan, trusted only now that the banner confirmed
            # the open chart's identity (the resume double-file defense).
            if item.fingerprint in self._dest.scanner.existing_fingerprints(dest_patient):
                self._to(item, UploadState.DUPLICATE_AT_DESTINATION, run_id)
                return True

            # dest_patient is already resolved; reusing it avoids a second,
            # CREATE-capable resolve that could POST a duplicate patient.
            self._verifier.verify_pre(item, patient, dest_patient)

            self._to(item, UploadState.UPLOADING, run_id)
            receipt = self._dest.driver.upload(item, dest_patient)

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
            # Unrecognised exceptions are transient (50): failing a flaky
            # upload permanently loses a chart, the worse outcome.
            self._to(item, UploadState.RETRY_WAIT, run_id, error_type=exc_tag(exc))
            logger.warning(
                "transient failure for item %s (%s)", safe_log_id(item.item_key), exc_tag(exc)
            )
            return False

    def _after_retry_wait(self, item: UploadItem, run_id: str) -> bool:
        """In RETRY_WAIT: gives up to FAILED if ``attempts`` (read from the
        ledger) is exhausted, else backs off and retries, returning
        whether it retried.
        """
        attempts = self._attempts(item.item_key)
        if attempts >= self._max_attempts:
            self._to(item, UploadState.FAILED, run_id, error_type="RetriesExhausted")
            logger.warning(
                "item %s failed after %d attempt(s)", safe_log_id(item.item_key), attempts
            )
            return False
        # Exponential backoff on the attempt just recorded (1-based).
        self._sleeper(self._backoff_base_s * 2 ** (attempts - 1))
        self._to(item, UploadState.RESOLVING_PATIENT, run_id)
        return True

    def _fail_permanent(self, item: UploadItem, run_id: str, exc: Exception) -> None:
        """Routes to PRE_VERIFY_FAILED/POST_VERIFY_FAILED/FAILED, read from
        the item's current ledger state so the terminal matches where the
        failure actually struck.
        """
        current = self._tracking.state_of(item.item_key)
        target = {
            UploadState.VERIFYING_PRE: UploadState.PRE_VERIFY_FAILED,
            UploadState.VERIFYING_POST: UploadState.POST_VERIFY_FAILED,
        }.get(current, UploadState.FAILED)
        self._to(item, target, run_id, error_type=exc_tag(exc))
        logger.warning(
            "permanent failure for item %s -> %s (%s)",
            safe_log_id(item.item_key),
            target.name,
            exc_tag(exc),
        )

    # --- helpers ---

    def _preflight_ok(self, item: UploadItem) -> bool:
        """File exists and re-hashes to what the manifest recorded, using
        the same streaming hasher the manifest measured with. Logs the
        item key only, never the path.
        """
        path = item.file_path
        if not path.exists():
            return False
        try:
            digest, size = hash_and_size(path)
        except OSError as exc:
            logger.warning(
                "preflight read failed for item %s (%s)", safe_log_id(item.item_key), exc_tag(exc)
            )
            return False
        return digest == item.sha256 and size == item.size_bytes

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
        logger.debug("item %s -> %s", safe_log_id(item.item_key), new_state.name)
