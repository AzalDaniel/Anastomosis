"""The shared EHR-to-EHR migration core, frontend-free like
:mod:`anastomosis.pipeline`/:mod:`anastomosis.core.commands`. Every
migration emits BOTH the structured C-CDA the target EHR imports
(``deliver.ccda_export.deliver_ccda``) and a rendered-PDF representation
(neutral pack, HL7 standard view, or vendor Jinja skin), plus a
``run_manifest.json`` binding it to profile hashes (53). The route is
resolved up front (:func:`anastomosis.deliver.router.plan_route`) as a
transit map. PHI (2): events carry counts/stage names/ids/exception types
only; :class:`MigrationProfiles` stores config only, never a path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from anastomosis.core.atomic import atomic_write_text

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from anastomosis.core.commands import DeliveryOutcome
    from anastomosis.core.model import PatientRecord
    from anastomosis.core.profiles import RunBinding
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
    "bind_migration",
    "resolve_pack",
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
    """A fully-specified migration, the unit both frontends build.
    ``source``/``destination`` are required — a migration is explicit,
    never auto-detected. ``render`` selects ``"neutral"``,
    ``"ccda-standard"``, or a Jinja pack name."""

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
    #: Re-bind an output folder whose recorded profile hashes disagree with
    #: this machine. Off by default and separate from ``force`` (overwrites
    #: RENDERS): replacing the profiles a folder's artifacts were made under
    #: is a statement that those artifacts are stale.
    rebind: bool = False


@dataclass
class MigrationResult:
    """What a migration run yields the caller. ``ccda_export`` is ALWAYS
    present; ``pipeline``/``ccda_view``/``pack`` are mutually exclusive
    with ccda-standard mode, ``None`` on whichever side did not run."""

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
# The three helpers below own output validation, source resolution +
# DETECT/INGEST emission, and manifest writing + the MANIFEST event once, so
# ``_run_pack_mode`` (via ``run_pipeline_command``) and ``_run_ccda_standard``
# (directly) share the same exit-code semantics and event order/shape. The
# parity test ``test_migrate_pack_and_ccda_standard_share_stage_contract``
# proves they stay in sync.


def _validate_outputs(targets: tuple[Path, ...]) -> None:
    """Pre-flight every output target, mapping a path-collision to a clean
    exit-2 :class:`PipelineError` (``kind="bad_output"``) rather than a
    raw OSError deep inside the renderer or deliverer."""
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
    """Resolve the adapter and load records, emitting DETECT + INGEST in
    the same order ``pipeline.run_pipeline`` does (2): DETECT carries only
    the adapter name, INGEST only a record count. The third element is
    the source ledger's reading, settled here so both render modes
    publish the same account of the same load."""
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
    """Persist the upload manifest and emit MANIFEST (2), written by
    default since a migration intends to deliver. ``pack`` is ``None`` in
    ccda-standard mode; ``route``/``gates`` are the reviewed context so an
    executor's refusal never depends on the representation chosen. The
    count comes from what the writer WROTE, not the input (#374)."""
    from anastomosis.deliver.browser.persist import write_upload_manifest
    from anastomosis.pipeline import STAGE_MANIFEST, StageEvent

    written = write_upload_manifest(docs, records, charts_dir, pack=pack, route=route, gates=gates)
    emit(StageEvent(STAGE_MANIFEST, counts={"items": written.items}))


def resolve_pack(render: str) -> str | None:
    """The Jinja pack a render mode resolves to, or ``None`` for no pack:
    one definition shared by the pack-mode run, the layout profile, and
    the run manifest. ``ccda-standard`` is a truthful ``None``, not a
    gap."""
    if render == RENDER_CCDA_STANDARD:
        return None
    return _NEUTRAL_PACK if render == RENDER_NEUTRAL else render


def _bind_run(cmd: MigrationCommand) -> RunBinding:
    """Capture the three profiles this run is about to be prepared under
    (53); pack discovery mirrors ``run_pipeline``'s exactly. ``trust_new``
    is deliberately NOT passed: profiling records nothing. An unknown
    source/destination is a clean exit-2 :class:`PipelineError`."""
    from anastomosis.core.profiles import ProfileError, capture_binding
    from anastomosis.pipeline import PipelineError
    from anastomosis.reconstruct.packtrust import default_pack_trust

    dirs = list(cmd.pack_dirs)
    try:
        return capture_binding(
            source=cmd.source,
            destination=cmd.destination,
            render_mode=cmd.render,
            pack=resolve_pack(cmd.render),
            pack_dirs=dirs,
            allow_external=bool(dirs),
            trust=default_pack_trust(),
        )
    except ProfileError as exc:
        raise PipelineError(str(exc), exit_code=2, kind="bad_binding") from None


def _refuse_destination_mismatch(binding: RunBinding) -> None:
    """Refuse a mapping taught for one destination being run at another
    (32), naming both ends. The second refusal is the same destination
    whose profile CHANGED since teaching (a version bump, a new
    capability): same name, different system."""
    from anastomosis.pipeline import PipelineError

    source = binding.source
    taught = source.taught_for_destination
    if taught is None:
        return  # unbound mapping — taught with no destination in view
    destination = binding.destination
    if taught != destination.name:
        raise PipelineError(
            f"source {source.name!r} was taught for destination {taught!r} and this run "
            f"targets {destination.name!r} — refusing rather than mapping columns for one "
            f"system into another. Teach a mapping for {destination.name!r}, or migrate "
            f"to {taught!r}.",
            exit_code=2,
            kind="destination_mismatch",
        )
    if source.taught_for_destination_hash != destination.profile_hash:
        raise PipelineError(
            f"destination {destination.name!r} has changed since source {source.name!r} was "
            f"taught for it (taught under {str(source.taught_for_destination_hash)[:12]}, now "
            f"{destination.profile_hash[:12]}, version {destination.version!r}) — re-teach the "
            f"mapping against the destination as it stands.",
            exit_code=2,
            kind="destination_mismatch",
        )


def _refuse_stale_folder(cmd: MigrationCommand, binding: RunBinding) -> None:
    """Refuse to re-run into a folder bound to profiles that have since
    changed (53): writing a second run's artifacts under DIFFERENT inputs
    would leave one tree that is two runs with nothing saying which file
    came from which. ``--rebind`` is the explicit override."""
    from anastomosis.core.runmanifest import (
        BindingError,
        RunManifestError,
        load_run_manifest,
        verify_binding,
    )
    from anastomosis.pipeline import PipelineError

    if cmd.rebind:
        return
    try:
        manifest = load_run_manifest(cmd.out_dir)
    except RunManifestError as exc:
        raise PipelineError(str(exc), exit_code=2, kind="bad_binding") from None
    if manifest is None:
        return  # a fresh folder, or one prepared before run manifests existed
    try:
        verify_binding(manifest, binding)
    except BindingError as exc:
        raise PipelineError(
            f"{exc} Pass --rebind to prepare this folder again under the current profiles.",
            exit_code=2,
            kind="binding_changed",
        ) from None


def bind_migration(cmd: MigrationCommand) -> RunBinding:
    """Capture this run's binding and make every refusal that precedes
    work (53): an unknown source, a mismatched destination, a stale output
    folder. Public so a frontend can refuse before printing the transit
    map. :func:`run_migration` calls this itself unless a caller already
    has, and passes the binding back to avoid a second capture."""
    binding = _bind_run(cmd)
    _refuse_destination_mismatch(binding)
    _refuse_stale_folder(cmd, binding)
    return binding


def _write_run_manifest(cmd: MigrationCommand, binding: RunBinding) -> None:
    """Record what this run was prepared under, beside its artifacts.
    ``prepared`` is the only state a migration writes (53); advancing
    past it needs a receipt (:func:`anastomosis.core.runmanifest.advance_state`)."""
    from anastomosis import __version__
    from anastomosis.core.runmanifest import RunManifest, export_dir_id, write_run_manifest

    write_run_manifest(
        cmd.out_dir,
        RunManifest(
            pipeline_version=__version__,
            source=cmd.source,
            destination=cmd.destination,
            render_mode=cmd.render,
            export_dir_id=export_dir_id(cmd.export_dir),
            binding=binding,
        ),
    )


def run_migration(
    cmd: MigrationCommand,
    on_event: EventSink | None = None,
    *,
    binding: RunBinding | None = None,
) -> MigrationResult:
    """Run a migration: resolve the route, render, emit the C-CDA payload
    plus ``run_manifest.json`` (53). Every binding refusal happens BEFORE
    anything is written (:func:`bind_migration`); ``binding`` lets a
    frontend that already refused avoid capturing twice."""
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

    bound = binding if binding is not None else bind_migration(cmd)

    if cmd.render == RENDER_CCDA_STANDARD:
        result = _run_ccda_standard(cmd, transit, on_event)
    else:
        result = _run_pack_mode(cmd, transit, on_event)
    _write_run_manifest(cmd, bound)
    return result


def _run_pack_mode(
    cmd: MigrationCommand, transit: TransitMap, on_event: EventSink | None
) -> MigrationResult:
    """Neutral / Jinja-pack mode: the full pipeline plus a ccda delivery,
    reusing :func:`run_pipeline_command` verbatim (locking, output
    validation, QA, event emission) rather than re-implementing it."""
    from anastomosis.core.commands import DeliveryCommand, PipelineCommand, run_pipeline_command
    from anastomosis.deliver.browser.gates import route_plan_of

    pack = resolve_pack(cmd.render)
    # Every mode reaching here resolves to a pack name (ccda-standard routes
    # elsewhere). Asserted rather than defaulted: a silent fallback here would
    # render through a layout nobody chose.
    assert pack is not None
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
# The standard-C-CDA-view path renders ONE whole-patient PDF per patient, not
# one per encounter, so it needs its own QA stage. HOW a whole-patient document
# is graded is not this module's business: that policy lives once in
# :mod:`anastomosis.qa.wholepatient`, shared with the pack pipeline's record
# summaries, so the two paths cannot drift into grading one more leniently.
# This stage only decides WHICH documents and WHERE the report lands.


def _run_ccda_standard_qa(
    view: CCDARenderResult,
    charts: Path,
    emit: Callable[[StageEvent], None],
) -> bool | None:
    """Contract: verify each whole-patient standard-C-CDA-view PDF.
    ``True`` when the report was OK, ``None`` when downgraded to a no-op
    (missing PyMuPDF); a FAIL raises. Grades ``view.by_path`` directly
    (#383), never a per-record re-derivation, since two records can share
    one rendered file."""
    from anastomosis.pipeline import STAGE_QA, StageEvent, settle_qa

    try:
        from anastomosis.qa import whole_patient_report
    except ImportError as exc:
        if exc.name != "pymupdf":  # only the optional dependency may downgrade QA
            raise
        emit(StageEvent(STAGE_QA, detail="skipped: install anastomosis[render] for PyMuPDF"))
        return None

    report = whole_patient_report(view.by_path.items())
    settle_qa(report, charts, emit)  # raises on a FAIL, so reaching here IS the pass
    return True


def _ccda_counts(result: CcdaExportResult) -> dict[str, int]:
    """The C-CDA outcome an operator reads, including its shape: bytes
    ride beside patient counts (#118) because this document goes to
    somebody else's EHR, and its size and preserved-vs-clinical-content
    share are that EHR's problem before they refuse it."""
    return {
        "patients": len(result.paths),
        "missing": result.missing_count,
        "bytes": result.total_bytes,
        "preserved_bytes": result.preserved_bytes,
        "largest_bytes": result.largest_bytes,
        "documents": result.artifact_count,
    }


def _run_ccda_standard(
    cmd: MigrationCommand, transit: TransitMap, on_event: EventSink | None
) -> MigrationResult:
    """Standard-C-CDA-view mode: no Jinja pack, HL7's own view per
    patient. Composes the same shared helpers pack mode uses (pre-flight,
    DETECT/INGEST, manifest writing); only render and delivery are
    mode-specific. The shared stage contract is pinned by
    ``test_migrate_pack_and_ccda_standard_share_stage_contract``."""
    from contextlib import ExitStack

    from anastomosis.core.commands import DeliveryOutcome
    from anastomosis.core.locking import OutputLockedError, output_lock
    from anastomosis.deliver.browser.gates import RunGates, route_plan_of
    from anastomosis.deliver.ccda_export import ArtifactNotDelivered, deliver_ccda
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
                qa_ok = _run_ccda_standard_qa(view, charts, emit)

            # Write the upload manifest by default. The whole-patient view has
            # no RenderedDoc list, so path:patient is recovered from the
            # records; the patient id stands in for the item_key's encounter
            # slot, since a whole-patient document truthfully has no DOS.
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
                # No Jinja layout produced these pages, so no layout hash; the
                # QA verdict comes from what QA actually did (qa_ok), never
                # from the flag that asked for it.
                gates=RunGates.from_run(qa_ok=qa_ok, layout_hash=None),
            )

            # The export directory is where this mode's source documents
            # still live — it renders the HL7 standard view rather than
            # running the pipeline's attachment carry — so it is what the
            # deliverer copies them out of. Same conservation rule as the
            # pipeline route, one function enforcing it (#373).
            ccda_result = deliver_ccda(records, ccda, artifacts_dir=cmd.export_dir)
    except OutputLockedError as exc:
        raise PipelineError(str(exc), exit_code=2, kind="output_locked") from None
    except ArtifactNotDelivered as exc:
        # Same refusal the pipeline route makes, at the same exit code: a
        # delivery that cannot carry a document the record names stops rather
        # than reporting a migration the documents are missing from (#373).
        raise PipelineError(str(exc), exit_code=1, kind="artifact_missing") from None

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
# recurring migration by name.


