"""The shared EHR-to-EHR migration core (one migration, two frontends).

A migration is a general EHR→EHR move; the PF→Tebra path is just one instance
of it. The honest output model this realizes:

* **structured C-CDA is the primary cross-EHR payload** — the artifact the
  target EHR imports and renders natively (``deliver.ccda_export.deliver_ccda``);
* the **rendered PDF** is the human-readable archive/fallback, in an
  operator-chosen *representation* (a neutral SOAP pack, the HL7 standard C-CDA
  view, or a vendor Jinja skin).

So every migration emits BOTH: a ``ccda`` payload directory and a ``charts``
directory. The route the destination would take is resolved up front
(:func:`anastomosis.deliver.router.plan_route`) and surfaced to the operator as
a transit map — the same data the wizard draws.

This module is frontend-free (no Typer, no Rich, no webview), mirroring
:mod:`anastomosis.pipeline` / :mod:`anastomosis.core.commands`: it emits the
SAME PHI-safe :class:`~anastomosis.pipeline.StageEvent`\\ s the pipeline emits
(so each frontend's presenter works unchanged) and raises
:class:`~anastomosis.pipeline.PipelineError` on loud failures.

Three render modes resolve as:

* ``"neutral"`` → the built-in ``generic_soap`` Jinja pack (the neutral default);
* ``"ccda-standard"`` → the HL7-stylesheet standard C-CDA view, one PDF per
  patient (:func:`anastomosis.reconstruct.ccda_standard.render_ccda_standard`),
  with NO Jinja pack at all;
* any other string → a Jinja pack name (e.g. ``"practice_fusion_soap"``).

PHI rule: events/logs carry counts, stage names, ids, and exception type names
only — never patient-derived values. :class:`MigrationProfiles` stores config
(source/destination/render/sections/qa) only — never export paths, never PHI.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from anastomosis.core.commands import DeliveryOutcome
    from anastomosis.core.model import PatientRecord
    from anastomosis.deliver.browser.gates import RoutePlan, RunGates
    from anastomosis.deliver.ccda_export import CcdaExportResult
    from anastomosis.deliver.router import TransitMap
    from anastomosis.pipeline import EventSink, PipelineResult, StageEvent
    from anastomosis.reconstruct.ccda_standard import CCDARenderResult
    from anastomosis.reconstruct.engine import RenderedDoc
    from anastomosis.sources.base import SourceAdapter

__all__ = [
    "RENDER_CCDA_STANDARD",
    "RENDER_NEUTRAL",
    "MigrationCommand",
    "MigrationProfiles",
    "MigrationResult",
    "run_migration",
    "user_migrations_path",
]

# The two named render modes. Anything else is taken as a Jinja pack name.
RENDER_NEUTRAL = "neutral"
RENDER_CCDA_STANDARD = "ccda-standard"

# The Jinja pack the neutral mode renders through (the neutral default).
_NEUTRAL_PACK = "generic_soap"


@dataclass(frozen=True)
class MigrationCommand:
    """A fully-specified migration — the unit both frontends build.

    ``source`` (the ``--from`` adapter name) is REQUIRED: a migration is
    explicit, never auto-detected — the operator declares both ends.
    ``destination`` is the ``--to`` registry name (resolved to a transit map).
    ``render`` selects the human-readable representation: ``"neutral"``,
    ``"ccda-standard"``, or a Jinja pack name. ``export_dir`` / ``out_dir`` are
    per-run paths (NOT persisted to a profile).
    """

    export_dir: Path
    out_dir: Path
    source: str
    destination: str
    render: str = RENDER_NEUTRAL
    pack_dirs: tuple[Path, ...] = ()
    trust_new: bool = False
    force: bool = False
    sections: Mapping[str, bool] = field(default_factory=dict)
    qa: bool = True


@dataclass
class MigrationResult:
    """What a migration run yields the caller (the CLI and GUI frontends).

    ``ccda_export`` (the structured payload) is ALWAYS present — it is what the
    target EHR imports. ``pipeline`` is the full pipeline result in neutral/pack
    mode and ``None`` in ccda-standard mode; ``ccda_view`` is the standard-view
    render result in ccda-standard mode and ``None`` otherwise. ``pack`` is the
    resolved Jinja pack name, or ``None`` in ccda-standard mode.
    """

    transit: TransitMap
    pipeline: PipelineResult | None
    ccda_view: CCDARenderResult | None
    ccda_export: DeliveryOutcome
    render_mode: str
    pack: str | None
    # The canonical records processed (same list in both render modes), so a
    # frontend can show per-patient detail (names/DOB/note counts) in every mode.
    # Local display only — never logged or emitted on an event.
    records: list[PatientRecord]
    # The source ledger's reading of the load — one PHI-free sentence per line,
    # same field in both render modes (see ``pipeline.settle_source_ledger``).
    # Empty for a source that keeps no ledger.
    source_reading: tuple[str, ...] = ()


def _charts_dir(out_dir: Path) -> Path:
    return out_dir / "charts"


def _ccda_dir(out_dir: Path) -> Path:
    return out_dir / "ccda"


# --- shared migration helpers -----------------------------------------------
#
# Previously ``_run_ccda_standard`` hand-rolled output validation, source
# resolution + DETECT/INGEST event emission, and manifest writing — work
# ``run_pipeline_command`` already does for pack mode. The three helpers
# below own that work once so both modes share the same:
#
# * exit-code semantics for output validation (PipelineError, kind="bad_output");
# * adapter resolution + DETECT/INGEST event ORDER and SHAPE (the stage
#   contract a parity test pins);
# * upload manifest writing + the MANIFEST event count payload.
#
# ``_run_pack_mode`` reaches these emissions through ``run_pipeline_command``
# (which calls into ``pipeline.run_pipeline``); ``_run_ccda_standard`` reaches
# them through these helpers. The parity test
# ``test_migrate_pack_and_ccda_standard_share_stage_contract`` proves they
# stay in sync.


def _validate_outputs(targets: tuple[Path, ...]) -> None:
    """Pre-flight every output target, mapping path-collisions to exit 2.

    Mirrors :func:`run_pipeline_command`'s pre-flight: a target path that is
    actually a file (a stale leftover from a different run, an operator typo,
    a name collision) fails closed with a clean
    :class:`PipelineError` (``kind="bad_output"``) rather than a raw OSError
    deep inside the renderer or deliverer.
    """
    from anastomosis.core.output import OutputPathError, validate_output_target
    from anastomosis.pipeline import PipelineError

    for target in targets:
        try:
            validate_output_target(target)
        except OutputPathError as exc:
            raise PipelineError(str(exc), exit_code=2, kind="bad_output") from None


def _resolve_source_and_load(
    cmd: MigrationCommand, emit: Callable[[StageEvent], None]
) -> tuple[SourceAdapter, list[PatientRecord], tuple[str, ...]]:
    """Resolve the adapter and load records, emitting DETECT + INGEST.

    Mirrors the same two-step emission sequence ``pipeline.run_pipeline`` uses
    so a migration that does NOT route through the pack pipeline still tells
    the same CLI/GUI presenters the same story in the same order. The events'
    PHI-safety contract is preserved: DETECT carries only the adapter name,
    INGEST carries only a record count. The third element is the source
    ledger's reading of the load, settled here for the reason the quarantine
    is: both render modes must publish the same account of the same load.
    """
    from anastomosis.pipeline import (
        STAGE_DETECT,
        STAGE_INGEST,
        StageEvent,
        load_records,
        resolve_source,
        settle_quarantine,
        settle_source_ledger,
    )

    adapter = resolve_source(cmd.export_dir, cmd.source)
    emit(StageEvent(STAGE_DETECT, detail=adapter.name))
    records = load_records(adapter, cmd.export_dir)
    # Rows the adapter held back land in <out>/quarantine.json, beside the
    # charts/ and ccda/ folders, and their count rides the INGEST event —
    # the same settlement run_pipeline makes, for the same rail. The ledger
    # settles beside it: loss_ledger.json in the same folder.
    emit(
        StageEvent(
            STAGE_INGEST,
            counts={"records": len(records), **settle_quarantine(adapter, cmd.out_dir)},
        )
    )
    return adapter, records, settle_source_ledger(adapter, cmd.out_dir)


def _write_manifest_with_event(
    docs: list[RenderedDoc],
    records: list[PatientRecord],
    charts_dir: Path,
    emit: Callable[[StageEvent], None],
    *,
    pack: str | None,
    route: RoutePlan | None = None,
    gates: RunGates | None = None,
) -> None:
    """Persist the upload manifest next to the charts and emit MANIFEST.

    A migration intends to deliver, so the upload manifest is written by
    default (matching ``run_pipeline_command``'s ``write_manifest=True``
    posture for migrations). The event count payload — ``items`` — is the
    same shape ``run_pipeline_command`` emits, so a parity test on the stage
    contract sees identical payloads from both render modes.

    ``pack`` names the Jinja pack the charts were rendered through, so the later
    ``anast upload`` can run L3 against the header fields it declares. It is
    ``None`` in ccda-standard mode, where the HL7 stylesheet renders the view and
    no pack declares anything for L3 to check.

    ``route`` and ``gates`` are the bundle's reviewed context — the destination
    route this migration resolved, and what the run checked before writing the
    manifest. The pack-mode path reaches the same fields through
    ``core.commands._write_pipeline_manifest``; both modes must record them or
    an executor's refusal would depend on which representation was chosen.
    """
    from anastomosis.deliver.browser.persist import write_upload_manifest
    from anastomosis.pipeline import STAGE_MANIFEST, StageEvent

    write_upload_manifest(docs, records, charts_dir, pack=pack, route=route, gates=gates)
    emit(StageEvent(STAGE_MANIFEST, counts={"items": len(docs)}))


def run_migration(cmd: MigrationCommand, on_event: EventSink | None = None) -> MigrationResult:
    """Run a migration: resolve the route, render the charts, emit the C-CDA payload.

    The structured C-CDA payload lands in ``<out>/ccda``, the human-readable
    charts in ``<out>/charts``. Events and failures follow the module contract.
    """
    from anastomosis.deliver.router import plan_route
    from anastomosis.destinations.registry import DestinationRegistry
    from anastomosis.pipeline import PipelineError

    # Resolve the transit map up front. An unknown destination is an operator
    # typo (exit 2) — surface it as a clean PipelineError, never a traceback.
    try:
        transit = plan_route(cmd.destination, DestinationRegistry.load())
    except KeyError as exc:
        raise PipelineError(
            str(exc.args[0] if exc.args else exc), exit_code=2, kind="bad_destination"
        ) from None

    if cmd.render == RENDER_CCDA_STANDARD:
        return _run_ccda_standard(cmd, transit, on_event)
    return _run_pack_mode(cmd, transit, on_event)


def _run_pack_mode(
    cmd: MigrationCommand, transit: TransitMap, on_event: EventSink | None
) -> MigrationResult:
    """Neutral / Jinja-pack mode: the full pipeline + a ccda delivery.

    The chart representation is a Jinja pack (``generic_soap`` for ``"neutral"``,
    else the named pack), and the structured payload rides the standard ``ccda``
    deliverer — so this reuses :func:`run_pipeline_command` verbatim (locking,
    output validation, QA, event emission) rather than re-implementing it.
    """
    from anastomosis.core.commands import DeliveryCommand, PipelineCommand, run_pipeline_command
    from anastomosis.deliver.browser.gates import route_plan_of

    pack = _NEUTRAL_PACK if cmd.render == RENDER_NEUTRAL else cmd.render
    out = cmd.out_dir
    result = run_pipeline_command(
        PipelineCommand(
            export_dir=cmd.export_dir,
            charts_dir=_charts_dir(out),
            source=cmd.source,
            pack=pack,
            pack_dirs=cmd.pack_dirs,
            force=cmd.force,
            trust_new=cmd.trust_new,
            sections=cmd.sections,
            qa=cmd.qa,
            deliveries=(DeliveryCommand("ccda", _ccda_dir(out)),),
            # A migration intends to deliver, so the upload manifest is written
            # by default (it lands in <out>/charts alongside the chart PDFs).
            write_manifest=True,
            # The route resolved before any of this ran. Recording it here is
            # what makes it part of what was reviewed rather than a line that
            # scrolled past on the terminal.
            route=route_plan_of(transit),
        ),
        on_event=on_event,
    )
    return MigrationResult(
        transit=transit,
        pipeline=result.pipeline,
        ccda_view=None,
        ccda_export=result.deliveries["ccda"],
        render_mode=cmd.render,
        pack=pack,
        records=result.pipeline.records,
        source_reading=result.pipeline.source_reading,
    )


# --- standard-C-CDA-view QA -------------------------------------------------
#
# The neutral/pack path reaches QA through ``run_pipeline``'s ``_run_qa_stage``
# (one document per encounter, PLUS the record summaries that path renders). The
# standard-C-CDA-view path has no per-encounter documents — it renders ONE
# whole-patient PDF each — so it needs its own QA stage.
#
# HOW a whole-patient document is graded is not this module's business, and used
# to be: the check subset, the skip reasons, the DOB identity anchor and the
# ``carries=CHARTABLE_KINDS`` posture lived here, and the pack pipeline now
# renders the SAME view as its record summary. Two copies of that policy would
# drift, and the direction they drift is a document graded more leniently in one
# path than the other. It lives once in :mod:`anastomosis.qa.wholepatient`; this
# stage only decides WHICH documents and WHERE the report lands.


def _run_ccda_standard_qa(
    records: list[PatientRecord],
    charts: Path,
    emit: Callable[[StageEvent], None],
) -> bool | None:
    """Verify each whole-patient standard-C-CDA-view PDF — the ccda-standard
    counterpart of the pipeline's QA stage.

    Returns ``True`` when the report was written and OK, and ``None`` when the
    stage downgraded to a no-op — the same shape ``run_pipeline`` gives its
    caller, so the run manifest's QA gate can record what QA DID rather than
    what the operator asked for. A FAIL does not return: it raises.

    Mirrors ``pipeline._run_qa_stage``: write ``qa_report.json`` next to the PDFs,
    emit a ``STAGE_QA`` counts event, and raise :class:`PipelineError` (exit 1,
    ``kind="qa_failed"``) when the report is not OK. A missing PyMuPDF (the
    optional ``render`` extra) downgrades QA to a no-op with a skip event, exactly
    as the neutral path does.

    The grading itself — document-generic checks run, encounter-scoped checks
    recorded as skipped-with-reason, every chartable kind declared carried — is
    :func:`anastomosis.qa.whole_patient_report`, shared with the record summaries
    the pack pipeline writes into every bundle.
    """
    from anastomosis.pipeline import STAGE_QA, StageEvent, settle_qa
    from anastomosis.reconstruct.ccda_standard import ccda_standard_doc_path

    try:
        from anastomosis.qa import whole_patient_report
    except ImportError as exc:
        if exc.name != "pymupdf":  # only the optional dependency may downgrade QA
            raise
        emit(StageEvent(STAGE_QA, detail="skipped: install anastomosis[render] for PyMuPDF"))
        return None

    report = whole_patient_report(
        (ccda_standard_doc_path(charts, record), record) for record in records
    )
    settle_qa(report, charts, emit)  # raises on a FAIL, so reaching here IS the pass
    return True


def _ccda_counts(result: CcdaExportResult) -> dict[str, int]:
    """The C-CDA outcome an operator reads, including its shape.

    Bytes ride beside the patient counts because this document is the one
    artifact handed to somebody else's EHR: its size is that EHR's problem, and
    the share of it that is preserved source fields rather than clinical
    content decides what a physician sees when it opens. Both were invisible
    until the destination refused a file (#118).
    """
    return {
        "patients": len(result.paths),
        "missing": result.missing_count,
        "bytes": result.total_bytes,
        "preserved_bytes": result.preserved_bytes,
        "largest_bytes": result.largest_bytes,
    }


def _run_ccda_standard(
    cmd: MigrationCommand, transit: TransitMap, on_event: EventSink | None
) -> MigrationResult:
    """Standard-C-CDA-view mode: no Jinja pack — render HL7's own view per patient.

    Composes the three shared helpers: output pre-flight, source resolution +
    DETECT/INGEST emission, and manifest writing + MANIFEST emission — the
    same primitives :func:`run_pipeline_command` uses for pack mode. Only
    the *render* (HL7 standard view, no Jinja) and the *delivery* (the
    structured C-CDA payload) are mode-specific. The stage contract that
    both modes emit is pinned by the parity test
    ``test_migrate_pack_and_ccda_standard_share_stage_contract``.
    """
    from contextlib import ExitStack

    from anastomosis.core.commands import DeliveryOutcome
    from anastomosis.core.locking import OutputLockedError, output_lock
    from anastomosis.deliver.browser.gates import RunGates, route_plan_of
    from anastomosis.deliver.ccda_export import deliver_ccda
    from anastomosis.pipeline import PipelineError
    from anastomosis.reconstruct.ccda_standard import (
        ccda_standard_doc_path,
        render_ccda_standard,
    )
    from anastomosis.reconstruct.engine import RenderedDoc

    emit = on_event or (lambda _event: None)
    out = cmd.out_dir
    charts = _charts_dir(out)
    ccda = _ccda_dir(out)

    # Pre-flight BOTH output targets — shared helper, same exit-2 semantics.
    _validate_outputs((charts, ccda))

    try:
        # Lock BOTH output dirs (deadlock-free sorted order), so a concurrent
        # run sharing either the charts or the ccda dir cannot interleave writes.
        with ExitStack() as stack:
            # Dedup on the resolved path (defensive; charts/ccda are distinct
            # subdirs here, but match run_pipeline_command's no-self-deadlock rule).
            for target in sorted({charts.resolve(), ccda.resolve()}):
                stack.enter_context(output_lock(target))

            # Resolve source + emit DETECT + load + emit INGEST — same shape
            # ``pipeline.run_pipeline`` emits for pack mode.
            _adapter, records, source_reading = _resolve_source_and_load(cmd, emit)

            view = render_ccda_standard(records, charts, force=cmd.force)
            if view.failed:
                # Loud render failure, mirroring the pipeline's render_failed
                # kind so the CLI reproduces its per-patient detail lines.
                raise PipelineError(
                    f"{len(view.failed)} patient(s) failed to render",
                    exit_code=1,
                    kind="render_failed",
                    failed=tuple(view.failed),
                )

            # Verify every rendered whole-patient view before delivering — the
            # ccda-standard counterpart to the pipeline's QA stage. A QA FAIL
            # aborts HERE (exit 1), before the manifest and the ccda payload are
            # written, exactly as run_pipeline's QA stage precedes delivery.
            qa_ok: bool | None = None
            if cmd.qa and view.documents:
                qa_ok = _run_ccda_standard_qa(records, charts, emit)

            # Write the upload manifest by default (a migration intends to
            # deliver). The whole-patient view has no RenderedDoc list, so the
            # path:patient association is recovered from the records via the
            # renderer's public allocator; the per-patient view has no encounter,
            # so the patient id stands in for the item_key's encounter slot — and
            # therefore no date of service, which is the truth about a
            # whole-patient document rather than a lost field.
            manifest_docs = [
                RenderedDoc(
                    path=ccda_standard_doc_path(charts, record),
                    encounter_id=record.patient.id,
                    patient_id=record.patient.id,
                )
                for record in records
            ]
            _write_manifest_with_event(
                manifest_docs,
                records,
                charts,
                emit,
                pack=None,
                route=route_plan_of(transit),
                # No Jinja layout produced these pages — HL7's own stylesheet
                # did — so there is no layout hash to record, and saying so is
                # the honest answer rather than a gap. The QA verdict comes
                # from what QA DID, never from the flag that asked for it: the
                # stage downgrades to a no-op when PyMuPDF is absent, and
                # reading the flag recorded `pass` for whole-patient views
                # nothing had graded — on the one machine least able to notice.
                gates=RunGates.from_run(qa_ok=qa_ok, layout_hash=None),
            )

            ccda_result = deliver_ccda(records, ccda)
    except OutputLockedError as exc:
        raise PipelineError(str(exc), exit_code=2, kind="output_locked") from None

    ccda_export = DeliveryOutcome(
        kind="ccda",
        out_dir=ccda,
        counts=_ccda_counts(ccda_result),
    )
    return MigrationResult(
        transit=transit,
        pipeline=None,
        ccda_view=view,
        ccda_export=ccda_export,
        render_mode=cmd.render,
        pack=None,
        records=records,
        source_reading=source_reading,
    )


# --- profile persistence ------------------------------------------------------
#
# A migration profile saves the REUSABLE config of a migration — source,
# destination, render representation, section flags, QA — so an operator runs a
# recurring migration by name. It deliberately does NOT save the per-run paths
# (export_dir / out_dir), and it carries no PHI: every value is a vendor
# identifier, a pack/render name, a section flag, or a boolean.


def user_migrations_path() -> Path:
    """The per-user migration-profiles store path.

    A plain ``~/.anastomosis/migrations.json`` (NOT ``platformdirs`` — no new
    dependency), matching
    :func:`anastomosis.destinations.loader.user_destinations_dir` and
    :func:`anastomosis.reconstruct.packtrust.user_pack_trust_path` so all
    Anastomosis user state lives under one root.
    """
    return Path.home() / ".anastomosis" / "migrations.json"


# The config keys a profile carries — config only, never paths, never PHI.
_PROFILE_KEYS: tuple[str, ...] = ("source", "destination", "render", "sections", "qa")


class MigrationProfiles:
    """A JSON store of named migration profiles (config only — no paths, no PHI).

    The store is ``{"<name>": {"source", "destination", "render", "sections",
    "qa"}}``. Mirrors :class:`anastomosis.reconstruct.packtrust.PackTrust`:
    defensive load (a missing or garbage store starts empty), atomic write, and
    owner-only (``0o600``) on POSIX.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._store: dict[str, dict[str, object]] = {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Missing or garbage store → start empty (a corrupt profile file
            # simply offers no profiles; it never crashes a run).
            return
        if isinstance(data, dict):
            self._store = {
                str(name): dict(profile)
                for name, profile in data.items()
                if isinstance(profile, dict)
            }

    def get(self, name: str) -> dict[str, object] | None:
        """Return the profile named ``name`` (a copy), or ``None`` if absent."""
        profile = self._store.get(name)
        return dict(profile) if profile is not None else None

    def names(self) -> list[str]:
        """Sorted profile names."""
        return sorted(self._store)

    def save(self, name: str, profile: dict[str, object]) -> None:
        """Persist ``profile`` under ``name`` and write the store.

        Only the config keys (:data:`_PROFILE_KEYS`) are stored — any stray
        keys (e.g. a path) are dropped, keeping the store PHI-free by
        construction. The write is atomic (a temp file is written then
        ``os.replace``\\ d into place, so a crash mid-write never corrupts an
        existing store) and owner-only from creation on POSIX (the temp is
        opened ``0o600``, leaving no umask-mode window).
        """
        self._store[name] = {key: profile[key] for key in _PROFILE_KEYS if key in profile}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._store, indent=2, sort_keys=True) + "\n"
        tmp = self._path.with_name(f".{self._path.name}.{os.getpid()}.tmp")
        try:
            if os.name == "posix":
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
            else:  # pragma: no cover - POSIX is the tested platform
                tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self._path)
        except BaseException:
            tmp.unlink(missing_ok=True)  # never leave a stray temp on failure
            raise


def default_migration_profiles() -> MigrationProfiles:
    """The :class:`MigrationProfiles` backed by :func:`user_migrations_path`."""
    return MigrationProfiles(user_migrations_path())
