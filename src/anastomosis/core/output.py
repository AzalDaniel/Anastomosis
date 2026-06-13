"""Output-directory hygiene (security backlog: output hygiene, M1).

Everything the pipeline writes — archives, rendered PDFs, QA reports,
delivery manifests — lands in a directory created here, so two guarantees
hold everywhere:

* **Owner-only permissions** (``0o700``) on POSIX. Reconstructed charts are
  PHI; a world-readable archive directory is a breach waiting to happen.
* **A PHI warning README** in every output root, so a folder found on disk
  months later explains itself before someone syncs it to a cloud drive.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

__all__ = ["OutputPathError", "secure_output_dir", "validate_output_target"]


class OutputPathError(Exception):
    """An output path cannot become a directory (it, or an ancestor, is a file).

    Raised *before* any backend work so the operator gets a clean message
    instead of a ``FileExistsError``/``NotADirectoryError`` traceback from deep
    in the render/delivery code. Callers map it to a clean exit (code 2).
    """


def validate_output_target(path: str | Path) -> None:
    """Raise :class:`OutputPathError` if ``path`` can't be created as a directory.

    ``Path.mkdir(parents=True)`` fails with ``FileExistsError`` when the target
    itself is a file, and ``NotADirectoryError`` when an ancestor is a file.
    Both reduce to: the nearest *existing* path component must be a directory.
    """
    target = Path(path)
    for component in (target, *target.parents):
        if component.exists():
            if not component.is_dir():
                where = "" if component == target else f" (ancestor of {target})"
                raise OutputPathError(f"Output path {component} is a file, not a directory{where}.")
            return  # nearest existing component is a directory — mkdir will succeed


_README_NAME = "_PHI_WARNING_README.txt"

_README_TEXT = """\
THIS FOLDER MAY CONTAIN PROTECTED HEALTH INFORMATION (PHI)
===========================================================

It was created by Anastomosis (https://github.com/AzalDaniel/Anastomosis)
while reconstructing or delivering clinical records.

Handle accordingly:
* Do NOT sync this folder to consumer cloud storage or share it by email.
* Do NOT commit it to version control.
* Store on encrypted media; delete securely when your retention need ends.
* Access is restricted to the file owner by default — keep it that way.

If you found this folder and don't know why it exists, contact the practice
or person who ran the export before opening anything else in it.
"""


def secure_output_dir(path: str | Path) -> Path:
    """Create (or harden) an output directory and return it.

    Idempotent: safe to call on every run. Permissions are tightened on
    POSIX; on platforms without POSIX modes (Windows) the chmod is a no-op
    and the README still lands.
    """
    root = Path(path)
    validate_output_target(root)  # clean OutputPathError instead of a raw OSError
    root.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        root.chmod(stat.S_IRWXU)  # 0o700 — owner only
    readme = root / _README_NAME
    if not readme.exists():
        readme.write_text(_README_TEXT, encoding="utf-8")
    return root
