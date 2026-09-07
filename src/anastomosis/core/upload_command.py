"""The shared browser-upload orchestration core: the CLI and the GUI drive
the resumable upload engine identically, reading the manifest inside the
output lock so the items acted on can never be a pre-lock copy a
concurrent render/migrate rewrote. The run binding is checked first
(:func:`check_run_binding`), and a clean landing records ``delivered``/
``verified`` (:func:`record_upload_state`). Verify defaults on and fails
closed (47). PHI: only counts/ids/state-names move through the ledger and
report; the operator's browser is never closed, only our ledger handle.
"""

from __future__ import annotations

import logging
import threading
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from anastomosis.core.logutil import exc_tag

if TYPE_CHECKING:
    from collections.abc import Callable

    from anastomosis.core.runmanifest import RunManifest
    from anastomosis.reconstruct.packs import LoadedPack

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "LEDGER_NAME",
    "UploadCommand",
    "UploadCommandResult",
    "VerificationUnavailableError",
    "check_run_binding",
    "record_upload_state",
    "resolve_manifest_root",
    "resolve_run_manifest_root",
    "run_upload_command",
]


class VerificationUnavailableError(RuntimeError):
    """``verify=True`` but the verification dependency is missing (47):
    fails closed rather than file charts unverified. Install the render
    extra, or pass ``--no-verify`` to explicitly accept the risk (the
    engine's wrong-patient banner check still runs)."""


# The retry budget per item before it is marked FAILED — one default shared
# by both frontends.
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
    """The TERMINAL states that are NOT a clean landing: the machine's
    ``TERMINAL_STATES`` minus the clean set, so a state newly added there
    counts against a clean run by default. Imported lazily so
    ``verify=False`` never pulls in the browser extra."""
    from anastomosis.deliver.browser.states import TERMINAL_STATES

    return frozenset(s.value for s in TERMINAL_STATES) - _CLEAN_UPLOAD_STATES


@dataclass
class UploadCommandResult:
    """What an upload run yields: ledger counts, abort reason, report path,
    and the derived verdict both frontends branch on (:attr:`is_clean`,
    :attr:`exit_code`) so neither classifies it independently."""

    counts: dict[str, int]
    aborted_reason: str | None
    report_path: Path

    @property
    def is_clean(self) -> bool:
        """Contract: true when every item reached a safe terminal end
        (``completed``, ``skipped_skiplist``, ``duplicate_at_destination``)
        and ``aborted_reason`` is unset. Non-terminal leftovers (a
        cooperatively stopped run's ``pending`` items) resume next run and
        do not by themselves make a run non-clean."""
        if self.aborted_reason is not None:
            return False
        return not any(self.counts.get(state, 0) for state in _nonclean_terminal_states())

    @property
    def exit_code(self) -> int:
        """``0`` when :attr:`is_clean`, else ``1`` — what ``anast upload``
        returns so a script can branch on a non-clean run."""
        return 0 if self.is_clean else 1

    def nonclean_summary(self) -> str:
        """A PHI-safe one-line summary of the non-clean terminal counts —
        state names and integers only, alphabetical for a stable string —
        so the GUI's error event says WHICH states blocked a clean landing
        instead of a false "upload complete"."""
        offenders = sorted(
            (state, self.counts[state])
            for state in _nonclean_terminal_states()
            if self.counts.get(state, 0)
        )
        total = sum(n for _state, n in offenders)
        detail = ", ".join(f"{state}={n}" for state, n in offenders)
        return f"{total} item(s) in non-clean terminal states: {detail}"


def resolve_run_manifest_root(out_dir: Path) -> Path:
    """Where the RUN manifest lives for an upload pointed at ``out_dir``.
    An operator may point this at the ``charts`` subfolder, as
    :func:`resolve_manifest_root` allows for the upload manifest; the run
    manifest sits one level up, so without this check_run_binding would
    find nothing and file every chart unchecked."""
    from anastomosis.core.runmanifest import run_manifest_path

    if run_manifest_path(out_dir).is_file():
        return out_dir
    parent = out_dir.parent
    if out_dir.name == "charts" and run_manifest_path(parent).is_file():
        return parent
    return out_dir


