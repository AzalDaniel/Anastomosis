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

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping

    from anastomosis.pipeline import EventSink, PipelineResult

__all__ = [
    "CommandResult",
    "DeliveryCommand",
    "DeliveryKind",
    "DeliveryOutcome",
    "PackInfo",
    "PipelineCommand",
    "ToolkitInfo",
    "deliver_outputs",
    "get_toolkit_info",
    "run_pipeline_command",
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
    sections: Mapping[str, bool] = field(default_factory=dict)
    qa: bool = True
    deliveries: tuple[DeliveryCommand, ...] = ()


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
                kind="bundle", out_dir=dc.out_dir, counts={"patients": len(written)}
            )
        elif dc.kind == "ccda":
            from anastomosis.deliver.ccda_export import deliver_ccda

            paths = deliver_ccda(result.records, dc.out_dir)
            outcomes["ccda"] = DeliveryOutcome(
                kind="ccda", out_dir=dc.out_dir, counts={"patients": len(paths)}
            )
    return outcomes


def run_pipeline_command(cmd: PipelineCommand, on_event: EventSink | None = None) -> CommandResult:
    """Run a :class:`PipelineCommand`: ingest → reconstruct → optional QA →
    requested deliveries. Raises :class:`anastomosis.pipeline.PipelineError` on
    any loud failure (the adapter maps it to its exit code / error event)."""
    from anastomosis.pipeline import run_pipeline

    section_args = [f"{k}={'on' if v else 'off'}" for k, v in sorted(cmd.sections.items())]
    result = run_pipeline(
        export_dir=cmd.export_dir,
        out=cmd.charts_dir,
        source=cmd.source,
        pack=cmd.pack,
        pack_dirs=list(cmd.pack_dirs) or None,
        force=cmd.force,
        section=section_args,
        qa=cmd.qa,
        on_event=on_event,
    )
    deliveries = deliver_outputs(result, cmd.charts_dir, cmd.deliveries)
    return CommandResult(pipeline=result, deliveries=deliveries)


# --- toolkit info (shared by `anast info` and the GUI dashboard header) ---------

# The extras probe both frontends show, in display order.
_EXTRAS: tuple[tuple[str, str], ...] = (
    ("render", "playwright"),
    ("render-qa", "fitz"),
    ("fhir", "fhir.resources"),
    ("gui", "webview"),
)


@dataclass(frozen=True)
class PackInfo:
    """One pack's discovery state for the info surface."""

    name: str
    available: bool
    origin: str
    diagnosis: str | None
    sections: dict[str, dict[str, object]]


@dataclass(frozen=True)
class ToolkitInfo:
    """PHI-free toolkit status: version, extras, sources, packs."""

    version: str
    extras: dict[str, bool]
    sources: list[tuple[str, str]]
    packs: list[PackInfo]


def _module_available(module: str) -> bool:
    try:
        __import__(module)
    except ImportError:
        return False
    return True


def get_toolkit_info() -> ToolkitInfo:
    """Probe installed extras, registered sources, and discovered packs.

    Pure data, no PHI (versions, names, booleans). The single source of truth
    behind ``anast info`` and :meth:`GuiController.info`.
    """
    import anastomosis
    import anastomosis.pipeline  # registers built-in source adapters at import
    from anastomosis.reconstruct import discover_packs
    from anastomosis.sources import available_sources

    extras = {extra: _module_available(module) for extra, module in _EXTRAS}
    sources = [(a.name, a.description) for a in available_sources()]
    packs: list[PackInfo] = []
    for status in discover_packs().values():
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
                available=status.available,
                origin=status.origin,
                diagnosis=status.diagnosis,
                sections=sections,
            )
        )
    return ToolkitInfo(version=anastomosis.__version__, extras=extras, sources=sources, packs=packs)
