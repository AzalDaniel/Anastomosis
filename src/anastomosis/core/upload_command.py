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

Verification is ON by default and fails CLOSED: ``UploadCommand.verify`` (default
``True``) runs the engine behind the L0-L6 :class:`LayeredVerifier` — the
wrong-chart/wrong-patient defense. Filing a chart that does not match its patient
is worse than not filing it, so an operator must EXPLICITLY pass ``--no-verify``
to skip the ladder, and if the verification dependency (PyMuPDF, the ``render``
extra) is missing the run is REFUSED (:class:`VerificationUnavailableError`)
rather than silently filing unverified. The engine's banner wrong-patient abort
runs regardless of this flag — it is the engine's own guard, not a verifier level.
"""

from __future__ import annotations

import threading
from contextlib import ExitStack
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
    "VerificationUnavailableError",
    "resolve_manifest_root",
    "run_upload_command",
]


class VerificationUnavailableError(RuntimeError):
    """``verify=True`` was requested but the verification dependency is missing.

    Fail closed: rather than file charts UNVERIFIED, the run refuses. Install the
    render extra (``pip install 'anastomosis[render]'``), or pass ``--no-verify``
    to explicitly accept an unverified upload (the engine's wrong-patient banner
    check still runs).
    """


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
    # Run the L0-L6 verification ladder (:class:`LayeredVerifier`) around each
    # upload. Default ON: filing a chart into the wrong patient is worse than not
    # filing it, so verification is the safe default and skipping it
    # (``--no-verify``) is an explicit operator choice. When on and the render
    # extra the ladder needs is missing, the run fails closed
    # (:class:`VerificationUnavailableError`) — never files unverified. The
    # engine's banner wrong-patient abort runs regardless of this flag.
    verify: bool = True


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

    # Fail closed BEFORE touching the browser: if verification is on but the
    # dependency that reads the PDFs is missing, refuse rather than file unverified.
    if cmd.verify:
        try:
            import fitz  # noqa: F401 — PyMuPDF; the L0-L6 ladder reads PDFs with it
        except ImportError as exc:
            raise VerificationUnavailableError(
                "upload verification (on by default) needs the render extra: "
                "pip install 'anastomosis[render]' — or pass --no-verify to file "
                "without the L0-L6 ladder (the engine's wrong-patient banner check "
                "still runs)."
            ) from exc

    manifest_root = resolve_manifest_root(cmd.out_dir)
    secure_output_dir(cmd.out_dir)
    with ExitStack() as stack:
        # Lock the output dir AND the manifest root. A `migrate` writes (and
        # locks) the manifest under <out>/charts — a DIFFERENT directory with a
        # different .anast.lock than <out> — so locking only <out> would still
        # race the producer (a time-of-check-to-time-of-use gap). Locking the
        # sorted, de-duplicated set fences both: sorted so two paths never
        # deadlock, de-duplicated so a collapsed (manifest-under-<out>) layout
        # locks once.
        for target in sorted({cmd.out_dir.resolve(), manifest_root.resolve()}):
            stack.enter_context(output_lock(target))
        # Lock-then-read: the authoritative manifest is the one under the lock.
        items, patients = read_upload_manifest(manifest_root)
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
            # Aggregate verification coverage (PHI-safe - counts + dedup'd
            # level-shape reason strings only) so the report tells the truth
            # about which L-levels actually ran for this run, instead of a
            # blanket "full L0-L6 ladder" claim that does not match what ran.
            # ``verifier`` is None when --no-verify was passed.
            coverage = verifier.coverage_summary() if verifier is not None else None
            report_path = write_run_report(
                tracking, run_id, cmd.out_dir, verification_coverage=coverage
            )
            counts = dict(tracking.counts())
        finally:
            # Release the resources WE own and nothing the operator owns:
            #  - our ledger handle, and
            #  - the destination's owned Playwright driver + CDP connection, via
            #    its one-shot release() (NOT close(), which is the manager's
            #    per-recycle hook). release() disconnects the CDP session and
            #    stops the driver; per Playwright that does NOT close a
            #    connect_over_cdp browser, so the operator's EHR browser stays
            #    open. Duck-typed so a destination without owned resources (the
            #    test FakeDestination, the FHIR pusher) is simply skipped.
            tracking.close()
            release = getattr(destination, "release", None)
            if callable(release):
                release()
    return UploadCommandResult(
        counts=counts, aborted_reason=result.aborted_reason, report_path=report_path
    )
