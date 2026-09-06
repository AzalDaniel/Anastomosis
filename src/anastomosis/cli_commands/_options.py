"""Command options shared verbatim by more than one command module.

A shared `Annotated[...]` alias is for options that are the same option; a
command needing different wording for a flag declares its own alias rather
than reusing this one."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from anastomosis.cli_commands._paths import out_dir

__all__ = [
    "ExportDir",
    "Force",
    "OutDir",
    "Pack",
    "PackDirs",
    "QaFlag",
    "Source",
    "TrustPack",
]

ExportDir = Annotated[Path, typer.Argument(exists=True, file_okay=False, readable=True)]

OutDir = Annotated[
    Path,
    typer.Option("--out", "-o", help="Output directory (created 0700).", parser=out_dir),
]

Source = Annotated[
    str | None,
    typer.Option("--source", "-s", help="Source adapter name (default: auto-detect)."),
]

Pack = Annotated[str, typer.Option("--pack", "-p", help="Which chart layout to use.")]

PackDirs = Annotated[
    list[Path] | None,
    typer.Option(
        "--pack-dir",
        help="Another folder to look for chart layouts in. Layouts contain code, "
        "so naming one here allows it to run.",
    ),
]

TrustPack = Annotated[
    bool,
    typer.Option(
        "--trust-pack",
        help="Trust the --pack-dir packs at their current code hash (records the hash; "
        "required the first time, and again after their code changes).",
    ),
]

Force = Annotated[bool, typer.Option("--force", help="Re-render documents that exist.")]

QaFlag = Annotated[
    bool, typer.Option("--qa/--no-qa", help="Verify every rendered document (default on).")
]
