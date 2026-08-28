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
from anastomosis.cli_commands._options import (
    ExportDir,
    Force,
    OutDir,
    Pack,
    PackDirs,
    QaFlag,
    Source,
    TrustPack,
)
from anastomosis.cli_commands._paths import out_dir


@pipeline_app.command("run")
def pipeline_run(
    export_dir: ExportDir,
    out: OutDir,
    source: Source = None,
    pack: Pack = "generic_soap",
    pack_dir: PackDirs = None,
    trust_pack: TrustPack = False,
    force: Force = False,
    section: Annotated[
        list[str] | None,
        typer.Option(
            "--section",
            help="Override a section flag, e.g. --section insurance=on --section addenda=off.",
        ),
    ] = None,
    qa: QaFlag = True,
    archive: Annotated[
        Path | None,
        typer.Option(
            "--archive",
            help="Also emit an offline browsable archive in this directory.",
            parser=out_dir,
        ),
    ] = None,
    bundle: Annotated[
        Path | None,
        typer.Option(
            "--bundle",
            help="Also emit one per-patient bundle subdirectory in this directory.",
            parser=out_dir,
        ),
    ] = None,
    ccda: Annotated[
        Path | None,
        typer.Option(
            "--ccda",
            help="Also emit one C-CDA / CCD XML per patient in this directory.",
            parser=out_dir,
        ),
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
