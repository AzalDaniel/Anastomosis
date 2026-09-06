"""The one write-via-sibling-temp-then-replace implementation (14).

A killed (not raised) run leaves its temp; the next write sweeps dead
writers' temps beside its own target (15) via :func:`_reap_dead_temps`.
"""

from __future__ import annotations

import glob
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

__all__ = ["atomic_copy", "atomic_replace", "atomic_write_bytes", "atomic_write_text"]

logger = logging.getLogger(__name__)


def _tmp_path_for(target: Path) -> Path:
    return target.with_name(f".{target.name}.{os.getpid()}.tmp")


def _writer_is_gone(tmp_name: str) -> bool:
    """True only when the pid in a ``.NAME.<pid>.tmp`` name is positively
    dead (15); every unreadable case — unparsable pid, another uid's process,
    no liveness probe at all — answers no and keeps the file."""
    if os.name != "posix":
        # os.kill(pid, 0) on Windows calls TerminateProcess, it does not ask;
        # with no probe available, keep the file.
        return False
    try:
        pid = int(tmp_name.removesuffix(".tmp").rsplit(".", 1)[1])
    except (ValueError, IndexError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (OSError, OverflowError):
        # EPERM and friends: alive, not ours to signal. OverflowError isn't an
        # OSError, but a pid too large for a C int raises it here, not int().
        return False
    return False


def _reap_dead_temps(target: Path) -> None:
    """Unlink ``target``'s dead-writer ``.NAME.<pid>.tmp`` siblings (15);
    the archive's orphan sweep globs ``*.pdf`` only and never sees them."""
    reaped = 0
    # `glob.escape`: the target's name is data, not pattern — an unescaped
    # `[`, `]`, `*` or `?` would match a different (or no) chart's temps.
    for stale in target.parent.glob(f".{glob.escape(target.name)}.*.tmp"):
        if not _writer_is_gone(stale.name):
            continue
        try:
            stale.unlink()
        except OSError:
            continue  # not ours to remove; leave it
        reaped += 1
    if reaped:
        logger.warning("removed %d stale temp file(s) left by a killed run", reaped)


@contextmanager
def atomic_replace(target: Path) -> Iterator[Path]:
    """Contract: yields a sibling temp path; ``os.replace``s it onto
    ``target`` on clean exit. ANY exception, including from the caller's own
    recovery logic, unlinks the temp and re-raises, so ``target`` is always
    the old file or the new one, never partial. A killed (not raised) run
    skips this; :func:`_reap_dead_temps` sweeps it next time."""
    _reap_dead_temps(target)
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
    """Write ``text`` to ``target`` atomically. ``mode`` (POSIX only) sets
    the temp file's permission bits up front, so it is never briefly
    world-readable; ignored on non-POSIX or when ``None``."""
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
    """Copy ``source`` onto ``target`` atomically: ``shutil.copyfile`` alone
    truncates and streams in place, so a crash partway would leave a
    half-written chart where a complete one was."""
    import shutil

    with atomic_replace(target) as tmp:
        shutil.copyfile(source, tmp)
