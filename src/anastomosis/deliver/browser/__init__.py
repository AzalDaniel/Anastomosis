"""Browser delivery: the resumable upload pipeline (M2 item 10).

Migration mode's last-resort route — file reconstructed charts into a
destination EHR through its web UI when no vendor API and no C-CDA import
exist (the common case for the practices this tool serves). The work is
inherently long-running and crash-prone (a browser, a flaky vendor UI, an
hours-long batch), so the design is built around resumability:

* :mod:`.states` — the 15-state upload machine and its legal-transition
  graph; one item walks it to exactly one terminal state.
* :mod:`.errors` — the delivery error taxonomy that drives retry/abort.
* :mod:`.tracking` — a WAL-mode SQLite ledger recording every item's state
  and an append-only audit trail, so a killed run resumes exactly where it
  stopped without double-filing any chart.

The engine, batch scheduler, parallel workers, CDP attach, and the fake
destination test double land in later PRs; this PR ships the contract, the
state machine, and the ledger.
"""

from __future__ import annotations

from .errors import (
    DeliveryError,
    IllegalTransitionError,
    PermanentDeliveryError,
    TransientDeliveryError,
    WrongPatientError,
)
from .states import (
    CRASH_RECOVERY,
    LEGAL_TRANSITIONS,
    TERMINAL_STATES,
    UploadState,
    validate_transition,
)
from .tracking import TrackingDB

__all__ = [
    "CRASH_RECOVERY",
    "LEGAL_TRANSITIONS",
    "TERMINAL_STATES",
    "DeliveryError",
    "IllegalTransitionError",
    "PermanentDeliveryError",
    "TrackingDB",
    "TransientDeliveryError",
    "UploadState",
    "WrongPatientError",
    "validate_transition",
]
