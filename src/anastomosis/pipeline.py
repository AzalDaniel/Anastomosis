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

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, get_origin

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
    """One PHI-safe progress signal from the pipeline.

    ``stage`` is one of the ``STAGE_*`` constants. ``counts`` carries only
    integers (records, rendered, skipped, failed, pass/warn/fail) — never
    patient-derived strings. ``detail`` is a small PHI-free string slot for
    facts like a detected source name or a chosen pack name.
    """

    stage: str
    counts: dict[str, int] = field(default_factory=dict)
    detail: str = ""
    #: The stage was downgraded to a no-op rather than run. A frontend must not
    #: paint it as completed: a physician who asked for verification and got a
    #: tick would believe their charts were checked when nothing read them.
    skipped: bool = False


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
    #: The source ledger's account of the load in chart vocabulary, one
    #: sentence per line (see ``settle_source_ledger``). Empty for a source
    #: that keeps no ledger; PHI-free by that function's contract, so a
    #: frontend may print it verbatim.
    source_reading: tuple[str, ...] = ()
    #: The layout this run rendered through, named by its bytes (see
    #: :mod:`anastomosis.reconstruct.provenance`). Carried on the result rather
    #: than only written to disk because the upload manifest records the same
    #: content hash as its layout gate, and two readers measuring it separately
    #: is how they come to disagree.
    provenance: RenderProvenance | None = None


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