def check_run_binding(out_dir: Path) -> RunManifest | None:
    """Contract (53): raises :class:`~anastomosis.core.runmanifest.BindingError`
    naming which profile moved if the folder's binding drifted since it was
    prepared. Returns the manifest when bound and current, ``None`` when
    none exists (logged, not a fault) — unreadable is a different, raised
    case."""
    from anastomosis.core.runmanifest import load_run_manifest, recapture_binding, verify_binding

    manifest = load_run_manifest(resolve_run_manifest_root(out_dir))
    if manifest is None:
        logger.warning(
            "no run manifest in the upload folder: this tree is not bound to a set of "
            "profiles, so nothing checks that the source, destination and layout are the "
            "ones its charts were prepared under"
        )
        return None
    verify_binding(manifest, recapture_binding(manifest))
    return manifest


def record_upload_state(out_dir: Path, result: UploadCommandResult, *, verified: bool) -> None:
    """Advance the bound run's state (53): a clean upload moves it to
    ``delivered``, and to ``verified`` too when the ladder ran; an unbound
    folder records nothing. Best-effort only in this direction — the
    charts are already filed, so a bookkeeping failure here logs rather
    than turning success into a reported failure."""
    from anastomosis.core.runmanifest import (
        BindingError,
        RunManifestError,
        RunState,
        RunStateError,
        advance_state,
        load_run_manifest,
    )

    if not result.is_clean:
        return
    receipt = result.report_path.name
    root = resolve_run_manifest_root(out_dir)
    try:
        # Separate steps, because a folder already at `delivered` — a second
        # upload, or a `--no-verify` run followed by a full one — makes the
        # first call raise, and one shared `try` swallowed that and stranded
        # the run one state short of the truth forever.
        manifest = load_run_manifest(root)
        if manifest is not None and manifest.state is RunState.PREPARED:
            advance_state(root, RunState.DELIVERED, receipt=receipt)
        if verified:
            advance_state(root, RunState.VERIFIED, receipt=receipt)
    except (BindingError, RunManifestError, RunStateError) as exc:
        # Type name only: the message can name an operator-chosen path, and the
        # charts are already filed. Loud in the log, never fatal here.
        logger.warning("could not record the run state after a clean upload (%s)", exc_tag(exc))


def resolve_manifest_root(out_dir: Path) -> Path:
    """Where the upload manifest lives: ``out_dir`` itself (``render``/
    ``pipeline run``), else ``out_dir/charts`` (``migrate``). Both
    frontends resolve it through this one helper."""
    from anastomosis.deliver.browser.persist import MANIFEST_NAME

    return out_dir if (out_dir / MANIFEST_NAME).is_file() else out_dir / "charts"


def _verification_pack(name: str | None) -> LoadedPack | None:
    """The template pack L3 reads ``verify_header_fields`` from, re-discovered
    by the manifest's recorded name (22): stays at ``allow_external=False``
    (no operator consent at upload time), and an edited learned layout
    refuses like anywhere else. Every refusal or missing name logs the
    reason; pack names are identifiers, safe to log."""
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
    """Contract: harden and lock the output dir, read the manifest inside
    the lock, check the run binding (53), ``attach()`` only once the
    bundle passes :func:`~anastomosis.deliver.browser.gates.assert_deliverable`,
    drive the engine, then record the run state (53); ``cmd.verify`` wires
    a lazy :class:`~anastomosis.deliver.verify.LayeredVerifier` (47)."""
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
        # Two questions under the SAME lock, both before the browser is
        # touched, because they are different questions. Is this bundle fit to
        # deliver — did its recorded gates pass, did its reviewed route find a
        # way in, do its charts still hash to what was reviewed? And is it
        # still bound to the source, destination and layout it was prepared
        # under? A manifest too old to carry gates warns instead (see
        # `deliver.browser.gates`); a drifted binding refuses naming which
        # profile moved. Either way, nothing is filed and reconciled after.
        assert_deliverable(manifest)
        check_run_binding(cmd.out_dir)
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
        # `verify_policies` says which items are the SOURCE's own documents
        # rather than charts this toolkit printed: the page-one text levels
        # cannot read a scan and skip, naming that reason in the report. A
        # pre-v4 manifest carries none, and every one of its items is a chart.
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
                verify_policies=manifest.verify_policies,
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
    outcome = UploadCommandResult(
        counts=counts, aborted_reason=result.aborted_reason, report_path=report_path
    )
    # Outside the lock: the artifacts are written and the browser is released, and
    # this only rewrites the run manifest's state fields (the bound hashes are
    # untouched). A clean, verified landing is what moves a run past `prepared`.
    record_upload_state(cmd.out_dir, outcome, verified=cmd.verify)
    return outcome
