"""Run reports for a browser upload run (M2 item 10): JSON file + console line.

:func:`write_run_report` and :func:`summary_line` both draw only from the
ledger's counts-and-types accessors — no ``file_path``, no item key, no
patient value (3, 49) — and both are deterministic (``sort_keys=True``,
stable ordering), so a re-write over the same ledger is byte-identical.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from anastomosis.core.atomic import atomic_write_text
from anastomosis.core.output import secure_output_dir

# From .verify.types (no project imports, 54), not .verify.composite, which
# would circle back here via browser.errors -> browser/__init__.
from anastomosis.deliver.verify.types import LevelCoverage

from .states import UploadState
from .tracking import TrackingDB

__all__ = ["summary_line", "write_run_report"]

# Fixed display order: non-terminal states first, then terminals ending at
# COMPLETED, mirroring UploadState's declaration order.
_SUMMARY_ORDER: tuple[UploadState, ...] = (
    UploadState.PENDING,
    UploadState.RESOLVING_PATIENT,
    UploadState.VERIFYING_PRE,
    UploadState.UPLOADING,
    UploadState.UPLOAD_INTERRUPTED,
    UploadState.RETRY_WAIT,
    UploadState.VERIFYING_POST,
    UploadState.SKIPPED_SKIPLIST,
    UploadState.PREFLIGHT_FAILED,
    UploadState.PATIENT_NOT_FOUND,
    UploadState.DUPLICATE_AT_DESTINATION,
    UploadState.PRE_VERIFY_FAILED,
    UploadState.FAILED,
    UploadState.POST_VERIFY_FAILED,
    UploadState.COMPLETED,
)


def summary_line(counts: Mapping[str, int]) -> str:
    """One console-safe line of per-state counts (``"completed=5 failed=1"``).

    Fixed :data:`_SUMMARY_ORDER`; zero-count and unknown states omitted;
    counts only, never an item key, path or patient-derived value (3).
    """
    parts = [
        f"{state.value}={counts[state.value]}"
        for state in _SUMMARY_ORDER
        if counts.get(state.value, 0)
    ]
    return " ".join(parts)


def write_run_report(
    tracking: TrackingDB,
    run_id: str,
    out_dir: Path,
    *,
    verification_coverage: Mapping[str, LevelCoverage] | None = None,
) -> Path:
    """Write ``run-report-{run_id}.json``: counts, type names, timestamps
    and ids only, never :attr:`UploadItem.file_path` or a patient value
    (3, 49). ``verification_coverage``, when given, embeds an L0-L6
    :class:`LevelCoverage` table under that key.
    """
    out = secure_output_dir(out_dir)
    run = tracking.run_info(run_id)
    report: dict[str, object] = {
        "run_id": run_id,
        "destination": run["destination"],
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
        "aborted_reason": run["aborted_reason"],
        "counts": dict(tracking.counts()),
        # JSON object keys are strings; stringify the integer attempt counts.
        "attempts_histogram": {str(k): v for k, v in tracking.attempts_histogram().items()},
        "error_type_histogram": dict(tracking.error_type_histogram(run_id)),
    }
    if verification_coverage is not None:
        # Materialize into plain JSON-safe dicts so the caller can supply
        # the TypedDict view without losing determinism here.
        report["verification_coverage"] = {
            level: {
                "pass_count": data["pass_count"],
                "fail_count": data["fail_count"],
                "skip_count": data["skip_count"],
                "skip_reasons": sorted(data["skip_reasons"]),
            }
            for level, data in verification_coverage.items()
        }
    path = out / f"run-report-{run_id}.json"
    atomic_write_text(path, json.dumps(report, sort_keys=True, indent=2) + "\n")
    return path
