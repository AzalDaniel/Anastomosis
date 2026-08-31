"""The shared learn-a-source command layer (one flow, two frontends).

``anast source init`` (:mod:`anastomosis.cli`) and the GUI's
:meth:`anastomosis.gui.controller.GuiController.source_init` both run the SAME
analyze -> confirm -> build -> round-trip -> save flow against
:mod:`anastomosis.core.sourcelearn`. That flow lives here exactly once, so the
same operator intent produces identical backend state regardless of which
frontend issued it (the CLI/GUI parity the maintainer asked for).

Mirrors :mod:`anastomosis.core.packinit`: a frozen ``SourceInitCommand``, a
``run_source_init_command`` returning structured, presentation-free data. It
never prints and never emits — each adapter presents the returned
:class:`SourceInitResult` (the CLI's Rich lines + exit codes, the GUI's dict +
events).

The two-step shape the frontends share (the learn-a-source wizard checkpoint):

* ``confirmed=False`` runs ANALYZE only — it returns the PHI-safe proposed
  mapping (grouping + per-column suggestions + summary) plus
  ``error="ConfirmationRequired"`` so the operator sees exactly what they are
  confirming, and writes NOTHING.
* ``confirmed=True`` builds the mapping, round-trips it against the example to
  PROVE no column is dropped, and only then saves it (owner-only) — returning the
  mapping directory and ``MAPPING.md``.

PHI rule (non-negotiable): nothing patient-derived is returned. The proposal
carries column NAMES, inferred type labels, counts, and digit/letter-masked
shapes only — never a cell value; the operator's example path is never echoed
back. On failure the ``error`` is an enumerated code; ``detail`` (when present)
is a sourcelearn diagnosis over column/target NAMES, never a cell value.

``sourcelearn`` and the learned-source package are imported lazily INSIDE
:func:`run_source_init_command`, so a minimal install imports this module cleanly.
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
    from anastomosis.sources.learned.spec import MappingSpec

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
    """One column's proposed mapping (PHI-safe: names, counts and a mask only).

    ``inferred_type`` and ``sample_shape`` are the column's own evidence — what
    the profiler saw, letters-for-letters and digits-for-digits, never a value.
    They are what lets a reviewer see that a 214-distinct text column is
    visibly not a date, without a vocabulary lesson.
    """

    source: str
    target: str | None
    transform: str
    confidence: float
    inferred_type: str = ""
    sample_shape: str = ""


@dataclass(frozen=True)
class SourceInitCommand:
    """A fully-specified learn-a-source request — the unit both frontends build.

    ``example`` is a structured FILE or a directory holding exactly one. ``name``
    is the lowercase mapping id (validated against :data:`PACK_NAME_RE`).
    ``out_dir`` is where the ``<name>/`` mapping dir is written (``None`` ->
    ``~/.anastomosis/sources``). ``confirmed`` is the review checkpoint: ``False``
    runs analyze-only and refuses to save, ``True`` round-trips and saves.
    """

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


@dataclass(frozen=True)
class SourceInitResult:
    """What a learn-a-source run yields the caller (the CLI and GUI frontends).

    ``ok`` is true only when a mapping was saved. ``error`` is ``None`` on
    success, else an enumerated code (``InvalidSourceName``, ``NoExampleFile``,
    ``AmbiguousExample``, ``CannotAnalyze``, ``ConfirmationRequired``,
    ``CannotBuildMapping``, ``MappingLoadFailed``, ``WouldDropColumns``,
    ``SaveFailed``). The proposal fields (``fmt_type`` .. ``mapped``) are
    populated whenever analyze succeeded (so a refusal still shows what was
    proposed). ``mapping_dir``/``mapping_md``/``record_count``/``unmapped`` are
    populated only on success. ``dropped_columns`` carries the offending column
    names for ``WouldDropColumns``; ``detail`` is a PHI-safe diagnosis for
    ``CannotAnalyze``/``CannotBuildMapping``/``MappingLoadFailed``.
    """

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


def resolve_example(example: Path) -> tuple[Path | None, str]:
    """Resolve an example to one structured file (the resolution both frontends share).

    Returns ``(file, "")`` for a file or a directory holding exactly one learnable
    file, else ``(None, code)`` where ``code`` is ``NoExampleFile`` (nothing of a
    learnable type) or ``AmbiguousExample`` (a directory holding more than one).
    Never raises.
    """
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
    """Run the analyze -> confirm -> build -> round-trip -> save flow; return data.

    No printing, no events — the adapter presents the result. The name is
    validated first (``InvalidSourceName``), then the example is resolved
    (``NoExampleFile``/``AmbiguousExample``), then analyzed. ``confirmed=False``
    returns the proposal with ``ConfirmationRequired`` (writes nothing);
    ``confirmed=True`` builds the mapping, proves it drops no column via a
    round-trip, and saves it owner-only.
    """
    # Type-guard the name up front so a malformed command returns a code rather
    # than raising — mirrors run_pack_init's contract.
    if not isinstance(cmd.name, str) or not PACK_NAME_RE.match(cmd.name):
        return SourceInitResult(ok=False, error="InvalidSourceName")

    # Asked BEFORE anything is analysed or written, and that ordering is the
    # fix. Registration already refuses to let a learned mapping shadow a
    # built-in — correctly, since `get_source("ccda")` must always be the C-CDA
    # adapter — but it refused at the END, in a log line, after save had
    # reported success and written the whole directory. An operator saw "saved",
    # got a folder full of their reviewed work, and could never select it: not
    # then, not after a restart, because the skip is permanent by design. The
    # collision is knowable from the name alone, so it is answered from the name
    # alone, before the operator spends any more of their attention on it.
    reserved = _reserved(cmd.name)
    if reserved is not None:
        return SourceInitResult(ok=False, error=reserved)

    resolved, resolve_error = resolve_example(cmd.example)
    if resolved is None:
        return SourceInitResult(ok=False, error=resolve_error)

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
    proposal = _proposal(analysis)

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

    # Available NOW, not after a restart. `pipeline` registers learned sources
    # once at import, so a format taught mid-session was written, valid, and
    # invisible: Charts and Migrate populate their choosers from the registry,
    # and nothing had told the registry to look again. `_reserved` above
    # guarantees the id is free, so there is no stale adapter to displace and
    # no ambiguity about which mapping answers to the name.
    _make_selectable(spec)

    return replace(
        proposal,
        ok=True,
        mapping_dir=mapping_dir,
        mapping_md=mapping_md,
        record_count=report.record_count,
        unmapped=len(spec.unmapped_source_fields),
    )


def _reserved(name: str) -> str | None:
    """Why ``name`` may not be taken, or ``None`` if it is free.

    Two refusals rather than one, because they are different situations for the
    person reading them. A built-in id can never be used — `ccda` belongs to the
    C-CDA adapter and always will. An id already learned is the operator's own
    earlier work, and refusing protects it: silently replacing a reviewed
    mapping would discard a decision somebody made about how a source file's
    columns become patient identity.

    Read from the live registry rather than a hardcoded list, so a source added
    later is reserved without anybody remembering to come back here.
    """
    from anastomosis.sources import available_sources
    from anastomosis.sources.learned import LearnedSourceAdapter

    for adapter in available_sources():
        if adapter.name == name:
            return (
                "SourceIdInUse" if isinstance(adapter, LearnedSourceAdapter) else "SourceIdReserved"
            )
    return None


def _make_selectable(spec: MappingSpec) -> None:
    """Register the just-saved mapping so this session can use it.

    Best effort on purpose: the mapping is on disk and valid either way, and a
    registry that refused would make a saved format unusable rather than merely
    unregistered. A failure here costs a restart, which is exactly where this
    stood before.
    """
    from anastomosis.sources import register
    from anastomosis.sources.learned import LearnedSourceAdapter

    try:
        register(LearnedSourceAdapter(spec))
    except Exception:  # pragma: no cover - defensive; the disk copy still stands
        logger.warning("saved mapping could not be registered in this session")


def _reviewed(analysis: SourceAnalysis, cmd: SourceInitCommand) -> SourceAnalysis:
    """The analysis with the review's grouping answers applied, when one exists.

    The three answers are authoritative together — the frontend always sends
    its current values — so ``None`` for the encounter key MEANS "each row is
    its own visit", not "not touched". No review, no change.
    """
    if cmd.decisions is None:
        return analysis
    return replace(
        analysis,
        patient_key=cmd.patient_key,
        encounter_key=cmd.encounter_key,
        row_scope=cmd.row_scope or analysis.row_scope,
    )


def _proposal(analysis: SourceAnalysis) -> SourceInitResult:
    """The PHI-safe proposal every post-analyze outcome is built from.

    Column names, target paths and counts — never a cell value. Lifted out of
    :func:`run_source_init_command` because it is pure construction from one
    argument and was carrying two comprehensions' worth of branching in a
    function that is otherwise a sequence of refusals.
    """
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
