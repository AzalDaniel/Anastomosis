"""The upload console: the browser-delivery operator surface.

Owns the read-only ledger views and the live drive
(``upload_start``/``upload_stop``); every method returns a JSON-safe dict,
never raises, emits only PHI-safe events.

The live-drive worker resolves ``_attach_destination``
(:mod:`anastomosis.gui.controller`) LATE via a lazy import, so tests can
monkeypatch the module-level seam.
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
from anastomosis.gui.shared import fail_result

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

    Runs ``anast upload``'s SAME pre-attach gates: loopback CDP, manifest,
    pack readiness, skiplist. A surprise input is left to RAISE.
    """
    # The deliver-browser imports are lazy so this module loads without the
    # extra; the pre-flight needs only cdp/persist/loader (no Playwright).
    from anastomosis.core.output import typed_path
    from anastomosis.core.upload_command import resolve_manifest_root
    from anastomosis.deliver.browser.cdp import CdpEndpoint
    from anastomosis.deliver.browser.persist import ManifestError, read_upload_manifest
    from anastomosis.destinations.loader import BrowserPackError, load_destination_pack

    # 1. Loopback gate — a hard ValueError, never weakened to a warning.
    try:
        CdpEndpoint(cdp_url)
    except ValueError:
        return {"ok": False, "error": "BadCdpEndpoint"}

    # 2. Validate the manifest (<dir> then <dir>/charts, matching the CLI);
    #    loud on missing/malformed. The AUTHORITATIVE read is later, under the output lock.
    out = typed_path(out_dir)
    try:
        read_upload_manifest(resolve_manifest_root(out))
    except (ManifestError, OSError):
        return {"ok": False, "error": "BadManifest"}

    # 3. Load the destination pack and gate on readiness (selectors found).
    try:
        loaded = load_destination_pack(pack_name, [typed_path(p) for p in pack_dirs or []])
    except BrowserPackError as exc:
        return {"ok": False, "error": exc_tag(exc)}
    if not loaded.ready:
        return {"ok": False, "error": "PackNotReady"}

    # 4. Normalize the skiplist (GUI parity for --skiplist): one key per
    #    entry, blanks/"#" comments ignored, matching deliver.browser.manifest.load_skiplist.
    skiplist_set = frozenset(
        entry.strip()
        for entry in (skiplist or [])
        if entry.strip() and not entry.strip().startswith("#")
    )
    return _UploadInputs(out=out, loaded=loaded, skiplist=skiplist_set)


