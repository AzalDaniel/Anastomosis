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
* :mod:`.manifest` — build the upload manifest from rendered documents and
  parse the operator skiplist.
* :mod:`.verify` — the pre/post verification seam (the L0-L6 ladder lands in
  a later PR).
* :mod:`.engine` — the sequential driver that walks each item through the
  state machine.
* :mod:`.fake` — the reference in-memory destination test double.

The batch scheduler, parallel workers, CDP attach, and reports land in the
next PR.
"""

from __future__ import annotations

from .engine import EngineResult, UploadEngine
from .errors import (
    DeliveryError,
    IllegalTransitionError,
    PermanentDeliveryError,
    TransientDeliveryError,
    WrongPatientError,
)
from .fake import FakeDestination
from .manifest import build_manifest, is_skiplisted, load_skiplist
from .states import (
    CRASH_RECOVERY,
    LEGAL_TRANSITIONS,
    TERMINAL_STATES,
    UploadState,
    validate_transition,
)
from .tracking import TrackingDB
from .verify import NullVerifier, Verifier

__all__ = [
    "CRASH_RECOVERY",
    "LEGAL_TRANSITIONS",
    "TERMINAL_STATES",
    "DeliveryError",
    "EngineResult",
    "FakeDestination",
    "IllegalTransitionError",
    "NullVerifier",
    "PermanentDeliveryError",
    "TrackingDB",
    "TransientDeliveryError",
    "UploadEngine",
    "UploadState",
    "Verifier",
    "WrongPatientError",
    "build_manifest",
    "is_skiplisted",
    "load_skiplist",
    "validate_transition",
]
