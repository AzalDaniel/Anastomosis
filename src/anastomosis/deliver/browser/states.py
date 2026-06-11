"""The 15-state upload machine and its legal-transition graph.

One :class:`UploadItem` walks this machine from :data:`UploadState.PENDING`
to exactly one terminal state. The graph below is the single source of
truth for what an item may do next; :func:`validate_transition` is the loud
guard the tracking ledger calls on every write, so an illegal move is a
raised error, never a silently corrupted ledger.

States
------
Non-terminal (work still owed on the item):

* ``PENDING`` — enqueued, not yet started.
* ``RESOLVING_PATIENT`` — locating the patient in the destination system.
* ``VERIFYING_PRE`` — pre-upload readback (banner check, the wrong-patient
  defense) before any bytes are sent.
* ``UPLOADING`` — the file is being pushed into the destination chart.
* ``UPLOAD_INTERRUPTED`` — an upload was cut off (crash/disconnect) and may
  or may not have landed; it must re-enter through the duplicate scan.
* ``RETRY_WAIT`` — a transient failure; the item backs off and retries.
* ``VERIFYING_POST`` — post-upload readback confirming the document filed.

Terminal (no work owed; the item is done one way or another):

* ``SKIPPED_SKIPLIST`` — excluded up front by the operator skiplist.
* ``PREFLIGHT_FAILED`` — failed a pre-run sanity check (missing file, bad
  hash) before any destination contact.
* ``PATIENT_NOT_FOUND`` — the resolver returned no match (never a guess).
* ``DUPLICATE_AT_DESTINATION`` — the document is already filed in the chart
  (the duplicate scan caught it); re-filing would double the chart.
* ``PRE_VERIFY_FAILED`` — pre-upload verification failed permanently.
* ``FAILED`` — a permanent failure (retries exhausted or non-retryable).
* ``POST_VERIFY_FAILED`` — the upload appeared to send but post-verify could
  not confirm the document landed.
* ``COMPLETED`` — uploaded and verified at the destination.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping

from .errors import IllegalTransitionError

__all__ = [
    "CRASH_RECOVERY",
    "LEGAL_TRANSITIONS",
    "TERMINAL_STATES",
    "UploadState",
    "validate_transition",
]


class UploadState(enum.Enum):
    """Every state one upload item can occupy. Values are lowercase snake,
    stored verbatim in the SQLite ledger."""

    # --- non-terminal ---
    PENDING = "pending"
    RESOLVING_PATIENT = "resolving_patient"
    VERIFYING_PRE = "verifying_pre"
    UPLOADING = "uploading"
    UPLOAD_INTERRUPTED = "upload_interrupted"
    RETRY_WAIT = "retry_wait"
    VERIFYING_POST = "verifying_post"
    # --- terminal ---
    SKIPPED_SKIPLIST = "skipped_skiplist"
    PREFLIGHT_FAILED = "preflight_failed"
    PATIENT_NOT_FOUND = "patient_not_found"
    DUPLICATE_AT_DESTINATION = "duplicate_at_destination"
    PRE_VERIFY_FAILED = "pre_verify_failed"
    FAILED = "failed"
    POST_VERIFY_FAILED = "post_verify_failed"
    COMPLETED = "completed"


TERMINAL_STATES: frozenset[UploadState] = frozenset(
    {
        UploadState.SKIPPED_SKIPLIST,
        UploadState.PREFLIGHT_FAILED,
        UploadState.PATIENT_NOT_FOUND,
        UploadState.DUPLICATE_AT_DESTINATION,
        UploadState.PRE_VERIFY_FAILED,
        UploadState.FAILED,
        UploadState.POST_VERIFY_FAILED,
        UploadState.COMPLETED,
    }
)


# The complete legal-transition graph. Every state is a key; terminal states
# map to the empty set (no work owed). UPLOAD_INTERRUPTED is the resume
# re-entry: it goes back through RESOLVING_PATIENT so the duplicate scan can
# catch an upload that landed just before a crash.
LEGAL_TRANSITIONS: Mapping[UploadState, frozenset[UploadState]] = {
    UploadState.PENDING: frozenset(
        {
            UploadState.SKIPPED_SKIPLIST,
            UploadState.PREFLIGHT_FAILED,
            UploadState.RESOLVING_PATIENT,
        }
    ),
    UploadState.RESOLVING_PATIENT: frozenset(
        {
            UploadState.PATIENT_NOT_FOUND,
            UploadState.DUPLICATE_AT_DESTINATION,
            UploadState.VERIFYING_PRE,
            UploadState.RETRY_WAIT,
            UploadState.FAILED,
        }
    ),
    UploadState.VERIFYING_PRE: frozenset(
        {
            UploadState.PRE_VERIFY_FAILED,
            UploadState.UPLOADING,
            UploadState.RETRY_WAIT,
            UploadState.FAILED,
        }
    ),
    UploadState.UPLOADING: frozenset(
        {
            UploadState.VERIFYING_POST,
            UploadState.RETRY_WAIT,
            UploadState.UPLOAD_INTERRUPTED,
            UploadState.FAILED,
        }
    ),
    UploadState.UPLOAD_INTERRUPTED: frozenset({UploadState.RESOLVING_PATIENT}),
    UploadState.RETRY_WAIT: frozenset({UploadState.RESOLVING_PATIENT, UploadState.FAILED}),
    UploadState.VERIFYING_POST: frozenset(
        {
            UploadState.COMPLETED,
            UploadState.POST_VERIFY_FAILED,
            UploadState.RETRY_WAIT,
            UploadState.FAILED,
        }
    ),
    # Terminal states own no further work.
    UploadState.SKIPPED_SKIPLIST: frozenset(),
    UploadState.PREFLIGHT_FAILED: frozenset(),
    UploadState.PATIENT_NOT_FOUND: frozenset(),
    UploadState.DUPLICATE_AT_DESTINATION: frozenset(),
    UploadState.PRE_VERIFY_FAILED: frozenset(),
    UploadState.FAILED: frozenset(),
    UploadState.POST_VERIFY_FAILED: frozenset(),
    UploadState.COMPLETED: frozenset(),
}


# Crash recovery: where an item in a mid-flight state lands when a killed run
# is resumed. RESOLVING_PATIENT and VERIFYING_PRE rewind to PENDING — nothing
# was sent, so starting over is free and correct. UPLOADING and VERIFYING_POST
# recover to UPLOAD_INTERRUPTED, NOT PENDING: the file may already have landed
# at the destination, and re-uploading without first re-running the duplicate
# scan would double-file a patient chart. UPLOAD_INTERRUPTED forces re-entry
# through RESOLVING_PATIENT, where the scan runs — so the bytes are checked
# before any re-send. These recovery edges intentionally do not all appear in
# LEGAL_TRANSITIONS (see TrackingDB.recover): recovery is a privileged path.
CRASH_RECOVERY: Mapping[UploadState, UploadState] = {
    UploadState.RESOLVING_PATIENT: UploadState.PENDING,
    UploadState.VERIFYING_PRE: UploadState.PENDING,
    UploadState.UPLOADING: UploadState.UPLOAD_INTERRUPTED,
    UploadState.VERIFYING_POST: UploadState.UPLOAD_INTERRUPTED,
}


def validate_transition(current: UploadState, new: UploadState) -> None:
    """Raise :class:`IllegalTransitionError` if ``current -> new`` is illegal.

    Loud by design: the tracking ledger calls this before every state write,
    so an illegal move (a skipped step, a move out of a terminal state) is a
    raised error and never a silently corrupted ledger.
    """
    if new not in LEGAL_TRANSITIONS[current]:
        raise IllegalTransitionError(
            f"illegal upload-state transition: {current.name} -> {new.name}"
        )
