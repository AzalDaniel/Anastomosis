"""Atomic write-via-sibling-temp-file-then-replace, in one shared place.

Every writer that must never leave a partial or half-written file behind
follows the same shape: write to a same-directory ``.NAME.<pid>.tmp``,
``os.replace`` it over the target so a reader (or a concurrent run) never
sees a torn file, and unlink the temp file if anything raises before the
replace. That shape was hand-rolled independently at each write site; this
module is the one place it lives now, so every site gets the unlink-on-
failure safety net without having to remember to write it.

That unlink runs on the way out of a raised exception, so a run that is killed
outright — SIGKILL, the OOM reaper, the power going — never reaches it and
leaves its temp on disk. Every write therefore also sweeps the temps left
beside its own target by writers that are no longer alive; see
:func:`_reap_dead_temps`.
"""

from __future__ import annotations

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
    """True only when the pid embedded in a ``.NAME.<pid>.tmp`` name is dead.

    "No" is always the safe answer here, because it keeps the file, so every
    case whose truth we cannot read answers no: a name whose pid will not
    parse, a pid the kernel says is alive, a pid alive under another uid
    (``os.kill`` raises ``PermissionError``), and a platform that offers no
    liveness probe at all. Only a pid the kernel positively reports as gone
    earns the deletion of a file that may hold a patient's chart.

    Best-effort by construction: a recycled pid reads as alive and its temp
    stays. That leaves a file for an operator to find, which is the side to
    err on when the alternative is deleting one out from under a live run.
    """
    if os.name != "posix":
        # Windows has no probe to offer: `os.kill(pid, 0)` there does not ask
        # whether a process is alive, it calls TerminateProcess and makes sure
        # it is not. With no answer available, we keep the file.
        return False
    try:
        pid = int(tmp_name.removesuffix(".tmp").rsplit(".", 1)[1])
    except (ValueError, IndexError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False  # EPERM and friends: alive, just not ours to signal
    return False


def _reap_dead_temps(target: Path) -> None:
    """Unlink the ``.NAME.<pid>.tmp`` siblings of ``target`` whose writer died.

    Nothing else on the system will. The temp name carries the pid of the run
    that made it, so the next run picks a different name, replaces the target
    cleanly and walks away leaving the corpse; and the archive's orphan sweep
    globs ``*.pdf``, so a dot-prefixed half-written chart is invisible to the
    one pass that walks the chart directory. The operator would be handed an
    output tree that looks complete with a hidden file of patient-derived
    bytes sitting in it.

    Our own pid is by definition alive, so the temp this very call is about to
    yield can never be a candidate: the liveness check, not any comparison of
    names, is what keeps our temp and a concurrent run's temp safe.
    """
    reaped = 0
    for stale in target.parent.glob(f".{target.name}.*.tmp"):
        if not _writer_is_gone(stale.name):
            continue
        try:
            stale.unlink()
        except OSError:
            continue  # already gone, or not ours to remove; either way, leave it
        reaped += 1
    if reaped:
        # Counted, never named: a temp under the output tree is named for the
        # chart it was going to be, which is named for a patient.
        logger.warning("removed %d stale temp file(s) left by a killed run", reaped)


@contextmanager
def atomic_replace(target: Path) -> Iterator[Path]:
    """Yield a sibling temp path; on clean exit ``os.replace`` it over ``target``.

    The caller fills the yielded path however it needs to (an external
    renderer call, a raw write). On ANY exception — including one raised by
    the caller's own recovery logic — the temp file is unlinked and the
    exception propagates, so a write that fails leaves neither a partial file
    at ``target`` nor a stray temp beside it.

    A run that is killed rather than unwound runs no handler and does leave
    its temp; that one is cleared by the next write to the same target, which
    is what :func:`_reap_dead_temps` is here for.
    """
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
