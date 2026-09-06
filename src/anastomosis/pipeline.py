"""The frontend-agnostic pipeline core: ingest -> reconstruct -> optional QA,
shared by the CLI and the GUI.

:class:`StageEvent` is the seam: PHI-safe structured events (stage names,
counts, ids, exception TYPE names) through an optional ``on_event``
callback; this module never formats user-facing prose itself.

Loud failures: a missing source, an unavailable pack, a render failure, or a
failing QA report raise :class:`PipelineError`. Nothing vanishes silently.
"""

from __future__ import annotations

import json
import types
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Union, get_args, get_origin

from anastomosis.core.ccda_codes import EXT_PRIOR_LOSS_NARRATIVE
from anastomosis.core.conservation import ConservationError
from anastomosis.core.logutil import exc_tag, safe_log_id
from anastomosis.sources import (
    SourceDataError,
    available_sources,
    detect_source,
    get_source,
    selection_rules,
    with_selection,
)
from anastomosis.sources.learned import register_learned_sources

# Register any formats the operator has taught from an example (the user-dir
# scan is defensive — a broken mapping is skipped, never crashes import).
register_learned_sources()

if TYPE_CHECKING:
    from anastomosis.core.model import DocumentArtifact, Patient, PatientRecord
    from anastomosis.qa import QAReport
    from anastomosis.reconstruct.ccda_standard import CCDARenderResult
    from anastomosis.reconstruct.engine import ReconstructionEngine, RenderResult
    from anastomosis.reconstruct.provenance import RenderProvenance
    from anastomosis.sources.base import QuarantinedRows, SourceAdapter

__all__ = [
    "EXT_FOLDED_RECORDS",
    "LOSS_LEDGER_FILENAME",
    "QUARANTINE_FILENAME",
    "RECORD_SUMMARY_DIRNAME",
    "STAGE_DETECT",
    "STAGE_INGEST",
    "STAGE_MANIFEST",
    "STAGE_QA",
    "STAGE_RECONSTRUCT",
    "PipelineError",
    "PipelineResult",
    "StageEvent",
    "load_records",
    "parse_section_overrides",
    "parse_selection_includes",
    "run_pipeline",
    "settle_source_ledger",
]


# Stage names, fixed so both frontends and the tests share one vocabulary.
STAGE_DETECT = "detect"
STAGE_INGEST = "ingest"
STAGE_RECONSTRUCT = "reconstruct"
STAGE_QA = "qa"
# The opt-in upload-manifest stage (emitted only when a command requests the
# manifest write, so a manifest-off run is byte-identical to before).
STAGE_MANIFEST = "manifest"


@dataclass(frozen=True)
class StageEvent:
    """Contract: one PHI-safe progress signal. ``counts`` carries only
    integers, never patient-derived strings; ``detail`` is a small PHI-free
    slot for facts like a detected source or chosen pack name.
    """

    stage: str
    counts: dict[str, int] = field(default_factory=dict)
    detail: str = ""
    #: True when the stage was downgraded to a no-op: a frontend must not
    #: paint it as completed, or an operator believes unrun work ran.
    skipped: bool = False


class PipelineError(Exception):
    """Contract: a loud, PHI-safe pipeline failure — ``message`` names
    sources/packs/counts/exception types only; ``exit_code`` is 2 for a
    missing source or pack, 1 otherwise. ``failed`` (per-encounter
    ``(encounter_id, exception_type)`` pairs, render failures only) is GUI-ignored.
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
        # no_source, bad_source, bad_input, empty_export, bad_pack,
        # bad_section, bad_selection, bad_output, bad_destination,
        # output_locked, render_failed, conservation_failed, qa_failed,
        # generic.
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
    #: The source ledger's account of the load, one sentence per line (see
    #: ``settle_source_ledger``); PHI-free, so a frontend may print it verbatim.
    source_reading: tuple[str, ...] = ()
    #: This run's rendered layout, named by its bytes; kept here (not only on
    #: disk) so the upload manifest and layout gate measure the same digest.
    provenance: RenderProvenance | None = None


EventSink = Callable[[StageEvent], None]


_SECTION_ON = frozenset({"on", "true", "1", "yes"})
_SECTION_OFF = frozenset({"off", "false", "0", "no"})


def parse_section_overrides(section: list[str] | None) -> dict[str, bool]:
    """Turn ``["insurance=on", "addenda=off"]`` into ``{"insurance": True, ...}``.
    Shared by both frontends so section toggles mean the same thing in each.
    Strict: an unrecognized value or a bare NAME (no ``=value``) raises
    :class:`PipelineError` (exit 2) rather than silently defaulting to False.
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


def parse_selection_includes(include: list[str] | None) -> frozenset[str]:
    """Turn ``["growth-charts"]`` into the set of selection rules to switch
    OFF, shared by both frontends. Strict like
    :func:`parse_section_overrides`: a blank ``--include`` raises
    :class:`PipelineError` (exit 2) rather than "include nothing".
    """
    names: set[str] = set()
    for item in include or []:
        name = item.strip()
        if not name:
            raise PipelineError(
                "--include needs the name of a selection rule (e.g. --include growth-charts).",
                exit_code=2,
                kind="bad_selection",
            )
        names.add(name)
    return frozenset(names)


