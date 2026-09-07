"""A second ``anast`` against the same output directory fails fast on a
kernel advisory lock, ``.anast.lock`` (19): ``fcntl.flock`` on POSIX,
``msvcrt.locking`` on Windows. Release on crash is the kernel's, not
ours (19) — no PID bookkeeping, no liveness probing, no reclaim race; the
marker file itself is harmless, only the descriptor holds the lock.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

__all__ = ["OutputLockedError", "output_lock"]

_LOCK_NAME = ".anast.lock"


class OutputLockedError(Exception):
    """The output directory is already locked by another live ``anast`` run."""


def _try_lock(fd: int) -> bool:
    """Try to take an exclusive, non-blocking advisory lock on ``fd``.

    Returns True on success, False if another descriptor already holds it.
    """
    # Branch on ``sys.platform`` (not ``os.name``): mypy narrows the former and
    # type-checks only the matching arm per ``--platform``, so the POSIX-only
    # ``fcntl`` and Windows-only ``msvcrt`` attributes each resolve on their own
    # platform without spurious attr-defined errors on the other.
    if sys.platform != "win32":
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False  # EWOULDBLOCK / EACCES — held by another descriptor
        return True
    else:  # explicit else keeps the win32-only arm a sys.platform guard arm
        import msvcrt

        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True


def _unlock(fd: int) -> None:
    if sys.platform != "win32":  # see _try_lock for the sys.platform rationale
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)
    else:
        import msvcrt

        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass  # best-effort: closing the fd below releases the lock regardless


@contextmanager
def output_lock(directory: str | Path) -> Iterator[Path]:
    """Hold an exclusive lock on ``directory`` (created if needed) for the
    block's duration (19). Raises :class:`OutputLockedError` if another
    live run already holds it; releases on exit or on crash."""
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / _LOCK_NAME
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    if not _try_lock(fd):
        os.close(fd)
        raise OutputLockedError(
            f"Output directory {root} is locked by another anast run. "
            f"Wait for it to finish, or choose a different output directory."
        )
    try:
        # Record the holder PID for human diagnosis only (never read back for
        # correctness — the kernel lock is the mechanism).
        try:
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode("ascii"))
        except OSError:
            pass
        yield lock_path
    finally:
        _unlock(fd)
        os.close(fd)
