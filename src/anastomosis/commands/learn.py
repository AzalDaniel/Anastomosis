"""One learn capability: a chart layout from sample PDFs, a flat format from
one example table. Both arms run analyze -> confirm -> emit through
:func:`run_learn` (28-36), PHI-safe and never printing; ``kind`` selects only
what differs — input resolution, the write shape, the trust store. The heavy
imports stay inside the arms so a minimal install imports this cleanly."""

from __future__ import annotations

import logging
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from anastomosis.core.logutil import exc_tag

if TYPE_CHECKING:  # the real types, without paying the import at runtime
    from anastomosis.commands.sourcelearn import SourceAnalysis
    from anastomosis.packgen.infer import PackAnalysis
    from anastomosis.sources.learned.spec import DestinationBinding, MappingSpec

logger = logging.getLogger(__name__)

__all__ = [
    "LEARNABLE_SUFFIXES",
    "LEARN_NAME_RE",
    "LOW_SAMPLE_FLOOR",
    "LearnCommand",
    "LearnKind",
    "LearnResult",
    "SourceSuggestion",
    "collect_sample_pdfs",
    "resolve_example",
    "run_learn",
]

LearnKind = Literal["layout", "tabular"]  # sample PDFs, or one example table
# The name becomes a directory and an identifier, for both arms alike.
LEARN_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
LOW_SAMPLE_FLOOR = 3  # below this the static/per-patient split is weak (33)
LEARNABLE_SUFFIXES = (".csv", ".tsv", ".json", ".ndjson", ".jsonl")  # one flat file


@dataclass(frozen=True)
class SourceSuggestion:
    """One column's proposed mapping. ``inferred_type``/``sample_shape`` are
    letters-for-letters, digits-for-digits evidence, never a value."""

    source: str
    target: str | None
    transform: str
    confidence: float
    inferred_type: str = ""
    sample_shape: str = ""


@dataclass(frozen=True)
class LearnCommand:
    """Contract: a fully-specified learn request, the unit both frontends
    build. ``out_dir=None`` means the per-user directory for the kind (36)."""

    kind: LearnKind
    name: str
    display: str | None = None
    out_dir: Path | None = None
    confirmed: bool = False
    samples: list[str] = field(default_factory=list)  # layout: dirs, globs, files
    # A switch, not a promise: with no engine installed an image-only sample
    # still refuses (``OcrRequiredError``), downloading nothing (34).
    allow_ocr: bool = True
    example: Path | None = None  # tabular: a file, or a dir holding exactly one
    # tabular: source column -> (target, transform), COMPLETE not sparse — a
    # column absent from it is unmapped-and-kept. The three overrides below
    # travel with it, always all three, so the optional encounter key has no
    # "unset or cleared" ambiguity. ``None`` takes the scorer's own proposal.
    decisions: dict[str, tuple[str, str]] | None = None
    patient_key: str | None = None
    encounter_key: str | None = None
    row_scope: str | None = None
    destination: str | None = None  # tabular: taught FOR this (32); None unbound


@dataclass(frozen=True)
class LearnResult:
    """Contract: what a learn run yields. ``error`` is an enumerated code or an
    exception TYPE name, never a message that could embed a sample path. The
    proposal fields survive a refusal; the written fields need ``ok``."""

    ok: bool
    error: str | None
    summary: list[str] = field(default_factory=list)
    written_dir: Path | None = None  # the draft pack, or the mapping directory
    review_md: str | None = None  # the ``DRAFT.md``/``MAPPING.md`` it left behind
    learned_name: str | None = None
    detail: str | None = None  # a PHI-safe diagnosis behind a code, never a value
    # layout
    caveat: str = ""
    sample_count: int = 0
    low_confidence: bool = False
    content_hash: str | None = None
    # tabular
    fmt_type: str | None = None
    columns: int = 0
    patient_key: str | None = None
    encounter_key: str | None = None
    row_scope: str | None = None
    suggestions: list[SourceSuggestion] = field(default_factory=list)
    mapped: int = 0
    record_count: int = 0
    unmapped: int = 0
    dropped_columns: list[str] = field(default_factory=list)
    # The structured pointer off a load refusal — which column, aimed at which
    # target, read how, "grouping" for the keys or the row grain — so a frontend
    # marks the exact row instead of scraping ``detail``.
    detail_column: str | None = None
    detail_target: str | None = None
    detail_transform: str | None = None
    detail_scope: str | None = None
    targets: list[str] = field(default_factory=list)  # the closed set (30)
    destination: str | None = None  # echoed back, so a frontend can show it


