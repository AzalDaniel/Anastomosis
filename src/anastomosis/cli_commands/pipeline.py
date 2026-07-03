# AI-assisted: written with Claude agents under the author's direction and review; see DESIGN.md.
"""``anast pipeline run`` — ingest an export and reconstruct chart PDFs.

The command body split out of :mod:`anastomosis.cli`; it registers against the
``pipeline_app`` defined there and drives the shared pipeline machinery
(``_run_command`` / ``_sections_or_exit``) resolved late through the ``cli``
module. See :mod:`anastomosis.cli_commands` for the facade rationale.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from anastomosis.cli import pipeline_app


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
    upload_manifest: Annotated[
        bool,
        typer.Option(
            "--upload-manifest",
            help="Also write upload_manifest.json (items + demographics) for `anast upload`.",
        ),
    ] = False,
) -> None:
    """Ingest an export and reconstruct every encounter into chart PDFs."""
    from anastomosis import cli as _cli
    from anastomosis.core.commands import DeliveryCommand, PipelineCommand

    sections = _cli._sections_or_exit(section, source=source, pack=pack)
    deliveries: list[DeliveryCommand] = []
    if archive is not None:
        deliveries.append(DeliveryCommand("archive", archive))
    if bundle is not None:
        deliveries.append(DeliveryCommand("bundle", bundle))
    if ccda is not None:
        deliveries.append(DeliveryCommand("ccda", ccda))
    _cli._run_command(
        PipelineCommand(
            export_dir=export_dir,
            charts_dir=out,
            source=source,
            pack=pack,
            pack_dirs=tuple(pack_dir or ()),
            force=force,
            trust_new=trust_pack,
            sections=sections,
            qa=qa,
            deliveries=tuple(deliveries),
            write_manifest=upload_manifest,
        )
    )
