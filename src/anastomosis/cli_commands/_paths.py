"""How every CLI option that names a path turns its argument into a ``Path``.

Two things go wrong between the shell and a ``Path``, and both happen before a
command body ever runs:

* ``--out ""`` becomes ``Path("")`` becomes ``Path(".")``. A blank value does not
  fail — it silently means "here", so charts named after patients land in
  whatever directory the operator launched from, that directory is hardened to
  0700 underneath them, and the run reports success. This is #123, whose fix
  reached the GUI door only.
* A pasted Windows path keeps the quotes Explorer's "Copy as path" put around
  it, which is not a path anybody meant to type. This is #131, whose fix also
  reached the GUI door only.

Both were fixed at the frontend boundary because that is where the raw string
still exists, and this is the CLI's equivalent of that boundary: Typer hands a
``parser=`` the argument exactly as typed. An option declared with one of these
gets both behaviours with no change to the command that receives it.
"""

from __future__ import annotations

from pathlib import Path

import typer

from anastomosis.core.output import clean_typed_path

__all__ = ["in_file", "out_dir", "out_file"]


def _typed_path(raw: str, what: str) -> Path:
    cleaned = clean_typed_path(raw)
    if not cleaned:
        # BadParameter, not ValueError: Typer's parser wrapper catches
        # ValueError and reports the *value* rather than the reason, which for
        # a blank value is an error message with nothing in it. A UsageError
        # travels intact.
        raise typer.BadParameter(
            f"no {what} was given. Name the {what} this command should write "
            "to, or leave the option off entirely."
        )
    return Path(cleaned)


def out_dir(raw: str) -> Path:
    """A folder to write into."""
    return _typed_path(raw, "output folder")


def out_file(raw: str) -> Path:
    """A single file to write."""
    return _typed_path(raw, "output file")


def in_file(raw: str) -> Path:
    """A file to read. Blank is still not "here"; it is a question unanswered."""
    cleaned = clean_typed_path(raw)
    if not cleaned:
        raise typer.BadParameter(
            "no file was given. Name the file to read, or leave the option off entirely."
        )
    return Path(cleaned)
