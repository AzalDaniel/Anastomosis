"""Tests for the shared atomic-write helper (core/atomic.py).

The property under test in every failure case: a crash mid-write must never
leave a stray ``.NAME.<pid>.tmp`` file behind, and must never leave a partial
file at the target either — the tmp+os.replace+unlink-on-failure shape every
write site in the codebase now shares.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from anastomosis.core.atomic import atomic_replace, atomic_write_text


def _tmp_files(directory: Path) -> list[Path]:
    return [p for p in directory.iterdir() if p.name.startswith(".") and p.name.endswith(".tmp")]


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
