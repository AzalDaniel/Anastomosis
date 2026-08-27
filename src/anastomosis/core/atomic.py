"""Atomic write-via-sibling-temp-file-then-replace, in one shared place.

Every writer that must never leave a partial or half-written file behind
follows the same shape: write to a same-directory ``.NAME.<pid>.tmp``,
``os.replace`` it over the target so a reader (or a concurrent run) never
sees a torn file, and unlink the temp file if anything raises before the
replace. That shape was hand-rolled independently at each write site; this
module is the one place it lives now, so every site gets the unlink-on-
failure safety net without having to remember to write it.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

__all__ = ["atomic_copy", "atomic_replace", "atomic_write_bytes", "atomic_write_text"]


def _tmp_path_for(target: Path) -> Path:
    return target.with_name(f".{target.name}.{os.getpid()}.tmp")


@contextmanager
def atomic_replace(target: Path) -> Iterator[Path]:
    """Yield a sibling temp path; on clean exit ``os.replace`` it over ``target``.

    The caller fills the yielded path however it needs to (an external
    renderer call, a raw write). On ANY exception — including one raised by
    the caller's own recovery logic — the temp file is unlinked and the
    exception propagates: a crash mid-write never leaves a partial file at
    ``target`` or a stray temp file behind.
    """
    tmp = _tmp_path_for(target)
    try:
        yield tmp
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def atomic_write_text(
    target: Path, text: str, *, encoding: str = "utf-8", mode: int | None = None
) -> None:
    """Write ``text`` to ``target`` atomically.

    ``mode`` (POSIX only) creates the temp file with those permission bits
    up front, for a target that must never be briefly world-readable; on a
    non-POSIX platform, or when ``mode`` is ``None``, the temp file is
    written with the process' default permissions.
    """
    with atomic_replace(target) as tmp:
        if mode is not None and os.name == "posix":
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
            with os.fdopen(fd, "w", encoding=encoding) as handle:
                handle.write(text)
        else:
            tmp.write_text(text, encoding=encoding)


def atomic_write_bytes(target: Path, data: bytes, *, mode: int | None = None) -> None:
    """Write ``data`` to ``target`` atomically. See :func:`atomic_write_text`."""
    with atomic_replace(target) as tmp:
        if mode is not None and os.name == "posix":
            tmp.touch(mode=mode)
        tmp.write_bytes(data)


def atomic_copy(source: Path, target: Path) -> None:
    """Copy ``source`` onto ``target`` atomically.

    ``shutil.copyfile`` truncates the destination and streams into it, so a
    crash partway leaves a truncated file where a complete one was. For a
    chart page that is a patient's record, half-written. Copying to a sibling
    temp and replacing means the target is either the old file or the new one,
    never part of both.
    """
    import shutil

    with atomic_replace(target) as tmp:
        shutil.copyfile(source, tmp)
