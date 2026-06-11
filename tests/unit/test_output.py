"""Tests for output-directory hygiene."""

import os
import stat
from pathlib import Path

import pytest

from anastomosis.core.output import _README_NAME, secure_output_dir


def test_creates_nested_dir_with_readme(tmp_path: Path) -> None:
    target = tmp_path / "archive" / "run-001"
    result = secure_output_dir(target)
    assert result == target
    assert target.is_dir()
    readme = target / _README_NAME
    assert "PROTECTED HEALTH INFORMATION" in readme.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_owner_only_permissions(tmp_path: Path) -> None:
    target = secure_output_dir(tmp_path / "out")
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_idempotent_and_tightens_existing(tmp_path: Path) -> None:
    target = tmp_path / "out"
    target.mkdir(mode=0o755)
    secure_output_dir(target)
    secure_output_dir(target)  # second call is a no-op, not an error
    if os.name == "posix":
        assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert (target / _README_NAME).exists()