def parse_selection_includes(include: list[str] | None) -> frozenset[str]:
    """Turn ``["growth-charts"]`` into the set of selection rules to switch OFF.

    The counterpart of :func:`parse_section_overrides` on the ingest side, and
    shared with both frontends for the same reason: a rule an operator switched
    off in the GUI and one they switched off on the command line have to mean
    the same run. Strict in the same way — a blank ``--include`` raises
    :class:`PipelineError` (exit 2) rather than being read as "include
    nothing", which is the one thing an operator who typed the flag did not
    mean. Rule-NAME validation happens later, against the resolved adapter's
    own :func:`~anastomosis.sources.base.selection_rules`.
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
    """The selection rules this run drops, checked against the adapter's own set.

    The ingest-side twin of the unknown-``--section`` check: a typo'd rule name
    used to be impossible to type at all (the rules were constants), and the
    one thing worse than not being able to switch a rule off is thinking you
    did. The message names the source and lists the rules it actually has.
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
    """Every render-selection rule this source has, and whether this run ran it.

    Rule names and rule reasons only — schema, never anything a rule read — in
    the adapter's own order, which is the order it applies them in. Without
    this a report saying nothing was excluded cannot be told apart from a
    report of a run whose rules were all switched off, and the two are
    opposite answers to "did this run leave anything out?".
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
    # Detection answers "which format is this?", so it cannot tell a folder of
    # unrecognised files from a folder that is not there. Saying "could not
    # identify the export format" for a bad path sends someone off to pick a
    # format, which then fails too — name the thing that is actually wrong.
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
    """Load an export into records, turning three failure modes into clean,
    PHI-safe :class:`PipelineError`\\ s (exit 2) instead of a raw traceback or a
    silent false-success:

    * an adapter's fail-closed refusal (:class:`SourceDataError` — orphan rows,
      unmapped tables, resources that cannot be attributed) → a ``bad_input``
      error carrying the adapter's OWN message verbatim. That message is the
      whole operational value of the refusal: it names the offending tables or
      resource types and their counts, which is what the operator repairs. It is
      PHI-safe by that class's contract — schema names and integers only;
    * any other malformed export (a bad XML/JSON/value the adapter chokes on) →
      a ``bad_input`` error naming only the source and the exception TYPE (never
      the offending value, which can be PHI, and an arbitrary exception's
      message frequently embeds it);
    * a load that yields ZERO records → an ``empty_export`` error. A source that
      reads nothing from a non-empty directory is a defect — most often a
      ``--from``/``--source`` that does not match the export — never a 0-document
      "success" that writes empty output and exits 0.

    A :class:`PipelineError` an adapter raises itself (e.g. the FHIR adapter's
    no-Patient guard) passes through unchanged.

    What comes back is one record per PATIENT
    (:func:`_fold_records_sharing_a_patient`), whatever the adapter yielded.
    """
    try:
        records = list(adapter.load(export_dir))
    except PipelineError:
        raise
    except ConservationError as exc:
        # The load's own instrument could not account for a document (the
        # C-CDA adapter ledgers each one as it parses). The same refusal the
        # render seam gets, for the same reason: the message names the unit
        # and the column that went short, and nothing downstream may treat
        # what loaded as the whole set. PHI-safe by Conservation's contract.
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
        raise PipelineError(
            f"No records loaded from the {adapter.name} export — is this a {adapter.name} export?",
            exit_code=2,
            kind="empty_export",
        )
    return _fold_records_sharing_a_patient(records)


#: How many source records were folded into one chart, written onto the merged
#: record's own ``extensions`` and only when a fold actually happened.
#:
#: A COUNT, not the file names. The one place this codebase stores the name of a
#: source document — ``provenance.source_file`` — the C-CDA exporter declares
#: non-narrated for exactly this reason, and the C-CDA adapter names an
#: unreadable document by POSITION because a C-CDA export names its files after
#: the patient. There is no PHI-safe precedent for carrying those names into a
#: delivered artifact, so the merged chart says how many source records it is
#: and no more. It travels into ``bundle.json`` with the rest of the record's
#: extensions, where someone reconciling "the run said N patients" against N
#: charts can read it.
#:
#: Model-level ``anast:`` namespace rather than a source's, for the reason
#: ``anast:inline_content`` is: the writer is the pipeline, which must not know
#: which adapter filled the records in.
EXT_FOLDED_RECORDS = "anast:folded_source_records"


def _fold_records_sharing_a_patient(records: list[PatientRecord]) -> list[PatientRecord]:
    """One patient is one chart, whatever the adapter yielded.

    Every per-patient destination is keyed by ``patient.id``: the C-CDA export
    writes ``<patient-id>.xml``, the archive and the bundle each write one
    directory. Two records under one id therefore land in one slot, and every
    writer there is exist_ok/overwrite — so the second silently replaced the
    first while the run reported two. The source-adapter contract already says
    ``load`` yields one record per patient; the C-CDA adapter yields one per
    DOCUMENT, because a document is the unit its conservation ledger has to
    measure, and a patient with three documents arrived as three records.

    So the fold happens here, at the seam every adapter passes through, rather
    than inside one adapter's parse. Records keep first-appearance order, and a
    patient with a single record is passed through as the object the adapter
    built rather than rebuilt into an equal one — an adapter that already meets
    the contract (the FHIR source pools its resources and groups before it
    yields) takes exactly the path it always took.
    """
    groups: dict[str, list[PatientRecord]] = {}
    for record in records:
        groups.setdefault(record.patient.id, []).append(record)
    return [group[0] if len(group) == 1 else _merged_record(group) for group in groups.values()]


def _merged_record(group: list[PatientRecord]) -> PatientRecord:
    """The one chart several source records for one patient make.

    Every collection unions in the order the records were read. De-duplication
    happens only where the model already defines identity: two encounters under
    one GUID ``<id root>`` are one visit when the halves agree
    (:func:`~anastomosis.sources.ccda.parser.fold_encounters_sharing_an_id` —
    the same rule inside one document and across two). An encounter under a
    vendor OID root does not reach this: the parser gives it one id PER
    DOCUMENT (:func:`~anastomosis.sources.ccda.parser._encounter_id` honours
    only a GUID-shaped root), so two documents naming the same visit under an
    OID keep two encounter objects here. Nothing else is deduped,
    because nothing else in the canonical model says when two objects are the
    same object, and a rule invented here would delete a real repeat prescription
    or a real second reading.

    The first record's ``id`` and ``provenance`` stay: a merged chart is the
    first document's record with the rest folded into it, and how many records
    that was rides :data:`EXT_FOLDED_RECORDS` — merged in as one more extensions
    dict rather than assigned, so that a source which has parked something under
    that key itself keeps it under the same no-silent-drop rule as every other
    key rather than losing it to this run's own bookkeeping.
    """
    from anastomosis.core.model import PatientRecord
    from anastomosis.sources.ccda.parser import fold_encounters_sharing_an_id

    update: dict[str, Any] = {
        name: [item for record in group for item in getattr(record, name)]
        for name in _record_list_fields(PatientRecord)
    }
    update["encounters"] = fold_encounters_sharing_an_id(update["encounters"])
    update["patient"] = _merged_patient([record.patient for record in group])
    update["extensions"] = _merged_extensions(
        [*(record.extensions for record in group), {EXT_FOLDED_RECORDS: len(group)}]
    )
    return group[0].model_copy(update=update)


#: What every canonical model carries besides its own content
#: (:class:`~anastomosis.core.model.AnastBase`): who it is, where it came from,
#: and what would not map. The fold handles all three by name rather than by
#: kind, on the record and on the patient alike.
_MODEL_IDENTITY_FIELDS = frozenset({"id", "provenance", "extensions"})

#: The :class:`~anastomosis.core.model.PatientRecord` fields the fold handles by
#: name. Everything else on that model is a collection, and the fold refuses to
#: run rather than quietly leaving a new kind of field behind.
_RECORD_NAMED_FIELDS = _MODEL_IDENTITY_FIELDS | {"patient"}


@cache
def _record_list_fields(model: type[PatientRecord]) -> tuple[str, ...]:
    """Every collection the fold unions, read off the model.

    Derived rather than listed here, so a collection added to
    :class:`~anastomosis.core.model.PatientRecord` is unioned by default instead
    of being silently skipped by a hand-written tuple nobody remembered to
    extend. A field that is neither a collection nor one of the four handled by
    name stops the fold loudly: losslessness is not a property to discover
    missing from a delivered chart.
    """
    collections = tuple(
        name for name, info in model.model_fields.items() if get_origin(info.annotation) is list
    )
    unhandled = sorted(set(model.model_fields) - set(collections) - _RECORD_NAMED_FIELDS)
    if unhandled:
        raise TypeError(
            f"PatientRecord field(s) {', '.join(unhandled)} are neither a collection nor "
            "identity, so the fold that merges two records for one patient would drop them"
        )
    return collections


def _merged_patient(patients: list[Patient]) -> Patient:
    """One patient's demographics from the several records that state them.

    A field one record states and another leaves empty is taken from the record
    that has it. A SINGLE-VALUED field two records state differently is a source
    that cannot say who this patient is, and the run refuses: filling in one of
    the two would put a birth date or a name on a chart that half the source
    contradicts, which is the misfiling this toolkit exists to prevent.

    A LIST-valued one is not that. Two documents naming different phone numbers
    are not two people; a patient has two phone numbers, and the model holds a
    list precisely so both fit. Refusing there would reject the ordinary export
    — the identifiers alone make it certain, because the parser also derives an
    identifier from the ``patientRole`` id, so any pair of documents filed
    under different assigning authorities states two identifiers for the same
    patient by design, not by disagreement. The lists are unioned in document
    order, keeping each distinct value once.
    """
    model = type(patients[0])
    update: dict[str, Any] = {}
    singles = {
        name: _stated_values(patients, name)
        for name in _patient_demographic_fields(model)
        if name not in _patient_list_fields(model)
    }
    _refuse_disagreeing_demographics(patients[0].id, singles, len(patients))
    update.update({name: values[0] for name, values in singles.items() if values})
    update.update(
        {
            name: _unioned([getattr(patient, name) for patient in patients])
            for name in _patient_list_fields(model)
        }
    )
    update["extensions"] = _merged_extensions([patient.extensions for patient in patients])
    return patients[0].model_copy(update=update)


#: Typing origins that state "several values at once" without being the
#: ``list[...]`` shape :func:`_patient_list_fields` looks for. A demographic
#: added to :class:`~anastomosis.core.model.Patient` in one of these shapes
#: would otherwise pass ``get_origin(...) is list`` as False and fall silently
#: into the SINGLE-valued bucket in :func:`_merged_patient` — where two
#: documents stating it in a different order becomes a refused "disagreement"
#: instead of the union it should be.
_SEQUENCE_LIKE_ORIGINS = (tuple, frozenset, set, Sequence)


@cache
def _patient_list_fields(model: type[Patient]) -> frozenset[str]:
    """The demographics that hold several values at once, read off the model.

    A field neither this collects nor :data:`_MODEL_IDENTITY_FIELDS` names is
    read by :func:`_merged_patient` as single-valued by default — correct for
    an ordinary scalar demographic, wrong for a collection this function
    failed to recognise. So a field whose origin still LOOKS like a collection
    (:data:`_SEQUENCE_LIKE_ORIGINS`) stops the fold loudly instead, the same
    way :func:`_record_list_fields` refuses to run rather than guess at a
    record-level field it does not recognise.
    """
    collections = frozenset(
        name
        for name, info in model.model_fields.items()
        if name not in _MODEL_IDENTITY_FIELDS and get_origin(info.annotation) is list
    )
    unhandled = sorted(
        name
        for name, info in model.model_fields.items()
        if name not in _MODEL_IDENTITY_FIELDS
        and name not in collections
        and get_origin(info.annotation) in _SEQUENCE_LIKE_ORIGINS
    )
    if unhandled:
        raise TypeError(
            f"Patient field(s) {', '.join(unhandled)} look like a collection but are not "
            "list[...], so the fold's single-valued/list split would misclassify them"
        )
    return collections


def _unioned(lists: list[list[Any]]) -> list[Any]:
    """Every distinct value the records state, in the order they first state it.

    Distinct by VALUE — two records that both carry the same address carry one
    address between them, and a chart listing it twice is a chart nobody wrote.
    Models compare by value here; anything unhashable falls back to a scan,
    which is fine at the length a person's demographics reach.
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
    """What the records actually say for one demographic field, gaps left out.

    ``None`` and an empty list are both "this record did not say", which is not
    a disagreement with a record that did. Everything else is a statement, and
    two different statements are the disagreement above.
    """
    return [
        value
        for value in (getattr(patient, name) for patient in patients)
        if value is not None and value != []
    ]


