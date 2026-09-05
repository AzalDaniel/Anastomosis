"""The 15-state upload machine and its legal-transition graph.

One :class:`UploadItem` walks this machine from :data:`UploadState.PENDING`
to exactly one terminal state; :func:`validate_transition` is the loud
guard the tracking ledger calls on every write (51). ``VERIFYING_PRE`` runs
the wrong-patient banner check before the duplicate scan, which is trusted
only once that identity is confirmed (see ``RULES_CANDIDATES.md``).
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


# RESOLVING_PATIENT -> DUPLICATE_AT_DESTINATION stays legal though today the
# engine only reaches DUPLICATE_AT_DESTINATION via VERIFYING_PRE.
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
            UploadState.DUPLICATE_AT_DESTINATION,
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


# UPLOADING/VERIFYING_POST recover to UPLOAD_INTERRUPTED, not PENDING, so a
# possibly-landed file re-enters through the duplicate scan before resend (51).
CRASH_RECOVERY: Mapping[UploadState, UploadState] = {
    UploadState.RESOLVING_PATIENT: UploadState.PENDING,
    UploadState.VERIFYING_PRE: UploadState.PENDING,
    UploadState.UPLOADING: UploadState.UPLOAD_INTERRUPTED,
    UploadState.VERIFYING_POST: UploadState.UPLOAD_INTERRUPTED,
}


def validate_transition(current: UploadState, new: UploadState) -> None:
    """Raise :class:`IllegalTransitionError` if ``current -> new`` is not in
    :data:`LEGAL_TRANSITIONS` — called before every ledger write so a bad
    move never corrupts state silently.
    """
    if new not in LEGAL_TRANSITIONS[current]:
        raise IllegalTransitionError(
            f"illegal upload-state transition: {current.name} -> {new.name}"
        )