def run_learn(cmd: LearnCommand) -> LearnResult:
    """Contract: analyze -> confirm -> emit (28), PHI-safe, never raising into
    the caller. ``confirmed=False`` returns the proposal with
    ``ConfirmationRequired`` and writes nothing; ``True`` writes (29)."""
    if error := _refuse_name(cmd.name, cmd.kind):
        return LearnResult(ok=False, error=error)
    proposal, write = _layout_arm(cmd) if cmd.kind == "layout" else _tabular_arm(cmd)
    if write is None:
        return proposal  # a resolve or analyze refusal, carrying what it knew
    if not cmd.confirmed:
        return replace(proposal, error="ConfirmationRequired")
    return write()


def _layout_name_taken(name: str) -> str | None:
    """Shadowing a built-in would disable it every run, diagnosed as untrusted."""
    from anastomosis.reconstruct.packs import builtin_pack_names

    return "BuiltinPackName" if name in builtin_pack_names() else None


def _source_name_taken(name: str) -> str | None:
    """Asked of the live registry, never a hardcoded list (31)."""
    from anastomosis.sources import available_sources
    from anastomosis.sources.learned import LearnedSourceAdapter

    for adapter in available_sources():
        if adapter.name == name:
            learned = isinstance(adapter, LearnedSourceAdapter)
            return "SourceIdInUse" if learned else "SourceIdReserved"
    return None


#: Per kind: the malformed-name code, and the registry that owns taken ids.
_NAME_REGISTRIES: dict[LearnKind, tuple[str, Callable[[str], str | None]]] = {
    "layout": ("InvalidPackName", _layout_name_taken),
    "tabular": ("InvalidSourceName", _source_name_taken),
}


def _refuse_name(name: object, kind: LearnKind) -> str | None:
    """Why this name may not be taught, or ``None`` — type-guarded, so a
    malformed command returns a code; asked before anything is read (31)."""
    malformed, taken = _NAME_REGISTRIES[kind]
    if not isinstance(name, str) or not LEARN_NAME_RE.match(name):
        return malformed
    return taken(name)


def collect_sample_pdfs(patterns: list[str]) -> list[Path]:
    """Contract: dir-or-glob arguments resolved to a sorted, de-duplicated PDF
    list — sorted so sample indices are deterministic (33)."""
    import glob as _glob

    found: set[Path] = set()
    for raw in patterns:
        candidate = Path(raw)
        if candidate.is_dir():
            found.update(p for p in candidate.glob("*.pdf"))
            continue
        if candidate.is_file():
            found.add(candidate)
            continue
        found.update(Path(match) for match in _glob.glob(raw) if Path(match).is_file())
    return sorted(found)


def _layout_arm(cmd: LearnCommand) -> tuple[LearnResult, Callable[[], LearnResult] | None]:
    pdfs = collect_sample_pdfs(cmd.samples) if isinstance(cmd.samples, list) else []
    if not pdfs:
        return LearnResult(ok=False, error="NoSamplesFound"), None

    from anastomosis.packgen import analyze, extract_samples
    from anastomosis.packgen.emit import SAME_PATIENT_CAVEAT
    from anastomosis.packgen.ocr import discover_worker

    # No worker is not a failure: only a scanned batch asks for one, and it
    # then gets OcrRequiredError naming what to install. Nothing downloads (34).
    worker = discover_worker() if cmd.allow_ocr else None
    try:
        analysis = analyze(extract_samples(pdfs, ocr=worker))
    except Exception as exc:  # unreadable/encrypted sample — type only, no path
        return LearnResult(ok=False, error=exc_tag(exc), sample_count=len(pdfs)), None

    proposal = LearnResult(
        ok=False,
        error=None,
        summary=list(analysis.summary_lines()),
        caveat=SAME_PATIENT_CAVEAT,
        sample_count=analysis.sample_count,
        low_confidence=analysis.low_confidence,
    )
    return proposal, lambda: _emit_layout(cmd, analysis, proposal)


