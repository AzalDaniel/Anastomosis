"""The shared learn-a-source command layer, mirroring
:mod:`anastomosis.core.packinit`: one analyze -> confirm -> build ->
round-trip -> save flow for the CLI and the GUI (28), never printing.
``confirmed=True`` round-trips the mapping to prove no column is dropped,
then saves owner-only (29). ``destination`` is resolved before analysis
and recorded as a profile hash; a later run at a different destination
refuses and names both ends (32). A name is refused before writing when it
collides with a built-in or the operator's own earlier work (31).
``sourcelearn`` imports lazily inside :func:`run_source_init_command`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from anastomosis.core.logutil import exc_tag
from anastomosis.core.packinit import PACK_NAME_RE

if TYPE_CHECKING:  # the real types, without paying the import at runtime
    from anastomosis.core.sourcelearn import SourceAnalysis
    from anastomosis.sources.learned.spec import DestinationBinding, MappingSpec

logger = logging.getLogger(__name__)

__all__ = [
    "LEARNABLE_SUFFIXES",
    "SourceInitCommand",
    "SourceInitResult",
    "SourceSuggestion",
    "resolve_example",
    "run_source_init_command",
]

# Structured-export file types the source learner can read (one flat file).
LEARNABLE_SUFFIXES = (".csv", ".tsv", ".json", ".ndjson", ".jsonl")


@dataclass(frozen=True)
class SourceSuggestion:
    """One column's proposed mapping (PHI-safe: names, counts, a mask
    only). ``inferred_type``/``sample_shape`` are letters-for-letters,
    digits-for-digits evidence, never a value — enough to see a
    214-distinct text column is visibly not a date."""

    source: str
    target: str | None
    transform: str
    confidence: float
    inferred_type: str = ""
    sample_shape: str = ""


@dataclass(frozen=True)
class SourceInitCommand:
    """Contract: a fully-specified learn-a-source request. ``example`` is a
    structured file or a directory holding exactly one. ``out_dir=None``
    means ``~/.anastomosis/sources``. ``confirmed`` is the review
    checkpoint (28-29)."""

    example: Path
    name: str
    display: str | None = None
    out_dir: Path | None = None
    confirmed: bool = False
    #: The review's corrections, or None for the scorer's own proposal. A dict
    #: maps source column -> (target_path, transform) and is COMPLETE, not
    #: sparse: a column absent from it (and not a key) is unmapped-and-kept.
    #: The three overrides below travel with it — always all three when a
    #: review happened, so there is no "unset or cleared" ambiguity for the
    #: optional encounter key.
    decisions: dict[str, tuple[str, str]] | None = None
    patient_key: str | None = None
    encounter_key: str | None = None
    row_scope: str | None = None
    #: The destination this format is taught for (a registry name), chosen
    #: before teaching (32); ``None`` teaches unbound.
    destination: str | None = None


@dataclass(frozen=True)
class SourceInitResult:
    """Contract: what a learn-a-source run yields. ``ok`` is true only when
    a mapping was saved. The proposal fields (``fmt_type``..``mapped``)
    populate whenever analyze succeeded, so a refusal still shows what was
    proposed; ``mapping_dir``/``mapping_md``/``record_count``/``unmapped``
    only on success. ``detail`` is a PHI-safe diagnosis, never a value."""

    ok: bool
    error: str | None
    fmt_type: str | None = None
    columns: int = 0
    patient_key: str | None = None
    encounter_key: str | None = None
    row_scope: str | None = None
    summary: list[str] = field(default_factory=list)
    suggestions: list[SourceSuggestion] = field(default_factory=list)
    mapped: int = 0
    mapping_dir: Path | None = None
    mapping_md: str | None = None
    record_count: int = 0
    unmapped: int = 0
    dropped_columns: list[str] = field(default_factory=list)
    detail: str | None = None
    #: The structured pointer off a load refusal: which column, aimed at which
    #: target, read how. Populated for ``MappingLoadFailed`` when the raise
    #: site knew; the frontend anchors the refusal to the exact row with these
    #: instead of scraping the sentence in ``detail``.
    detail_column: str | None = None
    detail_target: str | None = None
    detail_transform: str | None = None
    #: ``"grouping"`` when the load refusal points at the keys or row grain.
    detail_scope: str | None = None
    #: Every canonical target a review may aim a column at — the closed set the
    #: correction chooser is populated from, sent once with the proposal.
    targets: list[str] = field(default_factory=list)
    #: The destination this mapping was taught for, echoed back so a frontend
    #: can show what the format is now bound to. ``None`` for an unbound teach.
    destination: str | None = None


def resolve_example(example: Path) -> tuple[Path | None, str]:
    """Contract: ``(file, "")`` for a file or a directory holding exactly
    one learnable file, else ``(None, code)`` — ``NoExampleFile`` or
    ``AmbiguousExample``. Never raises."""
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


def run_source_init_command(cmd: SourceInitCommand) -> SourceInitResult:
    """Contract: run analyze -> confirm -> build -> round-trip -> save (28);
    no printing. ``confirmed=False`` returns the proposal with
    ``ConfirmationRequired``, writing nothing; ``True`` proves via
    round-trip that no column drops, then saves owner-only (29)."""
    resolved, binding, refusal = _resolve_inputs(cmd)
    if refusal is not None:
        return refusal
    assert resolved is not None  # a None refusal guarantees a resolved example

    # Lazy imports so a minimal install loads this module cleanly.
    from anastomosis.core.sourcelearn import (
        analyze_source,
        build_mapping,
        round_trip,
        save_mapping,
    )
    from anastomosis.sources.learned import user_sources_dir
    from anastomosis.sources.learned.spec import MappingError

    try:
        analysis = analyze_source(resolved)
    except Exception as exc:  # unreadable/garbled example — TYPE name only, no PHI
        return SourceInitResult(ok=False, error="CannotAnalyze", detail=exc_tag(exc))

    # The PHI-safe proposal, carried on every post-analyze outcome via
    # dataclasses.replace (which preserves the per-field types mypy checks).
    proposal = replace(_proposal(analysis), destination=cmd.destination)

    # The review checkpoint: an unconfirmed request refuses and writes nothing,
    # but still returns the proposal so the operator sees what they confirm.
    if not cmd.confirmed:
        return replace(proposal, error="ConfirmationRequired")

    analysis = _reviewed(analysis, cmd)
    try:
        spec = build_mapping(
            analysis,
            mapping_id=cmd.name,
            display=cmd.display or cmd.name,
            decisions=cmd.decisions,
            destination_binding=binding,
        )
    except MappingError as exc:
        # build over column/target NAMES — the message embeds no cell value.
        return replace(proposal, error="CannotBuildMapping", detail=str(exc))

    report = round_trip(spec, resolved)
    if not report.ok:
        # A LOAD failure (a mapped column's transform choked) is a fixable mapping
        # mistake, distinct from a column that would be silently dropped. Both
        # name columns/targets only (no cell value), so both are safe to surface.
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
    except (MappingError, OSError) as exc:
        # TYPE name only (a save path could embed an operator label) — preserves
        # the CLI's "(OSError)"-style detail without echoing the path.
        return replace(proposal, error="SaveFailed", detail=exc_tag(exc))

    # `pipeline` registers learned sources once at import, so without this a
    # format taught mid-session would be written and valid but invisible to
    # Charts/Migrate until a restart.
    _make_selectable(spec)

    return replace(
        proposal,
        ok=True,
        mapping_dir=mapping_dir,
        mapping_md=mapping_md,
        record_count=report.record_count,
        unmapped=len(spec.unmapped_source_fields),
    )


def _resolve_inputs(
    cmd: SourceInitCommand,
) -> tuple[Path | None, DestinationBinding | None, SourceInitResult | None]:
    """Contract: settle everything a teach needs before reading a row — is
    the name well-formed and free, does the example resolve to one file,
    does the destination exist. ``(example, binding, None)`` when all
    pass, else ``(None, None, refusal)`` on the first that does not."""
    # Type-guard the name up front so a malformed command returns a code rather
    # than raising — mirrors run_pack_init's contract.
    if not isinstance(cmd.name, str) or not PACK_NAME_RE.match(cmd.name):
        return None, None, SourceInitResult(ok=False, error="InvalidSourceName")

    # Checked from the name alone, before writing anything (31): a collision
    # refused only at registration time would report success on a folder
    # nobody could ever select, not even after a restart.
    reserved = _reserved(cmd.name)
    if reserved is not None:
        return None, None, SourceInitResult(ok=False, error=reserved)

    resolved, resolve_error = resolve_example(cmd.example)
    if resolved is None:
        return None, None, SourceInitResult(ok=False, error=resolve_error)

    binding, binding_error = _destination_binding(cmd.destination)
    if binding_error is not None:
        return (
            None,
            None,
            SourceInitResult(ok=False, error=binding_error, destination=cmd.destination),
        )
    return resolved, binding, None


def _destination_binding(name: str | None) -> tuple[DestinationBinding | None, str | None]:
    """Contract (32): ``(binding, None)`` for a known destination, ``(None,
    None)`` for an unbound teach, ``(None, "UnknownDestination")`` for a
    name the capability registry does not carry — a refusal, never a
    guess."""
    if name is None:
        return None, None
    from anastomosis.core.profiles import ProfileError, capture_destination_profile
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


def _reserved(name: str) -> str | None:
    """Why ``name`` may not be taken, or ``None`` if free (31): a built-in
    id is always ``SourceIdReserved``, an already-learned id is
    ``SourceIdInUse`` so a reviewed mapping is never silently replaced.
    Read from the live registry, not a hardcoded list."""
    from anastomosis.sources import available_sources
    from anastomosis.sources.learned import LearnedSourceAdapter

    for adapter in available_sources():
        if adapter.name == name:
            return (
                "SourceIdInUse" if isinstance(adapter, LearnedSourceAdapter) else "SourceIdReserved"
            )
    return None


def _make_selectable(spec: MappingSpec) -> None:
    """Register the just-saved mapping so this session can use it now, not
    only after a restart. Best effort on purpose: the mapping is on disk
    and valid either way."""
    from anastomosis.sources import register
    from anastomosis.sources.learned import LearnedSourceAdapter

    try:
        register(LearnedSourceAdapter(spec))
    except Exception:  # pragma: no cover - defensive; the disk copy still stands
        logger.warning("saved mapping could not be registered in this session")


def _reviewed(analysis: SourceAnalysis, cmd: SourceInitCommand) -> SourceAnalysis:
    """The analysis with the review's grouping answers applied. The three
    answers are authoritative together, so ``None`` for the encounter key
    means "each row is its own visit", not "not touched". No review, no
    change."""
    if cmd.decisions is None:
        return analysis
    return replace(
        analysis,
        patient_key=cmd.patient_key,
        encounter_key=cmd.encounter_key,
        row_scope=cmd.row_scope or analysis.row_scope,
    )


def _proposal(analysis: SourceAnalysis) -> SourceInitResult:
    """The PHI-safe proposal every post-analyze outcome is built from:
    column names, target paths and counts, never a cell value."""
    from anastomosis.core.model_paths import canonical_target_paths

    profiles = {
        profile.name: (profile.inferred_type, profile.sample_shape) for profile in analysis.profiles
    }
    return SourceInitResult(
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