def _switched_off(adapter: SourceAdapter, include: list[str] | None) -> frozenset[str]:
    """The selection rules this run drops, validated against the adapter's own
    set — thinking a rule was switched off when it was mistyped is worse than
    not being able to switch it off at all.
    """
    wanted = parse_selection_includes(include)
    known = {rule.name for rule in selection_rules(adapter)}
    unknown = sorted(wanted - known)
    if unknown:
        offered = ", ".join(sorted(known)) or "(none)"
        raise PipelineError(
            f"Unknown --include {', '.join(unknown)} for source {adapter.name!r}. "
            f"Known: {offered}.",
            exit_code=2,
            kind="bad_selection",
        )
    return wanted


def _selection_rules_report(
    adapter: SourceAdapter, include: frozenset[str]
) -> list[dict[str, object]]:
    """Every render-selection rule this source has, and whether this run ran
    it — names and reasons only (RULES.md 2), in the adapter's own order, so
    a report of nothing excluded is never confused with all rules off.
    """
    return [
        {
            "adapter": adapter.name,
            "rule": rule.name,
            "reason": rule.reason,
            "label": rule.label,
            "applied": rule.name not in include,
        }
        for rule in selection_rules(adapter)
    ]


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
    # Detection only answers "which format is this?", so it cannot tell an
    # unrecognized folder from a missing one — name what's actually wrong.
    if not export_dir.exists():
        raise PipelineError(
            f"There is no folder at {export_dir}. Check the path and try again.",
            exit_code=2,
            kind="bad_export_dir",
        )
    if not export_dir.is_dir():
        raise PipelineError(
            f"{export_dir} is a file, not a folder. Choose the folder your EHR "
            "gave you, not a file inside it.",
            exit_code=2,
            kind="bad_export_dir",
        )
    detected = detect_source(export_dir)
    if detected is None:
        known = ", ".join(a.name for a in available_sources())
        raise PipelineError(
            f"Could not identify the export format. Try --source ({known})",
            exit_code=2,
            kind="no_source",
        )
    return detected


def load_records(adapter: SourceAdapter, export_dir: Path) -> list[PatientRecord]:
    """Contract: converts three failure modes into a PHI-safe
    :class:`PipelineError` (exit 2) — an adapter's own refusal keeps its
    message; any other bad export names only the source and exception TYPE;
    zero records from a non-empty export is itself a defect.
    """
    try:
        records = list(adapter.load(export_dir))
    except PipelineError:
        raise
    except ConservationError as exc:
        # The load's own instrument (C-CDA's ledger) could not account for a
        # document; PHI-safe by Conservation's own contract (names, counts only).
        raise PipelineError(str(exc), exit_code=1, kind="conservation_failed") from None
    except SourceDataError as exc:
        raise PipelineError(
            f"Could not read the {adapter.name} export: {exc}",
            exit_code=2,
            kind="bad_input",
        ) from None
    except Exception as exc:
        raise PipelineError(
            f"Could not read the {adapter.name} export ({exc_tag(exc)}).",
            exit_code=2,
            kind="bad_input",
        ) from None
    if not records:
        # C-CDA's own `skipped_files` count (#384) means the export DID read
        # as ccda, just under the wrong extension — `getattr` since no other
        # adapter keeps this count.
        skipped = getattr(adapter, "skipped_files", 0)
        if skipped:
            from anastomosis.sources.ccda.ledger import skipped_files_clause

            raise PipelineError(
                f"No records loaded from the {adapter.name} export: "
                f"{skipped_files_clause(skipped)}.",
                exit_code=2,
                kind="empty_export",
            )
        raise PipelineError(
            f"No records loaded from the {adapter.name} export — is this a {adapter.name} export?",
            exit_code=2,
            kind="empty_export",
        )
    return _fold_records_sharing_a_patient(records)


#: How many source records folded into one chart, on the merged record's own
#: extensions (only when a fold happened) — a COUNT, never a source filename
#: (no PHI-safe precedent for carrying those into a delivered artifact).
#: Model-level ``anast:`` namespace, like ``anast:inline_content``: the writer
#: is the pipeline, which must not know which adapter filled the records in.
EXT_FOLDED_RECORDS = "anast:folded_source_records"


def _fold_records_sharing_a_patient(records: list[PatientRecord]) -> list[PatientRecord]:
    """Contract: one patient is one chart, whatever the adapter yielded —
    every destination is keyed by ``patient.id`` and overwrites, so two
    records under one id would silently replace each other. A lone record
    passes through unchanged; each keeps its load position for later.
    """
    groups: dict[str, list[tuple[int, PatientRecord]]] = {}
    for position, record in enumerate(records, start=1):
        groups.setdefault(record.patient.id, []).append((position, record))
    total = len(records)
    return [
        entries[0][1] if len(entries) == 1 else _merged_record(entries, total)
        for entries in groups.values()
    ]


def _merged_record(entries: list[tuple[int, PatientRecord]], total: int) -> PatientRecord:
    """Contract: the one chart several source records for one patient make.
    Collections union in read order; encounters fold per RULES.md 9 (#393),
    nothing else dedupes. The first record's ``id``/``provenance`` stay; how
    many merged rides :data:`EXT_FOLDED_RECORDS`, folded in like any other key.
    """
    from anastomosis.core.model import PatientRecord
    from anastomosis.sources.ccda.parser import fold_encounters_sharing_an_id

    positions = tuple(position for position, _ in entries)
    group = [record for _, record in entries]
    update: dict[str, Any] = {
        name: [item for record in group for item in getattr(record, name)]
        for name in _record_list_fields(PatientRecord)
    }
    update["encounters"] = fold_encounters_sharing_an_id(update["encounters"])
    update["patient"] = _merged_patient([record.patient for record in group], positions, total)
    update["extensions"] = _merged_extensions(
        [*(record.extensions for record in group), {EXT_FOLDED_RECORDS: len(group)}]
    )
    return group[0].model_copy(update=update)


