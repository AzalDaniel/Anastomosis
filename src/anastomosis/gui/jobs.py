# AI-assisted: written with Claude agents under the author's direction and review; see DESIGN.md.
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
    the JS already routes). ``on_start`` runs after the busy guard is
    acquired and before the worker spawns (emit the start event, stash
    per-run state); ``cleanup`` runs in the worker's ``finally`` AND on a
    spawn failure (clear per-run state), always before the guard releases.
    """

    name: str
    worker: Callable[[], None]
    stage: str | None = None
    on_start: Callable[[], None] | None = None
    cleanup: Callable[[], None] | None = None

    @property
    def stage_name(self) -> str:
        return self.stage if self.stage is not None else self.name


class GuiJobRunner:
    """Owns the busy guard and the spawn/release/error choreography."""

    def __init__(self, emit: Callable[[dict[str, object]], None]) -> None:
        self._emit = emit
        self._lock = threading.Lock()
        self._busy = False

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
            return self._fail(job.stage_name, exc)

        def _run() -> None:
            try:
                job.worker()
            except Exception as exc:
                # Safety net: a stray exception on the daemon thread becomes
                # a PHI-safe error event instead of dying silently. Job
                # bodies with outcome-specific terminal events handle their
                # own exceptions and never reach this.
                self._emit(error_event(job.stage_name, exc_tag(exc)))
            finally:
                self._cleanup(job)
                self.release()

        try:
            threading.Thread(target=_run, name=f"anast-{job.name}", daemon=True).start()
        except Exception as exc:
            # Thread.start() can raise (e.g. RuntimeError under thread
            # exhaustion). The worker never runs, so its finally never fires —
            # clean up and release HERE, then return the same no-traceback
            # error shape every job's spawn-failure path used before.
            self._cleanup(job)
            self.release()
            return self._fail(job.stage_name, exc)
        return {"ok": True, "started": True}

    # --- internals -------------------------------------------------------------

    def _cleanup(self, job: GuiJob) -> None:
        if job.cleanup is None:
            return
        try:
            job.cleanup()
        except Exception as exc:  # cleanup must never mask the real outcome
            logger.warning("gui job cleanup failed (%s)", exc_tag(exc))

    def _fail(self, stage: str, exc: BaseException) -> dict[str, object]:
        tag = exc_tag(exc)
        self._emit(error_event(stage, tag))
        return {"ok": False, "error": tag}
