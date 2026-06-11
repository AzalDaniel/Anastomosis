"""Anastomosis command-line interface.

Installed as both ``anast`` (everyday) and ``anastomosis`` (formal).
Sub-commands appear as their pipeline stages are implemented:

    anast ingest        EHI export / C-CDA  ->  canonical records
    anast reconstruct   canonical records   ->  rendered documents
    anast qa            rendered documents  ->  QA report
    anast archive       everything          ->  searchable offline archive
    anast pipeline run  one command, whole pipeline
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

import anastomosis
import anastomosis.sources.pf_tebra
from anastomosis.sources import available_sources, detect_source, get_source

app = typer.Typer(
    name="anast",
    help=__doc__,
    no_args_is_help=True,
    rich_markup_mode="rich",
)
pipeline_app = typer.Typer(help="Run pipeline stages end to end.")
app.add_typer(pipeline_app, name="pipeline")
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
) -> None:
    """Ingest an export and reconstruct every encounter into chart PDFs."""
    from anastomosis.reconstruct import discover_packs
    from anastomosis.reconstruct.chromium import ChromiumRenderer
    from anastomosis.reconstruct.engine import ReconstructionEngine

    if source:
        adapter = get_source(source)
    else:
        detected = detect_source(export_dir)
        if detected is None:
            known = ", ".join(a.name for a in available_sources())
            console.print(
                f"[red]Could not identify the export format.[/red] Try --source ({known})"
            )
            raise typer.Exit(code=2)
        adapter = detected
        console.print(f"Detected source: [cyan]{adapter.name}[/cyan]")

    pack_dirs = list(pack_dir or [])
    statuses = discover_packs(pack_dirs, allow_external=bool(pack_dirs))
    status = statuses.get(pack)
    if status is None or status.pack is None:
        diagnosis = status.diagnosis if status else f"unknown pack (have: {', '.join(statuses)})"
        console.print(f"[red]Pack {pack!r} unavailable:[/red] {diagnosis}")
        raise typer.Exit(code=2)

    overrides: dict[str, bool] = {}
    for item in section or []:
        key, _, value = item.partition("=")
        overrides[key.strip()] = value.strip().lower() in ("on", "true", "1", "yes")

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
    result = engine.run(adapter.load(export_dir), out, force=force)
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


if __name__ == "__main__":
    app()
