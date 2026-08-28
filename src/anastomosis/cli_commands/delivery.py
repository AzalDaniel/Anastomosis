"""``anast archive`` / ``anast bundle`` — full pipeline + an offline deliverable.

See :mod:`anastomosis.cli_commands` for the split/registration rationale.

Archive and bundle are the SAME command with one delivery kind apart: identical
options, identical body, three strings of wording. :func:`_register` builds and
registers the pair from those strings, so the two ``--help`` screens cannot
drift and a new option lands once instead of twice.

Deliberately NO ``from __future__ import annotations`` here (unlike its sibling
command modules): Typer resolves a command's parameter annotations by
``eval``-ing them against the module globals, and the per-kind ``help=`` texts
below are closure variables of :func:`_register` — invisible to a PEP-563
string annotation. Adding the future import makes this module raise
``NameError`` at import time; keep the annotations eager.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from anastomosis.cli import app
from anastomosis.cli_commands._options import (
    ExportDir,
    Force,
    Pack,
    PackDirs,
    QaFlag,
    Source,
    TrustPack,
)
from anastomosis.cli_commands._paths import out_dir

if TYPE_CHECKING:
    # Type-only: the runtime import stays inside the command body so `anast
    # --help` never pays for the command core. Safe as a *quoted* annotation on
    # a plain function — only the Typer-decorated command's annotations are
    # evaluated at runtime.
    from anastomosis.core.commands import DeliveryKind


def _register(kind: "DeliveryKind", *, summary: str, out_help: str, charts_help: str) -> None:
    """Register one delivery command under ``kind`` (``archive`` / ``bundle``).

    ``summary`` becomes the command's help text (its docstring), and the two
    help strings are the only option wording that differs between the pair.
    """

    def command(
        export_dir: ExportDir,
        # Not the shared --out: the pair says something different about where
        # its deliverable lands, and that wording is the only thing that
        # differs between archive and bundle.
        out: Annotated[Path, typer.Option("--out", "-o", help=out_help, parser=out_dir)],
        source: Source = None,
        pack: Pack = "generic_soap",
        pack_dir: PackDirs = None,
        trust_pack: TrustPack = False,
        force: Force = False,
        section: Annotated[
            list[str] | None,
            typer.Option("--section", help="Override a section flag."),
        ] = None,
        qa: QaFlag = True,
        charts_dir: Annotated[
            Path | None,
            typer.Option("--charts-dir", help=charts_help, parser=out_dir),
        ] = None,
    ) -> None:
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
                deliveries=(DeliveryCommand(kind, out),),
            )
        )

    command.__name__ = f"{kind}_cmd"
    command.__doc__ = summary
    app.command(kind)(command)


_register(
    "archive",
    summary="Rebuild the charts and write a searchable offline copy.",
    out_help="Archive output directory (0700).",
    charts_help=(
        "Where chart PDFs land before being copied into the archive (default: <out>/_charts)."
    ),
)
_register(
    "bundle",
    summary="Rebuild the charts and write one folder per patient.",
    out_help="Bundles output directory (0700).",
    charts_help=(
        "Where chart PDFs land before being copied into the bundles (default: <out>/_charts)."
    ),
)
