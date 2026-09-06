"""Tests for the shared atomic-write helper (core/atomic.py, rule 14).

A write that fails must never leave a stray ``.NAME.<pid>.tmp`` or a
partial target file (tmp+os.replace+unlink-on-failure). A run that is
*killed* unwinds nothing and does leave its temp behind — the other half
is that the next write to the same target reaps it, and reaps only the
temps whose writer is provably gone (rule 15).
"""

from __future__ import annotations

import logging
import os
import stat
import subprocess
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path
from unittest import mock

import pytest

import anastomosis
from anastomosis.core.atomic import atomic_replace, atomic_write_bytes, atomic_write_text

#: Where the child processes below must import the helper from. It comes off
#: the already-imported package rather than from whatever a fresh interpreter
#: would find, because an editable install and a checked-out worktree can
#: disagree and a child testing the other copy would prove nothing.
_PACKAGE_PATH = str(Path(anastomosis.__file__).resolve().parents[1])

#: A writer that parks inside the atomic_replace window with its temp on disk,
#: announces the temp's name, and waits to be killed.
_KILLED_WRITER = textwrap.dedent(
    """
    import sys
    import time
    from pathlib import Path

    from anastomosis.core.atomic import atomic_replace

    with atomic_replace(Path(sys.argv[1])) as tmp:
        tmp.write_bytes(b"%PDF-1.7 half a chart")
        print(tmp.name, flush=True)
        time.sleep(300)
    """
)


def _write_text(target: Path) -> None:
    atomic_write_text(target, '{"trusted": true}', mode=0o600)


def _write_bytes(target: Path) -> None:
    atomic_write_bytes(target, b'{"trusted": true}', mode=0o600)


def _tmp_files(directory: Path) -> list[Path]:
    return [p for p in directory.iterdir() if p.name.startswith(".") and p.name.endswith(".tmp")]


def _child_env() -> dict[str, str]:
    inherited = os.environ.get("PYTHONPATH", "")
    path = _PACKAGE_PATH + (os.pathsep + inherited if inherited else "")
    return {**os.environ, "PYTHONPATH": path}


def test_atomic_write_text_creates_target(tmp_path: Path) -> None:
    target = tmp_path / "report.json"
    atomic_write_text(target, '{"a": 1}')
    assert target.read_text(encoding="utf-8") == '{"a": 1}'
    assert _tmp_files(tmp_path) == []


def test_atomic_write_text_replaces_existing_target(tmp_path: Path) -> None:
    target = tmp_path / "report.json"
    target.write_text("old", encoding="utf-8")
    atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"
    assert _tmp_files(tmp_path) == []


def test_atomic_write_text_leaves_no_orphan_tmp_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import anastomosis.core.atomic as atomic

    target = tmp_path / "report.json"

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(atomic.os, "replace", _boom)
    with pytest.raises(OSError):
        atomic_write_text(target, "payload")
    assert not target.exists()
    assert _tmp_files(tmp_path) == []


def test_atomic_replace_propagates_and_cleans_up_on_caller_exception(tmp_path: Path) -> None:
    target = tmp_path / "out.pdf"

    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        with atomic_replace(target) as tmp:
            tmp.write_bytes(b"partial")
            raise _Boom("renderer crashed")
    assert not target.exists()
    assert _tmp_files(tmp_path) == []


def test_atomic_replace_survives_a_second_failure_after_a_caller_retry(tmp_path: Path) -> None:
    """Mirrors the engine's crash-relaunch shape: the caller catches its OWN
    exception and retries against the SAME yielded tmp path; a second failure
    still leaves no orphan tmp file."""
    target = tmp_path / "out.pdf"

    class _RendererCrashed(Exception):
        pass

    with pytest.raises(_RendererCrashed):
        with atomic_replace(target) as tmp:
            try:
                raise _RendererCrashed("first attempt")
            except _RendererCrashed:
                pass  # simulated relaunch-and-retry
            tmp.write_bytes(b"still bad")
            raise _RendererCrashed("second attempt")
    assert not target.exists()
    assert _tmp_files(tmp_path) == []


