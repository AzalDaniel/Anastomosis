"""Tests for tools.guard_count — the pinned regression-guard floor."""

from __future__ import annotations

from pathlib import Path

import pytest
import tools.guard_count as guard_count
from tools.guard_count import count_guard_refs, main


def test_the_real_repo_meets_its_own_pinned_floor() -> None:
    assert main() == 0


def test_a_floor_above_the_real_count_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The probe this guards against: an unrelated regex change elsewhere
    (prose_gate.py) must be unable to move this floor, because this module
    counts independently — proven here by moving only ITS OWN floor file."""
    inflated = tmp_path / "guard_baseline.txt"
    inflated.write_text(str(count_guard_refs() + 1000), encoding="utf-8")
    monkeypatch.setattr(guard_count, "BASELINE", inflated)
    assert main() == 1
    assert "DROPPED" in capsys.readouterr().out


def test_does_not_import_prose_gate() -> None:
    assert "prose_gate" not in vars(guard_count)
    assert not hasattr(guard_count, "measure")
