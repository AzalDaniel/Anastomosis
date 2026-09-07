"""Contract: what the two teach wizards (pack-from-samples, learn-a-source)
share — run the core command on a daemon worker, stash the result, close
with one terminal event.

Both stop halfway on purpose: they analyze, then refuse with
``ConfirmationRequired`` so a person confirms before anything writes. That
refusal closes as ``done`` (not ``error``), or the JS's failure branch would
hide the summary the operator must approve. Kept in one place so this rule
cannot drift between the two wizards.
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

    Subclasses set :attr:`_FLOW`/:attr:`_STAGE`, then call
    :meth:`_submit_step` with a callable running their core command.
    """

    #: The operation family this console owns (the per-page flow guard).
    _FLOW: str
    #: The event stage the wizard's JS listens for.
    _STAGE: str

    def __init__(self, emit: Callable[[dict[str, object]], None], jobs: GuiJobRunner) -> None:
        self._emit = emit
        self._jobs = jobs
        # Held for the wizard to fetch once the terminal event lands; PHI-safe
        # (the subclass's result mapper decides what goes in).
        self._last: dict[str, object] = {}

    def _submit_step(self, run: Callable[[], dict[str, object]]) -> dict[str, object]:
        """Run one wizard step on a daemon thread and close it with one event.

        Acquires the busy flag synchronously; returns ``{"started": True}``
        or ``{"error": "Busy"}``. Never raises: a crash emits an error TYPE name.
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