def _emit_layout(cmd: LearnCommand, analysis: PackAnalysis, proposal: LearnResult) -> LearnResult:
    from anastomosis.packgen.emit import emit_draft_pack
    from anastomosis.reconstruct import user_packs_dir
    from anastomosis.reconstruct.packtrust import default_pack_trust, pack_content_hash

    out_dir = cmd.out_dir if cmd.out_dir is not None else user_packs_dir()
    pack_dir: Path | None = None
    try:
        pack_dir = emit_draft_pack(
            analysis, name=cmd.name, display=cmd.display or cmd.name, out_dir=out_dir
        )
        review_md = (pack_dir / "DRAFT.md").read_text(encoding="utf-8")
        # Inside the try: a draft whose hash could not be recorded is a draft no
        # run will accept, and calling it written would be a false completion.
        content_hash = pack_content_hash(pack_dir)
        default_pack_trust().record(pack_dir, content_hash)
    except Exception as exc:  # emit/read/trust failure — type name only, no PHI
        if pack_dir is not None:
            shutil.rmtree(pack_dir, ignore_errors=True)  # never an untrusted stand-in (29)
        return replace(proposal, error=exc_tag(exc))

    return replace(
        proposal,
        ok=True,
        learned_name=cmd.name,
        written_dir=pack_dir,
        review_md=review_md,
        content_hash=content_hash,
    )


def resolve_example(example: Path) -> tuple[Path | None, str]:
    """Contract: ``(file, "")`` for a file or a directory holding exactly one
    learnable file, else ``(None, code)``. Never raises."""
    if example.is_file():
        return example, ""
    if not example.is_dir():
        return None, "NoExampleFile"
    candidates = sorted(
        p for p in example.iterdir() if p.is_file() and p.suffix.lower() in LEARNABLE_SUFFIXES
    )
    if len(candidates) == 1:
        return candidates[0], ""
    return (None, "NoExampleFile") if not candidates else (None, "AmbiguousExample")


def _destination_binding(name: str | None) -> tuple[DestinationBinding | None, str | None]:
    """Contract (32): a binding for a known destination, nothing for an unbound
    teach, ``UnknownDestination`` for a name the registry does not carry."""
    if name is None:
        return None, None
    from anastomosis.commands.profiles import ProfileError, capture_destination_profile
    from anastomosis.sources.learned.spec import DestinationBinding

    try:
        profile = capture_destination_profile(name)
    except ProfileError:
        return None, "UnknownDestination"
    return (
        DestinationBinding(
            destination=profile.name, version=profile.version, profile_hash=profile.profile_hash
        ),
        None,
    )


def _tabular_arm(cmd: LearnCommand) -> tuple[LearnResult, Callable[[], LearnResult] | None]:
    resolved, resolve_error = (
        resolve_example(cmd.example) if isinstance(cmd.example, Path) else (None, "NoExampleFile")
    )
    if resolved is None:
        return LearnResult(ok=False, error=resolve_error), None
    binding, binding_error = _destination_binding(cmd.destination)
    if binding_error is not None:
        return LearnResult(ok=False, error=binding_error, destination=cmd.destination), None

    from anastomosis.commands.sourcelearn import analyze_source

    try:
        analysis = analyze_source(resolved)
    except Exception as exc:  # unreadable/garbled example — type only, no PHI
        return LearnResult(ok=False, error="CannotAnalyze", detail=exc_tag(exc)), None

    proposal = replace(_proposal(analysis), destination=cmd.destination)
    reviewed = _reviewed(analysis, cmd)
    return proposal, lambda: _emit_tabular(cmd, reviewed, resolved, binding, proposal)


