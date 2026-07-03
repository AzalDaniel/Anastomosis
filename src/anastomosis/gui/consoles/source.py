# AI-assisted: written with Claude agents under the author's direction and review; see DESIGN.md.
"""The learn-a-source wizard backend (the source console).

Marshals the shared
:func:`anastomosis.core.source_init_command.run_source_init_command` core — the
analyze -> build -> round-trip -> save flow the CLI's ``anast source init`` runs
— into the wizard's JSON dict, offered both synchronously
(:meth:`SourceConsole.source_init`) and as a busy-guarded daemon job
(:meth:`SourceConsole.source_init_async`). PHI rule: the proposal carries column
names, inferred type labels, counts, and masked shapes only — never a cell
value; the example path the operator typed is not echoed back.
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
    from anastomosis.core.source_init_command import SourceInitResult

__all__ = ["SourceConsole"]


def _source_result_dict(result: SourceInitResult) -> dict[str, object]:
    """Marshal a :class:`SourceInitResult` into the learn-a-source wizard's dict.

    Preserves the wizard's JSON contract exactly: a pre-analyze failure
    (``InvalidSourceName`` / ``NoExampleFile`` / ``AmbiguousExample`` /
    ``CannotAnalyze``) is the bare ``{"ok": False, "error": <code>}``; once
    analysis succeeded the PHI-safe proposal rides along (column names, type
    labels, counts, masked shapes — never a cell value), plus the
    outcome-specific keys (``dropped`` / ``detail`` / the saved-mapping fields).
    """
    out: dict[str, object] = {"ok": result.ok, "error": result.error}
    if result.fmt_type is None:
        return out  # a pre-analyze failure carries no proposal
    out.update(
        {
            "format": result.fmt_type,
            "columns": result.columns,
            "patient_key": result.patient_key,
            "encounter_key": result.encounter_key,
            "row_scope": result.row_scope,
            "summary": list(result.summary),
            "suggestions": [
                {
                    "source": s.source,
                    "target": s.target,
                    "transform": s.transform,
                    "confidence": s.confidence,
                }
                for s in result.suggestions
            ],
            "mapped": result.mapped,
        }
    )
    if result.ok:
        out.update(
            {
                "mapping_dir": str(result.mapping_dir),
                "mapping_md": result.mapping_md,
                "record_count": result.record_count,
                "unmapped": result.unmapped,
            }
        )
    elif result.error == "WouldDropColumns":
        out["dropped"] = list(result.dropped_columns)
    elif result.error == "MappingLoadFailed":
        out["detail"] = result.detail
    return out


class SourceConsole:
    """The learn-a-source wizard backend."""

    def __init__(self, emit: Callable[[dict[str, object]], None], jobs: GuiJobRunner) -> None:
        self._emit = emit
        self._jobs = jobs
        # The most recent source_init_async run's result dict, held for the source
        # wizard to fetch via last_source_result() once the `source` done/error
        # event lands. PHI-safe (column names, type labels, counts, masked shapes,
        # mapping config); empty until the first async learn-a-source run.
        self._last_source: dict[str, object] = {}

    def source_init(
        self,
        example_path: str,
        name: str,
        display: str | None = None,
        confirmed: bool = False,
        out_dir: str | None = None,
    ) -> dict[str, object]:
        """Learn a new structured-export format from one example (wizard backend).

        Mirrors the CLI ``anast source init`` headlessly: resolve the example to
        a single structured file, analyze it LOCALLY, and propose a mapping to the
        canonical model. Without ``confirmed`` this REFUSES
        (``error: ConfirmationRequired``) and writes nothing — returning the
        proposed mapping so the operator sees what they are confirming (the same
        two-step shape as :meth:`pack_init`). With ``confirmed`` it builds the
        mapping, round-trips it against the example to PROVE no column is dropped,
        and only then saves it (owner-only), returning the mapping directory and
        ``MAPPING.md``; a mapping that would drop a column refuses with
        ``error: WouldDropColumns`` and the offending column names.

        PHI rule: ``summary``/``suggestions`` carry column names, inferred type
        labels, counts, and digit/letter-masked shapes only — never a cell value;
        the example path the operator typed is not echoed back. Returns JSON-safe
        data; never raises.

        The analyze -> build -> round-trip -> save flow lives in the SHARED
        :func:`anastomosis.core.source_init_command.run_source_init_command` core
        (the same one ``anast source init`` runs), so the two frontends cannot
        diverge; this method only marshals its result into the wizard's dict.
        """
        try:
            from anastomosis.core.source_init_command import (
                SourceInitCommand,
                run_source_init_command,
            )

            result = run_source_init_command(
                SourceInitCommand(
                    example=Path(example_path),
                    name=name,
                    display=display,
                    out_dir=Path(out_dir) if out_dir is not None else None,
                    confirmed=confirmed,
                )
            )
            return _source_result_dict(result)
        except Exception as exc:
            return self._fail("source_init", exc)

    def source_init_async(
        self,
        example_path: str,
        name: str,
        display: str | None = None,
        confirmed: bool = False,
        out_dir: str | None = None,
    ) -> dict[str, object]:
        """Run :meth:`source_init` on a daemon thread (the GUI stays responsive).

        Mirrors :meth:`pack_init_async`: acquires the busy flag SYNCHRONOUSLY
        before returning, emits a ``source`` ``start`` stage event, and runs the
        SAME shared
        :func:`anastomosis.core.source_init_command.run_source_init_command` flow
        on a daemon worker. Returns ``{"ok": True, "started": True}`` immediately,
        or ``{"ok": False, "error": "Busy"}`` if a run is already in flight. The
        result dict is stashed for :meth:`last_source_result` and a terminal event
        lands: a ``source`` ``done`` stage event for a saved mapping OR for the
        expected ``ConfirmationRequired`` analyze checkpoint (which carries the
        proposal the wizard renders), and a ``source`` ``error`` event for any
        other outcome (a bad name, an unanalyzable example, a would-drop-columns
        refusal, a save failure). The JS fetches :meth:`last_source_result` on
        BOTH so it can render the outcome-specific detail (dropped columns, the
        load-failure diagnosis), never raises.
        """

        def _worker() -> None:
            try:
                from anastomosis.core.source_init_command import (
                    SourceInitCommand,
                    run_source_init_command,
                )

                result_dict = _source_result_dict(
                    run_source_init_command(
                        SourceInitCommand(
                            example=Path(example_path),
                            name=name,
                            display=display,
                            out_dir=Path(out_dir) if out_dir is not None else None,
                            confirmed=confirmed,
                        )
                    )
                )
                self._last_source = result_dict
                # A saved mapping OR the expected ConfirmationRequired checkpoint
                # are both `done` (the wizard fetches last_source_result and routes
                # the proposal vs the saved result). Every other outcome is an
                # `error`; the JS still fetches the stashed result for its detail.
                if result_dict.get("ok") or result_dict.get("error") == "ConfirmationRequired":
                    self._emit(stage_event("source", "done"))
                else:
                    self._emit(error_event("source", str(result_dict.get("error"))))
            except Exception as exc:  # never-raise: stash + emit, swallow nothing else
                tag = exc_tag(exc)
                self._last_source = {"ok": False, "error": tag}
                self._emit(error_event("source", tag))

        return self._jobs.submit(
            GuiJob(
                name="source",
                worker=_worker,
                on_start=lambda: self._emit(stage_event("source", "start")),
            )
        )

    def last_source_result(self) -> dict[str, object]:
        """The most recent :meth:`source_init_async` result, for the wizard to fetch.

        The async path returns immediately with ``{"started": True}`` and streams
        PHI-safe ``source`` stage/error events back; the full result dict (the
        proposal for ``ConfirmationRequired``, the path + ``MAPPING.md`` for a
        saved mapping, or the dropped columns / load diagnosis for a refusal) is
        held here for the wizard to fetch once the terminal event lands. PHI-safe
        (column names, type labels, counts, masked shapes, mapping config).
        Returns ``{"ok": False, "error": "NoResult"}`` before the first async run.
        Never raises.
        """
        return (
            deepcopy(self._last_source) if self._last_source else {"ok": False, "error": "NoResult"}
        )

    def _fail(self, stage: str, exc: BaseException) -> dict[str, object]:
        """Convert a caught exception to the no-traceback error contract."""
        tag = exc_tag(exc)
        self._emit(error_event(stage, tag))
        return {"ok": False, "error": tag}
