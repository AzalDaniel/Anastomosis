"""Contract: ``GuiJobRunner`` runs one GUI action on a daemon thread behind
a busy guard, acquired SYNCHRONOUSLY before :meth:`submit` returns so two
quick clicks cannot both start. ``acquire``/``release`` are exposed
separately so the sync ``run_pipeline``/``run_migration`` paths contend on
the SAME guard. A stray worker exception becomes a PHI-safe error event
instead of dying silently — job bodies with their own outcome-specific
terminal events must never raise into this net.
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

    ``stage``/``flow`` default to ``name``; ``on_start`` runs after the
    guard acquires, ``cleanup`` in the worker's ``finally`` and on spawn failure.
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
        # Lets the shell's close barrier check `busy`/`join` an in-flight run;
        # daemon=True so a wedged run can never make the process unkillable.
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
        """Wait up to ``timeout`` seconds for the active worker; return whether it finished.

        Reads the handle under the lock but joins outside it — ``release``
        (in the worker's ``finally``) takes the same lock, so holding it here would deadlock.
        """
        with self._lock:
            worker = self._worker
        if worker is None:
            return True
        worker.join(timeout)
        return not worker.is_alive()

    # --- the async choreography ----------------------------------------------

    def submit(self, job: GuiJob) -> dict[str, object]:
        """Run ``job`` behind the busy guard; never raises.

        Returns ``{"ok": True, "started": True}``, ``{"ok": False, "error":
        "Busy"}``, or the no-traceback error shape if ``on_start``/``Thread.start()`` failed.
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
                # Safety net (module docstring); carries the job's own flow to its page.
                self._emit(error_event(job.flow_name, job.stage_name, exc_tag(exc)))
            except BaseException as exc:
                # BaseException is deliberate (upload's FakeCrash models process death
                # this way); re-raised after emitting, so a shutdown stays a shutdown (#117).
                self._emit(error_event(job.flow_name, job.stage_name, exc_tag(exc)))
                raise
            finally:
                self._cleanup(job)
                self.release()

        worker = threading.Thread(target=_run, name=f"anast-{job.name}", daemon=True)
        try:
            worker.start()
        except Exception as exc:
            # Thread.start() can raise (thread exhaustion); the worker never
            # runs, so its `finally` never fires — clean up and release here.
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