#: Every :class:`~anastomosis.core.model.AnastBase` field beyond content
#: itself (id, provenance, extensions); the fold handles these by name.
_MODEL_IDENTITY_FIELDS = frozenset({"id", "provenance", "extensions"})

#: PatientRecord fields the fold names explicitly; everything else on that
#: model is a collection, and an unnamed field makes the fold refuse to run.
_RECORD_NAMED_FIELDS = _MODEL_IDENTITY_FIELDS | {"patient"}

#: Origins ``get_origin`` reports for an ``X | None`` annotation, the shape
#: every optional canonical-model field is written in. ``types.UnionType``
#: covers ``X | None``; ``typing.Union`` covers ``Optional[X]``, reported
#: differently, so both are named.
_OPTIONAL_ORIGINS = (types.UnionType, Union)


def _classified_origin(annotation: Any) -> Any:
    """``get_origin(annotation)``, peeled through an ``X | None`` to the ``X``
    a collection guard needs — unpeeled, ``list[str] | None`` reports as
    ``types.UnionType``, so an optional collection field would vanish from
    classification entirely.
    """
    origin = get_origin(annotation)
    if origin in _OPTIONAL_ORIGINS:
        members = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(members) == 1:
            return get_origin(members[0])
    return origin


@cache
def _record_list_fields(model: type[PatientRecord]) -> tuple[str, ...]:
    """Every collection the fold unions, derived off the model (never
    hand-listed) so a new one is unioned by default, not silently skipped. A
    field that is neither a collection nor one of the four identity fields
    stops the fold loudly: losslessness is not discovered missing later.
    """
    collections = tuple(
        name
        for name, info in model.model_fields.items()
        if _classified_origin(info.annotation) is list
    )
    unhandled = sorted(set(model.model_fields) - set(collections) - _RECORD_NAMED_FIELDS)
    if unhandled:
        raise TypeError(
            f"PatientRecord field(s) {', '.join(unhandled)} are neither a collection nor "
            "identity, so the fold that merges two records for one patient would drop them"
        )
    return collections


def _merged_patient(patients: list[Patient], positions: tuple[int, ...], total: int) -> Patient:
    """Contract: one patient's demographics from several records. A field one
    record states and another leaves empty is taken from the one with it. A
    SINGLE-valued disagreement refuses the run; list-valued fields union
    instead (RULES.md 9). ``positions``/``total`` exist only for the refusal.
    """
    model = type(patients[0])
    update: dict[str, Any] = {}
    singles = {
        name: _stated_values(patients, name)
        for name in _patient_demographic_fields(model)
        if name not in _patient_list_fields(model)
    }
    _refuse_disagreeing_demographics(patients[0].id, singles, positions, total)
    update.update({name: values[0] for name, values in singles.items() if values})
    update.update(
        {
            name: _unioned([getattr(patient, name) for patient in patients])
            for name in _patient_list_fields(model)
        }
    )
    update["extensions"] = _merged_extensions([patient.extensions for patient in patients])
    return patients[0].model_copy(update=update)


#: Typing origins meaning "several values" without the ``list[...]`` shape
#: :func:`_patient_list_fields` looks for; missing one here means a new
#: collection demographic falls silently into the single-valued bucket.
_SEQUENCE_LIKE_ORIGINS = (tuple, frozenset, set, Sequence)


@cache
def _patient_list_fields(model: type[Patient]) -> frozenset[str]:
    """The demographics that hold several values at once. An unrecognised
    collection falls to the single-valued bucket by default, so this raises
    loudly on anything that still LOOKS like one after unwrapping — the
    parity :func:`_record_list_fields` keeps for record-level fields.
    """
    collections = frozenset(
        name
        for name, info in model.model_fields.items()
        if name not in _MODEL_IDENTITY_FIELDS and _classified_origin(info.annotation) is list
    )
    unhandled = sorted(
        name
        for name, info in model.model_fields.items()
        if name not in _MODEL_IDENTITY_FIELDS
        and name not in collections
        and _classified_origin(info.annotation) in _SEQUENCE_LIKE_ORIGINS
    )
    if unhandled:
        raise TypeError(
            f"Patient field(s) {', '.join(unhandled)} look like a collection but are not "
            "list[...], so the fold's single-valued/list split would misclassify them"
        )
    return collections


def _unioned(lists: list[list[Any]]) -> list[Any]:
    """Every distinct value the records state, in first-stated order.
    Distinct by VALUE: two records stating the same address contribute one,
    since a chart listing it twice is a chart nobody wrote.
    """
    out: list[Any] = []
    for value in (value for values in lists for value in values):
        if value not in out:
            out.append(value)
    return out


@cache
def _patient_demographic_fields(model: type[Patient]) -> tuple[str, ...]:
    """Every field of :class:`~anastomosis.core.model.Patient` that describes the
    person rather than the record — read off the model for the reason
    :func:`_record_list_fields` is."""
    return tuple(name for name in model.model_fields if name not in _MODEL_IDENTITY_FIELDS)


