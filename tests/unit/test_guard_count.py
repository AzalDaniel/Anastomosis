"""Tests for tools.guard_count — the pinned regression-guard floor."""

from __future__ import annotations

from tools.guard_count import main


def test_the_real_repo_meets_its_own_pinned_floor() -> None:
    assert main() == 0
