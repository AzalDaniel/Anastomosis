"""``anast pack init`` / ``anast source init`` — the two learn-from-example wizards.

See :mod:`anastomosis.cli_commands` for the split/registration rationale. Both
commands are thin adapters over their shared command cores
(:mod:`anastomosis.core.packinit`, :mod:`anastomosis.core.source_init_command`) so
the CLI and the GUI run ONE flow; this module keeps only the CLI's Rich UX (the
count line, the low-confidence warning, the same-patient confirm, the next-steps
block).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from anastomosis.cli import pack_app, source_app
from anastomosis.cli_commands._paths import out_dir
from anastomosis.core.outcome import declined

if TYPE_CHECKING:
    from anastomosis.core.model import PatientRecord
    from anastomosis.core.source_init_command import SourceInitResult


def _synthetic_preview_record() -> PatientRecord:
    """A tiny, fully-synthetic record for the side-by-side preview render.

    feedface- ids, a 555-exchange phone, an example.com-free facility, and the
    canonical synthetic patient (Testpatient, Synthia). Carries one signed SOAP
    encounter with vitals so every template branch the draft renders has data.
    """
    import datetime

    from anastomosis.core.model import (
        Encounter,
        Facility,
        NoteSection,
        Observation,
        ObservationCategory,
        Patient,
        PatientRecord,
        Practitioner,
        SectionKind,
    )

    patient = Patient(
        id="feedface-0000-0000-0000-0000000000aa",
        given_name="Synthia",
        family_name="Testpatient",
        birth_date=datetime.date(1985, 3, 14),
        sex="F",
    )
    facility = Facility(
        id="feedface-fac0-0000-0000-0000000000aa",
        name="Example Synthetic Clinic",
        address_line1="100 Placeholder Way",
        city="Springfield",
        state="WA",
        postal_code="98101",
        phone="(206) 555-0100",
    )
    provider = Practitioner(
        id="feedface-d0c0-0000-0000-0000000000aa",
        given_name="Pat",
        family_name="Provider",
        display_name="Dr. Pat Provider",
        credential="MD",
    )
    encounter = Encounter(
        id="feedface-e000-0000-0000-0000000000aa",
        patient_id=patient.id,
        facility_id=facility.id,
        provider_id=provider.id,
        signed_by_id=provider.id,
        date_of_service=datetime.date(2024, 1, 2),
        note_type="Progress Note",
        chief_complaint="Cough and congestion",
        signed_at=datetime.datetime(2024, 1, 2, 17, 30, tzinfo=datetime.UTC),
        sections=[
            NoteSection(
                kind=SectionKind.SUBJECTIVE,
                title="Subjective",
                text="Patient reports a productive cough for five days.",
            ),
            NoteSection(
                kind=SectionKind.OBJECTIVE,
                title="Objective",
                text="Lungs clear to auscultation bilaterally.",
            ),
            NoteSection(
                kind=SectionKind.ASSESSMENT,
                title="Assessment",
                text="Acute viral bronchitis.",
            ),
            NoteSection(
                kind=SectionKind.PLAN,
                title="Plan",
                text="Supportive care; return if symptoms persist.",
            ),
        ],
    )
    vitals = [
        Observation(
            id="feedface-0b50-0000-0000-0000000000a1",
            patient_id=patient.id,
            encounter_id=encounter.id,
            category=ObservationCategory.VITAL_SIGNS,
            code="8867-4",
            display="Heart rate",
            value="72",
            unit="bpm",
        ),
    ]
    return PatientRecord(
        id=patient.id,
        patient=patient,
        encounters=[encounter],
        facilities=[facility],
        practitioners=[provider],
        observations=vitals,
    )


def _render_preview(pack_dir: Path) -> Path | None:
    """Render one synthetic preview record through the draft pack.

    Returns the preview PDF path on success, or ``None`` when the Chromium
    renderer is unavailable (a draft still emitted — the operator can render
    later). PHI-safe by construction: only the synthetic preview record is used.
    """
    from anastomosis import cli as _cli
    from anastomosis.reconstruct import discover_packs
    from anastomosis.reconstruct.engine import ReconstructionEngine

    try:
        from anastomosis.reconstruct.chromium import ChromiumRenderer
    except ImportError:
        _cli.console.print(
            "[yellow]preview skipped[/yellow]: install anastomosis[render] for Chromium"
        )
        return None
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            pw.chromium.launch().close()
    except Exception as exc:  # browser not fetched / cannot launch
        _cli.console.print(
            f"[yellow]preview skipped[/yellow]: Chromium unavailable ({type(exc).__name__}); "
            "run 'playwright install chromium'"
        )
        return None

    status = discover_packs([pack_dir.parent], allow_external=True).get(pack_dir.name)
    if status is None or status.pack is None:
        diagnosis = status.diagnosis if status else "draft pack not discovered"
        _cli.console.print(f"[red]preview failed:[/red] {diagnosis}")
        raise typer.Exit(code=1)
    manifest = status.pack.manifest
    margins = {
        "top": manifest.page.margin_top,
        "right": manifest.page.margin_right,
        "bottom": manifest.page.margin_bottom,
        "left": manifest.page.margin_left,
    }
    engine = ReconstructionEngine(
        status.pack,
        lambda: ChromiumRenderer(page_size=manifest.page.size, margins=margins),
    )
    preview_dir = pack_dir / "preview"
    result = engine.run([_synthetic_preview_record()], preview_dir)
    if result.failed or not result.documents:
        _cli.console.print(f"[red]preview render failed[/red] ({len(result.failed)} error(s))")
        raise typer.Exit(code=1)
    return result.documents[0].path


def _report_analysis_failure(error: str | None, *, ocr_allowed: bool) -> None:
    """Say why the harvest stopped, in a way the operator can act on.

    The error is an exception TYPE name — never a message that could carry a
    sample path. ``OcrRequiredError`` is the one that has a next step attached:
    the sample is a scan and nothing on this machine can read it, so the
    install hint (a file to place, not a download) is printed with it.
    """
    from anastomosis import cli as _cli

    _cli.console.print(f"[red]analysis failed[/red] ({error})")
    if error != "OcrRequiredError":
        return
    from anastomosis.packgen.ocr import INSTALL_HINT

    _cli.console.print(
        "  A sample page is a scan with no readable text."
        + ("" if ocr_allowed else " You passed --no-ocr, so it was not recognized.")
    )
    _cli.console.print(f"  {INSTALL_HINT}")


def _note_ocr_evidence(pack_dir: Path) -> None:
    """Point at OCR_EVIDENCE.md when the draft has one, and say what it means.

    The file exists only when a page was recognized, so its presence IS the
    disclosure — and the operator hears it on the terminal they are already
    looking at, not only in a file they may never open.
    """
    from anastomosis import cli as _cli
    from anastomosis.packgen.emit import OCR_EVIDENCE_NAME

    evidence = pack_dir / OCR_EVIDENCE_NAME
    if not evidence.exists():
        return
    _cli.console.print(
        "[yellow]some of this layout was recognized from page images[/yellow] — read "
        f"{evidence} before trusting any of its text."
    )


def _print_pack_next_steps(
    name: str, pack_dir: Path, out_dir: Path | None, preview_path: Path | None
) -> None:
    """The three-step block a written draft ends with (review, edit, re-render).

    The re-render line names ``--pack-dir`` only when the operator chose a
    destination: a draft in the per-user directory is discovered by name from
    any working directory, so quoting a path there would teach a habit the tool
    no longer needs.
    """
    from anastomosis import cli as _cli

    _cli.console.print("\n[bold]Next steps[/bold] (see DRAFT.md):")
    if preview_path is not None:
        _cli.console.print(f"  1. Review {preview_path} against an original sample.")
    else:
        _cli.console.print(
            "  1. Render a preview (--render-preview) and compare to an original sample."
        )
    _cli.console.print(
        f"  2. Edit {pack_dir / 'template.html'} (reposition unplaced static text, tokens)."
    )
    where = "" if out_dir is None else f" --pack-dir {out_dir}"
    _cli.console.print(f"  3. Re-render:  anast pipeline run <export> -o out --pack {name}{where}")


@pack_app.command("init")
def pack_init(
    samples: Annotated[
        list[str],
        typer.Option(
            "--from-samples",
            help="Sample PDFs: a directory, a glob (./samples/*.pdf), or files.",
        ),
    ],
    name: Annotated[
        str,
        typer.Option(
            "--name", help="A name for this layout: lowercase, no spaces, e.g. acme_soap."
        ),
    ],
    out_dir: Annotated[
        Path | None,
        typer.Option(
            "--out-dir",
            help="Where to save (default: ~/.anastomosis/packs).",
            parser=out_dir,
        ),
    ] = None,
    render_preview: Annotated[
        bool,
        typer.Option(
            "--render-preview/--no-render-preview",
            help="Render one synthetic preview record through the draft (needs Chromium).",
        ),
    ] = False,
    display: Annotated[
        str | None,
        typer.Option("--display", help="Human label for the source format (default: the name)."),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip the interactive same-patient confirmation."),
    ] = False,
    ocr: Annotated[
        bool,
        typer.Option(
            "--ocr/--no-ocr",
            help=(
                "Read sample pages that are scans with the offline OCR engine, "
                "as LAYOUT evidence only. Nothing is downloaded."
            ),
        ),
    ] = True,
) -> None:
    """Learn a draft chart layout from sample notes an EHR printed.

    Reads the samples and works out what is fixed wording and what is a
    patient's own data. Only the fixed wording is summarised back to you — no
    patient data is echoed or kept. It then asks you to confirm the samples
    come from DIFFERENT patients, because text repeated across one patient's
    notes cannot be told apart from the layout itself.

    What you get is a draft and a starting point: compare a chart it produces
    against an original, edit the layout, and try again. It is not claimed to
    match.

    Pages that are scans have no text to read. If an offline OCR engine is
    installed, those pages are RECOGNIZED instead — as layout evidence, never
    as clinical text: the draft marks every recognized string and writes an
    OCR_EVIDENCE.md saying what recognized text may and may not be used for.
    Nothing is downloaded, ever. Pass --no-ocr to learn only from text that was
    genuinely read; with no engine installed, a scanned sample is refused
    either way and the message says what to install.

    The draft is saved under ~/.anastomosis/packs unless --out-dir says
    otherwise, and confirming this step also records its code hash — so the
    layout is offered by name on the next run, and any later edit to its
    context.py un-trusts it until it is confirmed again.
    """
    from anastomosis import cli as _cli
    from anastomosis.core.packinit import (
        LOW_SAMPLE_FLOOR,
        PackInitCommand,
        run_pack_init,
    )

    # Analyze step (confirmed=False): validate the name, collect + harvest the
    # samples, and produce the summary — not "the PHI-safe summary", which is
    # what this said and what the print below stopped calling it. The shared
    # core does the work; this command presents it and runs the interactive
    # confirm.
    analysis_result = run_pack_init(
        PackInitCommand(
            samples=samples,
            name=name,
            display=display,
            out_dir=out_dir,
            confirmed=False,
            allow_ocr=ocr,
        )
    )
    if analysis_result.error == "InvalidPackName":
        _cli.console.print(
            f"[red]invalid pack name {name!r}[/red] — use a lowercase identifier "
            "(letters, digits, underscores; starting with a letter)"
        )
        raise typer.Exit(code=2)
    if analysis_result.error == "NoSamplesFound":
        _cli.console.print(
            "[red]no sample PDFs found[/red] — pass --from-samples <dir>, a glob, or files"
        )
        raise typer.Exit(code=2)
    if analysis_result.error != "ConfirmationRequired":
        # An analysis failure (unreadable/encrypted sample) — type name only.
        _report_analysis_failure(analysis_result.error, ocr_allowed=ocr)
        raise typer.Exit(code=1) from None

    # PHI: log the COUNT only, never the sample paths (they may be named after
    # patients — the extract module's contract).
    _cli.console.print(f"Found [cyan]{analysis_result.sample_count}[/cyan] sample PDF(s).")
    if analysis_result.low_confidence or analysis_result.sample_count < LOW_SAMPLE_FLOOR:
        _cli.console.print(
            f"[yellow]warning: only {analysis_result.sample_count} sample(s)[/yellow] — "
            f"confidence is LOW. The static/per-patient text split needs >= {LOW_SAMPLE_FLOOR} "
            "DISTINCT-patient samples to be reliable."
        )

    # NOT "PHI-safe summary", which is what this said. The static labels below
    # have to be on every sample AND hold a spot nothing else uses, which is a
    # good filter and not a proof — a value all of these patients share, in a
    # fixed cell, still passes it. So the list can carry patient data and the
    # operator has to read it as such (#200).
    _cli.console.print("\n[bold]Inferred design[/bold] — read the static labels before confirming:")
    for line in analysis_result.summary:
        _cli.console.print(f"  {line}")

    _cli.console.print(f"\n[yellow]Before you confirm:[/yellow] {analysis_result.caveat}")
    if not yes and not typer.confirm("Are these samples from DIFFERENT patients?", default=False):
        _cli.console.print("Aborting — gather samples from distinct patients and re-run.")
        declined("No draft layout was written.")
        raise typer.Exit(code=0)

    # Emit step (confirmed=True): the shared core writes the draft pack.
    emit_result = run_pack_init(
        PackInitCommand(
            samples=samples,
            name=name,
            display=display,
            out_dir=out_dir,
            confirmed=True,
            allow_ocr=ocr,
        )
    )
    if not emit_result.ok:
        _cli.console.print(f"[red]emit failed[/red] ({emit_result.error})")
        raise typer.Exit(code=1) from None
    pack_dir = emit_result.pack_dir
    assert pack_dir is not None  # ok=True guarantees a pack_dir
    _cli.console.print(f"\n[green]wrote draft pack[/green] {_cli._glyphs().arrow} {pack_dir}")
    _note_ocr_evidence(pack_dir)

    preview_path: Path | None = None
    if render_preview:
        preview_path = _render_preview(pack_dir)
        if preview_path is not None:
            _cli.console.print(f"[green]preview[/green] {_cli._glyphs().arrow} {preview_path}")

    _print_pack_next_steps(name, pack_dir, out_dir, preview_path)


def _refuse_analysis(
    analysis: SourceInitResult, *, name: str, example: Path, to: str | None
) -> None:
    """Present the analyze step's refusals and exit; return only on the checkpoint.

    Every pre-confirm outcome except ``ConfirmationRequired`` is a refusal with
    its own line and its own exit code, and the set of them is a table, not
    logic. It lives here so ``source_init`` reads as the flow it is —
    analyze, show, confirm, save — rather than as five guard clauses with the
    flow threaded between them.
    """
    from anastomosis import cli as _cli

    refusals: dict[str, tuple[str, int]] = {
        "UnknownDestination": (
            f"[red]unknown destination {to!r}[/red] — run "
            "[cyan]anast destination list[/cyan] to see the names this build carries.",
            2,
        ),
        "InvalidSourceName": (
            f"[red]invalid mapping name {name!r}[/red] — use a lowercase identifier "
            "(letters, digits, underscores; starting with a letter)",
            2,
        ),
        "NoExampleFile": (
            f"[red]no csv/tsv/json/ndjson file found in {example}[/red] — "
            "point the example at the file itself",
            2,
        ),
        "AmbiguousExample": (
            f"[red]multiple csv/tsv/json/ndjson files in {example}[/red] — "
            "point at one structured file, not the directory",
            2,
        ),
        "CannotAnalyze": (
            f"[red]could not analyze the example[/red] ({analysis.detail})",
            1,
        ),
    }
    refusal = refusals.get(analysis.error or "")
    if refusal is not None:
        _cli.console.print(refusal[0])
        raise typer.Exit(code=refusal[1])
    # The only remaining pre-confirm outcome is the expected analyze checkpoint.
    assert analysis.error == "ConfirmationRequired"


@source_app.command("init")
def source_init(
    example: Annotated[
        Path,
        typer.Argument(
            exists=True,
            readable=True,
            help="Example export: a csv/tsv/json/ndjson FILE (or a dir holding one).",
        ),
    ],
    name: Annotated[
        str, typer.Option("--name", help="Mapping id (lowercase identifier, e.g. acme_csv).")
    ],
    display: Annotated[
        str | None,
        typer.Option("--display", help="Human label for the format (default: the name)."),
    ] = None,
    out_dir: Annotated[
        Path | None,
        typer.Option(
            "--out-dir",
            help="Where to save (default: ~/.anastomosis/sources).",
            parser=out_dir,
        ),
    ] = None,
    to: Annotated[
        str | None,
        typer.Option(
            "--to",
            help="Destination this format is being taught FOR (a registry name, e.g. tebra). "
            "The mapping records it, and a migration to another destination refuses.",
        ),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Accept the suggested mapping without confirmation."),
    ] = False,
) -> None:
    """Teach the toolkit a new flat export format from one example file.

    Analyzes the example LOCALLY (no value ever leaves the machine — the summary
    shows column names, inferred types, counts, and digit/letter-masked shapes
    only), proposes a mapping to the canonical model, proves the mapping drops no
    column, and saves it. After saving, that format auto-detects like any
    built-in source. Refine the saved ``mapping.json`` and re-run to re-verify.

    Pass ``--to`` to choose the destination BEFORE teaching: the mapping records
    which destination it was taught for, and a migration that runs it somewhere
    else refuses instead of mapping one system's columns into another.
    """
    from anastomosis import cli as _cli
    from anastomosis.core.source_init_command import SourceInitCommand, run_source_init_command

    # Analyze step (confirmed=False): validate the name, resolve + analyze the
    # example, and produce the PHI-safe proposal via the SHARED core (the same
    # flow the GUI source wizard runs); this command presents it and confirms.
    analysis = run_source_init_command(
        SourceInitCommand(
            example=example,
            name=name,
            display=display,
            out_dir=out_dir,
            confirmed=False,
            destination=to,
        )
    )
    _refuse_analysis(analysis, name=name, example=example, to=to)

    _cli.console.print("\n[bold]Analysis[/bold] (PHI-safe — no values shown):")
    for line in analysis.summary:
        _cli.console.print(f"  {line}")
    _cli.console.print(
        f"\n[cyan]{analysis.mapped}[/cyan] column(s) map to canonical fields; the rest are "
        "preserved in [cyan]extensions[/cyan] (nothing is dropped)."
    )

    if not yes and not typer.confirm("Save this mapping?", default=False):
        _cli.console.print(
            "Aborting — refine with --display/--name or edit the example, then re-run."
        )
        declined("The format was not saved.")
        raise typer.Exit(code=0)

    # Save step (confirmed=True): build the mapping, prove it drops no column via
    # a round-trip, and save it owner-only — all in the shared core.
    saved = run_source_init_command(
        SourceInitCommand(
            example=example,
            name=name,
            display=display,
            out_dir=out_dir,
            confirmed=True,
            destination=to,
        )
    )
    if saved.error == "CannotBuildMapping":
        _cli.console.print(f"[red]{saved.detail}[/red]")
        raise typer.Exit(code=2)
    if saved.error == "MappingLoadFailed":
        _cli.console.print(f"[red]the mapping failed to load the example[/red] ({saved.detail})")
        raise typer.Exit(code=1)
    if saved.error == "WouldDropColumns":
        # PHI-safe: column NAMES only.
        _cli.console.print(
            "[red]refusing to save — these columns would be dropped:[/red] "
            + ", ".join(saved.dropped_columns)
        )
        raise typer.Exit(code=1)
    if saved.error == "SaveFailed":
        _cli.console.print(f"[red]could not save the mapping[/red] ({saved.detail})")
        raise typer.Exit(code=1)
    if not saved.ok:  # defensive: any other unexpected non-ok outcome
        _cli.console.print(f"[red]could not save the mapping[/red] ({saved.error})")
        raise typer.Exit(code=1)

    assert saved.mapping_dir is not None  # ok=True guarantees a mapping_dir
    _cli.console.print(
        f"\n[green]learned source[/green] {name!r} {_cli._glyphs().arrow} {saved.mapping_dir} "
        f"({saved.record_count} record(s) round-tripped)"
    )
    _cli.console.print(
        f"  Review {saved.mapping_dir / 'MAPPING.md'}; refine mapping.json and re-run if needed."
    )
    if saved.destination is not None:
        _cli.console.print(
            f"  Taught for [cyan]{saved.destination}[/cyan]; migrating it to another "
            "destination will refuse."
        )
    # Only a source in the DEFAULT directory is discoverable: `pipeline run` has
    # no `--source-dir` to point at another one (unlike packs, which thread
    # `--pack-dir` — see `pack init` above). Printing the run command for a
    # custom `--out-dir` handed over a line that answers
    # "unknown source 'name' (available: …)".
    from anastomosis.sources.learned import user_sources_dir

    if saved.mapping_dir.parent == user_sources_dir():
        _cli.console.print(f"  Run it:  anast pipeline run <export-dir> --source {name} -o out")
    else:
        _cli.console.print(
            f"  Saved outside the folder Anastomosis reads ({user_sources_dir()}). "
            f"Move it there to use --source {name}."
        )
