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
        export_dir: Annotated[Path, typer.Argument(exists=True, file_okay=False, readable=True)],
        out: Annotated[Path, typer.Option("--out", "-o", help=out_help)],
        source: Annotated[
            str | None,
            typer.Option("--source", "-s", help="Source adapter name (default: auto-detect)."),
        ] = None,
        pack: Annotated[
            str, typer.Option("--pack", "-p", help="Template pack name.")
        ] = "generic_soap",
        pack_dir: Annotated[
            list[Path] | None,
            typer.Option(
                "--pack-dir", help="Extra pack directories (implies trusting their code)."
            ),
        ] = None,
        trust_pack: Annotated[
            bool,
            typer.Option(
                "--trust-pack",
                help="Trust the --pack-dir packs at their current code hash (records the hash; "
                "required the first time, and again after their code changes).",
            ),
        ] = False,
        force: Annotated[
            bool, typer.Option("--force", help="Re-render documents that exist.")
        ] = False,
        section: Annotated[
            list[str] | None,
            typer.Option("--section", help="Override a section flag."),
        ] = None,
        qa: Annotated[
            bool, typer.Option("--qa/--no-qa", help="Verify every rendered document (default on).")
        ] = True,
        charts_dir: Annotated[
            Path | None,
            typer.Option("--charts-dir", help=charts_help),
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
    summary="Run the full pipeline and write a searchable offline archive.",
    out_help="Archive output directory (0700).",
    charts_help=(
        "Where chart PDFs land before being copied into the archive (default: <out>/_charts)."
    ),
)
_register(
    "bundle",
    summary="Run the full pipeline and write one per-patient bundle directory each.",
    out_help="Bundles output directory (0700).",
    charts_help=(
        "Where chart PDFs land before being copied into the bundles (default: <out>/_charts)."
    ),
)
