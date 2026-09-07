"""The shared application/command layer: the CLI and the GUI are adapters
that parse operator intent, build one of the command objects here, and
present the :class:`CommandResult`. All orchestration policy lives here
once. :func:`run_pipeline_command` wraps the pipeline core and runs
requested deliveries through :func:`deliver_outputs`, which returns
structured :class:`DeliveryOutcome`\\ s and never prints or emits.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping

    from anastomosis.deliver.browser.gates import RoutePlan
    from anastomosis.pipeline import EventSink, PipelineResult

__all__ = [
    "CommandResult",
    "DeliveryCommand",
    "DeliveryKind",
    "DeliveryOutcome",
    "PackInfo",
    "PatientSummary",
    "PipelineCommand",
    "SourceInfo",
    "ToolkitInfo",
    "deliver_outputs",
    "get_toolkit_info",
    "run_pipeline_command",
    "summarize_patients",
]

DeliveryKind = Literal["archive", "bundle", "ccda"]

# The canonical deliverer order — archive, then bundle, then ccda — shared by
# both frontends so CLI line order and GUI per-deliverer event order agree.
_DELIVERY_ORDER: dict[str, int] = {"archive": 0, "bundle": 1, "ccda": 2}


@dataclass(frozen=True)
class DeliveryCommand:
    """One requested delivery: a kind and the directory it writes into.
    The adapter chooses ``out_dir`` — the CLI an operator-given path, the
    GUI a sibling subdirectory of the run's output dir."""

    kind: DeliveryKind
    out_dir: Path


@dataclass(frozen=True)
class PipelineCommand:
    """A fully-specified pipeline run — the unit both frontends build."""

    export_dir: Path
    charts_dir: Path
    source: str | None = None
    pack: str = "generic_soap"
    pack_dirs: tuple[Path, ...] = ()
    force: bool = False
    # Trust-on-first-use for --pack-dir packs: record (and trust) their current
    # code hash. Required the first time and again after their code changes.
    trust_new: bool = False
    sections: Mapping[str, bool] = field(default_factory=dict)
    # The source's render-selection rules this run does NOT apply, so the
    # encounters they would keep out of the render are rendered instead. Empty
    # is every rule applied — what the adapters did when the rules were
    # constants — so an existing run is unchanged.
    include: tuple[str, ...] = ()
    qa: bool = True
    deliveries: tuple[DeliveryCommand, ...] = ()
    # Opt-in: after a successful render (and QA), write the upload manifest
    # (<charts_dir>/upload_manifest.json) so a later `anast upload` can drive the
    # browser engine without re-running the pipeline. Off by default, so an
    # existing manifest-off run is byte-identical (the event/line is additive).
    write_manifest: bool = False
    # The destination route plan this run is preparing for, when it has one — a
    # migration resolves the transit map before rendering. It rides into the
    # upload manifest so the route that was reviewed is the route an executor
    # can be held to. None for a plain render, which names no destination.
    route: RoutePlan | None = None


@dataclass(frozen=True)
class DeliveryOutcome:
    """What one deliverer produced — structured, presentation-free.

    ``counts`` always carries ``"patients"``; the archive deliverer adds
    ``"encounters"`` and ``"pdfs"`` (the CLI's archive line reports all three).
    """

    kind: DeliveryKind
    out_dir: Path
    counts: dict[str, int]


@dataclass
class CommandResult:
    """The result of a :class:`PipelineCommand`: the pipeline state plus the
    per-kind delivery outcomes (empty when no deliveries were requested)."""

    pipeline: PipelineResult
    deliveries: dict[str, DeliveryOutcome]


@dataclass(frozen=True)
class PatientSummary:
    """A per-patient roll-up of a completed run, for LOCAL display only
    (2): unlike :class:`DeliveryOutcome`, this carries PHI
    (``display_name``, ``birth_date``) for direct on-screen display and
    must never be emitted as a progress event or logged."""

    patient_id: str
    display_name: str
    birth_date: str | None  # ISO-8601 date, or None when the source lacked one
    encounters: int
    documents: int


def summarize_patients(result: PipelineResult) -> list[PatientSummary]:
    """Per-patient roll-up (name, DOB, #encounters, #rendered docs), in
    ingest order (2): carries PHI for LOCAL display only, callers must
    never log it or put it on an event."""
    docs_by_patient: dict[str, int] = {}
    for doc in result.render_result.documents:
        docs_by_patient[doc.patient_id] = docs_by_patient.get(doc.patient_id, 0) + 1
    summaries: list[PatientSummary] = []
    for record in result.records:
        patient = record.patient
        summaries.append(
            PatientSummary(
                patient_id=patient.id,
                display_name=patient.display_name,
                birth_date=patient.birth_date.isoformat() if patient.birth_date else None,
                encounters=len(record.encounters),
                documents=docs_by_patient.get(patient.id, 0),
            )
        )
    return summaries


