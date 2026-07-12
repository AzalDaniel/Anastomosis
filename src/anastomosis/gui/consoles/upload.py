# AI-assisted: written with Claude agents under the author's direction and review; see DESIGN.md.
"""The upload console: the browser-delivery operator surface.

Owns the read-only tracking-ledger views (``upload_status``,
``upload_item_keys``, ``upload_manifest_preview``, ``upload_safety_notice``)
and the live drive (``upload_start`` / ``upload_stop``). Every method keeps the
controller's contract verbatim: return a JSON-safe dict, never raise, emit only
PHI-safe events (counts, state names, ids, exception TYPE names).

The live-drive worker attaches the browser behind the module-level
``_attach_destination`` seam in :mod:`anastomosis.gui.controller`; the GUI tests
monkeypatch that seam, so the console resolves it LATE (a lazy import inside the
worker body) instead of binding it at module load.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from anastomosis.core.logutil import exc_tag
from anastomosis.core.upload_command import DEFAULT_MAX_ATTEMPTS
from anastomosis.gui.events import error_event, stage_event
from anastomosis.gui.jobs import GuiJob, GuiJobRunner

if TYPE_CHECKING:
    from anastomosis.deliver.browser.tracking import TrackingDB
    from anastomosis.destinations.loader import LoadedBrowserPack

__all__ = ["UploadConsole"]


class _UploadInputs(NamedTuple):
    """The validated pre-flight inputs a clean upload drive needs.

    The successful return of :func:`_upload_preflight`; distinguished from the
    enumerated failure dict by type (a dict is a failure, this tuple is a go).
    """

    out: Path
    loaded: LoadedBrowserPack
    skiplist: frozenset[str]


def _upload_preflight(
    out_dir: str,
    cdp_url: str,
    pack_name: str,
    pack_dirs: list[str] | None,
    skiplist: list[str] | None,
) -> _UploadInputs | dict[str, object]:
    """The upload drive's never-raise pre-flight: validate, then normalize inputs.

    Runs the SAME cheap, pre-attach gates ``anast upload`` runs, in order: the
    loopback CDP gate, the manifest validation (``<dir>`` then ``<dir>/charts``,
    the migrate layout), the destination-pack load + readiness gate, and the
    operator-skiplist normalization. Returns the validated :class:`_UploadInputs`
    on a clean pre-flight, or the enumerated failure dict (``BadCdpEndpoint`` /
    ``BadManifest`` / ``PackNotReady`` / an :func:`exc_tag` pack-load type name),
    which emits no event and spawns no worker. A surprise (a non-string
    ``out_dir`` / ``pack_dirs``) is left to RAISE so the caller routes it through
    the no-traceback ``_fail`` path — matching the original inline behaviour.
    """
    # The deliver-browser imports are lazy so this module loads without the
    # extra; the pre-flight needs only cdp/persist/loader (no Playwright).
    from anastomosis.core.upload_command import resolve_manifest_root
    from anastomosis.deliver.browser.cdp import CdpEndpoint
    from anastomosis.deliver.browser.persist import ManifestError, read_upload_manifest
    from anastomosis.destinations.loader import BrowserPackError, load_destination_pack

    # 1. Loopback gate — a hard ValueError, never weakened to a warning.
    try:
        CdpEndpoint(cdp_url)
    except ValueError:
        return {"ok": False, "error": "BadCdpEndpoint"}

    # 2. Validate the manifest (cheap, pre-attach), trying <dir> then
    #    <dir>/charts (the migrate layout) — the SAME resolution the CLI
    #    uses, so a migrate output dir works in either frontend. Loud on
    #    missing/malformed. The AUTHORITATIVE read happens inside
    #    run_upload_command under the output lock (lock-then-read), so this
    #    early copy is validation only.
    out = Path(out_dir)
    try:
        read_upload_manifest(resolve_manifest_root(out))
    except (ManifestError, OSError):
        return {"ok": False, "error": "BadManifest"}

    # 3. Load the destination pack and gate on readiness (selectors found).
    try:
        loaded = load_destination_pack(pack_name, [Path(p) for p in pack_dirs or []])
    except BrowserPackError as exc:
        return {"ok": False, "error": exc_tag(exc)}
    if not loaded.ready:
        return {"ok": False, "error": "PackNotReady"}

    # 4. Normalize the operator skiplist (GUI parity for the CLI's
    #    --skiplist): one item_key/encounter_id per entry; blanks and
    #    "#" comments ignored, matching deliver.browser.manifest.load_skiplist.
    skiplist_set = frozenset(
        entry.strip()
        for entry in (skiplist or [])
        if entry.strip() and not entry.strip().startswith("#")
    )
    return _UploadInputs(out=out, loaded=loaded, skiplist=skiplist_set)


class UploadConsole:
    """The browser-delivery console: read-only ledger views + live driving."""

    # The operation family this console owns; stamped on every event so only the
    # upload console page consumes them (the P2-5 per-page flow guard).
    _FLOW = "upload"

    def __init__(self, emit: Callable[[dict[str, object]], None], jobs: GuiJobRunner) -> None:
        self._emit = emit
        self._jobs = jobs
        # The cooperative stop flag for the in-flight upload run, if any. Set by
        # upload_stop() and checked by the engine at item boundaries; None when
        # no upload is in flight. (The busy guard ensures at most one run at a
        # time, so a single flag is sufficient.)
        self._upload_stop: threading.Event | None = None

    def upload_status(self, db_path: str) -> dict[str, object]:
        """The upload console's read-only view of a tracking ledger.

        Opens the WAL SQLite ledger at ``db_path`` read-only (this method never
        writes; live driving is :meth:`upload_start`/:meth:`upload_stop`) and
        returns the
        state-machine counters grouped into pending/active/terminal, the latest
        run's info, and the attempts + error-TYPE histograms (from the same
        :mod:`reports` accessors the run report uses). Every value is a count, a
        state name, a destination/run id, an ISO timestamp, or an exception TYPE
        name — never an item key, a path, or any patient-derived value.

        A missing/garbage ledger file is a clean ``{"ok": False, ...}`` (the DB
        is opened defensively); never a traceback.
        """
        from anastomosis.gui.shared import _group_states

        tracking = None
        try:
            from anastomosis.deliver.browser.tracking import TrackingDB

            path = Path(db_path)
            if not path.is_file():
                return {"ok": False, "error": "FileNotFoundError"}
            tracking = TrackingDB(path)
            counts = tracking.counts()
            run = self._latest_run(tracking)
            return {
                "ok": True,
                "counts": dict(counts),
                "groups": _group_states(counts),
                "total": sum(counts.values()),
                "run": run,
                "attempts_histogram": {str(k): v for k, v in tracking.attempts_histogram().items()},
                "error_type_histogram": (
                    dict(tracking.error_type_histogram(str(run["run_id"])))
                    if run is not None
                    else {}
                ),
            }
        except Exception as exc:
            return self._fail("upload_status", exc)
        finally:
            if tracking is not None:
                tracking.close()

    def upload_item_keys(self, db_path: str, limit: int = 200) -> dict[str, object]:
        """The patient command sheet's payload: pending item KEYS only.

        Returns the opaque ``item_key`` values (``encounter_id:sha256[:12]``) of
        items still owing work, for the Cmd+K palette. These are ids by
        construction — never a patient name, never a file path. This is a
        read-only visibility accessor; a run is driven by
        :meth:`upload_start`/:meth:`upload_stop`. Capped at ``limit`` so a huge
        ledger cannot flood the palette.
        """
        tracking = None
        try:
            from anastomosis.deliver.browser.tracking import TrackingDB

            path = Path(db_path)
            if not path.is_file():
                return {"ok": False, "error": "FileNotFoundError"}
            tracking = TrackingDB(path)
            keys = [item.item_key for item in tracking.pending_items(limit=limit)]
            return {"ok": True, "item_keys": keys, "count": len(keys)}
        except Exception as exc:
            return self._fail("upload_item_keys", exc)
        finally:
            if tracking is not None:
                tracking.close()

    def upload_manifest_preview(self, out_dir: str) -> dict[str, object]:
        """Count the renderable PDFs an upload run would carry, from ``out_dir``.

        A thin, read-only preview over the reconstruction output directory: the
        number of ``*.pdf`` files (the unit of upload work) and their total
        bytes. No manifest is built and no hashing happens — that needs the
        per-encounter ids the upload engine carries, not on-disk files — so this
        is a count-and-size sketch only, by design. Counts and a byte total
        only; never a filename. A missing directory is a clean error.
        """
        try:
            path = Path(out_dir)
            if not path.is_dir():
                return {"ok": False, "error": "NotADirectoryError"}
            pdfs = sorted(path.glob("*.pdf"))
            total_bytes = sum(p.stat().st_size for p in pdfs)
            return {"ok": True, "renderable": len(pdfs), "total_bytes": total_bytes}
        except Exception as exc:
            return self._fail("upload_manifest_preview", exc)

    def upload_safety_notice(self) -> dict[str, object]:
        """The shared-machine warning the console must surface before any attach.

        The SINGLE source of truth for the warning text the JS displays in
        ``#safety-warning`` — the exact :data:`SHARED_MACHINE_WARNING` the CLI
        prints. The console fetches this on load and renders it via
        ``textContent``; there is no second copy of the wording in the assets.
        PHI-free by construction (a fixed security advisory). Never raises.
        """
        from anastomosis.deliver.browser.cdp import SHARED_MACHINE_WARNING

        return {"ok": True, "warning": SHARED_MACHINE_WARNING}

    def upload_start(
        self,
        out_dir: str,
        cdp_url: str,
        pack_name: str,
        pack_dirs: list[str] | None = None,
        skiplist: list[str] | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        verify: bool = True,
    ) -> dict[str, object]:
        """Drive the resumable browser upload engine over a CDP attach (async).

        The GUI equivalent of ``anast upload``: it mirrors that command's proven
        flow EXACTLY by driving the SAME shared core
        (:func:`anastomosis.core.upload_command.run_upload_command`) — validate
        the loopback CDP gate, validate the manifest, gate on pack readiness,
        then (only on a clean pre-flight) acquire the busy guard, spawn a daemon
        worker, and lock -> read-manifest -> attach -> recover -> run -> finish ->
        report. Returns immediately with ``{"ok": True, "started": True}``; the
        result arrives as ``upload`` stage/error events and the live counts come
        from the JS polling :meth:`upload_status` against the ledger.

        ``skiplist`` is an optional list of ``item_key``/``encounter_id`` lines to
        exclude (the GUI parity for the CLI's ``--skiplist``); blanks and ``#``
        comments are ignored. ``max_attempts`` is the per-item retry budget,
        defaulting to the SHARED :data:`~anastomosis.core.upload_command.DEFAULT_MAX_ATTEMPTS`
        both frontends now use (they previously diverged). ``verify`` (default
        ``True``, the GUI parity for the CLI's ``--verify``/``--no-verify``) runs
        the L0-L6 verification ladder around each upload; set it ``False`` to
        skip the ladder. The engine's wrong-patient banner abort runs regardless.

        Safety model (never weakened — the engine enforces it; this only drives):

        * **Loopback only.** The CDP endpoint is validated through
          :class:`CdpEndpoint`; any non-loopback host is a hard
          ``{"ok": False, "error": "BadCdpEndpoint"}`` BEFORE the busy guard is
          taken or any worker spawns — the operator's authenticated EHR session
          is never exposed to the network.
        * **Never closes the browser.** The operator launched and logged into the
          browser by hand; the worker attaches over CDP and, on finish, closes
          ONLY our own ledger handle. ``ManagedDestination.close`` is never
          called — see the comment in the worker's ``finally``.
        * **Cooperative stop.** :meth:`upload_stop` sets a flag the engine checks
          at item boundaries; an in-flight upload is never abandoned mid-item.
        * **Resume.** A re-start naturally resumes a prior run — ``recover``
          rewinds any mid-flight items, and already-terminal items are not
          re-driven (the ledger's resumability guarantee).
        * **PHI-safe.** Events carry stage/state/abort-TYPE names only; the
          manifest, ledger, and run state all live inside the 0700 ``out_dir``.

        Pre-flight failures return a clean enumerated error and DO NOT acquire
        the busy guard or spawn a worker: ``BadCdpEndpoint`` (non-loopback),
        ``BadManifest`` (missing/malformed manifest), ``PackNotReady`` (selectors
        undiscovered — run ``anast destination init``), an :func:`exc_tag` type
        name for a pack load error, or ``Busy`` (a run already in flight).
        """
        # Pre-flight, wrapped so it NEVER raises (the controller contract): a
        # non-string out_dir/pack_dirs or any other surprise becomes the
        # no-traceback error dict, like every sibling method. The enumerated
        # codes return their dict directly (no event); only a surprise takes the
        # _fail path here.
        try:
            preflight = _upload_preflight(out_dir, cdp_url, pack_name, pack_dirs, skiplist)
        except Exception as exc:  # never-raise: a malformed argument, etc.
            return self._fail("upload", exc)
        if isinstance(preflight, dict):
            return preflight  # an enumerated pre-flight failure — no busy guard taken

        # The cooperative cancel flag upload_stop() sets; published to self in
        # on_start (after the busy guard is held) so the stop request reaches
        # the engine's item-boundary check, and cleared in cleanup on every
        # exit path (worker finish OR spawn failure).
        stop = threading.Event()

        def _on_start() -> None:
            self._upload_stop = stop
            self._emit(stage_event(self._FLOW, "upload", "start"))

        def _cleanup() -> None:
            self._upload_stop = None

        # 5. Only now hand off to the job runner (a clean pre-flight never
        # blocks the busy guard). The runner owns acquire-or-Busy, the start
        # event via on_start, the worker's cleanup+release finally, and the
        # spawn-failure path (cleanup + release + upload error event).
        return self._jobs.submit(
            GuiJob(
                name="upload",
                flow=self._FLOW,
                worker=self._upload_worker(
                    preflight, cdp_url, stop, max_attempts=max_attempts, verify=verify
                ),
                on_start=_on_start,
                cleanup=_cleanup,
            )
        )

    def _upload_worker(
        self,
        inputs: _UploadInputs,
        cdp_url: str,
        stop: threading.Event,
        *,
        max_attempts: int,
        verify: bool,
    ) -> Callable[[], None]:
        """Build the daemon worker that drives the shared upload core to a terminal event.

        The closure the job runner spawns once the busy guard is held; it drives
        the SAME shared core (:func:`run_upload_command`) the CLI's ``anast
        upload`` drives, emitting the terminal ``done``/``error`` upload event
        from the returned outcome. Split out of :meth:`upload_start` so the
        entry reads preflight -> closures -> job submit; behaviour is unchanged.
        """
        out = inputs.out
        loaded = inputs.loaded
        skiplist_set = inputs.skiplist

        def _worker() -> None:
            # Drive the SAME shared core the CLI's `anast upload` drives, so the
            # two frontends cannot diverge: it harden-locks the output dir, reads
            # the manifest UNDER the lock (lock-then-read — closes the TOCTOU the
            # pre-flight copy would otherwise leave), attaches the browser behind
            # the monkeypatchable seam, and drives recover -> run -> finish ->
            # report. It closes ONLY our own ledger handle — never the operator's
            # browser. The terminal `done`/`error` event is emitted here from the
            # returned outcome.
            #
            # The `_attach_destination` seam stays module-level in
            # anastomosis.gui.controller (the GUI tests monkeypatch it there); it
            # is resolved LATE, at call time, through this lazy import so the
            # monkeypatch is honored.
            from anastomosis.core.locking import OutputLockedError
            from anastomosis.core.upload_command import UploadCommand, run_upload_command
            from anastomosis.gui import controller as _controller_module

            try:
                result = run_upload_command(
                    UploadCommand(
                        out_dir=out,
                        skiplist=skiplist_set,
                        max_attempts=max_attempts,
                        verify=verify,
                    ),
                    lambda: _controller_module._attach_destination(cdp_url, loaded),
                    stop=stop,
                )
                if result.aborted_reason is not None:
                    # A wrong-patient abort (or other safety stop) surfaces as an
                    # error event carrying the abort TYPE name — never a value.
                    self._emit(error_event(self._FLOW, "upload", result.aborted_reason))
                elif not result.is_clean:
                    # No abort, but items landed in non-clean TERMINAL states
                    # (failed / pre_verify_failed / ...). Emitting `done` here was
                    # the bug — "upload complete" for a run that actually failed.
                    # Surface an error carrying a PHI-safe state-name summary
                    # (state names + counts only) so the operator sees the truth.
                    self._emit(error_event(self._FLOW, "upload", result.nonclean_summary()))
                else:
                    self._emit(stage_event(self._FLOW, "upload", "done"))
            except OutputLockedError:
                # A CLI or GUI run already holds this output dir — refuse cleanly.
                self._emit(error_event(self._FLOW, "upload", "OutputLocked"))
            except Exception as exc:  # never-raise: type name only, no PHI
                self._emit(error_event(self._FLOW, "upload", exc_tag(exc)))

        return _worker

    def upload_stop(self) -> dict[str, object]:
        """Request the in-flight upload stop after the current document.

        Cooperative cancel: sets the flag the engine checks at item boundaries,
        so the current document finishes cleanly and later items stay PENDING (a
        later :meth:`upload_start` resumes them). A mid-item cancel is NOT safe
        and is deliberately not offered — the engine never abandons an in-flight
        upload. Returns ``{"ok": True, "stopping": True}`` when a run was asked
        to stop, or ``{"ok": False, "error": "NoRun"}`` when none is in flight.
        Never raises.
        """
        stop = self._upload_stop
        if stop is not None:
            stop.set()
            return {"ok": True, "stopping": True}
        return {"ok": False, "error": "NoRun"}

    @staticmethod
    def _latest_run(tracking: TrackingDB) -> dict[str, object] | None:
        """The most-recent run row (by started_at), as a JSON-safe dict, or None.

        Reuses :meth:`TrackingDB.latest_run_id` + :meth:`TrackingDB.run_info`
        (the upload console shows one current run). All values are log-safe: a
        run id, a destination name, ISO timestamps, and an abort TYPE name —
        never a patient value.
        """
        run_id = tracking.latest_run_id()
        if run_id is None:
            return None
        return {"run_id": run_id, **tracking.run_info(run_id)}

    def _fail(self, stage: str, exc: BaseException) -> dict[str, object]:
        """Convert a caught exception to the no-traceback error contract."""
        tag = exc_tag(exc)
        self._emit(error_event(self._FLOW, stage, tag))
        return {"ok": False, "error": tag}
