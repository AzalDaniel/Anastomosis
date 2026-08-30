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

from anastomosis.core.conservation import ConservationError
from anastomosis.core.logutil import exc_tag
from anastomosis.sources import SourceDataError, available_sources, detect_source, get_source
from anastomosis.sources.learned import register_learned_sources

# Register any formats the operator has taught from an example (the user-dir
# scan is defensive — a broken mapping is skipped, never crashes import).
register_learned_sources()

if TYPE_CHECKING:
    from anastomosis.core.model import PatientRecord
    from anastomosis.qa import QAReport
    from anastomosis.reconstruct.engine import ReconstructionEngine, RenderResult
    from anastomosis.sources.base import QuarantinedRows, SourceAdapter

__all__ = [
    "QUARANTINE_FILENAME",
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
    "run_pipeline",
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
    """
    try:
        records = list(adapter.load(export_dir))
    except PipelineError:
        raise
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
    return records


#: Where a run keeps the source attachments it carried through, inside the same
#: hardened output directory as the charts. The deliverers read charts out of
#: this directory and nothing else — they are never handed the export — so an
#: attachment has to be HERE by the time delivery runs, not fetched back out of
#: an export that a later `anast archive` may no longer be able to reach.
ATTACHMENTS_DIRNAME = "attachments"

#: Where a run persists the rows its adapter could not place on any patient
#: (see :class:`~anastomosis.sources.base.QuarantinedRows`). Written into the
#: same hardened output directory as the charts — the rows travel no further
#: than the charts do — while events, logs, and the CLI carry counts only.
QUARANTINE_FILENAME = "quarantine.json"


#: What the charts in an output directory were rendered from: which layout, and
#: which sections were switched on. Kept so a later run into the same directory
#: can tell whether the charts already there answer the question being asked.
RENDER_SETTINGS_NAME = "render_settings.json"


def _render_settings(pack: str, flags: dict[str, bool]) -> dict[str, object]:
    """The run's rendering intent, in a form two runs can be compared by."""
    return {"version": 1, "pack": pack, "sections": dict(sorted(flags.items()))}


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
    import json

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


def _settings_difference(previous: dict[str, object], current: dict[str, object]) -> str:
    """A short, PHI-free description of what the operator changed."""
    parts: list[str] = []
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


def _write_json(path: Path, payload: dict[str, object]) -> None:
    """One sidecar, written atomically and deterministically."""
    import json

    from anastomosis.core.atomic import atomic_write_text

    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True))


def _write_selection_report(out: Path, exclusions: list[dict[str, str]]) -> None:
    """Write the run's selection report, whatever it has to say.

    Written on every run, including the empty one. "Nothing was left out" is
    the answer an operator reconciling counts most often needs, and an absent
    file cannot distinguish it from a run that never looked.
    """
    _write_json(out / SELECTION_REPORT_NAME, {"version": 1, "excluded": exclusions})


def _carry_attachments(records: list[PatientRecord], export_dir: Path, out: Path) -> int:
    """Copy every attachment the source resolved into the run's output.

    A `DocumentArtifact` with a ``path`` names a real file in the export — a
    scanned referral, a lab report, the pages a chart is incomplete without.
    Nothing carried them out of the export, so they reached the operator as
    nothing at all: not rendered, not delivered, not named anywhere.

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
    from anastomosis.deliver._shared import (
        DeliveredNameCollision,
        claim_delivered_name,
        copy_delivered_file,
    )

    # (what the record points at, what it is called here, which document, its
    # digest) — bound once so the rest reads as files rather than as optionals.
    wanted = [
        (Path(doc.path), Path(doc.path).name, doc.id, doc.sha256)
        for record in records
        for doc in record.documents
        if doc.path
    ]
    if not wanted:
        return 0

    target = secure_output_dir(out / ATTACHMENTS_DIRNAME)
    root = export_dir.resolve()
    claims: dict[str, str] = {}
    failures: list[str] = []
    for relative, name, doc_id, digest in wanted:
        # The path is the source adapter's word, and a record can also arrive
        # from a FHIR bundle someone else wrote. Reading it must stay inside the
        # export the operator pointed at: a `../..` in a hand-made bundle would
        # otherwise copy a file from anywhere the process can read into an
        # output directory that gets delivered onward.
        source = (root / relative).resolve()
        if not source.is_relative_to(root):
            raise PipelineError(
                f"an attachment path points outside the export ({name!r}); refusing to read it",
                exit_code=1,
                kind="attachment_escape",
            )
        try:
            claim_delivered_name(claims, name, doc_id, kind="attachment", content=digest)
        except DeliveredNameCollision as exc:
            raise PipelineError(str(exc), exit_code=1, kind="attachment_collision") from None
        destination = target / name
        if destination.is_file():
            continue  # the same file claimed twice, or a resumed run
        failure = copy_delivered_file(source, destination)
        if failure:
            failures.append(failure)

    missing = sum(1 for _, name, _, _ in wanted if not (target / name).is_file())
    if missing:
        kinds = ", ".join(sorted(set(failures))) or "the file was not where the export said"
        raise PipelineError(
            f"{missing} of {len(wanted)} attachment(s) named by the records did not reach "
            f"the output ({kinds}); refusing to deliver charts without the documents they "
            "reference",
            exit_code=1,
            kind="attachment_missing",
        )
    return len({name for _, name, _, _ in wanted})


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
    from anastomosis.reconstruct.chromium import ChromiumRenderer, RendererUnavailable
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
    # Before any ingest work: if this folder already holds charts, do they answer
    # the question being asked? A mismatch is refused here rather than discovered
    # as a silently-unchanged output at the end.
    settings = _render_settings(pack, engine.section_flags)
    _guard_render_settings(out, settings, force=force)

    records = load_records(adapter, export_dir)
    emit(
        StageEvent(
            STAGE_INGEST,
            counts={"records": len(records), **settle_quarantine(adapter, out)},
        )
    )

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

    # `out` is hardened by the engine above, so this is the first point a
    # patient's own files may be written beside their charts.
    carried = _carry_attachments(records, export_dir, out)
    exclusions = _selection_exclusions(records)
    _write_selection_report(out, exclusions)
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

    A missing PyMuPDF (the optional ``render`` extra) downgrades QA to a
    no-op rather than failing the run — the only ``ImportError`` allowed to
    soften here, mirroring the original CLI behavior. A failing report raises
    :class:`PipelineError` (exit 1).
    """
    try:
        # The probe, not the whole surface: settle_qa imports what it needs
        # once QA has actually run, and by then pymupdf is known to be here.
        from anastomosis.qa import run_qa
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
    report = run_qa(
        ((d.path, *lookup[d.patient_id, d.encounter_id]) for d in result.documents),
        section_flags=engine.section_flags,
        page_size=page_size,
        render_tz=engine.timezone,
        render_day_stamps=engine.render_day_stamps,
        carries=engine.carries,
        omits=engine.omits,
    )
    settle_qa(report, out, emit)
    return report