def deliver_outputs(
    result: PipelineResult,
    charts_dir: Path,
    deliveries: tuple[DeliveryCommand, ...],
) -> dict[str, DeliveryOutcome]:
    """Run the requested deliverers once, in canonical order; return
    outcomes. No printing, no events — the adapter presents them. Each
    deliverer reads the rendered chart PDFs out of ``charts_dir``."""
    from anastomosis.core.output import OutputPathError, validate_output_target
    from anastomosis.deliver.ccda_export import ArtifactNotDelivered
    from anastomosis.pipeline import PipelineError

    ordered = sorted(deliveries, key=lambda d: _DELIVERY_ORDER[d.kind])
    # Pre-flight every delivery directory before invoking any deliverer, so a
    # path that is actually a file fails cleanly (exit 2) instead of raising a
    # raw OSError from inside a deliverer.
    for dc in ordered:
        try:
            validate_output_target(dc.out_dir)
        except OutputPathError as exc:
            raise PipelineError(str(exc), exit_code=2, kind="bad_output") from None
    outcomes: dict[str, DeliveryOutcome] = {}
    try:
        _run_deliveries(result, charts_dir, ordered, outcomes)
    except ArtifactNotDelivered as exc:
        # A delivery that cannot carry a document the record names is a run
        # that must stop, not a shortfall to print under a success line: the
        # operator would otherwise hand a receiving EHR a chart pointing at a
        # file that is not there. Exit 1, the code the pipeline already uses
        # when a stage cannot account for what it was given (#373).
        raise PipelineError(str(exc), exit_code=1, kind="artifact_missing") from None
    return outcomes


def _run_deliveries(
    result: PipelineResult,
    charts_dir: Path,
    ordered: list[DeliveryCommand],
    outcomes: dict[str, DeliveryOutcome],
) -> None:
    """Invoke each deliverer in canonical order, filling ``outcomes`` in place."""
    for dc in ordered:
        if dc.kind == "archive":
            from anastomosis.deliver.archive import ArchiveDeliverer

            arc = ArchiveDeliverer().deliver(
                result.records, charts_dir, dc.out_dir, qa_report=result.qa_report
            )
            outcomes["archive"] = DeliveryOutcome(
                kind="archive",
                out_dir=arc.out_dir,
                counts={
                    "patients": arc.patient_count,
                    "encounters": arc.encounter_count,
                    "pdfs": arc.pdf_count,
                    # What did NOT arrive. Both were already known at the point
                    # the deliverer discovered them and went no further than a
                    # log line; a frontend cannot report a number it is not given.
                    "missing": arc.missing_count,
                    "unattributed": arc.unattributed_count,
                },
            )
        elif dc.kind == "bundle":
            from anastomosis.deliver.bundle import BundleDeliverer

            # Single-pass per-patient attribution, not an O(patients x pdfs)
            # re-filter of every chart for every patient.
            written = BundleDeliverer().deliver_records(
                result.records, charts_dir, dc.out_dir, qa_report=result.qa_report
            )
            outcomes["bundle"] = DeliveryOutcome(
                kind="bundle",
                out_dir=dc.out_dir,
                counts={
                    "patients": len(written),
                    "missing": sum(w.missing_count for w in written),
                },
            )
        elif dc.kind == "ccda":
            from anastomosis.deliver.ccda_export import deliver_ccda
            from anastomosis.pipeline import ATTACHMENTS_DIRNAME

            # The run already put every source document the records name into
            # charts/attachments (and refused if any did not arrive), so this
            # is where the deliverer copies them from. Without it the C-CDA
            # deliverable was the one destination that got a patient's chart
            # with none of their documents on it (#373).
            ccda = deliver_ccda(
                result.records, dc.out_dir, artifacts_dir=charts_dir / ATTACHMENTS_DIRNAME
            )
            outcomes["ccda"] = DeliveryOutcome(
                kind="ccda",
                out_dir=dc.out_dir,
                # Bytes beside the counts: this document goes to somebody
                # else's EHR, so its size and how much of it is preserved
                # source fields rather than clinical content are the
                # operator's business before the destination makes them so.
                counts={
                    "patients": len(ccda.paths),
                    "missing": ccda.missing_count,
                    "bytes": ccda.total_bytes,
                    "preserved_bytes": ccda.preserved_bytes,
                    "largest_bytes": ccda.largest_bytes,
                    "documents": ccda.artifact_count,
                },
            )