@pytest.mark.skipif(os.name != "posix", reason="owner-only mode is POSIX-only")
def test_atomic_write_text_mode_sets_owner_only_permissions(tmp_path: Path) -> None:
    target = tmp_path / "trust.json"
    atomic_write_text(target, "{}", mode=0o600)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="owner-only mode is POSIX-only")
@pytest.mark.parametrize("write", [_write_text, _write_bytes])
def test_a_stale_same_pid_temp_does_not_carry_its_mode_onto_the_target(
    tmp_path: Path, write: Callable[[Path], None]
) -> None:
    """A killed run leaves ``.NAME.<pid>.tmp`` behind, and a later run whose
    pid the kernel recycled finds it alive and keeps it. Creation-time mode
    bits are ignored for a file that already exists, so the trust store would
    have landed at the stale temp's 0o644 without a chmod."""
    target = tmp_path / "source_trust.json"
    stale = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    stale.write_text("stale", encoding="utf-8")
    stale.chmod(0o644)
    write(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert _tmp_files(tmp_path) == []


def test_atomic_write_bytes_puts_exactly_the_bytes_it_was_given_on_disk(tmp_path: Path) -> None:
    """The learned-mapping writer hashes the string it hands over, so any
    newline translation, re-encoding or strip on the way to disk would make a
    trust record describe a file that was never written."""
    data = b"alpha\nbeta\r\ngamma\n"
    target = tmp_path / "mapping.json"
    atomic_write_bytes(target, data)
    assert target.read_bytes() == data


@pytest.mark.skipif(os.name != "posix", reason="owner-only mode is POSIX-only")
def test_atomic_write_bytes_mode_sets_owner_only_permissions(tmp_path: Path) -> None:
    target = tmp_path / "source_trust.json"
    atomic_write_bytes(target, b"{}", mode=0o600)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL and the pid probe are POSIX-only")
def test_a_killed_writers_temp_is_reaped_by_the_next_write(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """SIGKILL inside the window is the failure the except-handler cannot
    reach: nothing unwinds, so the temp stays, and a rerun (which picks a
    different name from its own pid) must be what clears it — otherwise
    the directory looks complete with a half-written chart hidden in it."""
    target = tmp_path / "Chart_0001.pdf"
    child = subprocess.Popen(
        [sys.executable, "-c", _KILLED_WRITER, str(target)],
        stdout=subprocess.PIPE,
        text=True,
        env=_child_env(),
    )
    try:
        assert child.stdout is not None
        stale_name = child.stdout.readline().strip()
        assert stale_name, "the child never reached the atomic_replace window"
    finally:
        child.kill()
        # Reap the zombie: an unwaited-for child is still a live pid to
        # os.kill, and the sweep would (correctly) decline to touch its temp.
        child.wait(timeout=30)

    stale = tmp_path / stale_name
    assert _tmp_files(tmp_path) == [stale], "the kill was supposed to leave a temp behind"

    with caplog.at_level(logging.WARNING, logger="anastomosis.core.atomic"):
        atomic_write_text(target, "%PDF-1.7 the rerun's chart")

    assert _tmp_files(tmp_path) == [], "the dead run's temp outlived a later write"
    assert target.read_text(encoding="utf-8") == "%PDF-1.7 the rerun's chart"

    swept = [r.getMessage() for r in caplog.records if r.name == "anastomosis.core.atomic"]
    assert swept, "the sweep deleted patient-derived bytes without saying so"
    # Counted, never named: the temp is named for the chart, the chart for a person.
    assert "1" in swept[0]
    assert stale_name not in swept[0]
    assert str(tmp_path) not in swept[0]


@pytest.mark.skipif(os.name != "posix", reason="the pid probe is POSIX-only")
def test_a_live_writers_temp_is_left_alone(tmp_path: Path) -> None:
    """Never over-clean. A second run writing the same target is mid-render into
    its own temp; deleting that destroys a chart being written right now, which
    is a worse outcome than the leftover the sweep exists to remove."""
    target = tmp_path / "Chart_0001.pdf"
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    try:
        theirs = tmp_path / f".{target.name}.{live.pid}.tmp"
        theirs.write_bytes(b"%PDF-1.7 another run's work in progress")
        atomic_write_text(target, "ours")
        assert theirs.exists(), "the sweep deleted a live run's temp file"
    finally:
        live.kill()
        live.wait(timeout=30)


def test_a_temp_whose_pid_will_not_parse_is_left_alone(tmp_path: Path) -> None:
    """Same rule from the other side: a name the sweep cannot read a pid out of
    is not a file it has established anything about, so it is not its to delete."""
    target = tmp_path / "report.json"
    stranger = tmp_path / f".{target.name}.notapid.tmp"
    stranger.write_text("someone else's file", encoding="utf-8")
    atomic_write_text(target, "{}")
    assert stranger.exists(), "the sweep deleted a temp it could not identify"


def test_a_pid_too_large_to_signal_keeps_the_file_and_the_write(tmp_path: Path) -> None:
    """``OverflowError`` is not an ``OSError``: ``int()`` takes any run of
    digits a filename offers, and ``os.kill`` raises converting one too
    large for a C int. A courtesy that cannot answer must cost neither the
    file nor the write."""
    target = tmp_path / "report.json"
    huge = tmp_path / f".{target.name}.{2**70}.tmp"
    huge.write_text("someone else's file", encoding="utf-8")

    atomic_write_text(target, "{}")  # must not raise

    assert target.read_text(encoding="utf-8") == "{}", "the write survived the unanswerable pid"
    assert huge.exists(), "an unanswerable pid is not a dead one"


def test_a_pid_alive_under_another_uid_keeps_its_file() -> None:
    """``PermissionError`` means alive, not gone: a pid under another uid
    answers EPERM rather than "no such process", and reading that as dead
    would delete a live writer's temp we merely lack permission to probe.
    Asserted by making the kernel give that answer, since as root no real
    process does."""
    from anastomosis.core import atomic

    with mock.patch.object(atomic.os, "kill", side_effect=PermissionError):
        assert atomic._writer_is_gone(".Chart.pdf.4242.tmp") is False


def test_a_platform_with_no_liveness_probe_reaps_nothing(tmp_path: Path) -> None:
    """The non-POSIX early return is load-bearing: ``os.kill(pid, 0)`` on
    Windows calls TerminateProcess rather than asking if a process is
    alive, so with no question to ask, every temp must stay. Pinned by
    faking the platform, not by needing a Windows runner."""
    from anastomosis.core import atomic

    with mock.patch.object(atomic.os, "name", "nt"):
        assert atomic._writer_is_gone(".Chart.pdf.999999.tmp") is False


def test_a_bracket_in_a_chart_name_is_not_a_glob_pattern(tmp_path: Path) -> None:
    """A patient's chart name is data the sweep matches on, not a pattern:
    ``safe_name`` keeps ``[`` and ``]``, and an unescaped bracket makes the
    glob ask the wrong question — matching another chart's temps, or none
    at all. Asserted against the PATTERN, not a deletion, since deletion
    is POSIX-only and would say nothing about the escaping on Windows."""
    import glob as globmod

    target = tmp_path / "Chart_[A-Z].pdf"
    mine = tmp_path / f".{target.name}.999999.tmp"
    mine.write_bytes(b"%PDF-1.7 half a chart")

    escaped = list(tmp_path.glob(f".{globmod.escape(target.name)}.*.tmp"))
    naive = list(tmp_path.glob(f".{target.name}.*.tmp"))

    assert escaped == [mine], "the escaped pattern must find the temp it is about"
    assert naive == [], "the unescaped pattern reads [A-Z] as a character class"


@pytest.mark.skipif(os.name != "posix", reason="permission bits are POSIX")
def test_a_fresh_temp_is_never_at_the_umask_default_before_the_chmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[int] = []
    real_chmod = os.chmod

    def spy(path: object, mode: int, **kw: object) -> None:
        seen.append(Path(str(path)).stat().st_mode & 0o777)
        real_chmod(path, mode, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "chmod", spy)
    atomic_write_bytes(tmp_path / "m.json", b"{}", mode=0o600)
    assert seen and all(m == 0o600 for m in seen)