def _refuse_disagreeing_demographics(
    patient_id: str, stated: dict[str, list[Any]], records: int
) -> None:
    """Stop the run when the records for one patient id describe two people.

    PHI: the refusal names the FIELDS and the counts — schema words and integers,
    the same contract every adapter refusal keeps — and never what the fields
    said. The operator repairs the export by looking at the named fields; the
    values are on their screen already and have no business in ours.

    It DOES carry the patient, as :func:`~anastomosis.core.logutil.safe_log_id`'s
    run-scoped surrogate — the same one ``deliver._shared.claim_delivered_name``
    already puts in a collision message. Without it, a refusal on a batch
    export named a field and a count and nothing else, and bisecting the
    export by hand to find which patient triggered it was the operator's only
    recourse: the same failure ``sources/ccda/__init__.py`` already refused to
    leave one with for a document it cannot parse.
    """
    disagreed = sorted(
        name for name, values in stated.items() if any(value != values[0] for value in values)
    )
    if not disagreed:
        return
    raise PipelineError(
        f"{records} records for patient {safe_log_id(patient_id)} share one patient id but "
        f"disagree on {len(disagreed)} demographic field(s) ({', '.join(disagreed)}); "
        "refusing to merge them into one chart — the source cannot say which of them is "
        "the patient",
        exit_code=2,
        kind="bad_input",
    )