def run_pipeline_command(cmd: PipelineCommand, on_event: EventSink | None = None) -> CommandResult:
    """Run a :class:`PipelineCommand`: ingest → reconstruct → optional QA →
    optional upload-manifest → requested deliveries. Raises
    :class:`anastomosis.pipeline.PipelineError` on any loud failure (the adapter
    maps it to its exit code / error event)."""
    from contextlib import ExitStack

    from anastomosis.core.locking import OutputLockedError, output_lock
    from anastomosis.core.output import OutputPathError, validate_output_target
    from anastomosis.pipeline import PipelineError, run_pipeline

    section_args = [f"{k}={'on' if v else 'off'}" for k, v in sorted(cmd.sections.items())]
    # Validate EVERY output target (charts + each delivery dir) BEFORE acquiring
    # any lock (the lock creates the directory): a path that is actually a file
    # stays a clean exit 2 rather than a raw OSError from the lock's mkdir.
    targets = [cmd.charts_dir, *(dc.out_dir for dc in cmd.deliveries)]
    for target in targets:
        try:
            validate_output_target(target)
        except OutputPathError as exc:
            raise PipelineError(str(exc), exit_code=2, kind="bad_output") from None
    try:
        # Lock charts AND every delivery dir (deadlock-free sorted order), so two
        # concurrent runs sharing any output dir (e.g. the same --ccda dir) cannot
        # interleave writes — only charts_dir was held before.
        with ExitStack() as stack:
            # Dedup on the RESOLVED path so one physical dir addressed two ways
            # (charts_dir == a delivery dir via relative-vs-absolute or a symlink)
            # locks ONCE — locking the same lock file twice would self-deadlock a
            # single run. Writes still use the raw paths; validation already ran.
            for target in sorted({t.resolve() for t in targets}):
                stack.enter_context(output_lock(target))
            result = run_pipeline(
                export_dir=cmd.export_dir,
                out=cmd.charts_dir,
                source=cmd.source,
                pack=cmd.pack,
                pack_dirs=list(cmd.pack_dirs) or None,
                force=cmd.force,
                section=section_args,
                qa=cmd.qa,
                trust_new=cmd.trust_new,
                include=list(cmd.include),
                on_event=on_event,
            )
            # Opt-in upload manifest: written after a successful render (and QA),
            # before deliveries, so `anast upload` can drive the browser engine
            # later. Additive — the event fires ONLY when requested.
            if cmd.write_manifest:
                _write_pipeline_manifest(
                    result, cmd.charts_dir, cmd.pack, on_event, route=cmd.route
                )
            deliveries = deliver_outputs(result, cmd.charts_dir, cmd.deliveries)
            return CommandResult(pipeline=result, deliveries=deliveries)
    except OutputLockedError as exc:
        raise PipelineError(str(exc), exit_code=2, kind="output_locked") from None


def _write_pipeline_manifest(
    result: PipelineResult,
    charts_dir: Path,
    pack: str,
    on_event: EventSink | None,
    *,
    route: RoutePlan | None = None,
) -> None:
    """Write the upload manifest into the hardened ``charts_dir`` and emit
    the PHI-safe ``manifest`` stage event — item COUNT only, never a name,
    DOB, or path (2). ``route``/gates are written here, the one moment
    both are known (after QA). The event's count comes from what the
    WRITER wrote, not from the documents handed to it (#374)."""
    from anastomosis.deliver.browser.gates import RunGates
    from anastomosis.deliver.browser.persist import write_upload_manifest
    from anastomosis.pipeline import STAGE_MANIFEST, StageEvent

    gates = RunGates.from_run(
        # None when QA did not run at all: the operator passed --no-qa, or the
        # optional dependency the checks read PDFs with is not installed. Either
        # way this bundle is unverified, and it has to say so.
        qa_ok=None if result.qa_report is None else result.qa_report.ok,
        layout_hash=None if result.provenance is None else result.provenance.content_hash,
    )
    written = write_upload_manifest(
        result.render_result.documents,
        result.records,
        charts_dir,
        pack=pack,
        route=route,
        gates=gates,
    )
    if on_event is not None:
        on_event(StageEvent(STAGE_MANIFEST, counts={"items": written.items}))


# --- toolkit info (shared by `anast info` and the GUI dashboard header) ---------

# The extras both frontends show, in display order, each with the modules
# that have to be importable for it to be usable at all — names must match
# what pyproject.toml actually declares.
#
# `gui` names a BACKEND alongside the wrapper: `import webview` succeeds
# with neither GTK nor Qt present, and pywebview only raises on launch, so
# probing the wrapper alone cannot tell readiness. `_gui_requirement`
# answers for the platform actually running.


def _gui_requirement(platform_name: str) -> tuple[str, ...]:
    """The wrapper plus drawing backend pywebview loads on
    ``platform_name``: Windows (``clr``) and macOS (``objc``) install it
    unconditionally with the ``gui`` extra, so presence is fair evidence;
    Linux/OpenBSD (``gi|qtpy``) does not, so absence is not. Still only
    evidence: ``anast doctor`` is what actually tries things."""
    if platform_name == "win32":
        return ("webview", "clr")
    if platform_name == "darwin":
        return ("webview", "objc")
    return ("webview", "gi|qtpy")


