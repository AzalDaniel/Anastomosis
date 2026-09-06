"""CLI path options: a raw argument becomes a ``Path`` through
``clean_typed_path`` before any command body runs, which strips a pasted
Windows path's quotes and refuses a blank value outright rather than
resolving it to the cwd (#123, #131)."""

from __future__ import annotations

from pathlib import Path

import typer

from anastomosis.core.output import clean_typed_path

__all__ = ["in_file", "out_dir", "out_file"]


def _typed_path(raw: str, what: str) -> Path:
    cleaned = clean_typed_path(raw)
    if not cleaned:
        # BadParameter, not ValueError: Typer's parser wrapper reports a bare
        # ValueError as just the value, which is empty here.
        # This parser backs both a required and an optional path option, so it
        # cannot suggest "leave it off" without sometimes being wrong.
        raise typer.BadParameter(
            f"no {what} was given. Name the {what} this command should write to."
        )
    return Path(cleaned)


def out_dir(raw: str) -> Path:
    return _typed_path(raw, "output folder")


def out_file(raw: str) -> Path:
    return _typed_path(raw, "output file")


def in_file(raw: str) -> Path:
    """A blank value raises rather than resolving to the cwd."""
    cleaned = clean_typed_path(raw)
    if not cleaned:
        raise typer.BadParameter(
            "no file was given. Name the file to read, or leave the option off entirely."
        )
    return Path(cleaned)
