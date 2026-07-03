"""``anast archive`` / ``anast bundle`` — full pipeline + an offline deliverable.

The two command bodies split out of :mod:`anastomosis.cli`; they register
against the top-level ``app`` defined there and drive the shared pipeline
machinery (``_run_command`` / ``_sections_or_exit``) resolved late through the
``cli`` module. See :mod:`anastomosis.cli_commands` for the facade rationale.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from anastomosis.cli import app


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
    trust_pack: Annotated[
        bool,
        typer.Option(
            "--trust-pack",
            help="Trust the --pack-dir packs at their current code hash (records the hash; "
            "required the first time, and again after their code changes).",
        ),
    ] = False,
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
    from anastomosis import cli as _cli
    from anastomosis.core.commands import DeliveryCommand, PipelineCommand

    sections = _cli._sections_or_exit(section, source=source, pack=pack)
    charts = charts_dir or (out / "_charts")
    _cli._run_command(
        PipelineCommand(
            export_dir=export_dir,
            charts_dir=charts,
            source=source,
            pack=pack,
            pack_dirs=tuple(pack_dir or ()),
            force=force,
            trust_new=trust_pack,
            sections=sections,
            qa=qa,
            deliveries=(DeliveryCommand("archive", out),),
        )
    )


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
    trust_pack: Annotated[
        bool,
        typer.Option(
            "--trust-pack",
            help="Trust the --pack-dir packs at their current code hash (records the hash; "
            "required the first time, and again after their code changes).",
        ),
    ] = False,
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
    from anastomosis import cli as _cli
    from anastomosis.core.commands import DeliveryCommand, PipelineCommand

    sections = _cli._sections_or_exit(section, source=source, pack=pack)
    charts = charts_dir or (out / "_charts")
    _cli._run_command(
        PipelineCommand(
            export_dir=export_dir,
            charts_dir=charts,
            source=source,
            pack=pack,
            pack_dirs=tuple(pack_dir or ()),
            force=force,
            trust_new=trust_pack,
            sections=sections,
            qa=qa,
            deliveries=(DeliveryCommand("bundle", out),),
        )
    )
