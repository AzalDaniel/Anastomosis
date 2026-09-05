"""Browser delivery: the resumable upload pipeline (M2 item 10).

Migration mode's last-resort route: file reconstructed charts into a
destination EHR through its web UI when no vendor API or C-CDA import
exists. A state machine (:mod:`.states`), a SQLite ledger (:mod:`.tracking`)
and an on-disk manifest (:mod:`.persist`) let a killed run resume without
double-filing a chart.

No Playwright import at module load anywhere in this package (75).
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
from .manifest import (
    AttachmentNotDeliverable,
    build_attachment_manifest,
    build_manifest,
    is_skiplisted,
    load_skiplist,
)
from .persist import (
    MANIFEST_NAME,
    MANIFEST_VERSION,
    POLICY_VERSION,
    SUPPORTED_MANIFEST_VERSIONS,
    ManifestError,
    UploadManifest,
    WrittenManifest,
    load_upload_manifest,
    read_upload_manifest,
    write_upload_manifest,
)
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
    "MANIFEST_NAME",
    "MANIFEST_VERSION",
    "POLICY_VERSION",
    "SHARED_MACHINE_WARNING",
    "SUPPORTED_MANIFEST_VERSIONS",
    "TERMINAL_STATES",
    "AttachmentNotDeliverable",
    "CdpEndpoint",
    "DeliveryError",
    "EngineResult",
    "FakeDestination",
    "IllegalTransitionError",
    "ManagedDestination",
    "ManifestError",
    "NullVerifier",
    "PermanentDeliveryError",
    "TrackingDB",
    "TransientDeliveryError",
    "UploadEngine",
    "UploadManifest",
    "UploadState",
    "Verifier",
    "WrittenManifest",
    "WrongPatientError",
    "build_attachment_manifest",
    "build_manifest",
    "connect_over_cdp",
    "is_skiplisted",
    "load_skiplist",
    "load_upload_manifest",
    "read_upload_manifest",
    "summary_line",
    "validate_transition",
    "write_run_report",
    "write_upload_manifest",
]
