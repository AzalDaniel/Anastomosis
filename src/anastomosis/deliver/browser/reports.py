"""Run reports for a browser upload run (M2 item 10): JSON file + console line.

Two outputs, two audiences, one shared rule — no patient-derived value leaves
the ledger through a report:

* :func:`write_run_report` writes a deterministic JSON file inside the
  hardened (``0o700``) output directory. It draws only on the ledger's
  counts-and-types accessors: the run row, per-state counts, an attempts
  histogram, and an error-type histogram from the audit trail. It NEVER copies
  ``file_path`` values out of the ledger — those can embed a patient-derived
  filename, and a report is the kind of artifact that gets emailed or synced.
  Only counts, type names, timestamps, and the destination/run identifiers
  appear.
* :func:`summary_line` builds a single console-safe line of counts only, in a
  fixed state order with zero-count states omitted. No item keys, no paths,
  no patient values — it is printed to a terminal that may be shoulder-surfed
  or logged.

Determinism: the JSON is written with ``sort_keys=True`` and stable ordering
throughout, so a re-write over the same ledger is byte-identical (golden-style
testing relies on it).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from anastomosis.core.output import secure_output_dir

from .states import UploadState
from .tracking import TrackingDB

__all__ = ["summary_line", "write_run_report"]

# Fixed display order for the console summary: non-terminal work states first,
# then terminals ending at COMPLETED. Mirrors the declaration order in
# :class:`UploadState` so the line reads predictably across runs.
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

    States appear in the fixed :data:`_SUMMARY_ORDER`; zero-count (and unknown)
    states are omitted. Counts only — never an item key, a path, or any
    patient-derived value.
    """
    parts = [
        f"{state.value}={counts[state.value]}"
        for state in _SUMMARY_ORDER
        if counts.get(state.value, 0)
    ]
    return " ".join(parts)


def write_run_report(tracking: TrackingDB, run_id: str, out_dir: Path) -> Path:
    """Write ``run-report-{run_id}.json`` into the hardened ``out_dir``.

    The report carries the run row (destination, timestamps, abort reason),
    per-state counts, an attempts histogram, and an error-type histogram from
    the transitions audit. Every value is a count, a type name, a timestamp,
    or a run/destination identifier — :attr:`UploadItem.file_path` values are
    deliberately NOT copied (they can embed a patient-derived filename, and the
    report is a sharable artifact). Written deterministically (``sort_keys``)
    so a re-write over the same ledger is byte-identical.
    """
    out = secure_output_dir(out_dir)
    run = tracking.run_info(run_id)
    report = {
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
    path = out / f"run-report-{run_id}.json"
    path.write_text(
        json.dumps(report, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return path
