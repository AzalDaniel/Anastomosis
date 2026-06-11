"""Anastomosis command-line interface.

Installed as both ``anast`` (everyday) and ``anastomosis`` (formal).
Sub-commands appear as their pipeline stages are implemented:

    anast ingest        EHI export / C-CDA  ->  canonical records
    anast reconstruct   canonical records   ->  rendered documents
    anast qa            rendered documents  ->  QA report
    anast archive       full pipeline       ->  searchable offline archive
    anast bundle        full pipeline       ->  per-patient bundles
    anast pipeline run  one command, whole pipeline (charts + optional archive/bundle)
"""

from __future__ import annotations

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


@destination_app.command("list")
def destination_list(
    registry: Annotated[
        Path | None,
        typer.Option("--registry", help="Overlay registry file (replaces packaged entries)."),
    ] = None,
) -> None:
    """List registered destinations and their declared capabilities."""
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
    table.add_column("oldest evidence")
    for name in sorted(reg.entries):
        entry: DestinationEntry = reg.entries[name]
        table.add_row(
            entry.name,
            entry.display,
            entry.doc_write_api.kind,
            entry.ccda_import.kind,
            entry.browser.kind,
            _oldest_evidence(entry),
        )
    console.print(table)


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
    if transit.chosen is None:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
