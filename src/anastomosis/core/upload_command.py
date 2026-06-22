"""The shared browser-upload orchestration core (one engine drive, two frontends).

``anast upload`` (CLI) and the GUI upload console must drive the resumable upload
engine IDENTICALLY: the same ledger file, the same retry budget, the same
recover -> run -> finish -> report, and — critically — the manifest is read
INSIDE the output lock (lock-then-read). Reading it under the lock means the
items the engine acts on are the ones present while the lock is held, never a
copy read before a concurrent ``render``/``migrate`` could rewrite the manifest
(the TOCTOU the codex review flagged). This module is that single orchestration;
each frontend keeps only its own pre-flight (loopback gate, operator
confirmation, pack readiness, skiplist source) and its own presentation.

PHI rule: this layer moves counts/ids/state-names through the ledger and report;
it prints/logs nothing patient-derived, and the ledger + report stay inside the
0700 output dir. The operator's browser is NEVER closed — only our own ledger
handle is.

Verification is OPT-IN: ``UploadCommand.verify`` (default ``False``) decides
whether the engine runs behind the L0-L6 :class:`LayeredVerifier` or the
default pass-through :class:`NullVerifier`. With verify off this path never
imports PyMuPDF (the ``render`` extra the verifier needs); the engine's banner
wrong-patient abort runs in BOTH cases — it is the engine's own guard, not a
verifier level.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "LEDGER_NAME",
    "UploadCommand",
    "UploadCommandResult",
    "resolve_manifest_root",
    "run_upload_command",
]

# The retry budget per item before it is marked FAILED — ONE default for BOTH
# frontends (they previously diverged: the CLI used 3, the GUI 4).
DEFAULT_MAX_ATTEMPTS = 3

# The upload ledger filename, inside the 0700 output dir. Both frontends share
# it so a run started by either resumes/monitors from the other.
LEDGER_NAME = "upload_ledger.sqlite"


@dataclass(frozen=True)
class UploadCommand:
    """A fully-specified upload run — the unit both frontends build."""

    out_dir: Path
    skiplist: frozenset[str] = field(default_factory=frozenset)
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    # Opt-in: run the L0-L6 verification ladder (:class:`LayeredVerifier`)
    # around each upload. Default OFF — the engine falls back to the
    # pass-through :class:`NullVerifier`, and PyMuPDF (the ``render`` extra the
    # ladder reads PDFs with) is never imported. The engine's banner
    # wrong-patient abort runs regardless of this flag.
    verify: bool = False


@dataclass
class UploadCommandResult:
    """What an upload run yields the caller: ledger counts, abort reason, report."""

    counts: dict[str, int]
    aborted_reason: str | None
    report_path: Path


def resolve_manifest_root(out_dir: Path) -> Path:
    """Where the upload manifest lives: ``out_dir`` itself, else ``out_dir/charts``.

    A ``render`` / ``pipeline run`` writes the manifest into the output dir; a
    ``migrate`` writes it into ``<out>/charts`` alongside the chart PDFs. Both
    frontends resolve it the same way through this one helper, so a migrate
    output dir uploads from either.
    """
    from anastomosis.deliver.browser.persist import MANIFEST_NAME

    return out_dir if (out_dir / MANIFEST_NAME).is_file() else out_dir / "charts"


def run_upload_command(
    cmd: UploadCommand,
    attach: Callable[[], object],
    *,
    stop: threading.Event | None = None,
) -> UploadCommandResult:
    """Drive the resumable upload engine under the output lock; return the outcome.

    Order (shared by both frontends, so they cannot drift):

    1. harden the output dir to 0700 and take the cross-process output lock — a
       CLI or GUI run already driving this dir is refused (``OutputLockedError``)
       rather than racing it into the one ledger (a patient-safety fence against
       double-filing);
    2. read the manifest INSIDE the lock (lock-then-read — closes the TOCTOU);
    3. ``attach()`` the destination (the single Playwright seam) and wrap it;
    4. ``begin_run`` -> ``recover`` (resume any prior killed run) -> drive ->
       ``finish_run`` (only on a clean finish; an abort stamps its own) ->
       ``write_run_report``.

    ``attach`` is invoked only after the lock is held and the manifest reads
    cleanly, so a locked dir or a missing/malformed manifest never touches the
    browser. ``stop`` is the cooperative cancel flag the engine checks at item
    boundaries (the GUI's ``upload_stop``); the CLI passes ``None``.

    When ``cmd.verify`` is set the engine is wired with a
    :class:`~anastomosis.deliver.verify.LayeredVerifier` (lazily imported so the
    ``render`` extra it needs stays off the default path); otherwise the engine
    falls back to its pass-through :class:`NullVerifier` and behavior is
    unchanged.
    """
    from anastomosis.core.locking import output_lock
    from anastomosis.core.output import secure_output_dir
    from anastomosis.deliver.browser.engine import UploadEngine
    from anastomosis.deliver.browser.manager import ManagedDestination
    from anastomosis.deliver.browser.persist import read_upload_manifest
    from anastomosis.deliver.browser.reports import write_run_report
    from anastomosis.deliver.browser.tracking import TrackingDB
    from anastomosis.destinations.base import Destination

    secure_output_dir(cmd.out_dir)
    with output_lock(cmd.out_dir):
        # Lock-then-read: the authoritative manifest is the one under the lock.
        items, patients = read_upload_manifest(resolve_manifest_root(cmd.out_dir))
        destination = attach()
        assert isinstance(destination, Destination)  # the seam must honor the protocol
        managed = ManagedDestination(destination)
        tracking = TrackingDB(cmd.out_dir / LEDGER_NAME)
        # Opt-in L0-L6 ladder. The LayeredVerifier import (and thus PyMuPDF) is
        # lazy so verify=False never pulls in the render extra. The verifier
        # reads the destination directly for its banner/metadata/round-trip
        # access (L4/L5/L6) — so it takes the UNwrapped Destination, while the
        # engine takes the ManagedDestination. There is no pack/records/
        # expected_pages in the upload path, so L3 SKIPs here (correctly): the
        # active levels are L0/L1/L2/L4 (+ L5/L6 when the destination supports
        # read-back).
        verifier = None
        if cmd.verify:
            from anastomosis.deliver.verify import LayeredVerifier

            verifier = LayeredVerifier(destination=destination)
        try:
            run_id = tracking.begin_run(managed.name)
            # The engine contract: the CALLER recovers any mid-flight items from a
            # prior killed run before driving (a re-start resumes cleanly).
            tracking.recover(run_id)
            result = UploadEngine(
                managed, tracking, verifier=verifier, max_attempts=cmd.max_attempts
            ).run(items, patients, run_id, skiplist=cmd.skiplist, stop=stop)
            # On a clean finish stamp the run done; an abort already stamped its
            # own finish_run inside the engine (manage_run defaults True).
            if result.aborted_reason is None:
                tracking.finish_run(run_id)
            report_path = write_run_report(tracking, run_id, cmd.out_dir)
            counts = dict(tracking.counts())
        finally:
            # Close ONLY our own ledger handle — NEVER the operator's browser.
            tracking.close()
    return UploadCommandResult(
        counts=counts, aborted_reason=result.aborted_reason, report_path=report_path
    )