def _merged_extensions(dicts: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """One ``extensions`` dict from several, keeping everything all of them hold.

    Three rules, in the order a key meets them:

    * a carried-forward loss ledger merges as one ledger — entries concatenate,
      the highest generation wins — because that is what one document's own
      round trip dedupes
      (:func:`~anastomosis.sources.ccda.parser.merge_loss_narrative`, the same
      rule that folds two stamped ledgers inside one document). This applies
      only when BOTH sides already have the ledger shape
      (:func:`~anastomosis.sources.ccda.parser.is_loss_ledger`): a hand-made
      FHIR bundle can park any JSON at all under this key, and an incoming
      ledger meeting a non-ledger occupant is a clash like any other — folding
      into it would silently discard whatever the occupant held;
    * a key only one record holds, or one they hold with EQUAL values, keeps its
      value at its own key;
    * a key two records hold with DIFFERENT values keeps BOTH: the later one is
      parked at the first free ``#2``, ``#3``, … variant, the way the C-CDA
      parser parks a repeated section
      (:func:`~anastomosis.sources.ccda.parser.free_key`). Preserving unmapped
      source data is the whole purpose of this dict, so a merge that dropped
      half of it would defeat it at the one point it exists to hold.
    """
    from anastomosis.sources.ccda.parser import free_key, is_loss_ledger, merge_loss_narrative

    merged: dict[str, Any] = {}
    for extensions in dicts:
        for key, value in extensions.items():
            existing = merged.get(key)
            if (
                key == EXT_PRIOR_LOSS_NARRATIVE
                and is_loss_ledger(value)
                and (existing is None or is_loss_ledger(existing))
            ):
                merge_loss_narrative(merged, value["generation"], value["entries"])
            elif key not in merged or merged[key] == value:
                merged[key] = value
            else:
                merged[free_key(merged, key)] = value
    return merged


#: Where a run keeps the source attachments it carried through, inside the same
#: hardened output directory as the charts. The deliverers read charts out of
#: this directory and nothing else — they are never handed the export — so an
#: attachment has to be HERE by the time delivery runs, not fetched back out of
#: an export that a later `anast archive` may no longer be able to reach.
ATTACHMENTS_DIRNAME = "attachments"

#: Where a run publishes what the source offered against what arrived: the
#: C-CDA ledger's full account, construct by construct (see
#: ``settle_source_ledger``). Beside the charts like ``quarantine.json``, and
#: PHI-vetted the same way the shape report is — ``assert_emittable`` walks it
#: at the point of writing.
LOSS_LEDGER_FILENAME = "loss_ledger.json"

#: Where a run persists the rows its adapter could not place on any patient
#: (see :class:`~anastomosis.sources.base.QuarantinedRows`). Written into the
#: same hardened output directory as the charts — the rows travel no further
#: than the charts do — while events, logs, and the CLI carry counts only.
QUARANTINE_FILENAME = "quarantine.json"


#: What the charts in an output directory were rendered from: which layout, and
#: which sections were switched on. Kept so a later run into the same directory
#: can tell whether the charts already there answer the question being asked.
#:
#: This file is the run's INTENT. What the machine actually used — the layout's
#: identity and a digest per pack file — lands beside it in
#: :data:`~anastomosis.reconstruct.provenance.RENDER_PROVENANCE_NAME`, which
#: argues in its own module docstring why the two are not one file.
RENDER_SETTINGS_NAME = "render_settings.json"


def _render_settings(
    pack: str, flags: dict[str, bool], include: frozenset[str]
) -> dict[str, object]:
    """The run's rendering intent, in a form two runs can be compared by.

    ``included`` rides only when this run actually switched a selection rule
    off. A run that did not writes exactly the record every run wrote before
    the rules were options at all, so re-running into a folder an older build
    filled is not refused over a key that was never in it.
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
    """Refuse a run whose settings differ from the ones that made these charts.

    The idempotent skip decided on ``target.exists()`` alone, so re-running with
    a section switched OFF into a directory that already had charts reported
    ``0 rendered, 6 skipped, 0 failed`` and left every chart carrying the
    section. Those flags are how an operator SUPPRESSES content, and the run
    said "done, and verified" while the output was exactly what they were trying
    not to produce. If the folder was then archived or uploaded, the suppression
    never happened at all.

    Refusing rather than silently re-rendering: charts in this directory may
    already have been delivered, and quietly rewriting them is its own kind of
    surprise. ``--force`` is the existing way to say "render them all again",
    and it is what the message names.
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
    """Refuse a run whose layout is not the layout that made these charts.

    The sibling of :func:`_guard_render_settings`, and it exists because that
    guard cannot see this: settings compare the layout's NAME, so a folder built
    from ``generic_soap`` before someone edited its template re-runs clean,
    reports every chart skipped, and leaves the operator holding pages produced
    by bytes that no longer exist anywhere. The trust gate does not catch it
    either — an asset is outside the content hash, and re-trusting an edited
    pack is what ``--trust-pack`` is FOR.

    Absent or unreadable record: nothing to compare, so nothing to refuse. A
    folder filled by an older build has no provenance in it, and refusing every
    one of those would be a guard that only ever punished upgrading.
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
    """Publish what the charts were produced from, and refuse a mid-run swap.

    ``templates`` is what the engine's recording loader actually handed the
    compiler. A digest there that disagrees with the one measured before the
    render means the layout was edited WHILE the batch ran, so the folder holds
    charts from two different layouts and no single record could honestly name
    them. That is a loud failure, taken before the record is written — a file
    saying "these charts came from X" must not exist beside charts that did not.

    What it does NOT do is mark the folder. A run that fails here leaves charts
    from two layouts and no record of either, so a LATER run into that folder
    reads like a folder an older build filled and is not refused. The remedy is
    the one the message names — ``--force`` into an empty folder — and it is
    named rather than enforced: a poison marker would be a fourth sidecar whose
    absence means nothing, which is the shape of guarantee this file exists to
    stop making.
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
    """Every encounter an adapter's selection rules kept out of the render.

    Ids and rule names only. An encounter id is a source identifier and a
    reason is the name of a rule, so this carries none of the chart — which is
    what lets it be written beside the charts and read back without care.
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
    """Persist what the adapter held back; return the INGEST event's extra counts.

    Both orchestrators (``run_pipeline`` and ``core.migrate``) settle the
    quarantine the same way for the same reason ``settle_qa`` is shared: an
    operator reading the stage rail cannot tell which one produced the run.
    The held rows go to ``quarantine.json`` verbatim — inside the hardened
    output directory, which already holds the full charts — grouped by table
    and PHI-free reason, so a reviewer can decide what the dangling rows were
    and whether the export needs repair. The returned ``{"quarantined": N}``
    rides the INGEST event; empty when nothing was held, so a clean run's
    event payload is byte-identical to before this artifact existed.

    A clean run also REMOVES a stale ``quarantine.json``: re-running into the
    same folder after repairing the export must not leave last run's held rows
    reading as this run's.
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
    """Publish the source ledger's account of the load; return its reading.

    The C-CDA adapter ledgers every document as it parses (see issue #315: the
    instrument shipped in every build and nothing ran it — the operator still
    could not see what was lost, one level up from the original mistake). Both
    orchestrators settle it here the way they settle the quarantine: the full
    construct-by-construct account goes to ``loss_ledger.json`` beside the
    charts, and the RETURN value is the reading in chart vocabulary — one
    PHI-free sentence per line, ready for the CLI's end of run and the GUI's
    summary. An adapter that keeps no ledger (every non-C-CDA source today)
    returns an empty reading and writes nothing, and a re-run that ledgered
    nothing removes the stale artifact for the reason ``settle_quarantine``
    does.
    """
    from anastomosis.core.output import secure_output_dir

    ledgers = list(getattr(adapter, "ledgers", ()))
    if not ledgers:
        (out / LOSS_LEDGER_FILENAME).unlink(missing_ok=True)
        return ()
    from anastomosis.sources.ccda.ledger import aggregate, physician_reading

    corpus = aggregate(ledgers)
    _write_json(secure_output_dir(out) / LOSS_LEDGER_FILENAME, corpus.as_report())
    return physician_reading(corpus)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    """One sidecar, written atomically and deterministically."""
    from anastomosis.core.atomic import atomic_write_text

    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True))


