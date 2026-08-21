"""Tests for the output-directory advisory lock (core/locking.py).

The lock is a kernel advisory lock (flock / msvcrt) held on an open descriptor:
the marker file ``.anast.lock`` persists on disk, but the *lock* lives only
while a holder's descriptor is open, so it releases on block-exit or on crash.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from anastomosis.core.locking import OutputLockedError, output_lock


def test_lock_blocks_a_live_second_holder(tmp_path: Path) -> None:
    out = tmp_path / "out"
    with output_lock(out):
        with pytest.raises(OutputLockedError):  # a live holder blocks a second acquire
            with output_lock(out):
                pass


def test_lock_released_after_block(tmp_path: Path) -> None:
    out = tmp_path / "out"
    with output_lock(out):
        pass
    # The marker file persists, but the lock is free — a fresh acquire succeeds.
    with output_lock(out):
        pass


def test_lock_acquires_a_leftover_unheld_marker(tmp_path: Path) -> None:
    """A ``.anast.lock`` left on disk by a finished/crashed run (no descriptor
    holding it) is freely acquirable — the kernel released the lock on exit."""
    out = tmp_path / "out"
    out.mkdir()
    (out / ".anast.lock").write_text("99999")  # stale marker, nobody holding it
    with output_lock(out):
        pass


def test_lock_creates_missing_directory(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "out"
    with output_lock(nested):
        assert nested.is_dir()


def test_lock_concurrent_is_exclusive(tmp_path: Path) -> None:
    """Many threads contending on the same directory must never both enter —
    the kernel advisory lock is per open descriptor, so each thread's own
    descriptor contends correctly."""
    out = tmp_path / "out"
    out.mkdir()

    counter_lock = threading.Lock()
    concurrent = 0
    max_concurrent = 0
    entered = 0

    def worker() -> None:
        nonlocal concurrent, max_concurrent, entered
        try:
            with output_lock(out):
                with counter_lock:
                    concurrent += 1
                    entered += 1
                    max_concurrent = max(max_concurrent, concurrent)
                time.sleep(0.02)
                with counter_lock:
                    concurrent -= 1
        except OutputLockedError:
            pass  # a loser correctly refused rather than double-entering

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert max_concurrent == 1
    assert entered >= 1  # at least one acquired (losers raised OutputLockedError)
