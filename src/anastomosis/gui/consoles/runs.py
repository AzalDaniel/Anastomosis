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

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from anastomosis.core.logutil import exc_tag
from anastomosis.gui.events import done_event, error_event, progress_event, stage_event
from anastomosis.gui.jobs import GuiJob, GuiJobRunner
from anastomosis.gui.shared import _STAGE_MAP, _transit_to_dict

if TYPE_CHECKING:
    from anastomosis.core.commands import DeliveryOutcome
    from anastomosis.deliver.router import TransitMap
    from anastomosis.pipeline import StageEvent

__all__ = ["MigrationConsole", "PipelineConsole", "SummaryStore"]


class SummaryStore:
    """A bounded, keyed store of runs' per-patient detail (PHI, local only).

    Per-run per-patient detail (display name, DOB, note counts) is keyed by an
    opaque summary id the `done` event carries, for the dashboard/wizard to
    fetch via :meth:`get`. Keyed (not a single slot) so a rapid SECOND run cannot
    overwrite the slot the first run's UI then reads (the summary race). Bounded
    to the most recent few. PHI: local display only, never logged or emitted.
    """

    # Bound how many runs' detail we retain (PHI in memory, local only): keep the
    # most recent few so a UI fetch right after `done` always finds its run.
    _SUMMARY_CAP = 16

    def __init__(self) -> None:
        self._summaries: dict[str, list[dict[str, object]]] = {}

    def store_summary(self, patients: list[dict[str, object]]) -> str:
        """Stash a run's per-patient detail under a fresh opaque id; return the id.

        The id is random hex (never patient-derived) and rides the `done` event;
        the front end passes it back to :meth:`get`. Oldest entries are evicted
        past ``_SUMMARY_CAP`` so PHI does not accumulate unbounded.
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


class PipelineConsole:
    """The pipeline run flow (reconstruct-and-deliver)."""

    # The operation family this console owns; stamped on every event so the
    # dashboard page consumes them and the wizard (flow "migration") does not
    # (both emit identical stage/progress/done/error kinds — the per-page flow guard).
    _FLOW = "pipeline"

    def __init__(
        self,
        emit: Callable[[dict[str, object]], None],
        jobs: GuiJobRunner,
        store: SummaryStore,
    ) -> None:
        self._emit = emit
        self._jobs = jobs
        self._store = store

    def run_pipeline(
        self,
        export_dir: str,
        out_dir: str,
        pack: str = "generic_soap",
        source: str | None = None,
        sections: dict[str, bool] | None = None,
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

        Returns the final roll-up dict (also emitted as a ``done`` event), with
        a ``patients`` key carrying the per-patient detail for local display
        (names/DOB/note counts — never emitted as events; see
        :meth:`last_run_summary`). Any failure becomes ``{"ok": False, "error":
        <type-or-diagnosis>}`` plus an ``error`` event. The ``busy`` guard
        rejects a second concurrent run.

        ``force`` re-renders documents that already exist; ``pack_dirs`` makes
        extra pack directories available and ``trust_new`` records (trusts)
        their current code hash on first use — the same backend levers the CLI
        exposes, no longer hard-coded off. Deliverer flags
        (``archive``/``bundle``/``ccda``) write into sibling subdirectories of
        ``out_dir`` since the GUI has one output-dir field.
        """
        if not self._jobs.acquire():
            return {"ok": False, "error": "Busy"}
        try:
            return self._run_pipeline_locked(
                export_dir=export_dir,
                out_dir=out_dir,
                pack=pack,
                source=source,
                sections=sections or {},
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

        Acquires the busy flag SYNCHRONOUSLY before returning, so two quick
        clicks can't both get ``{"started": True}`` (the worker then runs the
        locked body and releases in ``finally``). Returns ``{"ok": True,
        "started": True}`` on success or ``{"ok": False, "error": "Busy"}`` if a
        run is already in flight. The result arrives as
        ``stage``/``progress``/``done``/``error`` events; the per-patient detail
        is fetched after ``done`` via :meth:`last_run_summary` (the events stay
        count-only).
        """

        def _worker() -> None:
            self._run_pipeline_locked(
                export_dir=export_dir,
                out_dir=out_dir,
                pack=pack,
                source=source,
                sections=sections or {},
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
        qa: bool,
        archive: bool,
        bundle: bool,
        ccda: bool,
        force: bool = False,
        pack_dirs: list[str] | None = None,
        trust_new: bool = False,
        write_manifest: bool = False,
    ) -> dict[str, object]:
        from anastomosis.core.commands import (
            DeliveryCommand,
            PipelineCommand,
            run_pipeline_command,
            summarize_patients,
        )
        from anastomosis.pipeline import PipelineError

        out = Path(out_dir)
        rollup: dict[str, int] = {}

        def _on_event(event: StageEvent) -> None:
            stage = _STAGE_MAP.get(event.stage)
            if stage is None:
                return  # the detect stage has no rail of its own
            self._emit(stage_event(self._FLOW, stage, "start"))
            self._emit(progress_event(self._FLOW, stage, **event.counts))
            self._emit(stage_event(self._FLOW, stage, "done"))
            rollup.update(event.counts)

        # GUI deliveries land in sibling subdirectories of the output dir (the
        # GUI has one output-dir field), through the same command path the CLI
        # uses with operator-chosen paths.
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
                    export_dir=Path(export_dir),
                    charts_dir=out,
                    source=source,
                    pack=pack,
                    pack_dirs=tuple(Path(p) for p in pack_dirs or []),
                    force=force,
                    trust_new=trust_new,
                    sections=sections,
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

        # Per-patient detail rides the RETURN value (and last_run_summary), never
        # an event: names/DOB are local display only, the event stream is counts.
        # Store before emitting `done` so the dashboard's done handler can fetch
        # it immediately.
        patients: list[dict[str, object]] = [
            {
                "patient_id": s.patient_id,
                "display_name": s.display_name,
                "birth_date": s.birth_date,
                "encounters": s.encounters,
                "documents": s.documents,
            }
            for s in summarize_patients(result.pipeline)
        ]
        summary_id = self._store.store_summary(patients)
        self._emit(done_event(self._FLOW, summary_id=summary_id, **rollup))
        return {"ok": True, **rollup, "patients": patients}

    def _present_deliveries(
        self, deliveries: dict[str, DeliveryOutcome], rollup: dict[str, int]
    ) -> None:
        """Emit the deliver-rail events from the completed delivery outcomes.

        The deliverers themselves ran inside the shared command core; this only
        presents the counts. PHI rule: each event carries a COUNT of artifacts
        written, never the rendered filenames or the operator's chosen paths.
        """
        self._emit(stage_event(self._FLOW, "deliver", "start"))
        for kind in ("archive", "bundle", "ccda"):
            outcome = deliveries.get(kind)
            if outcome is None:
                continue
            patients = outcome.counts["patients"]
            rollup[f"{kind}_patients"] = patients
            self._emit(progress_event(self._FLOW, "deliver", deliverer=kind, patients=patients))
        self._emit(stage_event(self._FLOW, "deliver", "done"))

    def _fail(self, stage: str, exc: BaseException) -> dict[str, object]:
        """Convert a caught exception to the no-traceback error contract."""
        tag = exc_tag(exc)
        self._emit(error_event(self._FLOW, stage, tag))
        return {"ok": False, "error": tag}


class MigrationConsole:
    """The migration run flow (EHR-to-EHR; PF→Tebra is one instance)."""

    # The operation family this console owns; stamped on every event so the
    # wizard page consumes them and the dashboard (flow "pipeline") does not —
    # the per-page flow guard against one page consuming the other's terminal event.
    _FLOW = "migration"

    def __init__(
        self,
        emit: Callable[[dict[str, object]], None],
        jobs: GuiJobRunner,
        store: SummaryStore,
    ) -> None:
        self._emit = emit
        self._jobs = jobs
        self._store = store

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

        Mirrors :meth:`run_pipeline` exactly for the contract: never raises (a
        failure is ``{"ok": False, "error": <type-or-diagnosis>}`` plus an
        ``error`` event), busy-guarded, PHI-safe events only, and the per-patient
        roll-up stored for :meth:`last_run_summary`. The resolved transit map
        rides the return value (``route``) so the wizard can draw the chosen
        route the migration would take. Returns ``{"ok": True, **rollup,
        "route": {...}, "patients": [...]}``.
        """
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

        Mirrors :meth:`run_pipeline_async`: acquires the busy flag SYNCHRONOUSLY
        so two quick clicks can't both start, returns ``{"ok": True, "started":
        True}`` (or ``{"ok": False, "error": "Busy"}``), and streams the result
        as ``stage``/``progress``/``done``/``error`` events. The per-patient
        detail and the route are fetched after ``done`` via
        :meth:`last_run_summary` (the route also rides the synchronous return of
        :meth:`run_migration`; the async path's done event carries counts only).
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
        from anastomosis.pipeline import PipelineError

        rollup: dict[str, int] = {}

        def _on_event(event: StageEvent) -> None:
            stage = _STAGE_MAP.get(event.stage)
            if stage is None:
                return  # the detect stage has no rail of its own
            self._emit(stage_event(self._FLOW, stage, "start"))
            self._emit(progress_event(self._FLOW, stage, **event.counts))
            self._emit(stage_event(self._FLOW, stage, "done"))
            rollup.update(event.counts)

        try:
            result = run_migration(
                MigrationCommand(
                    export_dir=Path(export_dir),
                    out_dir=Path(out_dir),
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

        # Per-patient detail rides the RETURN value (and last_run_summary), never
        # an event. In neutral/pack mode the pipeline result yields it; in
        # ccda-standard mode (no pipeline) it is derived from the loaded records
        # and the per-patient view (one document per patient).
        if result.render_mode == RENDER_CCDA_STANDARD:
            patients = self._ccda_standard_patients(result)
        else:
            assert result.pipeline is not None  # pack mode always carries a pipeline
            patients = [
                {
                    "patient_id": s.patient_id,
                    "display_name": s.display_name,
                    "birth_date": s.birth_date,
                    "encounters": s.encounters,
                    "documents": s.documents,
                }
                for s in summarize_patients(result.pipeline)
            ]
        summary_id = self._store.store_summary(patients)
        route = _transit_to_dict(result.transit)

        # The SAME shared verdict the CLI uses. A migration with no viable
        # automated route still WROTE its artifacts (the C-CDA + charts), but the
        # operator must import them by hand — so surface it as a manual-import
        # (error) event, never a silent `done`, exactly as the CLI exits 1 with a
        # loud notice. This keeps CLI/GUI parity: both frontends surface a
        # no-viable-route migration as a manual-import event, never a silent done.
        status = classify_migration(result)
        if status.needs_manual_import:
            notice = manual_import_notice(status)
            self._emit(error_event(self._FLOW, "deliver", notice))
            return {
                "ok": False,
                "error": notice,
                "manual_import": True,
                "summary_id": summary_id,
                **rollup,
                "route": route,
                "patients": patients,
            }
        # A route resolved: the artifacts + the verified route plan ARE written,
        # but `migrate` executes no delivery route, so the honest verdict is
        # PREPARED, never delivered. Carry the outcome + the prepared notice on the
        # `done` event (and the return dict) so the wizard renders it truthfully —
        # "prepared, delivery not yet executed" — matching the CLI's exit-0 notice.
        notice = prepared_notice(status)
        done = done_event(self._FLOW, summary_id=summary_id, **rollup)
        done["outcome"] = status.outcome.value
        done["notice"] = notice
        self._emit(done)
        return {
            "ok": True,
            "outcome": status.outcome.value,
            "notice": notice,
            **rollup,
            "route": route,
            "patients": patients,
        }

    @staticmethod
    def _ccda_standard_patients(result: object) -> list[dict[str, object]]:
        """Per-patient roll-up for ccda-standard mode (no pipeline result).

        The standard-view render has no Jinja pack and thus no
        :class:`PipelineResult` to feed :func:`summarize_patients`, but the
        migration retains the canonical records, so the same per-patient detail
        is available here as in pack mode: display name, DOB, encounter count,
        and one C-CDA-view document per patient (this mode renders exactly one
        whole-patient PDF each). PHI: LOCAL display only — these values ride the
        return value / :meth:`last_run_summary`, never an event or a log.
        """
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

        A destination whose browser route is viable names a pack in the
        BROWSER option's ``requires``; we load it defensively to report
        ``ready`` (selectors discovered) vs ``needs-discovery``. Destinations
        with no browser pack return ``None`` — the wizard simply omits the
        readiness chip. Loud failures from the loader are swallowed into a
        diagnosis (type name), never raised.
        """
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
            return {"name": name, "ready": False, "diagnosis": exc_tag(exc)}
        return {
            "name": loaded.name,
            "ready": loaded.ready,
            "builtin": loaded.builtin,
        }

    def _fail(self, stage: str, exc: BaseException) -> dict[str, object]:
        """Convert a caught exception to the no-traceback error contract."""
        tag = exc_tag(exc)
        self._emit(error_event(self._FLOW, stage, tag))
        return {"ok": False, "error": tag}


def _failed_stage(message: str) -> str:
    """Best-effort: which rail stage a PipelineError belongs to (for the event).

    Maps the loud failure messages the pipeline core raises onto a rail name so
    the error banner can highlight the right card. Falls back to ``ingest`` (the
    earliest stage) for source/pack-resolution failures.
    """
    if message.startswith("QA failed"):
        return "qa"
    if "failed to render" in message:
        return "reconstruct"
    return "ingest"