def _write_selection_report(
    out: Path, exclusions: list[dict[str, str]], rules: list[dict[str, object]]
) -> None:
    """Write the run's selection report, whatever it has to say.

    Written on every run, including the empty one. "Nothing was left out" is
    the answer an operator reconciling counts most often needs, and an absent
    file cannot distinguish it from a run that never looked. ``rules`` is there
    for the same reason one step up: an empty ``excluded`` with no statement of
    what was asked cannot distinguish a rule that found nothing from a rule
    that was switched off.
    """
    _write_json(
        out / SELECTION_REPORT_NAME,
        {"version": SELECTION_REPORT_VERSION, "excluded": exclusions, "rules": rules},
    )


def _carry_attachments(records: list[PatientRecord], export_dir: Path, out: Path) -> int:
    """Put every attachment the source resolved into the run's output.

    A `DocumentArtifact` with a ``path`` names the file a chart is incomplete
    without — a scanned referral, a lab report. Nothing carried them out of the
    export, so they reached the operator as nothing at all: not rendered, not
    delivered, not named anywhere.

    Each file keeps the name the export gave it (its storage id), which is what
    the record already points at, so a deliverer needs no extra index to find
    one — the document's own ``path`` locates it. Names are claimed through the
    delivery ledger with the file's digest as witness, so a source that reused
    one storage id for two different files is refused here rather than filing
    one patient's scan under another's document.

    Returns the number of files carried. Raises :class:`PipelineError` if any
    attachment the records claim did not arrive: a chart delivered without the
    documents it references is the silent loss this exists to end, so the run
    stops rather than reporting a success it cannot back up.
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

    Two kinds arrive here and both leave as one file under one claimed name. A
    source whose export holds the file names it and it is COPIED. A source whose
    artifact came inside the record it was read from — a C-CDA Unstructured
    Document's scan is inside the XML, not beside it — carries its own bytes on
    the artifact and they are WRITTEN. Nothing downstream is told which: the
    archive, the bundle and the deliverers all read one thing out of this
    directory, a document on disk beside the charts, so an artifact that has no
    file to copy is not a second delivery path to keep in step with this one.
    """
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
    """Copy the export's own file into the output; the failure tag, or ``None``.

    The path is the source adapter's word, and a record can also arrive from a
    FHIR bundle someone else wrote. Reading it must stay inside the export the
    operator pointed at: a ``../..`` in a hand-made bundle would otherwise copy
    a file from anywhere the process can read into an output directory that gets
    delivered onward.
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
    """Write an artifact the record carries its own bytes for.

    Base64 that will not decode is the artifact arriving as nothing, so it is
    reported as a failure the conservation check above turns into a refusal —
    the same answer a file that would not copy gets, because it is the same
    outcome for the chart.

    PHI: the return is an exception TYPE name. The bytes are a patient's
    document and are written, never logged.
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