class UploadConsole:
    """The browser-delivery console: read-only ledger views + live driving."""

    # The operation family this console owns; stamped on every event so only the
    # upload console page consumes them (the per-page flow guard).
    _FLOW = "upload"

    def __init__(self, emit: Callable[[dict[str, object]], None], jobs: GuiJobRunner) -> None:
        self._emit = emit
        self._jobs = jobs
        # Cooperative stop flag for the in-flight run; set by upload_stop(),
        # checked at item boundaries. None when idle (the busy guard caps this at one run).
        self._upload_stop: threading.Event | None = None

    def upload_status(self, db_path: str) -> dict[str, object]:
        """The upload console's read-only view of a tracking ledger.

        Opens the WAL SQLite ledger read-only; returns state counters, the
        latest run's info, and attempts/error-TYPE histograms. A missing ledger fails clean.
        """
        from anastomosis.gui.shared import _group_states

        tracking = None
        try:
            from anastomosis.core.output import typed_path
            from anastomosis.deliver.browser.tracking import TrackingDB

            path = typed_path(db_path)
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
        """Pending item KEYS for the Uploads search, and how many there are.

        Opaque ``item_key`` values (``encounter_id:sha256[:12]``) — ids by
        construction. ``limit`` caps the list; ``total`` is the ledger's real count.
        """
        tracking = None
        try:
            from anastomosis.core.output import typed_path
            from anastomosis.deliver.browser.tracking import TrackingDB

            path = typed_path(db_path)
            if not path.is_file():
                return {"ok": False, "error": "FileNotFoundError"}
            tracking = TrackingDB(path)
            keys = [item.item_key for item in tracking.pending_items(limit=limit)]
            return {
                "ok": True,
                "item_keys": keys,
                "count": len(keys),
                "total": tracking.pending_count(),
            }
        except Exception as exc:
            return self._fail("upload_item_keys", exc)
        finally:
            if tracking is not None:
                tracking.close()

    def upload_manifest_preview(self, out_dir: str) -> dict[str, object]:
        """Count the renderable PDFs an upload run would carry, from ``out_dir``.

        A read-only ``*.pdf`` count + byte total — no manifest, no hashing
        (that needs ids the engine carries). Counts and bytes only.
        """
        try:
            from anastomosis.core.output import typed_path

            path = typed_path(out_dir)
            if not path.is_dir():
                return {"ok": False, "error": "NotADirectoryError"}
            pdfs = sorted(path.glob("*.pdf"))
            total_bytes = sum(p.stat().st_size for p in pdfs)
            return {"ok": True, "renderable": len(pdfs), "total_bytes": total_bytes}
        except Exception as exc:
            return self._fail("upload_manifest_preview", exc)

    def upload_safety_notice(self) -> dict[str, object]:
        """The shared-machine warning the console must surface before any attach.

        The single source of truth for the JS's ``#safety-warning`` text —
        :data:`SHARED_MACHINE_WARNING`, the same the CLI prints.
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

        The GUI twin of ``anast upload`` (:func:`run_upload_command`);
        pre-flight (loopback gate, 52) can refuse first. Never closes the operator's browser.
        """
        # Wrapped so this NEVER raises: any surprise becomes the no-traceback
        # error dict; enumerated codes return their dict directly (no event, no _fail).
        try:
            preflight = _upload_preflight(out_dir, cdp_url, pack_name, pack_dirs, skiplist)
        except Exception as exc:  # never-raise: a malformed argument, etc.
            return self._fail("upload", exc)
        if isinstance(preflight, dict):
            return preflight  # an enumerated pre-flight failure — no busy guard taken

        # Published to self in on_start (after the guard is held) so upload_stop()
        # reaches the engine's item-boundary check; cleared in cleanup on every exit.
        stop = threading.Event()

        def _on_start() -> None:
            self._upload_stop = stop
            self._emit(stage_event(self._FLOW, "upload", "start"))

        def _cleanup() -> None:
            self._upload_stop = None

        # 5. Hand off to the job runner only now (a clean pre-flight never
        #    blocks the busy guard); it owns acquire-or-Busy, the start event, and cleanup+release.
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

        The closure the job runner spawns once the busy guard is held;
        drives :func:`run_upload_command`, emitting the terminal event.
        """
        out = inputs.out
        loaded = inputs.loaded
        skiplist_set = inputs.skiplist

        def _worker() -> None:
            # Locks, reads the manifest UNDER the lock (closing the pre-flight
            # copy's TOCTOU), attaches, recovers, runs, reports — never the operator's browser.
            from anastomosis.core.locking import OutputLockedError
            from anastomosis.core.upload_command import UploadCommand, run_upload_command
            from anastomosis.deliver.browser.gates import DeliveryRefused
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
                    # Non-clean TERMINAL states (failed / pre_verify_failed / ...) must
                    # surface as an error, not `done` — names+counts only, PHI-safe.
                    self._emit(error_event(self._FLOW, "upload", result.nonclean_summary()))
                else:
                    self._emit(stage_event(self._FLOW, "upload", "done"))
            except DeliveryRefused as exc:
                # PHI-free by that module's contract, names the remedy; surfaced
                # whole so an operator switching frontends sees the same sentence the CLI prints.
                self._emit(error_event(self._FLOW, "upload", str(exc)))
            except OutputLockedError:
                # A CLI or GUI run already holds this output dir — refuse cleanly.
                self._emit(error_event(self._FLOW, "upload", "OutputLocked"))
            except Exception as exc:  # never-raise: type name only, no PHI
                self._emit(error_event(self._FLOW, "upload", exc_tag(exc)))

        return _worker

    def upload_stop(self) -> dict[str, object]:
        """Request the in-flight upload stop after the current document.

        Cooperative: sets the flag the engine checks at item boundaries, so
        later items stay PENDING for a later :meth:`upload_start` to resume.
        """
        stop = self._upload_stop
        if stop is not None:
            stop.set()
            return {"ok": True, "stopping": True}
        return {"ok": False, "error": "NoRun"}

    @staticmethod
    def _latest_run(tracking: TrackingDB) -> dict[str, object] | None:
        """The most-recent run row (by started_at), as a JSON-safe dict, or None.

        Run id, destination name, ISO timestamps, and an abort TYPE name only
        — never a patient value.
        """
        run_id = tracking.latest_run_id()
        if run_id is None:
            return None
        return {"run_id": run_id, **tracking.run_info(run_id)}

    def _fail(self, stage: str, exc: BaseException) -> dict[str, object]:
        """Convert a caught exception to the no-traceback error contract."""
        return fail_result(self._emit, self._FLOW, stage, exc)
