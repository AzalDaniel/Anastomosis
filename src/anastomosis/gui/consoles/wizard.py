"""What the two teach wizards do identically, in one place.

Both wizards — learn a template pack from sample PDFs, learn a new source
format from one example export — have different domain logic and share a
choreography: run the core command on a daemon worker, stash the result dict
for the wizard to fetch, and close the run with one terminal event.

The rule worth having in one place is which event that is. Both wizards stop
halfway ON PURPOSE: they analyze, then refuse with ``ConfirmationRequired`` so
a person can confirm what was found before anything is written. That refusal is
the wizard's ordinary middle step, not a failure, so it closes the run with
``done`` — and the JS fetches the stashed result on ``done`` to render the
summary for confirming. Emitting it as ``error`` would route the normal path
through the failure branch and the operator would never see the summary they
are meant to approve. Written out twice, that rule could drift in one wizard
and the tests would still pass; here it cannot.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy

from anastomosis.core.logutil import exc_tag
from anastomosis.gui.events import error_event, stage_event
from anastomosis.gui.jobs import GuiJob, GuiJobRunner
from anastomosis.gui.shared import fail_result

__all__ = ["WizardConsole"]


class WizardConsole:
    """Base for the two teach-wizard console backends.

    Subclasses set :attr:`_FLOW` (the operation family stamped on every event,
    so only the owning wizard page consumes them) and :attr:`_STAGE` (the event
    stage the JS routes on), then call :meth:`_submit_step` with a callable that
    runs their core command and returns the wizard's JSON-safe dict.
    """

    #: The operation family this console owns (the per-page flow guard).
    _FLOW: str
    #: The event stage the wizard's JS listens for.
    _STAGE: str

    def __init__(self, emit: Callable[[dict[str, object]], None], jobs: GuiJobRunner) -> None:
        self._emit = emit
        self._jobs = jobs
        # The most recent async run's result dict, held for the wizard to fetch
        # once the terminal event lands. PHI-safe (the subclass's result mapper
        # decides what goes in); empty until the first async run.
        self._last: dict[str, object] = {}

    def _submit_step(self, run: Callable[[], dict[str, object]]) -> dict[str, object]:
        """Run one wizard step on a daemon thread and close it with one event.

        Acquires the busy flag SYNCHRONOUSLY before returning, so two quick
        clicks cannot both get ``{"started": True}``. Returns ``{"ok": True,
        "started": True}`` immediately, or ``{"ok": False, "error": "Busy"}`` if
        a run is already in flight. ``run`` executes on the worker; whatever it
        returns is stashed for :meth:`_last_result` and routed to the terminal
        event per the module docstring. Never raises: a crash in ``run`` is
        stashed and emitted as an error with its exception TYPE name only.
        """

        def _worker() -> None:
            try:
                result = run()
                self._last = result
                if result.get("ok") or result.get("error") == "ConfirmationRequired":
                    self._emit(stage_event(self._FLOW, self._STAGE, "done"))
                else:
                    self._emit(error_event(self._FLOW, self._STAGE, str(result.get("error"))))
            except Exception as exc:  # never-raise: stash + emit, swallow nothing else
                tag = exc_tag(exc)
                self._last = {"ok": False, "error": tag}
                self._emit(error_event(self._FLOW, self._STAGE, tag))

        return self._jobs.submit(
            GuiJob(
                name=self._STAGE,
                flow=self._FLOW,
                worker=_worker,
                on_start=lambda: self._emit(stage_event(self._FLOW, self._STAGE, "start")),
            )
        )

    def _last_result(self) -> dict[str, object]:
        """The stashed result, or ``NoResult`` before the first async run.

        Returned as a deep copy so the wizard cannot mutate what the console
        still holds.
        """
        return deepcopy(self._last) if self._last else {"ok": False, "error": "NoResult"}

    def _fail(self, stage: str, exc: BaseException) -> dict[str, object]:
        """Convert a caught exception to the no-traceback error contract."""
        return fail_result(self._emit, self._FLOW, stage, exc)