def _render_record_summaries(records: list[PatientRecord], out: Path, *, force: bool) -> None:
    """Render one whole-patient record summary per patient into the bundle.

    A visit note is a note about ONE visit, so every layout selects by encounter
    — and a fact the record holds that no encounter claims (a laboratory result
    that arrived with no visit attached, a standing list no SOAP note has a
    section for) reached no page at all. The chart was not wrong; it was
    partial, and nothing in the bundle said so.

    So the bundle also carries the record. This is HL7's own whole-patient view
    of the same C-CDA the migration delivers — no encounter is guessed, nothing
    is invented, and QA grades it with every chartable kind declared carried, so
    a fact family that reaches neither the charts nor this page fails the run.

    Loud on failure: a summary that did not render is a patient whose record has
    no whole-record page in the bundle, and the run stops rather than shipping a
    bundle that quietly lost one. The ``(patient_id, exception-type)`` pairs ride
    on the error exactly as the per-encounter render failures do — pseudonymous
    ids and type names, never exception text.
    """
    from anastomosis.reconstruct.ccda_standard import render_ccda_standard

    view = render_ccda_standard(records, out / RECORD_SUMMARY_DIRNAME, force=force)
    if view.failed:
        raise PipelineError(
            f"{len(view.failed)} record summary/summaries failed to render",
            exit_code=1,
            kind="render_failed",
            failed=tuple(view.failed),
        )


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
    """The full pipeline (ingest -> reconstruct -> optional QA), frontend-free.

    Emits PHI-safe :class:`StageEvent`\\ s through ``on_event`` as each stage
    completes, returns rich state so a caller can layer archive/bundle/ccda
    delivery without re-loading records or re-rendering charts, and raises
    :class:`PipelineError` on any loud failure.

    ``section`` overrides the layout's section flags; ``include`` names the
    source's render-selection rules this run does NOT apply, so the encounters
    they would have kept out of the render are rendered. Both default to
    nothing, which is what every run did before either was a choice.
    """
    from anastomosis.core.output import OutputPathError, validate_output_target
    from anastomosis.reconstruct import discover_packs
    from anastomosis.reconstruct.chromium import ChromiumRenderer, RendererUnavailable
    from anastomosis.reconstruct.engine import ReconstructionEngine
    from anastomosis.reconstruct.packtrust import default_pack_trust
    from anastomosis.reconstruct.provenance import pack_provenance

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
    # The run's selection choices, settled against the resolved source before
    # anything is read: a rule name this source does not have is an operator
    # error, and it costs nothing to say so before the export is opened.
    switched_off = _switched_off(adapter, include)
    rules_report = _selection_rules_report(adapter, switched_off)
    adapter = with_selection(adapter, switched_off)

    dirs = list(pack_dirs or [])
    # The trust store is always consulted now, not only when --pack-dir is in
    # play: a learned layout lives in the per-user pack directory and is
    # hash-gated there too, so a run that names one has to be able to prove the
    # code it is about to execute is the code that was confirmed. Built-ins need
    # no store and never consult it.
    statuses = discover_packs(
        dirs,
        allow_external=bool(dirs),
        trust=default_pack_trust(),
        trust_new=trust_new,
    )
    status = statuses.get(pack)
    if status is None or status.pack is None:
        # No fallback, ever. A layout that is missing, changed since it was
        # confirmed, or untrusted refuses the run — rendering the operator's
        # charts through some OTHER layout would be the same false completion in
        # a costlier place.
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
    # Before any ingest work: if this folder already holds charts, do they answer
    # the question being asked? A mismatch is refused here rather than discovered
    # as a silently-unchanged output at the end.
    settings = _render_settings(pack, engine.section_flags, switched_off)
    _guard_render_settings(out, settings, force=force)
    # And the same question about the layout's BYTES rather than its name:
    # measured here, before the render, because a folder whose charts came from
    # different bytes has to refuse before it is filled with a second layout's
    # pages. `status.pack` is not None — the unavailable case raised above.
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
        # A property of the machine, not of a chart. It reaches the operator as
        # the loud, PHI-safe failure the CLI prints verbatim — exit 2, the code
        # this pipeline already uses for "a capability this run needs is not
        # available here", the same class as an unavailable pack. Previously it
        # was tagged onto every encounter, so a base install answered with six
        # identical "(RuntimeError)" lines and threw away the one sentence
        # naming what to install.
        raise PipelineError(str(exc), exit_code=2, kind="render_unavailable") from None
    except ConservationError as exc:
        # The seam lost work. Not a chart's failure and not the machine's: the
        # stage was handed N encounters and cannot say what became of all of
        # them, so nothing downstream may treat the survivors as the whole set.
        # PHI-safe by construction — the message carries counts and column
        # names only.
        raise PipelineError(str(exc), exit_code=1, kind="conservation_failed") from None
    if result.failed:
        # Loud render failure, before the stage is announced as finished and
        # before anything is carried: a run that could not render every chart
        # has no business scattering a patient's scanned records around, and
        # the render failure is the message an operator needs, not a
        # consequence of it. The (encounter_id, type) pairs ride on the error
        # so the CLI can print its per-encounter detail lines; PHI-safe.
        raise PipelineError(
            f"{len(result.failed)} encounter(s) failed to render",
            exit_code=1,
            kind="render_failed",
            failed=tuple(result.failed),
        )

    # The charts are one visit each; this is the record. Rendered before
    # attachments are carried and before QA, so a bundle that could not carry
    # the whole record for every patient stops here rather than being graded and
    # delivered as complete.
    _render_record_summaries(records, out, force=force)

    # `out` is hardened by the engine above, so this is the first point a
    # patient's own files may be written beside their charts.
    carried = _carry_attachments(records, export_dir, out)
    exclusions = _selection_exclusions(records)
    _write_selection_report(out, exclusions, rules_report)
    # Provenance settles BEFORE the settings record, from the SAME measurement
    # the guard compared plus what the renderer actually read — so the record
    # and the refusal can never disagree about which layout this run held, and
    # a mid-render layout swap refuses before EITHER record claims this folder
    # is coherent.
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
    if qa and result.documents:
        qa_report = _run_qa_stage(records, result, engine, out, manifest.page.size, emit)
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

    Both orchestrators end QA this way, and they have to end it the SAME way —
    an operator reading the stage rail cannot tell which one produced the run,
    and a script reading the exit code must not care. There is already a parity
    test asserting the two share a stage contract; this makes it the contract
    rather than a claim about two copies that happened to agree.
    """
    from anastomosis.qa import Verdict, write_report

    write_report(report, out)
    counts = {
        "pass": report.count(Verdict.PASS),
        "warn": report.count(Verdict.WARN),
        "fail": report.count(Verdict.FAIL),
    }
    # Only when there is something to say. Three green counts over a batch whose
    # layout had no place for the problem list is true of every check and false
    # of the run, and the rail is where an operator reads the run.
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
    engine: ReconstructionEngine,
    out: Path,
    page_size: str,
    emit: EventSink,
) -> QAReport | None:
    """Verify every rendered document; return the report (None if QA downgraded).

    Two populations, ONE report. The charts are graded against the pack's own
    ``carries``/``omits`` — a SOAP note is allowed to have no problem list, and
    the count it does not carry is reported rather than graded green. The record
    summaries are graded as whole-patient documents, with every chartable kind
    declared carried: between them the run cannot come back clean while a fact
    family the record holds reached no page at all. One report because there is
    one bundle, and an operator reading two summaries has to reconcile them.

    A missing PyMuPDF (the optional ``render`` extra) downgrades QA to a
    no-op rather than failing the run — the only ``ImportError`` allowed to
    soften here, mirroring the original CLI behavior. A failing report raises
    :class:`PipelineError` (exit 1).
    """
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
    from anastomosis.reconstruct.ccda_standard import ccda_standard_doc_path

    lookup = {(r.patient.id, e.id): (e, r) for r in records for e in r.encounters}
    report = run_qa(
        ((d.path, *lookup[d.patient_id, d.encounter_id]) for d in result.documents),
        section_flags=engine.section_flags,
        page_size=page_size,
        render_tz=engine.timezone,
        render_day_stamps=engine.render_day_stamps,
        carries=engine.carries,
        omits=engine.omits,
    )
    # ``documents`` is the report's only state — ``ok`` and ``not_carried`` are
    # derived from it — so extending it merges the two batches soundly.
    summaries = out / RECORD_SUMMARY_DIRNAME
    report.documents.extend(
        whole_patient_report(
            (ccda_standard_doc_path(summaries, record), record) for record in records
        ).documents
    )
    settle_qa(report, out, emit)
    return report
