"""CLI path options: a raw argument becomes a ``Path`` through
``clean_typed_path`` before any command body runs, which strips a pasted
Windows path's quotes and refuses a blank value outright rather than
resolving it to the cwd (#123, #131)."""

from __future__ import annotations

from pathlib import Path

import typer

from anastomosis.core.output import clean_typed_path

__all__ = ["in_file", "out_dir"]


def out_dir(raw: str) -> Path:
    cleaned = clean_typed_path(raw)
    if not cleaned:
        # BadParameter, not ValueError: Typer's parser wrapper reports a bare
        # ValueError as just the value, which is empty here. This option is
        # required on one command and optional on another, so it cannot
        # suggest "leave it off" without sometimes being wrong.
        raise typer.BadParameter(
            "no output folder was given. Name the output folder this command should write to."
        )
    return Path(cleaned)


def in_file(raw: str) -> Path:
    """A blank value raises rather than resolving to the cwd."""
    cleaned = clean_typed_path(raw)
    if not cleaned:
        raise typer.BadParameter(
            "no file was given. Name the file to read, or leave the option off entirely."
        )
    return Path(cleaned)
