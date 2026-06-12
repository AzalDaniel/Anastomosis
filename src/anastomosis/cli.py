"""Anastomosis command-line interface.

Installed as both ``anast`` (everyday) and ``anastomosis`` (formal).
Sub-commands appear as their pipeline stages are implemented:

    anast ingest        EHI export / C-CDA  ->  canonical records
    anast reconstruct   canonical records   ->  rendered documents
    anast qa            rendered documents  ->  QA report
    anast archive       full pipeline       ->  searchable offline archive
    anast bundle        full pipeline       ->  per-patient bundles
    anast pipeline run  one command, whole pipeline (charts + optional archive/bundle)
    anast pack init     sample PDFs         ->  a draft template pack
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console

import anastomosis
import anastomosis.sources.ccda
import anastomosis.sources.pf_tebra
from anastomosis.sources import available_sources, detect_source, get_source

if TYPE_CHECKING:
    from anastomosis.core.model import PatientRecord
    from anastomosis.qa import QAReport
    from anastomosis.reconstruct.engine import ReconstructionEngine, RenderResult

app = typer.Typer(
    name="anast",
    help=__doc__,
    no_args_is_help=True,
    rich_markup_mode="rich",
)
pipeline_app = typer.Typer(help="Run pipeline stages end to end.")
app.add_typer(pipeline_app, name="pipeline")
destination_app = typer.Typer(help="Inspect destinations and plan delivery routes.")
app.add_typer(destination_app, name="destination")
pack_app = typer.Typer(help="Build and inspect template packs.")
app.add_typer(pack_app, name="pack")
console = Console()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"anastomosis {anastomosis.__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    """Reconstruct, verify, and re-home clinical records."""


@app.command()
def info() -> None:
    """Show toolkit status: installed extras, sources, packs, environment."""
    from anastomosis.reconstruct import discover_packs

    console.print(f"[bold]anastomosis[/bold] {anastomosis.__version__}")
    for extra, module in (
        ("render", "playwright"),
        ("render-qa", "fitz"),
        ("fhir", "fhir.resources"),
        ("gui", "webview"),
    ):
        try:
            __import__(module)
            console.print(f"  extra [green]{extra}[/green]: available")
        except ImportError:
            console.print(f"  extra [dim]{extra}[/dim]: not installed")
    for adapter in available_sources():
        console.print(f"  source [cyan]{adapter.name}[/cyan]: {adapter.description}")
    for status in discover_packs().values():
        if status.available:
            console.print(f"  pack [cyan]{status.name}[/cyan]: available ({status.origin})")
        else:
            console.print(f"  pack [red]{status.name}[/red]: {status.diagnosis}")


# --- shared pipeline machinery ---------------------------------------------


@dataclass
class _PipelineResult:
    """What a pipeline run yields the caller (the CLI commands)."""

    records: list[PatientRecord]
    render_result: RenderResult
    engine: ReconstructionEngine
    qa_report: QAReport | None
    page_size: str


def _resolve_source(export_dir: Path, source: str | None) -> object:
    if source:
        return get_source(source)
    detected = detect_source(export_dir)
    if detected is None:
        known = ", ".join(a.name for a in available_sources())
        console.print(f"[red]Could not identify the export format.[/red] Try --source ({known})")
        raise typer.Exit(code=2)
    console.print(f"Detected source: [cyan]{detected.name}[/cyan]")
    return detected


def _parse_section_overrides(section: list[str] | None) -> dict[str, bool]:
    overrides: dict[str, bool] = {}
    for item in section or []:
        key, _, value = item.partition("=")
        overrides[key.strip()] = value.strip().lower() in ("on", "true", "1", "yes")
    return overrides


def _run_pipeline(
    *,
    export_dir: Path,
    out: Path,
    source: str | None,
    pack: str,
    pack_dirs: list[Path] | None,
    force: bool,
    section: list[str] | None,
    qa: bool,
) -> _PipelineResult:
    """The full pipeline (ingest → reconstruct → optional QA).

    Returns rich enough state that the caller can layer on archive/bundle
    delivery without re-loading records or re-rendering charts.
    """
    from anastomosis.reconstruct import discover_packs
    from anastomosis.reconstruct.chromium import ChromiumRenderer
    from anastomosis.reconstruct.engine import ReconstructionEngine

    adapter = _resolve_source(export_dir, source)

    statuses = discover_packs(list(pack_dirs or []), allow_external=bool(pack_dirs))
    status = statuses.get(pack)
    if status is None or status.pack is None:
        diagnosis = status.diagnosis if status else f"unknown pack (have: {', '.join(statuses)})"
        console.print(f"[red]Pack {pack!r} unavailable:[/red] {diagnosis}")
        raise typer.Exit(code=2)

    overrides = _parse_section_overrides(section)
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
        section_overrides=overrides,
    )
    records = list(adapter.load(export_dir))  # type: ignore[attr-defined]
    result = engine.run(records, out, force=force)
    console.print(
        f"[green]{len(result.rendered)} rendered[/green], "
        f"{len(result.skipped)} skipped, "
        f"{'[red]' if result.failed else ''}{len(result.failed)} failed"
        f"{'[/red]' if result.failed else ''} → {out}"
    )
    if result.failed:
        for encounter_id, exc_type in result.failed:
            console.print(f"  [red]failed[/red] encounter {encounter_id} ({exc_type})")
        raise typer.Exit(code=1)

    qa_report = None
    if qa and result.documents:
        qa_report = _run_qa_stage(records, result, engine, out, manifest.page.size)
    return _PipelineResult(
        records=records,
        render_result=result,
        engine=engine,
        qa_report=qa_report,
        page_size=manifest.page.size,
    )


def _run_qa_stage(
    records: list[PatientRecord],
    result: RenderResult,
    engine: ReconstructionEngine,
    out: Path,
    page_size: str,
) -> QAReport | None:
    try:
        from anastomosis.qa import Verdict, run_qa, write_report
    except ImportError as exc:
        if exc.name != "fitz":  # only the optional dependency may downgrade QA
            raise
        console.print("[yellow]QA skipped[/yellow]: install anastomosis[render] for PyMuPDF")
        return None
    lookup = {(r.patient.id, e.id): (e, r) for r in records for e in r.encounters}
    report = run_qa(
        ((d.path, *lookup[d.patient_id, d.encounter_id]) for d in result.documents),
        section_flags=engine.section_flags,
        page_size=page_size,
    )
    report_path = write_report(report, out)
    console.print(
        f"QA: [green]{report.count(Verdict.PASS)} pass[/green], "
        f"{report.count(Verdict.WARN)} warn, "
        f"{'[red]' if not report.ok else ''}{report.count(Verdict.FAIL)} fail"
        f"{'[/red]' if not report.ok else ''} → {report_path.name}"
    )
    if not report.ok:
        raise typer.Exit(code=1)
    return report


# --- pipeline run -----------------------------------------------------------


@pipeline_app.command("run")
def pipeline_run(
    export_dir: Annotated[Path, typer.Argument(exists=True, file_okay=False, readable=True)],
    out: Annotated[Path, typer.Option("--out", "-o", help="Output directory (created 0700).")],
    source: Annotated[
        str | None,
        typer.Option("--source", "-s", help="Source adapter name (default: auto-detect)."),
    ] = None,
    pack: Annotated[str, typer.Option("--pack", "-p", help="Template pack name.")] = "generic_soap",
    pack_dir: Annotated[
        list[Path] | None,
        typer.Option("--pack-dir", help="Extra pack directories (implies trusting their code)."),
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Re-render documents that exist.")] = False,
    section: Annotated[
        list[str] | None,
        typer.Option(
            "--section",
            help="Override a section flag, e.g. --section insurance=on --section addenda=off.",
        ),
    ] = None,
    qa: Annotated[
        bool, typer.Option("--qa/--no-qa", help="Verify every rendered document (default on).")
    ] = True,
    archive: Annotated[
        Path | None,
        typer.Option("--archive", help="Also emit an offline browsable archive in this directory."),
    ] = None,
    bundle: Annotated[
        Path | None,
        typer.Option(
            "--bundle", help="Also emit one per-patient bundle subdirectory in this directory."
        ),
    ] = None,
    ccda: Annotated[
        Path | None,
        typer.Option("--ccda", help="Also emit one C-CDA / CCD XML per patient in this directory."),
    ] = None,
) -> None:
    """Ingest an export and reconstruct every encounter into chart PDFs."""
    pipeline = _run_pipeline(
        export_dir=export_dir,
        out=out,
        source=source,
        pack=pack,
        pack_dirs=pack_dir,
        force=force,
        section=section,
        qa=qa,
    )
    if archive is not None:
        _deliver_archive(pipeline, pdfs_dir=out, out=archive)
    if bundle is not None:
        _deliver_bundles(pipeline, pdfs_dir=out, out=bundle)
    if ccda is not None:
        _deliver_ccda(pipeline, out=ccda)


# --- anast archive ----------------------------------------------------------


@app.command("archive")
def archive_cmd(
    export_dir: Annotated[Path, typer.Argument(exists=True, file_okay=False, readable=True)],
    out: Annotated[Path, typer.Option("--out", "-o", help="Archive output directory (0700).")],
    source: Annotated[
        str | None,
        typer.Option("--source", "-s", help="Source adapter name (default: auto-detect)."),
    ] = None,
    pack: Annotated[str, typer.Option("--pack", "-p", help="Template pack name.")] = "generic_soap",
    pack_dir: Annotated[
        list[Path] | None,
        typer.Option("--pack-dir", help="Extra pack directories (implies trusting their code)."),
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Re-render documents that exist.")] = False,
    section: Annotated[
        list[str] | None,
        typer.Option("--section", help="Override a section flag."),
    ] = None,
    qa: Annotated[
        bool, typer.Option("--qa/--no-qa", help="Verify every rendered document (default on).")
    ] = True,
    charts_dir: Annotated[
        Path | None,
        typer.Option(
            "--charts-dir",
            help="Where chart PDFs land before being copied into the archive "
            "(default: <out>/_charts).",
        ),
    ] = None,
) -> None:
    """Run the full pipeline and write a searchable offline archive."""
    charts = charts_dir or (out / "_charts")
    pipeline = _run_pipeline(
        export_dir=export_dir,
        out=charts,
        source=source,
        pack=pack,
        pack_dirs=pack_dir,
        force=force,
        section=section,
        qa=qa,
    )
    _deliver_archive(pipeline, pdfs_dir=charts, out=out)


# --- anast bundle -----------------------------------------------------------


@app.command("bundle")
def bundle_cmd(
    export_dir: Annotated[Path, typer.Argument(exists=True, file_okay=False, readable=True)],
    out: Annotated[Path, typer.Option("--out", "-o", help="Bundles output directory (0700).")],
    source: Annotated[
        str | None,
        typer.Option("--source", "-s", help="Source adapter name (default: auto-detect)."),
    ] = None,
    pack: Annotated[str, typer.Option("--pack", "-p", help="Template pack name.")] = "generic_soap",
    pack_dir: Annotated[
        list[Path] | None,
        typer.Option("--pack-dir", help="Extra pack directories (implies trusting their code)."),
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Re-render documents that exist.")] = False,
    section: Annotated[
        list[str] | None,
        typer.Option("--section", help="Override a section flag."),
    ] = None,
    qa: Annotated[
        bool, typer.Option("--qa/--no-qa", help="Verify every rendered document (default on).")
    ] = True,
    charts_dir: Annotated[
        Path | None,
        typer.Option(
            "--charts-dir",
            help="Where chart PDFs land before being copied into the bundles "
            "(default: <out>/_charts).",
        ),
    ] = None,
) -> None:
    """Run the full pipeline and write one per-patient bundle directory each."""
    charts = charts_dir or (out / "_charts")
    pipeline = _run_pipeline(
        export_dir=export_dir,
        out=charts,
        source=source,
        pack=pack,
        pack_dirs=pack_dir,
        force=force,
        section=section,
        qa=qa,
    )
    _deliver_bundles(pipeline, pdfs_dir=charts, out=out)


# --- delivery wiring --------------------------------------------------------


def _deliver_archive(pipeline: _PipelineResult, *, pdfs_dir: Path, out: Path) -> None:
    from anastomosis.deliver.archive import ArchiveDeliverer

    deliverer = ArchiveDeliverer()
    result = deliverer.deliver(pipeline.records, pdfs_dir, out, qa_report=pipeline.qa_report)
    console.print(
        f"Archive: [green]{result.patient_count} patients[/green], "
        f"{result.encounter_count} encounters, {result.pdf_count} pdfs → {result.out_dir}"
    )


def _deliver_bundles(pipeline: _PipelineResult, *, pdfs_dir: Path, out: Path) -> None:
    from anastomosis.deliver.bundle import BundleDeliverer

    deliverer = BundleDeliverer()
    pdfs = sorted(pdfs_dir.glob("*.pdf")) if pdfs_dir.is_dir() else []
    written = 0
    for record in pipeline.records:
        deliverer.deliver(record, pdfs, out, qa_report=pipeline.qa_report)
        written += 1
    console.print(f"Bundles: [green]{written} patients[/green] → {out}")


def _deliver_ccda(pipeline: _PipelineResult, *, out: Path) -> None:
    from anastomosis.deliver.ccda_export import deliver_ccda

    written = deliver_ccda(pipeline.records, out)
    console.print(f"C-CDA: [green]{len(written)} patients[/green] → {out}")


# --- anast destination ------------------------------------------------------


def _load_registry(registry: Path | None) -> object:
    """Load the destination registry (overlay if given), raising loud on error."""
    from anastomosis.destinations.registry import DestinationRegistry

    if registry is not None:
        return DestinationRegistry.merged(registry)
    return DestinationRegistry.load()


def _oldest_evidence(entry: object) -> str:
    from anastomosis.destinations.registry import DestinationEntry

    assert isinstance(entry, DestinationEntry)
    dates = [
        cap.evidence.verified
        for cap in (entry.doc_write_api, entry.ccda_import, entry.browser)
        if cap.evidence is not None
    ]
    return min(dates).isoformat() if dates else "—"


def _local_pack_status(name: str) -> str:
    """Describe whether a discovered browser pack exists locally for ``name``.

    Surfaced in `destination list`/`route` so the operator can see a pack is
    present (and whether the wizard has been run) without it ever auto-affecting
    routing — the registry overlay stays the single routing truth.
    """
    from anastomosis.destinations.loader import BrowserPackError, load_destination_pack

    try:
        loaded = load_destination_pack(name)
    except BrowserPackError:
        return "—"
    return "ready" if loaded.ready else "needs-discovery"


@destination_app.command("list")
def destination_list(
    registry: Annotated[
        Path | None,
        typer.Option("--registry", help="Overlay registry file (replaces packaged entries)."),
    ] = None,
) -> None:
    """List registered destinations and their declared capabilities."""
    from rich.console import Console
    from rich.table import Table

    from anastomosis.destinations.registry import DestinationEntry, DestinationRegistry

    reg = _load_registry(registry)
    assert isinstance(reg, DestinationRegistry)
    table = Table(title="destinations")
    table.add_column("name", style="cyan")
    table.add_column("display")
    table.add_column("doc_write_api")
    table.add_column("ccda_import")
    table.add_column("browser")
    table.add_column("pack")
    table.add_column("oldest evidence")
    for name in sorted(reg.entries):
        entry: DestinationEntry = reg.entries[name]
        table.add_row(
            entry.name,
            entry.display,
            entry.doc_write_api.kind,
            entry.ccda_import.kind,
            entry.browser.kind,
            _local_pack_status(entry.name),
            _oldest_evidence(entry),
        )
    # A wide, non-truncating console so the seven columns (and their cell text)
    # survive intact regardless of the calling terminal width — the table is a
    # data dump the operator scrolls, not a width-fit layout.
    Console(width=200).print(table)


@destination_app.command("route")
def destination_route(
    name: Annotated[str, typer.Argument(help="Destination name (see `anast destination list`).")],
    registry: Annotated[
        Path | None,
        typer.Option("--registry", help="Overlay registry file (replaces packaged entries)."),
    ] = None,
) -> None:
    """Print the shortest-path transit map; exit 1 if no viable route exists."""
    from anastomosis.deliver.router import plan_route
    from anastomosis.destinations.registry import DestinationRegistry

    reg = _load_registry(registry)
    assert isinstance(reg, DestinationRegistry)
    try:
        transit = plan_route(name, reg)
    except KeyError as exc:
        # KeyError carries the known-names list (no PHI) — show it, not a traceback.
        console.print(f"[red]{exc.args[0] if exc.args else exc}[/red]")
        # Exit-code contract: 2 = unknown destination NAME (operator typo),
        # 1 = known destination with NO viable route (capability gap). Tests
        # pin both; scripts branch on them.
        raise typer.Exit(code=2) from None
    console.print(transit.render())
    # Surface a locally present browser pack WITHOUT auto-flipping routing: the
    # registry overlay remains the single routing truth, so we only note that a
    # pack exists and how the operator declares it.
    pack_status = _local_pack_status(name)
    if pack_status != "—" and transit.options[-1].kind.value == "browser":
        if not transit.options[-1].viable:
            console.print(
                f"note: browser pack present locally ({pack_status}) — declare it in your "
                "registry overlay (kind: pack) to route through it"
            )
        else:
            console.print(f"note: browser pack present locally ({pack_status})")
    if transit.chosen is None:
        raise typer.Exit(code=1)


# --- anast destination init (the selector-discovery wizard) -----------------

# The maximum re-entry attempts for a not-found selector under --validate before
# the operator must either accept it unvalidated or give up.
_VALIDATE_MAX_TRIES = 3


def _make_validator(cdp_url: str) -> object:
    """Build the live selector validator for ``--validate`` (the SEAM tests mock).

    Attaches over CDP (loopback-only, validated) to the browser the operator
    launched and logged into, wraps its first page in the
    :class:`PlaywrightPageAdapter`, and returns a
    :class:`~anastomosis.destinations.wizard.CdpSelectorValidator`. Tests
    monkeypatch this whole function so the validation flow needs no browser.
    Playwright is imported only here (lazily, via ``connect_over_cdp``).
    """
    from anastomosis.deliver.browser.cdp import CdpEndpoint, connect_over_cdp
    from anastomosis.destinations.browserpack import PlaywrightPageAdapter
    from anastomosis.destinations.wizard import CdpSelectorValidator

    browser = connect_over_cdp(CdpEndpoint(cdp_url))
    # The operator has their EHR open; drive its existing context/page.
    context = browser.contexts[0]
    page = context.pages[0]
    return CdpSelectorValidator(PlaywrightPageAdapter(page))


def _prompt_slot(
    slot: str,
    *,
    required: bool,
    guidance: str,
    validator: object | None,
) -> str:
    """Prompt for one selector slot, optionally validating it against the page.

    Optional slots accept an empty entry (= skip). With a ``validator``, a
    selector matching zero elements may be re-entered up to
    :data:`_VALIDATE_MAX_TRIES` times or accepted with an explicit confirmation
    (the ``--allow-unvalidated`` consent at the slot level). Without one, the
    selector is accepted as-is. PHI: prompts and prints carry slot names and
    selectors only — never patient data.
    """
    from anastomosis.destinations.wizard import SelectorValidator

    label = "required" if required else "optional, blank to skip"
    for attempt in range(1, _VALIDATE_MAX_TRIES + 1):
        raw: str = typer.prompt(f"  {slot} ({label}) — {guidance}", default="")
        value = raw.strip()
        if not value:
            if not required:
                return ""
            console.print("    [yellow]a value is required[/yellow]")
            continue
        if validator is None:
            return value
        assert isinstance(validator, SelectorValidator)
        count = validator.count(value)
        if count >= 1:
            console.print(f"    [green]found {count} element(s)[/green]")
            return value
        console.print(f"    [yellow]selector matched 0 elements[/yellow] (try {attempt})")
        if attempt < _VALIDATE_MAX_TRIES:
            continue
        if typer.confirm("    accept this unvalidated selector anyway?", default=False):
            return value
    # Exhausted tries without acceptance: re-raise as an explicit operator abort.
    console.print(f"[red]gave up discovering {slot!r} (no matching selector)[/red]")
    raise typer.Exit(code=1)


@destination_app.command("init")
def destination_init(
    name: Annotated[str, typer.Argument(help="Destination pack name, e.g. tebra.")],
    out_dir: Annotated[
        Path | None,
        typer.Option("--out-dir", help="Where to write selectors.yaml (default: user dir)."),
    ] = None,
    validate: Annotated[
        bool,
        typer.Option("--validate", help="Check each selector against a live page (needs --cdp)."),
    ] = False,
    cdp: Annotated[
        str | None,
        typer.Option("--cdp", help="Loopback CDP endpoint, e.g. http://127.0.0.1:9222."),
    ] = None,
    pack_dir: Annotated[
        list[Path] | None,
        typer.Option("--pack-dir", help="Extra directories to find the pack scaffold in."),
    ] = None,
) -> None:
    """Discover a browser pack's CSS selectors against your live EHR session.

    Loads the pack scaffold, prompts for each selector slot (required first),
    optionally validates each against your attached browser (``--validate
    --cdp``), then writes ``selectors.yaml`` into your user directory. The
    packaged registry is never modified — a paste-able overlay snippet is printed
    so you declare the now-discovered pack in your own routing overlay.
    """
    from anastomosis.deliver.browser.cdp import SHARED_MACHINE_WARNING
    from anastomosis.destinations.browserpack import SelectorMap
    from anastomosis.destinations.loader import (
        BrowserPackError,
        load_destination_pack,
        user_destinations_dir,
    )
    from anastomosis.destinations.wizard import (
        SLOT_GUIDANCE,
        registry_overlay_snippet,
        write_selectors,
    )

    try:
        loaded = load_destination_pack(name, list(pack_dir or []))
    except BrowserPackError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from None

    validator: object | None = None
    if validate:
        if cdp is None:
            console.print("[red]--validate requires --cdp (a loopback CDP endpoint)[/red]")
            raise typer.Exit(code=2)
        console.print(SHARED_MACHINE_WARNING)
        try:
            validator = _make_validator(cdp)
        except Exception as exc:  # attach/launch failure — name the type, no PHI
            console.print(f"[red]could not attach for validation ({type(exc).__name__})[/red]")
            raise typer.Exit(code=2) from None
    elif cdp is not None:
        # --cdp without --validate: still warn (a debug port was named).
        console.print(SHARED_MACHINE_WARNING)
    else:
        console.print(
            "[yellow]selectors accepted as-is[/yellow] — preflight validates them at run time"
        )

    console.print(f"Discovering selectors for [cyan]{loaded.name}[/cyan]:")
    discovered: dict[str, str] = {}
    for slot in SelectorMap.required_slots():
        discovered[slot] = _prompt_slot(
            slot, required=True, guidance=SLOT_GUIDANCE.get(slot, ""), validator=validator
        )
    for slot in SelectorMap.optional_slots():
        discovered[slot] = _prompt_slot(
            slot, required=False, guidance=SLOT_GUIDANCE.get(slot, ""), validator=validator
        )

    target_root = out_dir or user_destinations_dir()
    written = write_selectors(loaded.name, discovered, target_root)
    console.print(f"[green]wrote[/green] {written}")
    console.print(
        "\nNext steps — declare this pack in your registry overlay (NOT the packaged one):"
    )
    console.print(registry_overlay_snippet(loaded.name))
    console.print(
        f"Then route it:  anast destination route {loaded.name} --registry <your-overlay>.yaml"
    )


# --- anast pack init (the pack-from-samples wizard) -------------------------

# A pack name must be a safe directory + manifest identifier (it becomes the
# pack's directory name and YAML `name:`). Mirrors the loader's expectations.
_PACK_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
# Below this many samples the static/per-patient split is statistically weak;
# the wizard warns loudly (the learner still runs).
_LOW_SAMPLE_FLOOR = 3


def _collect_sample_pdfs(patterns: list[str]) -> list[Path]:
    """Resolve dir-or-glob arguments into a sorted, de-duplicated PDF list.

    A bare directory contributes its ``*.pdf`` children; a glob is expanded;
    a direct path is taken as-is. Sorted for deterministic sample indices.
    """
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
        # Treat as a glob (supports ``./samples/*.pdf``).
        found.update(Path(match) for match in _glob.glob(raw) if Path(match).is_file())
    return sorted(found)


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
    from anastomosis.reconstruct import discover_packs
    from anastomosis.reconstruct.engine import ReconstructionEngine

    try:
        from anastomosis.reconstruct.chromium import ChromiumRenderer
    except ImportError:
        console.print("[yellow]preview skipped[/yellow]: install anastomosis[render] for Chromium")
        return None
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            pw.chromium.launch().close()
    except Exception as exc:  # browser not fetched / cannot launch
        console.print(
            f"[yellow]preview skipped[/yellow]: Chromium unavailable ({type(exc).__name__}); "
            "run 'playwright install chromium'"
        )
        return None

    status = discover_packs([pack_dir.parent], allow_external=True).get(pack_dir.name)
    if status is None or status.pack is None:
        diagnosis = status.diagnosis if status else "draft pack not discovered"
        console.print(f"[red]preview failed:[/red] {diagnosis}")
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
        console.print(f"[red]preview render failed[/red] ({len(result.failed)} error(s))")
        raise typer.Exit(code=1)
    return result.documents[0].path


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
        str, typer.Option("--name", help="Pack name (lowercase identifier, e.g. acme_soap).")
    ],
    out_dir: Annotated[
        Path,
        typer.Option("--out-dir", help="Directory to write the pack into (default: ./packs)."),
    ] = Path("packs"),
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
) -> None:
    """Learn a draft template pack from sample PDFs of an EHR's note format.

    Collects the samples, harvests + analyzes them (PHI-safe — only static
    template text is summarized), echoes the same-patient caveat for explicit
    confirmation, then writes a loadable DRAFT pack. The draft is a STARTING
    POINT: review the rendered preview against an original sample, edit the
    template, and re-render. Fidelity is not claimed.
    """
    from anastomosis.core.logutil import exc_tag
    from anastomosis.packgen import analyze, extract_samples
    from anastomosis.packgen.emit import SAME_PATIENT_CAVEAT, emit_draft_pack

    if not _PACK_NAME_RE.match(name):
        console.print(
            f"[red]invalid pack name {name!r}[/red] — use a lowercase identifier "
            "(letters, digits, underscores; starting with a letter)"
        )
        raise typer.Exit(code=2)

    pdfs = _collect_sample_pdfs(samples)
    if not pdfs:
        console.print(
            "[red]no sample PDFs found[/red] — pass --from-samples <dir>, a glob, or files"
        )
        raise typer.Exit(code=2)
    # PHI: log the COUNT only, never the sample paths (they may be named after
    # patients — the extract module's contract).
    console.print(f"Found [cyan]{len(pdfs)}[/cyan] sample PDF(s).")
    if len(pdfs) < _LOW_SAMPLE_FLOOR:
        console.print(
            f"[yellow]warning: only {len(pdfs)} sample(s)[/yellow] — confidence is LOW. "
            f"The static/per-patient text split needs >= {_LOW_SAMPLE_FLOOR} DISTINCT-patient "
            "samples to be reliable."
        )

    try:
        analysis = analyze(extract_samples(pdfs))
    except Exception as exc:  # unreadable/encrypted sample — type only, no path/PHI
        console.print(f"[red]analysis failed[/red] ({exc_tag(exc)})")
        raise typer.Exit(code=1) from None

    console.print("\n[bold]Inferred design[/bold] (PHI-safe summary):")
    for line in analysis.summary_lines():
        console.print(f"  {line}")

    console.print(f"\n[yellow]Same-patient caveat:[/yellow] {SAME_PATIENT_CAVEAT}")
    if not yes and not typer.confirm("Are these samples from DIFFERENT patients?", default=False):
        console.print("Aborting — gather samples from distinct patients and re-run.")
        raise typer.Exit(code=0)

    try:
        pack_dir = emit_draft_pack(analysis, name=name, display=display or name, out_dir=out_dir)
    except Exception as exc:
        console.print(f"[red]emit failed[/red] ({exc_tag(exc)})")
        raise typer.Exit(code=1) from None
    console.print(f"\n[green]wrote draft pack[/green] → {pack_dir}")

    preview_path: Path | None = None
    if render_preview:
        preview_path = _render_preview(pack_dir)
        if preview_path is not None:
            console.print(f"[green]preview[/green] → {preview_path}")

    console.print("\n[bold]Next steps[/bold] (see DRAFT.md):")
    if preview_path is not None:
        console.print(f"  1. Review {preview_path} against an original sample.")
    else:
        console.print("  1. Render a preview (--render-preview) and compare to an original sample.")
    console.print(
        f"  2. Edit {pack_dir / 'template.html'} (reposition unplaced static text, tokens)."
    )
    console.print(
        f"  3. Re-render:  anast pipeline run <export> -o out --pack {name} --pack-dir {out_dir}"
    )


if __name__ == "__main__":
    app()
