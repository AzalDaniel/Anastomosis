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
* :mod:`.manager` — session-lifecycle management (recycling + crash relaunch)
  decorating one destination.
* :mod:`.parallel` — the patient-partitioned parallel runner.
* :mod:`.cdp` — loopback-only CDP attach configuration (no Playwright at
  import time).
* :mod:`.reports` — the run report (deterministic JSON) and the console
  summary line.

No Playwright import lives at module load anywhere in this package: importing
it must work on a machine without the ``deliver-browser`` extra.
"""

from __future__ import annotations

from .cdp import SHARED_MACHINE_WARNING, CdpEndpoint, connect_over_cdp
from .engine import EngineResult, UploadEngine
from .errors import (
    DeliveryError,
    IllegalTransitionError,
    PermanentDeliveryError,
    TransientDeliveryError,
    WrongPatientError,
)
from .fake import FakeDestination
from .manager import ManagedDestination
from .manifest import build_manifest, is_skiplisted, load_skiplist
from .parallel import ParallelResult, run_parallel
from .reports import summary_line, write_run_report
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
    "SHARED_MACHINE_WARNING",
    "TERMINAL_STATES",
    "CdpEndpoint",
    "DeliveryError",
    "EngineResult",
    "FakeDestination",
    "IllegalTransitionError",
    "ManagedDestination",
    "NullVerifier",
    "ParallelResult",
    "PermanentDeliveryError",
    "TrackingDB",
    "TransientDeliveryError",
    "UploadEngine",
    "UploadState",
    "Verifier",
    "WrongPatientError",
    "build_manifest",
    "connect_over_cdp",
    "is_skiplisted",
    "load_skiplist",
    "run_parallel",
    "summary_line",
    "validate_transition",
    "write_run_report",
]
