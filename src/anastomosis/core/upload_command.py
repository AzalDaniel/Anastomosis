"""The shared browser-upload orchestration core (one engine drive, two frontends).

``anast upload`` (CLI) and the GUI upload console must drive the resumable upload
engine IDENTICALLY: the same ledger file, the same retry budget, the same
recover -> run -> finish -> report, and — critically — the manifest is read
INSIDE the output lock (lock-then-read). Reading it under the lock means the
items the engine acts on are the ones present while the lock is held, never a
copy read before a concurrent ``render``/``migrate`` could rewrite the manifest
(the TOCTOU a read-before-lock would open). This module is that single orchestration;
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

import logging
import threading
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from anastomosis.reconstruct.packs import LoadedPack

logger = logging.getLogger(__name__)

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


# Terminal states that count as a CLEAN landing — an item reached a safe end.
# Anything else terminal (FAILED, PRE/POST_VERIFY_FAILED, PATIENT_NOT_FOUND,
# PREFLIGHT_FAILED) is a non-clean outcome. This is the SINGLE source of truth
# for the verdict both frontends read (:meth:`UploadCommandResult.is_clean` /
# :meth:`UploadCommandResult.exit_code`): it lived in the CLI, but the GUI needs
# the same classification, so keeping it here is what stops the two from drifting
# (the bug where the GUI emitted a `done` "upload complete" for a failed run).
_CLEAN_UPLOAD_STATES: frozenset[str] = frozenset(
    {"completed", "skipped_skiplist", "duplicate_at_destination"}
)


def _nonclean_terminal_states() -> frozenset[str]:
    """The TERMINAL state values that are NOT a clean landing.

    Derived from the upload machine's ``TERMINAL_STATES`` minus the clean set,
    so a terminal state newly added to the machine is classified non-clean by
    default (fail-loud: an unclassified terminal counts AGAINST a clean run,
    never silently for it). Imported lazily to keep this module free of the
    ``deliver.browser`` (browser-extra) dependency at import time — the whole
    module follows that discipline so ``verify=False`` never pulls the extra.
    """
    from anastomosis.deliver.browser.states import TERMINAL_STATES

    return frozenset(s.value for s in TERMINAL_STATES) - _CLEAN_UPLOAD_STATES


@dataclass
class UploadCommandResult:
    """What an upload run yields the caller: ledger counts, abort reason, report.

    Carries the derived verdict both frontends branch on — :attr:`is_clean` (and
    its :attr:`exit_code` projection) — so the CLI's process exit and the GUI's
    done-vs-error terminal event are decided by ONE classifier here, not by two
    that can drift.
    """

    counts: dict[str, int]
    aborted_reason: str | None
    report_path: Path

    @property
    def is_clean(self) -> bool:
        """Whether the run landed cleanly: no abort, and no non-clean TERMINAL item.

        Clean means every item reached a SAFE terminal end — ``completed``
        (filed + verified), ``skipped_skiplist`` (excluded up front), or
        ``duplicate_at_destination`` (already on file). It is NOT clean when
        ``aborted_reason`` is set (the engine's wrong-patient/safety stop) OR any
        item landed in a non-clean terminal state (``failed``,
        ``pre_verify_failed``, ``post_verify_failed``, ``patient_not_found``,
        ``preflight_failed``). Non-terminal leftovers (e.g. a cooperatively
        stopped run's ``pending`` items) are NOT failures — they resume on the
        next run — so they do not by themselves make a run non-clean.
        """
        if self.aborted_reason is not None:
            return False
        return not any(self.counts.get(state, 0) for state in _nonclean_terminal_states())

    @property
    def exit_code(self) -> int:
        """The process exit code for the run: ``0`` when :attr:`is_clean`, else ``1``.

        ``anast upload`` returns this so a script can branch on a non-clean run —
        an abort, or any item left in a non-clean terminal state.
        """
        return 0 if self.is_clean else 1

    def nonclean_summary(self) -> str:
        """A PHI-safe one-line summary of the non-clean TERMINAL counts.

        State NAMES and integer counts only — e.g.
        ``"3 item(s) in non-clean terminal states: failed=1, pre_verify_failed=2"``
        — never an item key, a path, or any patient value. The GUI worker uses it
        as the ``error`` event message when :attr:`is_clean` is False without an
        abort, so the operator sees WHICH states blocked a clean landing instead
        of a false ``done`` ("upload complete"). States are listed alphabetically
        for a stable, testable string.
        """
        offenders = sorted(
            (state, self.counts[state])
            for state in _nonclean_terminal_states()
            if self.counts.get(state, 0)
        )
        total = sum(n for _state, n in offenders)
        detail = ", ".join(f"{state}={n}" for state, n in offenders)
        return f"{total} item(s) in non-clean terminal states: {detail}"


def resolve_manifest_root(out_dir: Path) -> Path:
    """Where the upload manifest lives: ``out_dir`` itself, else ``out_dir/charts``.

    A ``render`` / ``pipeline run`` writes the manifest into the output dir; a
    ``migrate`` writes it into ``<out>/charts`` alongside the chart PDFs. Both
    frontends resolve it the same way through this one helper, so a migrate
    output dir uploads from either.
    """
    from anastomosis.deliver.browser.persist import MANIFEST_NAME

    return out_dir if (out_dir / MANIFEST_NAME).is_file() else out_dir / "charts"


def _verification_pack(name: str | None) -> LoadedPack | None:
    """The template pack L3 reads ``verify_header_fields`` from, by manifest name.

    The manifest records WHICH pack rendered its charts; the pack itself is not
    copied into the output tree, so it is re-discovered here by that name.
    Built-ins and the operator's own learned layouts — an external
    ``--pack-dir`` pack executes its own ``context.py`` and an upload run holds
    no operator consent for that, so discovery stays at its default
    (``allow_external=False``). A learned layout is different in exactly the way
    that matters: consent for its code was recorded as a content hash when the
    operator confirmed it, so the trust store is passed and the pack loads only
    while it still matches. An edited one is refused here like anywhere else and
    L3 skips with the reason logged.

    Never a silent downgrade: a manifest naming no pack (a v1 file, or the
    ccda-standard whole-patient view, which renders through no Jinja pack) and a
    named pack that is not discoverable here BOTH log the reason, so an operator
    reading a report where L3 skipped can see WHY instead of trusting a level
    that checked nothing. Pack names are identifiers, never patient-derived, so
    naming one in a log line is safe.
    """
    if name is None:
        logger.warning(
            "upload manifest names no template pack: L3 (pack header/DOS fields) SKIPS for this run"
        )
        return None
    from anastomosis.reconstruct.packs import discover_packs
    from anastomosis.reconstruct.packtrust import default_pack_trust

    status = discover_packs(trust=default_pack_trust()).get(name)
    if status is None or status.pack is None:
        logger.warning(
            "template pack %r from the upload manifest is not available here (%s): "
            "L3 (pack header/DOS fields) SKIPS for this run",
            name,
            status.diagnosis if status is not None else "not discovered",
        )
        return None
    return status.pack


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

    ``attach`` is invoked only after the lock is held, the manifest reads
    cleanly, AND the bundle passes
    :func:`~anastomosis.deliver.browser.gates.assert_deliverable` — so a locked
    dir, a missing/malformed manifest, or a bundle whose gates did not pass
    never touches the browser. ``stop`` is the cooperative cancel flag the engine checks at item
    boundaries (the GUI's ``upload_stop``); the CLI passes ``None``.

    When ``cmd.verify`` is set the engine is wired with a
    :class:`~anastomosis.deliver.verify.LayeredVerifier` (lazily imported so the
    ``render`` extra it needs stays off the default path); otherwise the engine
    falls back to its pass-through :class:`NullVerifier` and behavior is
    unchanged. The ladder verifies against the manifest's own record of the
    render run — the template pack, the per-item expected page count, and the
    per-item date of service — so an upload checks what the render produced, not
    what the upload machine happens to hold today. A pre-v2 manifest carries
    none of those; it still uploads, with the levels that need them degraded and
    said so out loud.
    """
    from anastomosis.core.locking import output_lock
    from anastomosis.core.output import secure_output_dir
    from anastomosis.deliver.browser.engine import UploadEngine
    from anastomosis.deliver.browser.gates import assert_deliverable
    from anastomosis.deliver.browser.manager import ManagedDestination
    from anastomosis.deliver.browser.persist import load_upload_manifest
    from anastomosis.deliver.browser.reports import write_run_report
    from anastomosis.deliver.browser.tracking import TrackingDB
    from anastomosis.destinations.base import Destination

    # Fail closed BEFORE touching the browser: if verification is on but the
    # dependency that reads the PDFs is missing, refuse rather than file unverified.
    if cmd.verify:
        try:
            import pymupdf  # noqa: F401 — the L0-L6 ladder reads PDFs with it
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
        # The FULL read (not the (items, patients) projection): the ladder below
        # verifies against the rest of it, and a v1 file announces its degraded
        # coverage from inside this read.
        manifest = load_upload_manifest(manifest_root)
        # The gate, before the browser is touched: a bundle whose recorded gates
        # did not pass, whose reviewed route found no way in, or whose charts no
        # longer hash to what was reviewed is refused here rather than filed
        # item by item and reconciled afterwards. A manifest too old to carry
        # gates warns instead — see `deliver.browser.gates`.
        assert_deliverable(manifest)
        destination = attach()
        assert isinstance(destination, Destination)  # the seam must honor the protocol
        # Register each resource with the ExitStack the INSTANT we own it, so a
        # failure while constructing a LATER resource (the TrackingDB below, or
        # the LayeredVerifier) cannot leak the one we already hold. Release the
        # destination's owned Playwright driver + CDP connection via its one-shot
        # release() (NOT close(), the manager's per-recycle hook): release()
        # disconnects the CDP session and stops the driver; per Playwright that
        # does NOT close a connect_over_cdp browser, so the operator's EHR browser
        # stays open. Duck-typed so a destination with no owned resources (the
        # test FakeDestination, the FHIR pusher) has no release() and is skipped.
        release = getattr(destination, "release", None)
        if callable(release):
            stack.callback(release)
        managed = ManagedDestination(destination)
        # Our own ledger handle — registered right after construction so it is
        # closed on every exit path (success, engine failure, or a verifier
        # construction failure below). The stack unwinds LIFO, so registering
        # release() first then tracking.close() second means, on exit,
        # tracking.close() runs, then release(), then the output locks LAST.
        tracking = TrackingDB(cmd.out_dir / LEDGER_NAME)
        stack.callback(tracking.close)
        # Opt-in L0-L6 ladder. The LayeredVerifier import (and thus PyMuPDF) is
        # lazy so verify=False never pulls in the render extra. The verifier
        # reads the destination directly for its banner/metadata/round-trip
        # access (L4/L5/L6) — so it takes the UNwrapped Destination, while the
        # engine takes the ManagedDestination.
        #
        # The whole ladder runs here, against what the render run itself recorded
        # in the manifest: `pack` is the template pack whose declared header
        # fields L3 checks, `records` are the DOS-only encounters L3's `dos`
        # field reads, and `expected_pages` is each PDF's page count as rendered,
        # which turns L1's ">= 1 page" into "exactly N pages". Whatever the
        # manifest could not supply degrades to a SKIP that names its reason in
        # the run report (and, for a pre-v2 manifest or an unavailable pack, a
        # warning in the log) — never to a level that passes without checking.
        #
        # If this constructor raises, the stack already owns both the
        # destination release and the ledger close, so neither leaks.
        verifier = None
        if cmd.verify:
            from anastomosis.deliver.verify import LayeredVerifier

            verifier = LayeredVerifier(
                destination=destination,
                pack=_verification_pack(manifest.pack),
                records=manifest.encounters,
                expected_pages=manifest.expected_pages,
            )
        run_id = tracking.begin_run(managed.name)
        # The engine contract: the CALLER recovers any mid-flight items from a
        # prior killed run before driving (a re-start resumes cleanly).
        tracking.recover(run_id)
        result = UploadEngine(
            managed, tracking, verifier=verifier, max_attempts=cmd.max_attempts
        ).run(manifest.items, manifest.patients, run_id, skiplist=cmd.skiplist, stop=stop)
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
        # The ExitStack now releases (LIFO): our ledger handle, the destination's
        # owned Playwright resources, then the output locks — exactly once each.
    return UploadCommandResult(
        counts=counts, aborted_reason=result.aborted_reason, report_path=report_path
    )
