# AI-assisted: written with Claude agents under the author's direction and review; see DESIGN.md.
"""The pack-from-samples wizard backend (the packgen console).

A thin adapter over the shared :func:`anastomosis.core.packinit.run_pack_init`
core — the analyze -> confirm -> emit flow the CLI's ``anast pack init`` runs —
offered both synchronously (:meth:`PackgenConsole.pack_init`) and as a
busy-guarded daemon job (:meth:`PackgenConsole.pack_init_async`). Every method
keeps the controller's contract: JSON-safe dict, never raise, PHI-safe events
(static template text + counts only, never a sample path or a cell value).
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING

from anastomosis.core.logutil import exc_tag
from anastomosis.gui.events import error_event, stage_event
from anastomosis.gui.jobs import GuiJob, GuiJobRunner

if TYPE_CHECKING:
    from anastomosis.core.packinit import PackInitResult

__all__ = ["PackgenConsole"]


class PackgenConsole:
    """The pack-from-samples wizard backend."""

    # The operation family this console owns; stamped on every event so only the
    # pack-from-samples wizard page consumes them (the P2-5 per-page flow guard).
    # The event STAGE stays "packgen"/"pack_init"; the FLOW is the page-owning
    # family name.
    _FLOW = "pack_init"

    def __init__(self, emit: Callable[[dict[str, object]], None], jobs: GuiJobRunner) -> None:
        self._emit = emit
        self._jobs = jobs
        # The most recent pack_init_async run's result dict, held for the wizard
        # to fetch via last_pack_result() once the packgen `done` event lands.
        # PHI-safe (static template text, counts, pack config); empty until the
        # first async pack-init run.
        self._last_pack: dict[str, object] = {}

    def pack_init(
        self,
        samples_dir: str,
        name: str,
        display: str | None = None,
        confirmed_distinct_patients: bool = False,
        out_dir: str | None = None,
    ) -> dict[str, object]:
        """Learn a DRAFT template pack from sample PDFs (the wizard's backend).

        A thin adapter over the shared
        :func:`anastomosis.core.packinit.run_pack_init` — the SAME analyze →
        confirm → emit flow the CLI's ``anast pack init`` runs. Validate the
        pack name, collect the sample PDFs, harvest + analyze them, render the
        PHI-safe :meth:`PackAnalysis.summary_lines` digest, and — only with
        ``confirmed_distinct_patients`` checked (the CLI's interactive
        same-patient guard, ported as a required checkbox) — emit the draft and
        return its path plus the ``DRAFT.md`` text for display.

        Without the confirmation this REFUSES (``ok: False``, ``error:
        ConfirmationRequired``) and writes nothing — the same guard the CLI
        enforces with ``typer.confirm``. The single-sample text-suppression
        behavior is inherited from ``summary_lines`` (the draft never echoes
        per-patient text).

        PHI rule: ``summary`` carries only static template text (recurring
        across distinct samples) and counts; sample paths are never echoed (the
        count is). Returns JSON-safe data; never raises.
        """
        try:
            from anastomosis.core.packinit import PackInitCommand, run_pack_init

            result = run_pack_init(
                PackInitCommand(
                    samples=[samples_dir],
                    name=name,
                    display=display,
                    out_dir=Path(out_dir) if out_dir is not None else Path("packs"),
                    confirmed=confirmed_distinct_patients,
                )
            )
            return self._pack_init_result_dict(result)
        except Exception as exc:
            return self._fail("pack_init", exc)

    def _pack_init_result_dict(
        self, result: PackInitResult, *, emit_failure: bool = True
    ) -> dict[str, object]:
        """Map a :class:`PackInitResult` to the wizard's JSON-safe dict.

        Shared by the sync :meth:`pack_init` and the async worker so both
        present an identical surface. The PHI-free validation errors
        (``InvalidPackName`` / ``NoSamplesFound``) return the bare code; the
        refusal carries the summary + caveat so the operator can confirm;
        success carries the pack path + ``DRAFT.md``. An analyze/emit failure
        (``error`` is an :func:`exc_tag` type name) ALSO emits a ``pack_init``
        ``error`` event (the sync controller's loud-failure contract) — but only
        when ``emit_failure`` is set. The async worker passes
        ``emit_failure=False`` because IT emits the single ``packgen`` error
        event on its own channel; the gate prevents a double, stage-mismatched
        error event on the async path.
        """
        if result.error in {"InvalidPackName", "NoSamplesFound"}:
            return {"ok": False, "error": result.error}
        if result.error == "ConfirmationRequired":
            return {
                "ok": False,
                "error": "ConfirmationRequired",
                "caveat": result.caveat,
                "summary": result.summary,
                "sample_count": result.sample_count,
                "low_confidence": result.low_confidence,
            }
        if result.ok:
            return {
                "ok": True,
                "pack_dir": str(result.pack_dir),
                "draft_md": result.draft_md,
                "summary": result.summary,
                "sample_count": result.sample_count,
                "low_confidence": result.low_confidence,
            }
        # An analyze/emit failure: a type-name diagnosis. Mirror _fail — emit an
        # error event AND return the no-traceback error dict (sync path only;
        # the async worker emits its own packgen error event).
        error = result.error or "PackInitError"
        if emit_failure:
            self._emit(error_event(self._FLOW, "pack_init", error))
        return {"ok": False, "error": error}

    def pack_init_async(
        self,
        samples_dir: str,
        name: str,
        display: str | None = None,
        confirmed_distinct_patients: bool = False,
        out_dir: str | None = None,
    ) -> dict[str, object]:
        """Run :meth:`pack_init` on a daemon thread (the GUI stays responsive).

        Mirrors :meth:`run_pipeline_async`: acquires the busy flag SYNCHRONOUSLY
        before returning, emits a ``packgen`` ``start`` stage event, and runs the
        shared :func:`anastomosis.core.packinit.run_pack_init` flow on a daemon
        worker. Returns ``{"ok": True, "started": True}`` immediately, or
        ``{"ok": False, "error": "Busy"}`` if a run is already in flight. The
        result dict is stashed for :meth:`last_pack_result` and a terminal event
        lands: a ``packgen`` ``done`` stage event for a written draft OR for a
        ``ConfirmationRequired`` refusal (the expected analyze checkpoint, which
        carries the summary the wizard renders), and a ``packgen`` ``error``
        event only for a genuine failure (bad name, no samples, an analyze/emit
        crash). The JS fetches :meth:`last_pack_result` on ``done`` to route the
        result, so ConfirmationRequired must be ``done``, not ``error``.

        The same-patient semantics match the sync path: ``confirmed=False`` runs
        analyze-only and stashes the ``ConfirmationRequired`` dict (with the
        summary + caveat), so the JS can use the async path for BOTH wizard
        steps. Never raises.
        """

        def _worker() -> None:
            try:
                from anastomosis.core.packinit import PackInitCommand, run_pack_init

                result_dict = self._pack_init_result_dict(
                    run_pack_init(
                        PackInitCommand(
                            samples=[samples_dir],
                            name=name,
                            display=display,
                            out_dir=Path(out_dir) if out_dir is not None else Path("packs"),
                            confirmed=confirmed_distinct_patients,
                        )
                    ),
                    emit_failure=False,  # the worker emits the single packgen error below
                )
                self._last_pack = result_dict
                # A written draft OR the expected ConfirmationRequired refusal are
                # both `done` (the wizard fetches last_pack_result and routes the
                # summary vs the draft). Only a genuine failure is an `error`.
                if result_dict.get("ok") or result_dict.get("error") == "ConfirmationRequired":
                    self._emit(stage_event(self._FLOW, "packgen", "done"))
                else:
                    self._emit(error_event(self._FLOW, "packgen", str(result_dict.get("error"))))
            except Exception as exc:  # never-raise: stash + emit, swallow nothing else
                tag = exc_tag(exc)
                self._last_pack = {"ok": False, "error": tag}
                self._emit(error_event(self._FLOW, "packgen", tag))

        return self._jobs.submit(
            GuiJob(
                name="packgen",
                flow=self._FLOW,
                worker=_worker,
                on_start=lambda: self._emit(stage_event(self._FLOW, "packgen", "start")),
            )
        )

    def last_pack_result(self) -> dict[str, object]:
        """The most recent :meth:`pack_init_async` result, for the wizard to fetch.

        The async path returns immediately with ``{"started": True}`` and streams
        PHI-safe ``packgen`` stage/error events back; the full result dict (the
        summary + caveat for ``ConfirmationRequired``, or the pack path +
        ``DRAFT.md`` for a written draft) is held here for the wizard to fetch
        once the ``done``/``error`` event lands. PHI-safe (static template text,
        counts, pack config). Returns ``{"ok": False, "error": "NoResult"}``
        before the first async run. Never raises.
        """
        return deepcopy(self._last_pack) if self._last_pack else {"ok": False, "error": "NoResult"}

    def _fail(self, stage: str, exc: BaseException) -> dict[str, object]:
        """Convert a caught exception to the no-traceback error contract."""
        tag = exc_tag(exc)
        self._emit(error_event(self._FLOW, stage, tag))
        return {"ok": False, "error": tag}