_EXTRAS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("render", ("playwright", "pymupdf")),
    ("deliver-browser", ("playwright",)),
    ("fhir", ("fhir.resources",)),
    ("gui", _gui_requirement(sys.platform)),
)


@dataclass(frozen=True)
class PackInfo:
    """One pack's discovery state for the info surface."""

    name: str
    #: The pack's resolved root, so a frontend names the exact directory a run
    #: will bind to rather than a name that three directories could answer to.
    #: ``None`` only if discovery could not resolve one.
    root: str | None
    #: What a person reads. Falls back to ``name`` for a pack that declares
    #: none, so a third-party pack written before ``display`` existed still
    #: shows something — the front end's derivation then tidies the id.
    display: str
    available: bool
    origin: str
    diagnosis: str | None
    sections: dict[str, dict[str, object]]


@dataclass(frozen=True)
class SourceInfo:
    """One source adapter's state for the info surface, the twin of
    :class:`PackInfo`: a source declares which of its render-selection
    rules a run may switch off, so both surfaces can OFFER those choices
    rather than wait to be told a name that turns out wrong."""

    name: str
    #: What a person should read instead of ``name``, falling back to it.
    display: str
    description: str
    #: ``{rule name: {"label": ..., "reason": ...}}`` — the rules this source
    #: applies unless a run names one in ``--include`` / unticks it in the GUI.
    #: Empty for a source that keeps nothing out of the render.
    selection: dict[str, dict[str, object]]


@dataclass(frozen=True)
class ToolkitInfo:
    """PHI-free toolkit status: version, extras, sources, packs."""

    version: str
    extras: dict[str, bool]
    sources: list[SourceInfo]
    packs: list[PackInfo]


def _module_available(module: str) -> bool:
    """Is this module importable, without importing it — ``find_spec``
    looks rather than runs the module's own import side effects, at
    roughly no cost. A dotted name still imports its PARENT package (how
    ``find_spec`` resolves one), so a missing parent raises rather than
    returning ``None``."""
    from importlib.util import find_spec

    try:
        return find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _extra_available(modules: tuple[str, ...]) -> bool:
    """Every module the extra needs is present; ``a|b`` means either will do."""
    return all(
        any(_module_available(name) for name in requirement.split("|")) for requirement in modules
    )


def _source_infos() -> list[SourceInfo]:
    """Every registered adapter, as the pickers and ``anast info`` read
    it. ``getattr`` for ``display`` (falling back to the adapter's own id)
    since the registry is open to code this repository never
    type-checked; ``selection_rules`` answers empty for a source with
    none."""
    from anastomosis.sources import available_sources, selection_rules

    return [
        SourceInfo(
            name=adapter.name,
            display=getattr(adapter, "display", "") or adapter.name,
            description=adapter.description,
            selection={
                rule.name: {"label": rule.label, "reason": rule.reason}
                for rule in selection_rules(adapter)
            },
        )
        for adapter in available_sources()
    ]


def _pack_infos() -> list[PackInfo]:
    """Every layout a run form may offer, with why an unavailable one is
    not. The trust store is passed so an operator-taught layout is
    discovered here at its confirmed hash and can be selected on the run
    forms this feeds."""
    from anastomosis.reconstruct import discover_packs
    from anastomosis.reconstruct.packtrust import default_pack_trust

    packs: list[PackInfo] = []
    for status in discover_packs(trust=default_pack_trust()).values():
        pack = status.pack
        sections: dict[str, dict[str, object]] = {}
        if pack is not None:
            sections = {
                name: {"label": flag.label, "default": flag.default}
                for name, flag in pack.manifest.sections.items()
            }
        packs.append(
            PackInfo(
                name=status.name,
                root=str(status.root) if status.root is not None else None,
                display=(pack.manifest.display if pack is not None else "") or status.name,
                available=status.available,
                origin=status.origin,
                diagnosis=status.diagnosis,
                sections=sections,
            )
        )
    return packs


def get_toolkit_info() -> ToolkitInfo:
    """Probe installed extras, registered sources, and discovered packs.

    Pure data, no PHI (versions, names, booleans). The single source of truth
    behind ``anast info`` and :meth:`GuiController.info`.
    """
    import anastomosis
    import anastomosis.pipeline  # registers built-in source adapters at import

    return ToolkitInfo(
        version=anastomosis.__version__,
        extras={extra: _extra_available(modules) for extra, modules in _EXTRAS},
        sources=_source_infos(),
        packs=_pack_infos(),
    )
