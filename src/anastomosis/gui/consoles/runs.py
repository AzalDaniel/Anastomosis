"""The two long run flows: the pipeline and the migration consoles.

Both drive a shared command core, emit PHI-safe stage/progress events, are
busy-guarded (sync and async entries contend on the SAME
:class:`~anastomosis.gui.jobs.GuiJobRunner`), and stash their per-run
per-patient roll-up in a shared :class:`SummaryStore` for the dashboard to fetch
by summary id. The per-patient detail (names/DOB/note counts) is PHI by design:
it rides the RETURN value and the summary store for LOCAL display only, never an
event and never a log.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar
from uuid import uuid4

from anastomosis.core.logutil import exc_tag
from anastomosis.gui.events import done_event, error_event, progress_event, stage_event
from anastomosis.gui.jobs import GuiJob, GuiJobRunner
from anastomosis.gui.shared import _STAGE_MAP, _transit_to_dict, fail_result

if TYPE_CHECKING:
    from anastomosis.core.commands import DeliveryOutcome, PatientSummary
    from anastomosis.deliver.router import TransitMap
    from anastomosis.pipeline import StageEvent

__all__ = ["MigrationConsole", "PipelineConsole", "SummaryStore"]


class SummaryStore:
    """A bounded, keyed store of runs' per-patient detail (PHI, local only).

    Keyed by an opaque summary id (not a single slot) so a rapid second run
    cannot overwrite the first's slot before its UI reads it. Bounded to
    the most recent few; local display only, never logged or emitted.
    """

    # Bound how many runs' detail we retain (PHI in memory, local only): keep the
    # most recent few so a UI fetch right after `done` always finds its run.
    _SUMMARY_CAP = 16

    def __init__(self) -> None:
        self._summaries: dict[str, list[dict[str, object]]] = {}

    def store_summary(self, patients: list[dict[str, object]]) -> str:
        """Stash a run's per-patient detail under a fresh opaque id; return the id.

        The id is random hex (never patient-derived); oldest entries are
        evicted past ``_SUMMARY_CAP``.
        """
        summary_id = uuid4().hex
        self._summaries[summary_id] = patients
        while len(self._summaries) > self._SUMMARY_CAP:
            oldest = next(iter(self._summaries))
            del self._summaries[oldest]
        return summary_id

    def get(self, summary_id: str | None = None) -> list[dict[str, object]]:
        """This run's per-patient detail by summary id; empty for an unknown id.

        Returns a fresh list copy; an unknown or missing id yields ``[]`` (no
        global slot — a bare fetch never sees another run's detail).
        """
        return list(self._summaries.get(summary_id or "", []))


class _RunConsole:
    """What the pipeline and migration consoles do identically.

    Shared wiring: construction, the stage-rail bridge, the error contract,
    the roll-up shape. :attr:`_FLOW` is left empty, not defaulted, so a forgotten one fails loud.
    """

    #: The operation family this console owns — set by every subclass.
    _FLOW: ClassVar[str] = ""

    def __init__(
        self,
        emit: Callable[[dict[str, object]], None],
        jobs: GuiJobRunner,
        store: SummaryStore,
    ) -> None:
        self._emit = emit
        self._jobs = jobs
        self._store = store

    def _stage_emitter(self, rollup: dict[str, int]) -> Callable[[StageEvent], None]:
        """The pipeline-stage → rail-event bridge for ONE run.

        Returns the ``on_event`` callback the shared core drives: paints
        the rail, accumulates counts into ``rollup``; a railless stage (``detect``) is skipped."""

        def _on_event(event: StageEvent) -> None:
            stage = _STAGE_MAP.get(event.stage)
            if stage is None:
                return  # the detect stage has no rail of its own
            self._emit(stage_event(self._FLOW, stage, "start"))
            self._emit(progress_event(self._FLOW, stage, **event.counts))
            # A downgraded stage closes "skipped", never "done": a tick over a
            # verification that never ran would tell the physician something untrue.
            self._emit(stage_event(self._FLOW, stage, "skipped" if event.skipped else "done"))
            rollup.update(event.counts)

        return _on_event

    @staticmethod
    def _patient_rows(summaries: Iterable[PatientSummary]) -> list[dict[str, object]]:
        """The per-patient roll-up as JSON-safe rows for the front end.

        PHI: names/DOB ride the RETURN value and the summary store for LOCAL
        display only — never an event, never a log (the consoles' standing rule).
        """
        return [
            {
                "patient_id": s.patient_id,
                "display_name": s.display_name,
                "birth_date": s.birth_date,
                "encounters": s.encounters,
                "documents": s.documents,
            }
            for s in summaries
        ]

    def _fail(self, stage: str, exc: BaseException) -> dict[str, object]:
        """Convert a caught exception to the no-traceback error contract."""
        return fail_result(self._emit, self._FLOW, stage, exc)

    @staticmethod
    def _carry_reading(event: dict[str, object], reading: list[str]) -> dict[str, object]:
        """Attach the source ledger's reading to an event, when the run has one.

        A ledgerless run's events carry no empty key. The sentences are
        PHI-free by ``physician_reading``'s contract.
        """
        if reading:
            event["source_reading"] = reading
        return event


class PipelineConsole(_RunConsole):
    """The pipeline run flow (reconstruct-and-deliver)."""

    # Stamped on every event so the dashboard consumes them and the wizard
    # (flow "migration") does not — both emit identical event kinds.
    _FLOW = "pipeline"

    def run_pipeline(
        self,
        export_dir: str,
        out_dir: str,
        pack: str = "generic_soap",
        source: str | None = None,
        sections: dict[str, bool] | None = None,
        include: list[str] | None = None,
        qa: bool = True,
        archive: bool = False,
        bundle: bool = False,
        ccda: bool = False,
        force: bool = False,
        pack_dirs: list[str] | None = None,
        trust_new: bool = False,
        write_manifest: bool = False,
    ) -> dict[str, object]:
        """Drive the shared pipeline core, emitting stage/progress events.

        Returns the roll-up dict (also a ``done`` event) with ``patients``
        for local display; a failure is ``{"error": <diagnosis>}`` plus an
        event. ``sections``/``include`` mirror the CLI's ``--section``/``--include``."""
        if not self._jobs.acquire():
            return {"ok": False, "error": "Busy"}
        try:
            return self._run_pipeline_locked(
                export_dir=export_dir,
                out_dir=out_dir,
                pack=pack,
                source=source,
                sections=sections or {},
                include=list(include or ()),
                qa=qa,
                archive=archive,
                bundle=bundle,
                ccda=ccda,
                force=force,
                pack_dirs=pack_dirs,
                trust_new=trust_new,
                write_manifest=write_manifest,
            )
        finally:
            self._jobs.release()

    def run_pipeline_async(
        self,
        export_dir: str,
        out_dir: str,
        pack: str = "generic_soap",
        source: str | None = None,
        sections: dict[str, bool] | None = None,
        include: list[str] | None = None,
        qa: bool = True,
        archive: bool = False,
        bundle: bool = False,
        ccda: bool = False,
        force: bool = False,
        pack_dirs: list[str] | None = None,
        trust_new: bool = False,
        write_manifest: bool = False,
    ) -> dict[str, object]:
        """Run the pipeline on a daemon thread (the GUI stays responsive).

        Acquires the busy flag synchronously; returns ``{"started": True}``
        or ``{"error": "Busy"}``. Detail comes from :meth:`last_run_summary` after ``done``."""

        def _worker() -> None:
            self._run_pipeline_locked(
                export_dir=export_dir,
                out_dir=out_dir,
                pack=pack,
                source=source,
                sections=sections or {},
                include=list(include or ()),
                qa=qa,
                archive=archive,
                bundle=bundle,
                ccda=ccda,
                force=force,
                pack_dirs=pack_dirs,
                trust_new=trust_new,
                write_manifest=write_manifest,
            )

        # No on_start: the locked body emits its own stage events. The runner
        # owns acquire-or-Busy, release-in-finally, and the spawn-failure path
        # (release + run_pipeline error event, the same shape as before).
        return self._jobs.submit(
            GuiJob(name="pipeline", stage="run_pipeline", flow=self._FLOW, worker=_worker)
        )

    def _run_pipeline_locked(
        self,
        *,
        export_dir: str,
        out_dir: str,
        pack: str,
        source: str | None,
        sections: dict[str, bool],
        # Required like `sections`: a dropped keyword would silently revert to
        # "every rule applied" with nothing failing.
        include: list[str],
        qa: bool,
        archive: bool,
        bundle: bool,
        ccda: bool,
        # No defaults: a default would make a dropped keyword legal (silent
        # revert, no mypy error). See test_every_pipeline_option_survives_the_async_entry.
        force: bool,
        pack_dirs: list[str] | None,
        trust_new: bool,
        write_manifest: bool,
    ) -> dict[str, object]:
        from anastomosis.core.commands import (
            DeliveryCommand,
            PipelineCommand,
            run_pipeline_command,
            summarize_patients,
        )
        from anastomosis.core.output import (
            OutputPathError,
            clean_typed_path,
            require_output_dir,
        )
        from anastomosis.pipeline import PipelineError

        # Caught here, not in validate_output_target: Path("") is Path(".") —
        # an empty field would silently write patient-named charts into the launch dir.
        try:
            out = require_output_dir(out_dir)
        except OutputPathError as exc:
            self._emit(error_event(self._FLOW, _failed_stage(str(exc)), str(exc)))
            return {"ok": False, "error": str(exc)}
        rollup: dict[str, int] = {}
        _on_event = self._stage_emitter(rollup)

        # Sibling subdirectories of the output dir (the GUI has one output-dir
        # field), through the same command path the CLI uses.
        deliveries: list[DeliveryCommand] = []
        if archive:
            deliveries.append(DeliveryCommand("archive", out / "archive"))
        if bundle:
            deliveries.append(DeliveryCommand("bundle", out / "bundles"))
        if ccda:
            deliveries.append(DeliveryCommand("ccda", out / "ccda"))

        try:
            result = run_pipeline_command(
                PipelineCommand(
                    export_dir=Path(clean_typed_path(export_dir)),
                    charts_dir=out,
                    source=source,
                    pack=pack,
                    pack_dirs=tuple(Path(p) for p in pack_dirs or []),
                    force=force,
                    trust_new=trust_new,
                    sections=sections,
                    include=tuple(include),
                    qa=qa,
                    deliveries=tuple(deliveries),
                    write_manifest=write_manifest,
                ),
                on_event=_on_event,
            )
        except PipelineError as exc:
            self._emit(error_event(self._FLOW, _failed_stage(str(exc)), str(exc)))
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # any non-pipeline crash: type name only, no PHI
            return self._fail("run_pipeline", exc)

        if result.deliveries:
            self._present_deliveries(result.deliveries, rollup)

        # Stored before `done` is emitted, so the dashboard's done handler can
        # fetch it immediately. (Why it rides the return value: module docstring.)
        patients = self._patient_rows(summarize_patients(result.pipeline))
        summary_id = self._store.store_summary(patients)
        reading = list(result.pipeline.source_reading)
        self._emit(
            self._carry_reading(done_event(self._FLOW, summary_id=summary_id, **rollup), reading)
        )
        return {"ok": True, **rollup, "patients": patients, "source_reading": reading}

    def _present_deliveries(
        self, deliveries: dict[str, DeliveryOutcome], rollup: dict[str, int]
    ) -> None:
        """Emit the deliver-rail events from the completed delivery outcomes.

        The deliverers ran inside the shared core; this only presents
        counts — never rendered filenames or chosen paths.
        """
        self._emit(stage_event(self._FLOW, "deliver", "start"))
        for kind in ("archive", "bundle", "ccda"):
            outcome = deliveries.get(kind)
            if outcome is None:
                continue
            patients = outcome.counts["patients"]
            rollup[f"{kind}_patients"] = patients
            # What could NOT be filed travels with what was; zero is omitted
            # so the rail stays short on an ordinary run and a non-zero count stands out.
            shortfall = {
                key: outcome.counts[key]
                for key in ("missing", "unattributed")
                if outcome.counts.get(key)
            }
            for key, value in shortfall.items():
                rollup[f"{kind}_{key}"] = value
            self._emit(
                progress_event(
                    self._FLOW, "deliver", deliverer=kind, patients=patients, **shortfall
                )
            )
        self._emit(stage_event(self._FLOW, "deliver", "done"))


class MigrationConsole(_RunConsole):
    """The migration run flow (EHR-to-EHR; PF→Tebra is one instance)."""

    # Stamped on every event so the wizard consumes them and the dashboard
    # (flow "pipeline") does not — the per-page flow guard.
    _FLOW = "migration"

    def run_migration(
        self,
        export_dir: str,
        out_dir: str,
        source: str,
        destination: str,
        render: str = "neutral",
        sections: dict[str, bool] | None = None,
        qa: bool = True,
        force: bool = False,
        pack_dirs: list[str] | None = None,
        trust_new: bool = False,
    ) -> dict[str, object]:
        """Drive the shared migration core, emitting stage/progress events.

        Mirrors :meth:`run_pipeline`'s contract exactly; the resolved
        transit map rides the return value (``route``) for the wizard to draw."""
        if not self._jobs.acquire():
            return {"ok": False, "error": "Busy"}
        try:
            return self._run_migration_locked(
                export_dir=export_dir,
                out_dir=out_dir,
                source=source,
                destination=destination,
                render=render,
                sections=sections or {},
                qa=qa,
                force=force,
                pack_dirs=pack_dirs,
                trust_new=trust_new,
            )
        finally:
            self._jobs.release()

    def run_migration_async(
        self,
        export_dir: str,
        out_dir: str,
        source: str,
        destination: str,
        render: str = "neutral",
        sections: dict[str, bool] | None = None,
        qa: bool = True,
        force: bool = False,
        pack_dirs: list[str] | None = None,
        trust_new: bool = False,
    ) -> dict[str, object]:
        """Run the migration on a daemon thread (the GUI stays responsive).

        Mirrors :meth:`run_pipeline_async`. The route rides the SYNC return
        of :meth:`run_migration`; the async ``done`` event carries counts only.
        """

        def _worker() -> None:
            self._run_migration_locked(
                export_dir=export_dir,
                out_dir=out_dir,
                source=source,
                destination=destination,
                render=render,
                sections=sections or {},
                qa=qa,
                force=force,
                pack_dirs=pack_dirs,
                trust_new=trust_new,
            )

        # No on_start: the locked body emits its own stage events (see
        # run_pipeline_async for the identical rationale).
        return self._jobs.submit(
            GuiJob(name="migration", stage="run_migration", flow=self._FLOW, worker=_worker)
        )

    def _run_migration_locked(
        self,
        *,
        export_dir: str,
        out_dir: str,
        source: str,
        destination: str,
        render: str,
        sections: dict[str, bool],
        qa: bool,
        force: bool,
        pack_dirs: list[str] | None,
        trust_new: bool,
    ) -> dict[str, object]:
        from anastomosis.core.commands import summarize_patients
        from anastomosis.core.migrate import (
            RENDER_CCDA_STANDARD,
            MigrationCommand,
            run_migration,
        )
        from anastomosis.core.migration_status import (
            classify_migration,
            manual_import_notice,
            prepared_notice,
        )
        from anastomosis.core.output import OutputPathError, clean_typed_path, require_output_dir
        from anastomosis.pipeline import PipelineError

        # Same boundary discipline as the rebuild: a pasted Windows path keeps
        # its quotes, and a blank folder is not a folder anybody chose.
        try:
            out = require_output_dir(out_dir)
        except OutputPathError as exc:
            # _fail reports the exception TYPE (its PHI contract). This message
            # is PHI-free and tells the person what to do, so it is surfaced.
            self._emit(error_event(self._FLOW, _failed_stage(str(exc)), str(exc)))
            return {"ok": False, "error": str(exc)}
        rollup: dict[str, int] = {}
        _on_event = self._stage_emitter(rollup)

        try:
            result = run_migration(
                MigrationCommand(
                    export_dir=Path(clean_typed_path(export_dir)),
                    out_dir=out,
                    source=source,
                    destination=destination,
                    render=render,
                    pack_dirs=tuple(Path(p) for p in pack_dirs or []),
                    trust_new=trust_new,
                    force=force,
                    sections=sections,
                    qa=qa,
                ),
                on_event=_on_event,
            )
        except PipelineError as exc:
            self._emit(error_event(self._FLOW, _failed_stage(str(exc)), str(exc)))
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # any non-migration crash: type name only, no PHI
            return self._fail("run_migration", exc)

        # The structured C-CDA payload count rides the roll-up (the headline of a
        # migration: how many patients' charts moved as importable C-CDA).
        rollup["ccda_patients"] = result.ccda_export.counts["patients"]

        # Pack mode has a pipeline result to summarize; ccda-standard has
        # none, so detail is derived from the loaded records instead.
        if result.render_mode == RENDER_CCDA_STANDARD:
            patients = self._ccda_standard_patients(result)
        else:
            assert result.pipeline is not None  # pack mode always carries a pipeline
            patients = self._patient_rows(summarize_patients(result.pipeline))
        summary_id = self._store.store_summary(patients)
        route = _transit_to_dict(result.transit)

        # No viable route still WRITES its artifacts, but needs manual import
        # — surfaced as an error event (never silent `done`), matching the CLI's exit 1.
        reading = list(result.source_reading)
        status = classify_migration(result)
        if status.needs_manual_import:
            notice = manual_import_notice(status)
            # Matters most here: a person about to hand-import the transfer
            # document should know what it carries as data vs. text.
            self._emit(self._carry_reading(error_event(self._FLOW, "deliver", notice), reading))
            return {
                "ok": False,
                "error": notice,
                "manual_import": True,
                "summary_id": summary_id,
                **rollup,
                "route": route,
                "patients": patients,
                "source_reading": reading,
            }
        # `migrate` executes no delivery route, so a resolved route is
        # honestly PREPARED, never delivered — carried on `done` matching the CLI's exit-0 notice.
        notice = prepared_notice(status)
        done = done_event(self._FLOW, summary_id=summary_id, **rollup)
        done["outcome"] = status.outcome.value
        done["notice"] = notice
        self._emit(self._carry_reading(done, reading))
        return {
            "ok": True,
            "outcome": status.outcome.value,
            "notice": notice,
            **rollup,
            "route": route,
            "patients": patients,
            "source_reading": reading,
        }

    @staticmethod
    def _ccda_standard_patients(result: object) -> list[dict[str, object]]:
        """Per-patient roll-up for ccda-standard mode (no pipeline result).

        No :class:`PipelineResult` exists in this mode, so detail is
        derived from the canonical records instead. Local display only, never an event."""
        from anastomosis.core.migrate import MigrationResult

        assert isinstance(result, MigrationResult)
        return [
            {
                "patient_id": record.patient.id,
                "display_name": record.patient.display_name,
                "birth_date": (
                    record.patient.birth_date.isoformat() if record.patient.birth_date else None
                ),
                "encounters": len(record.encounters),
                "documents": 1,  # ccda-standard renders one whole-patient PDF
            }
            for record in result.records
        ]

    def _pack_readiness(self, transit: TransitMap) -> dict[str, object] | None:
        """Resolve the browser pack for a transit map, if it has one.

        Loads the pack the BROWSER option's ``requires`` names, reporting
        ``ready`` vs ``needs-discovery``; loader failures become a diagnosis, never raised."""
        from anastomosis.deliver.router import RouteKind
        from anastomosis.destinations.loader import BrowserPackError, load_destination_pack

        name = transit.destination
        browser = next(
            (opt for opt in transit.options if opt.kind == RouteKind.BROWSER),
            None,
        )
        if browser is None or not browser.viable:
            return None
        try:
            loaded = load_destination_pack(name)
        except BrowserPackError as exc:
            return {
                "name": name,
                "ready": False,
                "diagnosis": exc_tag(exc),
                "advice": f"anast destination init {name}",
            }
        return {
            "name": loaded.name,
            "ready": loaded.ready,
            "builtin": loaded.builtin,
            # What a person actually runs; carried here (not typed into the JS)
            # so the command lives beside the code that knows it, like pack_freshness's advice.
            "advice": f"anast destination init {loaded.name}",
        }


def _failed_stage(message: str) -> str:
    """Best-effort: which rail stage a PipelineError belongs to (for the event).

    Maps known failure-message prefixes to a rail name; falls back to
    ``ingest`` for source/pack-resolution failures.
    """
    if message.startswith("QA failed"):
        return "qa"
    if "failed to render" in message:
        return "reconstruct"
    return "ingest"
