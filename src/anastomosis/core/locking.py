"""A cross-platform advisory lock over an output directory.

Two ``anast`` runs writing into the same output directory at once would
interleave renders, QA, and delivery — one run's QA could read another's
half-written charts. This guards the directory with a kernel advisory lock so
the second run fails fast with a clean message instead of corrupting the first.

The lock is an OS advisory lock held on an open file descriptor to
``.anast.lock`` — ``fcntl.flock`` on POSIX, ``msvcrt.locking`` on Windows. The
kernel guarantees exclusion (across processes AND threads, since each acquirer
opens its own descriptor) and, crucially, releases the lock automatically when
the holder exits or crashes. That sidesteps the stale-lockfile problem
entirely: there is no PID bookkeeping, no liveness probing, and no
reclaim/unlink race — the marker file is left in place on disk (harmless) and
the *lock* lives only as long as a holder's descriptor.
"""

from __future__ import annotations

import os
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
    if os.name == "posix":
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False  # EWOULDBLOCK / EACCES — held by another descriptor
        return True
    try:
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    if os.name == "posix":
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    import msvcrt

    try:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
    except OSError:
        pass  # best-effort: closing the fd below releases the lock regardless


@contextmanager
def output_lock(directory: str | Path) -> Iterator[Path]:
    """Hold an exclusive lock on ``directory`` for the duration of the block.

    Raises :class:`OutputLockedError` if another live run already holds it. The
    directory is created if needed (parents included). The lock releases when
    the block exits — or automatically if the process dies.
    """
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
