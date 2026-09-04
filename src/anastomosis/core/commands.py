"""The shared application/command layer.

The CLI (:mod:`anastomosis.cli`) and the GUI (:mod:`anastomosis.gui.controller`)
are *adapters*: they parse operator intent (flags / a JS payload), build one of
the command objects here, and present the :class:`CommandResult`. All
orchestration policy — which deliverers run, against which directories, in what
order — lives here exactly once, so the same intent produces identical backend
state regardless of which frontend issued it.

Design notes:

* :func:`run_pipeline_command` wraps the frontend-free pipeline core
  (:func:`anastomosis.pipeline.run_pipeline`) and then runs the requested
  deliveries through the single :func:`deliver_outputs` implementation.
* :func:`deliver_outputs` does NOT print or emit — it returns structured
  :class:`DeliveryOutcome`\\ s. Presentation (the CLI's Rich lines, the GUI's
  progress events) stays in each adapter, so each frontend keeps its exact,
  test-pinned output while sharing the orchestration.
* :func:`get_toolkit_info` consolidates the extras/sources/packs probe that
  ``anast info`` and the GUI dashboard header both need.
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

# The canonical deliverer order — archive, then bundle, then ccda — matching the
# order both frontends historically ran them in (preserves CLI line order and
# the GUI's per-deliverer event order).
_DELIVERY_ORDER: dict[str, int] = {"archive": 0, "bundle": 1, "ccda": 2}


@dataclass(frozen=True)
class DeliveryCommand:
    """One requested delivery: a kind and the directory it writes into.

    The adapter chooses ``out_dir``: the CLI uses the operator's
    ``--archive/--bundle/--ccda`` path; the GUI uses a sibling subdirectory of
    the run's output dir (``<out>/archive`` etc.).
    """

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
    """A per-patient roll-up of a completed run — for LOCAL display only.

    Unlike :class:`DeliveryOutcome` (counts only), this carries
    patient-identifying values — ``display_name`` and ``birth_date`` — so a
    frontend can show the operator *which* patients a run produced and how many
    notes each yielded. Those values are PHI: they ride the command layer's
    return value for direct on-screen display and must NEVER be emitted as
    progress events or written to any log (the event/log stream stays
    count-only). ``documents`` is the number of chart PDFs the engine actually
    rendered (or verified) for the patient; ``encounters`` is how many the
    source carried.
    """

    patient_id: str
    display_name: str
    birth_date: str | None  # ISO-8601 date, or None when the source lacked one
    encounters: int
    documents: int


def summarize_patients(result: PipelineResult) -> list[PatientSummary]:
    """Per-patient roll-up (name, DOB, #encounters, #rendered docs), in ingest order.

    Joins the canonical records with the render result's per-document
    ``patient_id`` attribution, so a frontend can render which patients the run
    processed without re-deriving anything. Pure data transformation; carries
    PHI (names/DOB) for LOCAL display only — callers must never log it or put it
    on an event. Order follows ``result.records`` (the stable ingest order).
    """
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
    """Run the requested deliverers once, in canonical order; return outcomes.

    No printing, no events — the adapter presents the returned outcomes. Each
    deliverer reads the rendered chart PDFs out of ``charts_dir`` and writes
    into its command's ``out_dir``.
    """
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

            # Single-pass per-patient attribution (was an O(patients x pdfs)
            # re-filter of every chart for every patient).
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
    """Write the upload manifest and emit the PHI-safe ``manifest`` stage event.

    The manifest carries demographics, so it lands ONLY in the hardened
    ``charts_dir`` (the writer enforces that); the emitted event carries the item
    COUNT only — never a name, DOB, or path.

    ``pack`` is the pack this run rendered through — the run resolved it before
    rendering, so a manifest written here always names a real pack — and is what
    lets the later ``anast upload`` run L3 against the header fields that pack
    declares.

    ``route`` and the gates derived here are the bundle's REVIEWED context: what
    this run was preparing for, and what it checked first. An executor refuses
    on them (:func:`~anastomosis.deliver.browser.gates.assert_deliverable`), so
    they are written at the one moment both are actually known — after QA, from
    the run's own result, rather than re-derived by whoever opens the folder
    next.

    The event's counts come from what the WRITER wrote, not from the documents
    handed to it: counting the rendered charts announced ``0 item(s)`` over a
    manifest holding two source documents, which is the same silence #374 was
    filed about, one line further along.
    """
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

# The extras both frontends show, in display order, each with the modules that
# have to be importable for it to be usable at all.
#
# These names are the ones `pyproject.toml` actually declares. They drifted:
# `render-qa` was listed here and has never been an extra (pymupdf ships inside
# `render`), so `pip install "anastomosis[render-qa]"` warned and installed
# nothing; `deliver-browser` is real and was never mentioned.
#
# `gui` names a BACKEND alongside the wrapper. `import webview` succeeds on a
# machine with neither GTK nor Qt bindings, and pywebview then raises on launch
# — so probing the wrapper alone reported the desktop app as ready on a machine
# where `anast gui` could not start. WHICH backend that is depends on the
# platform, so `_gui_requirement` answers for the one we are running on.


def _gui_requirement(platform_name: str) -> tuple[str, ...]:
    """The wrapper plus the drawing backend pywebview would load *here*.

    pywebview picks its backend by platform, so a probe naming one platform's
    backend is wrong on the others: `gi|qtpy` was hard-coded, and a Windows or
    macOS install that draws the window perfectly well — GTK and Qt neither
    installed nor wanted there — reported the desktop app as unavailable.

    Windows draws through WinForms/WebView2 via pythonnet (`clr`) and macOS
    through Cocoa via PyObjC (`objc`); pywebview depends on those
    unconditionally on those platforms, so `pip install "anastomosis[gui]"`
    has already brought them and their presence is fair evidence. GTK and Qt
    are the Linux/OpenBSD pair, either will do and pywebview picks — but pip
    installs neither alongside pywebview, which is why the wrapper alone is no
    evidence there.

    Still only evidence: the Edge WebView2 Runtime a Windows machine also needs
    is an OS component, not an importable module, so no probe of this shape can
    see it. `anast doctor` remains the command that actually tries things.
    """
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
    """One source adapter's state for the info surface.

    The twin of :class:`PackInfo`, and it exists for the same reason: an
    adapter carries something a name-and-description tuple has nowhere to put.
    A pack declares which sections a run may switch; a source declares which of
    its render-selection rules a run may switch off, and both surfaces need to
    OFFER those choices rather than wait to be told a name that turns out to be
    wrong.
    """

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
    """Is this module importable, without importing it?

    ``__import__`` was the test, which meant asking "is pymupdf installed" cost
    105 ms of executing pymupdf, and asking about the GUI wrapper ran a
    toolkit probe inside a read-only status command. ``find_spec`` answers the
    same question by looking, at roughly no cost.

    A dotted name still imports its PARENT package (that is how ``find_spec``
    resolves one), so a missing parent raises rather than returning None.
    """
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
    """Every registered adapter, as the pickers and ``anast info`` read it.

    ``getattr``, though the protocol declares ``display``: the registry is
    open, and an adapter registered by code this repository never type-checked
    is exactly the case a fallback exists for. It reads as its own id, which is
    what every adapter read as before. ``selection_rules`` is read the same way
    for the same reason, and answers empty for a source that has none.
    """
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
    """Every layout a run form may offer, with why an unavailable one is not.

    The trust store is passed so a layout the operator taught (it lands in the
    per-user pack directory) is discovered here at its confirmed hash and can
    be SELECTED on the run forms this feeds. Without it every learned layout
    reported unavailable and only the built-ins were ever offered — a run form
    that cannot name the layout the app just said it wrote.
    """
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
