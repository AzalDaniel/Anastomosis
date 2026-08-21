"""GuiJobRunner: the one owner of the GUI's async-job choreography.

Every long-running GUI action used to hand-roll the same six steps —
acquire the busy guard (or return ``Busy``), set up per-job state, emit a
start event, spawn a daemon worker whose ``finally`` releases the guard,
handle a ``Thread.start()`` failure by cleaning up + releasing + returning
the no-traceback error dict, and return ``{"ok": True, "started": True}``.
Five copies of that choreography lived in ``controller.py`` (upload,
packgen, source-init, pipeline, migration). This module owns it once.

Contract highlights, all preserved verbatim from the hand-rolled copies:

* the busy guard is acquired SYNCHRONOUSLY before ``submit`` returns, so
  two quick clicks cannot both get ``{"started": True}``;
* a rejected submit is exactly ``{"ok": False, "error": "Busy"}``;
* the worker's ``finally`` runs the job's ``cleanup`` and releases the
  guard, whatever the body did;
* ``Thread.start()`` failure (thread exhaustion) runs ``cleanup``,
  releases the guard, emits the stage error event, and returns
  ``{"ok": False, "error": <exc_tag>}`` — the busy flag can never leak;
* the runner's catch around the worker is a safety net that emits an
  error event instead of letting a stray exception die silently on the
  daemon thread — job bodies that need outcome-specific terminal events
  (packgen's ``ConfirmationRequired``-is-``done`` routing, upload's
  ``OutputLocked``) keep their own internal handling and simply never
  raise into the net.

The runner also owns the busy flag itself, exposing ``acquire``/``release``
for the synchronous busy-guarded paths (``run_pipeline``/``run_migration``
sync variants) so async and sync entries contend on the SAME guard.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

from anastomosis.core.logutil import exc_tag
from anastomosis.gui.events import error_event

__all__ = ["GuiJob", "GuiJobRunner"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GuiJob:
    """One long-running GUI action, described declaratively.

    ``name`` is the daemon-thread suffix (``anast-{name}``); ``stage`` is
    the event-stage label error events carry (defaults to ``name`` — the
    pipeline/migration jobs use their method names, matching the events
    the JS already routes). ``flow`` is the owning operation family the
    runner stamps onto the error events IT raises (the safety net + the
    spawn-failure path), so those events reach the same page as the job's
    own events; it defaults to ``name`` for jobs whose family equals their
    thread name. ``on_start`` runs after the busy guard is acquired and
    before the worker spawns (emit the start event, stash per-run state);
    ``cleanup`` runs in the worker's ``finally`` AND on a spawn failure
    (clear per-run state), always before the guard releases.
    """

    name: str
    worker: Callable[[], None]
    stage: str | None = None
    flow: str | None = None
    on_start: Callable[[], None] | None = None
    cleanup: Callable[[], None] | None = None

    @property
    def stage_name(self) -> str:
        return self.stage if self.stage is not None else self.name

    @property
    def flow_name(self) -> str:
        return self.flow if self.flow is not None else self.name


class GuiJobRunner:
    """Owns the busy guard and the spawn/release/error choreography."""

    def __init__(self, emit: Callable[[dict[str, object]], None]) -> None:
        self._emit = emit
        self._lock = threading.Lock()
        self._busy = False
        # A handle on the most recently spawned worker, kept so the shell's
        # window-close barrier can tell a run is in flight (``busy``) and wait
        # for its in-flight PDF/ledger writes to finish (``join``). The worker
        # stays ``daemon=True`` — a wedged run must never make the process
        # unkillable — so this handle is a graceful barrier, not a hard lock.
        self._worker: threading.Thread | None = None

    # --- the busy guard (shared by sync and async entries) -------------------

    def acquire(self) -> bool:
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            return True

    def release(self) -> None:
        with self._lock:
            self._busy = False

    @property
    def busy(self) -> bool:
        """True while a run holds the guard (the shell's close barrier reads this)."""
        with self._lock:
            return self._busy

    def join(self, timeout: float | None = None) -> bool:
        """Wait up to ``timeout`` seconds for the active worker to finish.

        Returns ``True`` when no worker is active or the active one finished
        within ``timeout``; ``False`` when a worker is still running after the
        timeout. The shell's window-close barrier uses this so an in-flight
        PDF/ledger write is given a chance to complete before the window goes.
        The worker handle is read under the lock, but the join happens OUTSIDE
        it — the worker's ``finally`` calls ``release`` (which takes the lock),
        so holding it here would deadlock.
        """
        with self._lock:
            worker = self._worker
        if worker is None:
            return True
        worker.join(timeout)
        return not worker.is_alive()

    # --- the async choreography ----------------------------------------------

    def submit(self, job: GuiJob) -> dict[str, object]:
        """Run ``job`` on a daemon thread behind the busy guard.

        Returns ``{"ok": True, "started": True}`` when the worker spawned,
        ``{"ok": False, "error": "Busy"}`` when a run is already in flight,
        or the no-traceback ``{"ok": False, "error": <exc_tag>}`` shape when
        ``on_start`` or ``Thread.start()`` failed (guard released, cleanup
        run, stage error event emitted). Never raises.
        """
        if not self.acquire():
            return {"ok": False, "error": "Busy"}

        try:
            if job.on_start is not None:
                job.on_start()
        except Exception as exc:  # on_start must never leak the busy flag
            self._cleanup(job)
            self.release()
            return self._fail(job, exc)

        def _run() -> None:
            try:
                job.worker()
            except Exception as exc:
                # Safety net: a stray exception on the daemon thread becomes
                # a PHI-safe error event instead of dying silently. Job
                # bodies with outcome-specific terminal events handle their
                # own exceptions and never reach this. The event carries the
                # job's own flow so it reaches the same page as its siblings.
                self._emit(error_event(job.flow_name, job.stage_name, exc_tag(exc)))
            finally:
                self._cleanup(job)
                self.release()

        worker = threading.Thread(target=_run, name=f"anast-{job.name}", daemon=True)
        try:
            worker.start()
        except Exception as exc:
            # Thread.start() can raise (e.g. RuntimeError under thread
            # exhaustion). The worker never runs, so its finally never fires —
            # clean up and release HERE, then return the same no-traceback
            # error shape every job's spawn-failure path used before.
            self._cleanup(job)
            self.release()
            return self._fail(job, exc)
        with self._lock:
            self._worker = worker
        return {"ok": True, "started": True}

    # --- internals -------------------------------------------------------------

    def _cleanup(self, job: GuiJob) -> None:
        if job.cleanup is None:
            return
        try:
            job.cleanup()
        except Exception as exc:  # cleanup must never mask the real outcome
            logger.warning("gui job cleanup failed (%s)", exc_tag(exc))

    def _fail(self, job: GuiJob, exc: BaseException) -> dict[str, object]:
        tag = exc_tag(exc)
        self._emit(error_event(job.flow_name, job.stage_name, tag))
        return {"ok": False, "error": tag}
