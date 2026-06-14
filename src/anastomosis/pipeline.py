"""The frontend-agnostic pipeline core (one pipeline, two frontends).

``ingest -> reconstruct -> optional QA`` lived inside :mod:`anastomosis.cli`
as ``_run_pipeline``. The GUI (M4) drives the *same* pipeline, so the mechanics
moved here — pure Python, no Typer, no Rich, no webview — and both frontends
consume it:

* the CLI wraps each step with its existing ``console.print`` formatting and
  ``typer.Exit`` codes (byte-identical output — the lagging strand);
* the GUI's controller forwards the structured progress events to its event
  sink.

The seam between the two is :class:`StageEvent`: this module *emits* PHI-safe
structured events (stage names, counts, ids, exception TYPE names) through an
optional ``on_event`` callback and never formats user-facing prose itself. A
frontend decides how to render them.

Loud failures: a missing source, an unavailable pack, render failures, and a
failing QA report each raise :class:`PipelineError` carrying a PHI-safe message
and the exit code the CLI has always used. Nothing vanishes silently.

PHI rule: events carry counts, stage names, and ids (encounter ids are
pseudonymous ``feedface-`` GUIDs in fixtures), plus exception type names via
:func:`anastomosis.core.logutil.exc_tag`. They never carry patient-derived
field values or rendered filenames — only counts of them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import anastomosis.sources.ccda
import anastomosis.sources.fhir_r4
import anastomosis.sources.oracle_ehi
import anastomosis.sources.pf_tebra  # noqa: F401  registers the built-in adapters at import
from anastomosis.sources import available_sources, detect_source, get_source

if TYPE_CHECKING:
    from anastomosis.core.model import PatientRecord
    from anastomosis.qa import QAReport
    from anastomosis.reconstruct.engine import ReconstructionEngine, RenderResult
    from anastomosis.sources.base import SourceAdapter

__all__ = [
    "PipelineError",
    "PipelineResult",
    "StageEvent",
    "parse_section_overrides",
    "run_pipeline",
]


# Stage names, fixed so both frontends and the tests share one vocabulary.
STAGE_DETECT = "detect"
STAGE_INGEST = "ingest"
STAGE_RECONSTRUCT = "reconstruct"
STAGE_QA = "qa"


@dataclass(frozen=True)
class StageEvent:
    """One PHI-safe progress signal from the pipeline.

    ``stage`` is one of the ``STAGE_*`` constants. ``counts`` carries only
    integers (records, rendered, skipped, failed, pass/warn/fail) — never
    patient-derived strings. ``detail`` is a small PHI-free string slot for
    facts like a detected source name or a chosen pack name.
    """

    stage: str
    counts: dict[str, int] = field(default_factory=dict)
    detail: str = ""


class PipelineError(Exception):
    """A loud, PHI-safe pipeline failure carrying the CLI exit code.

    The message is already PHI-free (it names sources, packs, counts, and
    exception types only); the CLI prints it verbatim and exits with
    ``exit_code`` — preserving the codes the CLI has always returned (2 for a
    missing source / unavailable pack, 1 for render or QA failure).

    ``failed`` carries the per-encounter ``(encounter_id, exception_type)``
    pairs for a render failure so the CLI can reproduce its per-encounter
    detail lines; it is PHI-safe (pseudonymous ids + exception type names) and
    empty for non-render failures. The GUI ignores it — its error event carries
    only the count.
    """

    def __init__(
        self,
        message: str,
        *,
        exit_code: int,
        kind: str = "generic",
        failed: tuple[tuple[str, str], ...] = (),
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        # A stable, PHI-free discriminator the CLI switches on to choose its
        # output line (replaces brittle message-prose matching). One of:
        # no_source, bad_source, bad_pack, bad_section, bad_output,
        # bad_destination, output_locked, render_failed, qa_failed, generic.
        self.kind = kind
        self.failed = failed


@dataclass
class PipelineResult:
    """What a pipeline run yields the caller (the CLI and GUI frontends)."""

    records: list[PatientRecord]
    render_result: RenderResult
    engine: ReconstructionEngine
    qa_report: QAReport | None
    page_size: str
    source_name: str


EventSink = Callable[[StageEvent], None]


_SECTION_ON = frozenset({"on", "true", "1", "yes"})
_SECTION_OFF = frozenset({"off", "false", "0", "no"})


def parse_section_overrides(section: list[str] | None) -> dict[str, bool]:
    """Turn ``["insurance=on", "addenda=off"]`` into ``{"insurance": True, ...}``.

    Shared verbatim with the CLI (which previously owned this helper) so the
    GUI and CLI interpret section toggles identically. Strict: a value outside
    the on/off vocabulary, or an item with no ``=value``, raises
    :class:`PipelineError` (exit 2) instead of silently coercing a typo to
    ``False`` and quietly changing backend state. Section-NAME validation
    (against the pack's manifest) happens later in :func:`run_pipeline`.
    """
    overrides: dict[str, bool] = {}
    for item in section or []:
        key, sep, value = item.partition("=")
        key = key.strip()
        value = value.strip().lower()
        if not sep or not key or not value:
            raise PipelineError(
                f"--section {item!r} must be NAME=on or NAME=off.",
                exit_code=2,
                kind="bad_section",
            )
        if value in _SECTION_ON:
            overrides[key] = True
        elif value in _SECTION_OFF:
            overrides[key] = False
        else:
            raise PipelineError(
                f"--section {key}={value!r}: value must be on/off (got {value!r}).",
                exit_code=2,
                kind="bad_section",
            )
    return overrides


def resolve_source(export_dir: Path, source: str | None) -> SourceAdapter:
    """Pick the source adapter: explicit ``--source`` or structural auto-detect.

    Raises :class:`PipelineError` (exit 2) when neither names nor sniffs a
    known format — the message lists the known adapter names (no PHI).
    """
    if source:
        # get_source raises KeyError listing known names; let the CLI/GUI see a
        # PipelineError instead so neither ever surfaces a raw traceback.
        try:
            return get_source(source)
        except KeyError as exc:
            raise PipelineError(
                str(exc.args[0] if exc.args else exc), exit_code=2, kind="bad_source"
            ) from None
    detected = detect_source(export_dir)
    if detected is None:
        known = ", ".join(a.name for a in available_sources())
        raise PipelineError(
            f"Could not identify the export format. Try --source ({known})",
            exit_code=2,
            kind="no_source",
        )
    return detected


def run_pipeline(
    *,
    export_dir: Path,
    out: Path,
    source: str | None,
    pack: str,
    pack_dirs: list[Path] | None,
    force: bool,
    section: list[str] | None,
    qa: bool,
    trust_new: bool = False,
    on_event: EventSink | None = None,
) -> PipelineResult:
    """The full pipeline (ingest -> reconstruct -> optional QA), frontend-free.

    Emits PHI-safe :class:`StageEvent`\\ s through ``on_event`` as each stage
    completes, returns rich state so a caller can layer archive/bundle/ccda
    delivery without re-loading records or re-rendering charts, and raises
    :class:`PipelineError` on any loud failure.
    """
    from anastomosis.core.output import OutputPathError, validate_output_target
    from anastomosis.reconstruct import discover_packs
    from anastomosis.reconstruct.chromium import ChromiumRenderer
    from anastomosis.reconstruct.engine import ReconstructionEngine
    from anastomosis.reconstruct.packtrust import default_pack_trust

    emit = on_event or (lambda _event: None)

    # Pre-flight the output dir BEFORE any ingest/render work, so a path that is
    # actually a file fails in milliseconds with a clean message rather than
    # raising deep in the engine after a long run.
    try:
        validate_output_target(out)
    except OutputPathError as exc:
        raise PipelineError(str(exc), exit_code=2, kind="bad_output") from None

    adapter = resolve_source(export_dir, source)
    emit(StageEvent(STAGE_DETECT, detail=adapter.name))

    dirs = list(pack_dirs or [])
    # Enforce hash-pinned trust only for external packs (--pack-dir); builtins
    # need no store. trust=None when there are no external dirs keeps the
    # consent-only path unchanged.
    statuses = discover_packs(
        dirs,
        allow_external=bool(dirs),
        trust=default_pack_trust() if dirs else None,
        trust_new=trust_new,
    )
    status = statuses.get(pack)
    if status is None or status.pack is None:
        diagnosis = status.diagnosis if status else f"unknown pack (have: {', '.join(statuses)})"
        raise PipelineError(f"Pack {pack!r} unavailable: {diagnosis}", exit_code=2, kind="bad_pack")

    overrides = parse_section_overrides(section)
    manifest = status.pack.manifest
    # Section-NAME validation: a typo'd or unknown section silently changed
    # backend state before. Reject it loudly against the pack's own matrix.
    unknown = sorted(set(overrides) - set(manifest.sections))
    if unknown:
        known = ", ".join(sorted(manifest.sections)) or "(none)"
        raise PipelineError(
            f"Unknown --section {', '.join(unknown)} for pack {pack!r}. Known: {known}.",
            exit_code=2,
            kind="bad_section",
        )
    margins = {
        "top": manifest.page.margin_top,
        "right": manifest.page.margin_right,
        "bottom": manifest.page.margin_bottom,
        "left": manifest.page.margin_left,
    }
    engine = ReconstructionEngine(
        status.pack,
        lambda: ChromiumRenderer(page_size=manifest.page.size, margins=margins),
        section_overrides=overrides,
    )
    records = list(adapter.load(export_dir))
    emit(StageEvent(STAGE_INGEST, counts={"records": len(records)}))

    result = engine.run(records, out, force=force)
    emit(
        StageEvent(
            STAGE_RECONSTRUCT,
            counts={
                "rendered": len(result.rendered),
                "skipped": len(result.skipped),
                "failed": len(result.failed),
            },
        )
    )
    if result.failed:
        # Loud render failure. The (encounter_id, type) pairs ride on the error
        # so the CLI can print its per-encounter detail lines; they are PHI-safe.
        raise PipelineError(
            f"{len(result.failed)} encounter(s) failed to render",
            exit_code=1,
            kind="render_failed",
            failed=tuple(result.failed),
        )

    qa_report = None
    if qa and result.documents:
        qa_report = _run_qa_stage(records, result, engine, out, manifest.page.size, emit)
    return PipelineResult(
        records=records,
        render_result=result,
        engine=engine,
        qa_report=qa_report,
        page_size=manifest.page.size,
        source_name=adapter.name,
    )


def _run_qa_stage(
    records: list[PatientRecord],
    result: RenderResult,
    engine: ReconstructionEngine,
    out: Path,
    page_size: str,
    emit: EventSink,
) -> QAReport | None:
    """Verify every rendered document; return the report (None if QA downgraded).

    A missing PyMuPDF (the optional ``render`` extra) downgrades QA to a
    no-op rather than failing the run — the only ``ImportError`` allowed to
    soften here, mirroring the original CLI behavior. A failing report raises
    :class:`PipelineError` (exit 1).
    """
    try:
        from anastomosis.qa import Verdict, run_qa, write_report
    except ImportError as exc:
        if exc.name != "fitz":  # only the optional dependency may downgrade QA
            raise
        emit(StageEvent(STAGE_QA, detail="skipped: install anastomosis[render] for PyMuPDF"))
        return None
    lookup = {(r.patient.id, e.id): (e, r) for r in records for e in r.encounters}
    report = run_qa(
        ((d.path, *lookup[d.patient_id, d.encounter_id]) for d in result.documents),
        section_flags=engine.section_flags,
        page_size=page_size,
    )
    write_report(report, out)
    emit(
        StageEvent(
            STAGE_QA,
            counts={
                "pass": report.count(Verdict.PASS),
                "warn": report.count(Verdict.WARN),
                "fail": report.count(Verdict.FAIL),
            },
        )
    )
    if not report.ok:
        raise PipelineError(
            f"QA failed: {report.count(Verdict.FAIL)} document(s)", exit_code=1, kind="qa_failed"
        )
    return report
