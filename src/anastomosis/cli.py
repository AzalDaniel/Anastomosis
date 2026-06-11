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

import typer
from rich.console import Console

import anastomosis

app = typer.Typer(
    name="anast",
    help=__doc__,
    no_args_is_help=True,
    rich_markup_mode="rich",
)
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
    """Show toolkit status: installed extras, discovered packs, environment."""
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


if __name__ == "__main__":
    app()