def _emit_tabular(
    cmd: LearnCommand,
    analysis: SourceAnalysis,
    example: Path,
    binding: DestinationBinding | None,
    proposal: LearnResult,
) -> LearnResult:
    from anastomosis.commands.sourcelearn import build_mapping, round_trip, save_mapping
    from anastomosis.sources.learned import user_sources_dir
    from anastomosis.sources.learned.spec import MappingError

    try:
        spec = build_mapping(
            analysis,
            mapping_id=cmd.name,
            display=cmd.display or cmd.name,
            decisions=cmd.decisions,
            destination_binding=binding,
        )
    except MappingError as exc:  # over column/target NAMES; embeds no cell value
        return replace(proposal, error="CannotBuildMapping", detail=str(exc))

    report = round_trip(spec, example)
    if not report.ok:
        # A choked transform is a fixable mistake, distinct from a dropped
        # column; both name columns only, so both are safe to surface (29).
        if report.error is not None:
            return replace(
                proposal,
                error="MappingLoadFailed",
                detail=report.error,
                detail_column=report.bad_column,
                detail_target=report.bad_target,
                detail_transform=report.bad_transform,
                detail_scope=report.bad_scope,
            )
        return replace(proposal, error="WouldDropColumns", dropped_columns=report.dropped_columns)

    try:
        base = cmd.out_dir if cmd.out_dir is not None else user_sources_dir()
        mapping_dir = save_mapping(spec, base)
        mapping_md = (mapping_dir / "MAPPING.md").read_text(encoding="utf-8")
    except (MappingError, OSError) as exc:  # a save path could embed a label
        return replace(proposal, error="SaveFailed", detail=exc_tag(exc))

    _make_selectable(spec)
    return replace(
        proposal,
        ok=True,
        learned_name=cmd.name,
        written_dir=mapping_dir,
        review_md=mapping_md,
        record_count=report.record_count,
        unmapped=len(spec.unmapped_source_fields),
    )


def _make_selectable(spec: MappingSpec) -> None:
    """Usable now, not only after a restart (31); best effort either way."""
    from anastomosis.sources import register
    from anastomosis.sources.learned import LearnedSourceAdapter

    try:
        register(LearnedSourceAdapter(spec))
    except Exception:  # pragma: no cover - defensive; the disk copy still stands
        logger.warning("saved mapping could not be registered in this session")


def _reviewed(analysis: SourceAnalysis, cmd: LearnCommand) -> SourceAnalysis:
    """The review's grouping answers, authoritative together — so ``None`` for
    the encounter key means "each row is its own visit", not "not touched"."""
    if cmd.decisions is None:
        return analysis
    return replace(
        analysis,
        patient_key=cmd.patient_key,
        encounter_key=cmd.encounter_key,
        row_scope=cmd.row_scope or analysis.row_scope,
    )


def _proposal(analysis: SourceAnalysis) -> LearnResult:
    """Column names, target paths and counts — never a cell value."""
    from anastomosis.core.model_paths import canonical_target_paths

    profiles = {
        profile.name: (profile.inferred_type, profile.sample_shape) for profile in analysis.profiles
    }
    return LearnResult(
        ok=False,
        error=None,
        fmt_type=analysis.fmt.type,
        columns=len(analysis.fmt.columns),
        patient_key=analysis.patient_key,
        encounter_key=analysis.encounter_key,
        row_scope=analysis.row_scope,
        summary=list(analysis.summary_lines()),
        suggestions=[
            SourceSuggestion(
                source=s.source_path,
                target=s.target_path,
                transform=s.transform,
                confidence=round(s.confidence, 2),
                inferred_type=profiles.get(s.source_path, ("", ""))[0],
                sample_shape=profiles.get(s.source_path, ("", ""))[1],
            )
            for s in analysis.suggestions
        ],
        mapped=sum(1 for s in analysis.suggestions if s.target_path is not None),
        targets=sorted(canonical_target_paths()),
    )