def _stated_values(patients: list[Patient], name: str) -> list[Any]:
    """What the records actually say for one field, gaps left out — ``None``
    and ``[]`` both mean "did not say", not a disagreement with one that did.
    """
    return [
        value
        for value in (getattr(patient, name) for patient in patients)
        if value is not None and value != []
    ]


def _position_list(positions: tuple[int, ...]) -> str:
    """Renders as "7 and 8" / "7, 8 and 9" — colliding records' 1-based
    load-order positions, worded for a one-line refusal message. Never a
    filename, never a value: a position is the one PHI-free way to point at a
    record that :func:`_refuse_disagreeing_demographics` names.
    """
    ordered = [str(position) for position in sorted(positions)]
    if len(ordered) == 1:
        return ordered[0]
    return f"{', '.join(ordered[:-1])} and {ordered[-1]}"


def _refuse_disagreeing_demographics(
    patient_id: str, stated: dict[str, list[Any]], positions: tuple[int, ...], total: int
) -> None:
    """Stop the run when the records for one patient id describe two people.
    Names FIELDS and counts only (RULES.md 2), never values. Locates the
    collision by the records' load-order POSITIONS (never a filename): a
    run-scoped surrogate alone says which patient, not which record.
    """
    disagreed = sorted(
        name for name, values in stated.items() if any(value != values[0] for value in values)
    )
    if not disagreed:
        return
    raise PipelineError(
        f"{len(positions)} records ({_position_list(positions)} of {total}, in load order) "
        f"for patient {safe_log_id(patient_id)} share one patient id but disagree on "
        f"{len(disagreed)} demographic field(s) ({', '.join(disagreed)}); refusing to merge "
        "them into one chart — the source cannot say which of them is the patient",
        exit_code=2,
        kind="bad_input",
    )