def user_migrations_path() -> Path:
    """``~/.anastomosis/migrations.json``, matching
    :func:`anastomosis.destinations.loader.user_destinations_dir` so all
    Anastomosis user state lives under one root."""
    return Path.home() / ".anastomosis" / "migrations.json"


# The config keys a profile carries — config only, never paths, never PHI.
_PROFILE_KEYS: tuple[str, ...] = ("source", "destination", "render", "sections", "qa")


class MigrationProfiles:
    """A JSON store of named migration profiles (2: config only, no
    paths). Mirrors :class:`anastomosis.reconstruct.packtrust.PackTrust`:
    defensive load, atomic write, owner-only (``0o600``) on POSIX."""

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
        """Persist ``profile`` under ``name``: only the config keys
        (:data:`_PROFILE_KEYS`) are stored, any stray key dropped, keeping the
        store PHI-free (2); the write is atomic and owner-only on POSIX (14)."""
        self._store[name] = {key: profile[key] for key in _PROFILE_KEYS if key in profile}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._store, indent=2, sort_keys=True) + "\n"
        atomic_write_text(self._path, payload, mode=0o600)


def default_migration_profiles() -> MigrationProfiles:
    """The :class:`MigrationProfiles` backed by :func:`user_migrations_path`."""
    return MigrationProfiles(user_migrations_path())