def _merged_extensions(dicts: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Contract: one ``extensions`` dict from several, keeping everything all
    of them hold. A loss ledger merges per RULES.md 61; an agreeing or
    lone-stated key keeps its value; a key two sides state DIFFERENTLY keeps
    BOTH, the second parked at the next free ``#2``/``#3`` variant.
    """
    from anastomosis.sources.ccda.parser import free_key, is_loss_ledger, merge_loss_narrative

    merged: dict[str, Any] = {}
    for extensions in dicts:
        for key, value in extensions.items():
            existing = merged.get(key)
            if (
                key == EXT_PRIOR_LOSS_NARRATIVE
                and is_loss_ledger(value)
                and (key not in merged or is_loss_ledger(existing))
            ):
                merge_loss_narrative(merged, value["generation"], value["entries"])
            elif key not in merged or merged[key] == value:
                merged[key] = value
            else:
                merged[free_key(merged, key)] = value
    return merged


#: Where a run keeps carried-through source attachments, inside the same
#: hardened output directory as the charts — deliverers read only this
#: directory, never the export, which a later `anast archive` may not reach.
ATTACHMENTS_DIRNAME = "attachments"

#: Where a run publishes the C-CDA ledger's full source-vs-arrived account
#: (see ``settle_source_ledger``), beside the charts and PHI-vetted the same
#: way — ``assert_emittable`` walks it at the point of writing.
LOSS_LEDGER_FILENAME = "loss_ledger.json"

#: Where a run persists rows its adapter could not place on any patient (see
#: :class:`~anastomosis.sources.base.QuarantinedRows`); events, logs and the
#: CLI carry counts only.
QUARANTINE_FILENAME = "quarantine.json"


#: What the charts in an output directory were rendered from: layout and
#: switched-on sections, so a later run into the same directory can tell if
#: it already answers the question being asked. Records the run's INTENT;
#: what actually ran lands beside it in
#: :data:`~anastomosis.reconstruct.provenance.RENDER_PROVENANCE_NAME`.
RENDER_SETTINGS_NAME = "render_settings.json"


def _render_settings(
    pack: str, flags: dict[str, bool], include: frozenset[str]
) -> dict[str, object]:
    """The run's rendering intent, comparable across two runs. ``included``
    rides only when a selection rule was actually switched off, so an older
    build's folder is never refused over a key that was never in it.
    """
    settings: dict[str, object] = {
        "version": 1,
        "pack": pack,
        "sections": dict(sorted(flags.items())),
    }
    if include:
        settings["included"] = sorted(include)
    return settings


def _guard_render_settings(out: Path, settings: dict[str, object], *, force: bool) -> None:
    """Refuse a run whose settings differ from the ones that made these
    charts — the idempotent skip decides on ``target.exists()`` alone, so a
    section switched OFF would otherwise "complete" while every existing
    chart still carries it. ``--force`` says to rebuild anyway.
    """
    if force:
        return
    record = out / RENDER_SETTINGS_NAME
    if not record.is_file():
        return
    try:
        previous = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return  # unreadable: treat as absent rather than block a run over it
    if previous == settings:
        return

    changed = _settings_difference(previous, settings)
    raise PipelineError(
        f"The charts already in this folder were built with different settings "
        f"({changed}). Re-run with --force to rebuild them, or choose an empty "
        f"folder — otherwise they would be left as they are and the new "
        f"settings would have no effect.",
        exit_code=2,
        kind="settings_changed",
    )


def _guard_render_provenance(out: Path, provenance: RenderProvenance, *, force: bool) -> None:
    """Refuse a run whose layout is not the layout that made these charts —
    the sibling of :func:`_guard_render_settings`, which compares the
    layout's NAME, not its edited bytes. Absent or unreadable record: nothing
    to compare, so nothing to refuse.
    """
    from anastomosis.reconstruct.provenance import RENDER_PROVENANCE_NAME, provenance_difference

    if force:
        return
    record = out / RENDER_PROVENANCE_NAME
    if not record.is_file():
        return
    try:
        previous = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return  # unreadable: treat as absent rather than block a run over it
    if not isinstance(previous, dict):
        return
    changed = provenance_difference(previous, provenance.as_json())
    if not changed:
        return
    raise PipelineError(
        f"The charts already in this folder were rendered from different layout bytes "
        f"({changed}). They cannot be re-run into as though nothing moved: review the "
        f"layout, then re-run with --force to rebuild every chart from what it holds "
        f"now, or choose an empty folder.",
        exit_code=2,
        kind="layout_changed",
    )


def _settle_render_provenance(
    out: Path, provenance: RenderProvenance, templates: dict[str, str]
) -> None:
    """Contract: publish what the charts were produced from; refuse a
    mid-run layout swap before writing the record, since a digest mismatch
    means two layouts rendered this batch. Failure marks nothing; ``--force``
    into an empty folder is the named remedy.
    """
    from anastomosis.reconstruct.provenance import RENDER_PROVENANCE_NAME, swapped_templates

    settled = provenance.with_templates(templates)
    swapped = swapped_templates(settled)
    if swapped:
        raise PipelineError(
            f"The layout changed while this batch was rendering ({', '.join(swapped)}), so "
            f"these charts did not all come from one layout. Nothing here can say which "
            f"chart came from which; re-run with --force into an empty folder.",
            exit_code=1,
            kind="layout_changed",
        )
    _write_json(out / RENDER_PROVENANCE_NAME, settled.as_json())


def _as_list(value: object) -> list[object]:
    """A settings value read back off disk, as a list — anything else is none."""
    return value if isinstance(value, list) else []


def _included_difference(previous: dict[str, object], current: dict[str, object]) -> list[str]:
    """The selection rules this run switches off that the last one did not, and
    the other way round. Rule names only."""
    was = {str(name) for name in _as_list(previous.get("included"))}
    now = {str(name) for name in _as_list(current.get("included"))}
    return [f"selection rule {name} now included" for name in sorted(now - was)] + [
        f"selection rule {name} applied again" for name in sorted(was - now)
    ]


def _settings_difference(previous: dict[str, object], current: dict[str, object]) -> str:
    """A short, PHI-free description of what the operator changed."""
    parts: list[str] = _included_difference(previous, current)
    if previous.get("pack") != current.get("pack"):
        parts.append(f"layout {previous.get('pack')!r} -> {current.get('pack')!r}")
    was = previous.get("sections")
    now = current.get("sections")
    if isinstance(was, dict) and isinstance(now, dict):
        parts += [
            f"{name} {'on' if was.get(name) else 'off'} -> {'on' if value else 'off'}"
            for name, value in sorted(now.items())
            if was.get(name) != value
        ]
        parts += [f"{name} no longer offered" for name in sorted(set(was) - set(now))]
    return "; ".join(parts) or "settings differ"


#: Adapters park the encounters their own selection rules kept out of the
#: render under an extension key ending in this, so nothing is dropped. The
#: suffix is the convention; the prefix is the adapter's namespace, as in
#: ``pf_tebra:skipped_encounters``.
SELECTION_EXCLUDED_SUFFIX = ":skipped_encounters"

#: The reconciliation artifact. An operator counting 8 encounter rows into a
#: run and 6 charts out of it needs somewhere to read the other two.
SELECTION_REPORT_NAME = "selection_report.json"

#: Report schema. Version 2 added ``rules``: every selection rule the source
#: has and whether this run applied it. Version 1 could say what was left out
#: but not what was ASKED, so two runs made under different options could not
#: be read against each other — an empty ``excluded`` meant either "the rules
#: found nothing" or "there were no rules running", and those are opposite
#: answers.
SELECTION_REPORT_VERSION = 2


def _selection_exclusions(records: list[PatientRecord]) -> list[dict[str, str]]:
    """Every encounter an adapter's selection rules kept out of the render —
    ids and rule names only, so this carries none of the chart and can be
    written beside the charts and read back without care.
    """
    exclusions: list[dict[str, str]] = []
    for record in records:
        for key, value in record.extensions.items():
            if not key.endswith(SELECTION_EXCLUDED_SUFFIX) or not isinstance(value, list):
                continue
            for entry in value:
                if not isinstance(entry, dict):
                    continue
                encounter = entry.get("encounter")
                exclusions.append(
                    {
                        "patient_id": record.patient.id,
                        "encounter_id": (
                            str(encounter.get("id", "")) if isinstance(encounter, dict) else ""
                        ),
                        "reason": str(entry.get("reason", "")),
                        "rule_source": key.removesuffix(SELECTION_EXCLUDED_SUFFIX),
                    }
                )
    return exclusions


def settle_quarantine(adapter: SourceAdapter, out: Path) -> dict[str, int]:
    """Persist what the adapter held back; return the INGEST event's extra
    counts. Shared by both orchestrators so an operator reading the stage
    rail cannot tell which produced the run. Rows go to ``quarantine.json``,
    grouped by table and reason; a clean run also removes a stale one.
    """
    from anastomosis.core.output import secure_output_dir

    held = list(getattr(adapter, "quarantine", ()))
    if not held:
        (out / QUARANTINE_FILENAME).unlink(missing_ok=True)
        return {}
    payload, total = _quarantine_payload(held)
    _write_json(secure_output_dir(out) / QUARANTINE_FILENAME, payload)
    return {"quarantined": total}


def _quarantine_payload(held: list[QuarantinedRows]) -> tuple[dict[str, object], int]:
    """``quarantine.json``'s shape: grouped by table and reason, rows verbatim,
    sorted for a deterministic artifact; plus the total for the INGEST event."""
    total = sum(len(entry.rows) for entry in held)
    payload: dict[str, object] = {
        "quarantine": [
            {
                "table": entry.table,
                "reason": entry.reason,
                "rows": [dict(row) for row in entry.rows],
            }
            for entry in sorted(held, key=lambda entry: (entry.table, entry.reason))
        ],
        "total_rows": total,
    }
    return payload, total


def settle_source_ledger(adapter: SourceAdapter, out: Path) -> tuple[str, ...]:
    """Contract: publish the source ledger's construct-by-construct account
    to ``loss_ledger.json`` beside the charts; return its reading as
    PHI-free, chart-vocabulary sentences. No ledger means an empty reading
    and nothing written; a re-run that ledgers nothing removes stale output.
    """
    from anastomosis.core.output import secure_output_dir

    ledgers = list(getattr(adapter, "ledgers", ()))
    skipped_files = getattr(adapter, "skipped_files", 0)
    if not ledgers:
        (out / LOSS_LEDGER_FILENAME).unlink(missing_ok=True)
        return ()
    from anastomosis.sources.ccda.ledger import aggregate, physician_reading

    corpus = aggregate(ledgers, skipped_files=skipped_files)
    _write_json(secure_output_dir(out) / LOSS_LEDGER_FILENAME, corpus.as_report())
    return physician_reading(corpus)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    """One sidecar, written atomically and deterministically."""
    from anastomosis.core.atomic import atomic_write_text

    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True))


def _write_selection_report(
    out: Path, exclusions: list[dict[str, str]], rules: list[dict[str, object]]
) -> None:
    """Write the run's selection report on every run, including an empty
    one: an absent file cannot distinguish "nothing was left out" from a run
    that never looked. ``rules`` says what was ASKED, for the same reason.
    """
    _write_json(
        out / SELECTION_REPORT_NAME,
        {"version": SELECTION_REPORT_VERSION, "excluded": exclusions, "rules": rules},
    )


def _carry_attachments(records: list[PatientRecord], export_dir: Path, out: Path) -> int:
    """Contract: put every source-resolved attachment into the run's output,
    keeping the export's own storage-id name, claimed through the delivery
    ledger with the file's digest as witness (two files reusing one id are
    refused). Raises :class:`PipelineError` if a claimed attachment is missing.
    """
    from anastomosis.core.output import secure_output_dir

    named = [doc for record in records for doc in record.documents if doc.path]
    if not named:
        return 0

    target = secure_output_dir(out / ATTACHMENTS_DIRNAME)
    root = export_dir.resolve()
    claims: dict[str, str] = {}
    failures: list[str] = []
    for doc in named:
        if failure := _carry_one(doc, root, target, claims):
            failures.append(failure)

    missing = sum(1 for doc in named if not (target / _delivered_name(doc)).is_file())
    if missing:
        kinds = ", ".join(sorted(set(failures))) or "the file was not where the export said"
        raise PipelineError(
            f"{missing} of {len(named)} attachment(s) named by the records did not reach "
            f"the output ({kinds}); refusing to deliver charts without the documents they "
            "reference",
            exit_code=1,
            kind="attachment_missing",
        )
    return len({_delivered_name(doc) for doc in named})


def _delivered_name(doc: DocumentArtifact) -> str:
    """What this artifact is called inside the output directory."""
    return Path(str(doc.path)).name


def _carry_one(
    doc: DocumentArtifact, root: Path, target: Path, claims: dict[str, str]
) -> str | None:
    """Land one artifact in ``target``; a PHI-safe failure tag, or ``None``.
    A source naming a file in its export gets it COPIED; a source carrying
    its own bytes (e.g. a C-CDA scan) gets them WRITTEN — either way, one
    file under one claimed name."""
    from anastomosis.core.model import EXT_INLINE_CONTENT
    from anastomosis.deliver._shared import DeliveredNameCollision, claim_delivered_name

    name = _delivered_name(doc)
    try:
        claim_delivered_name(claims, name, doc.id, kind="attachment", content=doc.sha256)
    except DeliveredNameCollision as exc:
        raise PipelineError(str(exc), exit_code=1, kind="attachment_collision") from None
    destination = target / name
    if destination.is_file():
        return None  # the same file claimed twice, or a resumed run
    inline = doc.extensions.get(EXT_INLINE_CONTENT)
    if inline is None:
        return _copy_from_export(Path(str(doc.path)), name, root, destination)
    return _write_inline(str(inline), destination)


def _copy_from_export(relative: Path, name: str, root: Path, destination: Path) -> str | None:
    """Copy the export's own file into the output; the failure tag, or
    ``None``. Reading stays inside the export root: a ``../..`` in a
    hand-made bundle must not copy a file from anywhere else into an output
    directory that gets delivered onward.
    """
    from anastomosis.deliver._shared import copy_delivered_file

    source = (root / relative).resolve()
    if not source.is_relative_to(root):
        raise PipelineError(
            f"an attachment path points outside the export ({name!r}); refusing to read it",
            exit_code=1,
            kind="attachment_escape",
        )
    return copy_delivered_file(source, destination)


def _write_inline(content: str, destination: Path) -> str | None:
    """Write an artifact the record carries its own bytes for; return the
    failure's exception TYPE name, or ``None``. Undecodable base64 is the
    artifact arriving as nothing, same as a file that would not copy. The
    bytes are a patient's document: written, never logged.
    """
    import base64
    import binascii

    from anastomosis.core.atomic import atomic_write_bytes
    from anastomosis.core.logutil import exc_tag

    try:
        atomic_write_bytes(destination, base64.b64decode(content, validate=True))
    except (OSError, binascii.Error, ValueError) as exc:
        return exc_tag(exc)
    return None


#: Where the whole-patient record summaries land inside the output directory,
#: one PDF per patient beside the per-encounter charts.
RECORD_SUMMARY_DIRNAME = "record-summary"


def _render_record_summaries(
    records: list[PatientRecord], out: Path, *, force: bool
) -> CCDARenderResult:
    """Contract: render one whole-patient record summary per patient (HL7's
    whole-patient C-CDA view), since an encounter-scoped layout misses a
    fact no encounter claims. Loud on failure: raises with pseudonymous
    ``(patient_id, exception-type)`` pairs. Returns the render result."""
    from anastomosis.reconstruct.ccda_standard import render_ccda_standard

    view = render_ccda_standard(records, out / RECORD_SUMMARY_DIRNAME, force=force)
    if view.failed:
        raise PipelineError(
            f"{len(view.failed)} record summary/summaries failed to render",
            exit_code=1,
            kind="render_failed",
            failed=tuple(view.failed),
        )
    return view


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
    include: list[str] | None = None,
    on_event: EventSink | None = None,
) -> PipelineResult:
    """Contract: the full pipeline (ingest -> reconstruct -> optional QA),
    frontend-free. Emits PHI-safe :class:`StageEvent`\\ s through
    ``on_event``, raises :class:`PipelineError` on failure, and returns
    state for delivery. ``section``/``include`` override layout and rules."""
    from anastomosis.core.output import OutputPathError, validate_output_target
    from anastomosis.reconstruct import discover_packs
    from anastomosis.reconstruct.chromium import ChromiumRenderer, RendererUnavailable
    from anastomosis.reconstruct.engine import ReconstructionEngine
    from anastomosis.reconstruct.packtrust import default_pack_trust
    from anastomosis.reconstruct.provenance import pack_provenance

    emit = on_event or (lambda _event: None)

    # Pre-flight the output dir before any ingest/render work, so a bad path
    # fails in milliseconds rather than deep in the engine after a long run.
    try:
        validate_output_target(out)
    except OutputPathError as exc:
        raise PipelineError(str(exc), exit_code=2, kind="bad_output") from None

    adapter = resolve_source(export_dir, source)
    emit(StageEvent(STAGE_DETECT, detail=adapter.name))
    # Selection choices settle against the resolved source before anything is
    # read: an unknown rule name is cheap to reject before the export opens.
    switched_off = _switched_off(adapter, include)
    rules_report = _selection_rules_report(adapter, switched_off)
    adapter = with_selection(adapter, switched_off)

    dirs = list(pack_dirs or [])
    # The trust store is always consulted, since a learned layout in the
    # per-user pack dir must prove its code is what was confirmed; built-ins
    # need none.
    statuses = discover_packs(
        dirs,
        allow_external=bool(dirs),
        trust=default_pack_trust(),
        trust_new=trust_new,
    )
    status = statuses.get(pack)
    if status is None or status.pack is None:
        # No fallback, ever: rendering through some OTHER layout would be the
        # same false completion, just in a costlier place.
        diagnosis = status.diagnosis if status else f"unknown pack (have: {', '.join(statuses)})"
        raise PipelineError(f"Pack {pack!r} unavailable: {diagnosis}", exit_code=2, kind="bad_pack")

    overrides = parse_section_overrides(section)
    manifest = status.pack.manifest
    # A typo'd or unknown section must not silently change backend state:
    # reject it loudly against the pack's own matrix.
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
    # Before any ingest work: if this folder already holds charts, do they
    # answer the question being asked? Refuse here, not as a silent no-op later.
    settings = _render_settings(pack, engine.section_flags, switched_off)
    _guard_render_settings(out, settings, force=force)
    # Same question about the layout's BYTES, not its name: refuse before a
    # second layout's pages get mixed in. `status.pack` is not None here —
    # the unavailable case already raised.
    provenance = pack_provenance(status.pack, status.origin)
    _guard_render_provenance(out, provenance, force=force)

    records = load_records(adapter, export_dir)
    emit(
        StageEvent(
            STAGE_INGEST,
            counts={"records": len(records), **settle_quarantine(adapter, out)},
        )
    )
    source_reading = settle_source_ledger(adapter, out)

    try:
        result = engine.run(records, out, force=force)
    except RendererUnavailable as exc:
        # A property of the machine, not of a chart: exit 2, the code this
        # pipeline uses for "a capability this run needs is not available
        # here", the same class as an unavailable pack.
        raise PipelineError(str(exc), exit_code=2, kind="render_unavailable") from None
    except ConservationError as exc:
        # The seam lost work: the stage can't say what became of all N
        # encounters, so nothing downstream may treat survivors as the whole set.
        raise PipelineError(str(exc), exit_code=1, kind="conservation_failed") from None
    if result.failed:
        # Loud before the stage announces finished or anything is carried: a
        # run that could not render every chart must not scatter records around.
        raise PipelineError(
            f"{len(result.failed)} encounter(s) failed to render",
            exit_code=1,
            kind="render_failed",
            failed=tuple(result.failed),
        )

    # The charts are one visit each; this is the record, rendered before
    # attachments/QA so an incomplete record stops here, not delivered as whole.
    summaries = _render_record_summaries(records, out, force=force)

    # `out` is hardened by the engine above, so this is the first point a
    # patient's own files may be written beside their charts.
    carried = _carry_attachments(records, export_dir, out)
    exclusions = _selection_exclusions(records)
    _write_selection_report(out, exclusions, rules_report)
    # Provenance settles before the settings record, from the same
    # measurement the guard used, so a mid-render layout swap refuses before
    # either record claims the folder is coherent.
    _settle_render_provenance(out, provenance, engine.templates_read)
    _write_json(out / RENDER_SETTINGS_NAME, settings)
    emit(
        StageEvent(
            STAGE_RECONSTRUCT,
            counts={
                "rendered": len(result.rendered),
                "skipped": len(result.skipped),
                "excluded": len(exclusions),
                "failed": len(result.failed),
                "attachments": carried,
            },
        )
    )

    qa_report = None
    if qa:
        qa_report = _run_qa_stage(records, result, summaries, engine, out, manifest.page.size, emit)
    return PipelineResult(
        records=records,
        render_result=result,
        engine=engine,
        qa_report=qa_report,
        page_size=manifest.page.size,
        source_name=adapter.name,
        source_reading=source_reading,
        provenance=provenance,
    )


def settle_qa(report: QAReport, out: Path, emit: EventSink) -> None:
    """Write the QA report, announce it, and refuse the run if it failed.
    Both orchestrators end QA this way, so an operator reading the stage
    rail, or a script reading the exit code, cannot tell which produced the
    run — a parity test pins the two to this one shared contract.
    """
    from anastomosis.qa import Verdict, write_report

    write_report(report, out)
    counts = {
        "pass": report.count(Verdict.PASS),
        "warn": report.count(Verdict.WARN),
        "fail": report.count(Verdict.FAIL),
    }
    # Only when there is something to say: three green counts over a batch
    # with no place for the problem list is true of every check, false of the run.
    if report.not_carried:
        counts["not_carried"] = report.not_carried
    emit(StageEvent(STAGE_QA, counts=counts))
    if not report.ok:
        raise PipelineError(
            f"QA failed: {report.count(Verdict.FAIL)} document(s)", exit_code=1, kind="qa_failed"
        )


def _run_qa_stage(
    records: list[PatientRecord],
    result: RenderResult,
    summaries: CCDARenderResult,
    engine: ReconstructionEngine,
    out: Path,
    page_size: str,
    emit: EventSink,
) -> QAReport | None:
    """Contract: verify every rendered document; return the report, or
    ``None`` if downgraded (missing PyMuPDF) or nothing rendered. Grades
    per-encounter charts against the pack's carries/omits and record
    summaries as whole-patient, matched by ``summaries.by_path`` so
    shared-patient records (#383) grade once."""
    try:
        # The probe, not the whole surface: settle_qa imports what it needs
        # once QA has actually run, and by then pymupdf is known to be here.
        from anastomosis.qa import run_qa, whole_patient_report
    except ImportError as exc:
        if exc.name != "pymupdf":  # only the optional dependency may downgrade QA
            raise
        emit(
            StageEvent(
                STAGE_QA,
                detail="skipped: install anastomosis[render] for PyMuPDF",
                skipped=True,
            )
        )
        return None

    lookup = {(r.patient.id, e.id): (e, r) for r in records for e in r.encounters}
    # Keyed by patient id, not re-derived per encounter: `summaries.by_path`
    # already resolved which record's render wrote each summary (#383).
    record_summary_paths = {record.patient.id: path for path, record in summaries.by_path.items()}
    report = run_qa(
        ((d.path, *lookup[d.patient_id, d.encounter_id]) for d in result.documents),
        section_flags=engine.section_flags,
        page_size=page_size,
        render_tz=engine.timezone,
        render_day_stamps=engine.render_day_stamps,
        carries=engine.carries,
        omits=engine.omits,
        record_summary_paths=record_summary_paths,
    )
    # ``documents`` is the report's only state — ``ok`` and ``not_carried`` are
    # derived from it — so extending it merges the two batches soundly.
    report.documents.extend(whole_patient_report(summaries.by_path.items()).documents)

    if not report.documents:
        # Both populations empty: a report that graded nothing is not
        # evidence of anything passing, so this downgrades like the
        # missing-PyMuPDF branch — a tick over unrun verification is false.
        emit(
            StageEvent(
                STAGE_QA,
                detail="skipped: nothing rendered to verify",
                skipped=True,
            )
        )
        return None

    settle_qa(report, out, emit)
    return report
