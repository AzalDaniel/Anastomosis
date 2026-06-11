"""Self-tests for the PHI scanner — the repo's most important guardrail.

These tests never touch real PHI: the deny-list under test is built from
made-up canary tokens, proving the mechanism works without the plaintext
ever existing in this repository.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import phi_scan


@pytest.fixture()
def canary_denylist(tmp_path: Path) -> Path:
    token = "zzq phantomson"  # made-up name bigram, exists nowhere
    hashes = {
        "sha256": [
            hashlib.sha256(token.encode()).hexdigest(),
            hashlib.sha256(b"phantomson").hexdigest(),
        ]
    }
    path = tmp_path / "hashes.json"
    path.write_text(json.dumps(hashes))
    return path


def run_scan(paths: list[Path], hashes: Path) -> int:
    return phi_scan.main([*map(str, paths), "--hashes", str(hashes)])


def test_clean_file_passes(tmp_path: Path, canary_denylist: Path) -> None:
    f = tmp_path / "clean.py"
    f.write_text("def add(a, b):\n    return a + b\n")
    assert run_scan([f], canary_denylist) == 0


def test_denylisted_name_fails(tmp_path: Path, canary_denylist: Path) -> None:
    f = tmp_path / "leak.md"
    f.write_text("The patient Zzq Phantomson was seen on Tuesday.\n")
    assert run_scan([f], canary_denylist) == 1


def test_denylisted_surname_alone_fails(tmp_path: Path, canary_denylist: Path) -> None:
    f = tmp_path / "leak2.md"
    f.write_text("see chart for PHANTOMSON\n")
    assert run_scan([f], canary_denylist) == 1


def test_non_fixture_guid_fails(tmp_path: Path, canary_denylist: Path) -> None:
    f = tmp_path / "code.py"
    f.write_text('OWNER = "deadbeef-1234-5678-9abc-def012345678"\n')
    assert run_scan([f], canary_denylist) == 1


def test_fixture_guid_passes(tmp_path: Path, canary_denylist: Path) -> None:
    f = tmp_path / "fixture.py"
    f.write_text(
        'A = "feedface-0000-0000-0000-000000000001"\nB = "00000000-1111-2222-3333-444444444444"\n'
    )
    assert run_scan([f], canary_denylist) == 0


def test_real_looking_ssn_fails(tmp_path: Path, canary_denylist: Path) -> None:
    f = tmp_path / "data.txt"
    f.write_text("ssn: 123-45-6789\n")
    assert run_scan([f], canary_denylist) == 1


def test_synthetic_ssn_ranges_pass(tmp_path: Path, canary_denylist: Path) -> None:
    f = tmp_path / "data.txt"
    f.write_text("ssn: 000-12-3456 or 666-12-3456 or 987-65-4321\n")
    assert run_scan([f], canary_denylist) == 0


def test_phone_outside_555_fails(tmp_path: Path, canary_denylist: Path) -> None:
    f = tmp_path / "notes.md"
    f.write_text("call (212) 867-1234\n")
    assert run_scan([f], canary_denylist) == 1


def test_555_phone_passes(tmp_path: Path, canary_denylist: Path) -> None:
    f = tmp_path / "notes.md"
    f.write_text("call (212) 555-0142\n")
    assert run_scan([f], canary_denylist) == 0


def test_dob_adjacent_date_fails(tmp_path: Path, canary_denylist: Path) -> None:
    f = tmp_path / "notes.md"
    f.write_text("DOB: 4/12/1957\n")
    assert run_scan([f], canary_denylist) == 1


def test_repo_denylist_exists_and_is_hashes_only() -> None:
    data = json.loads(phi_scan.DEFAULT_HASHES.read_text())
    assert data["sha256"], "deny-list must not be empty"
    assert all(len(h) == 64 and int(h, 16) >= 0 for h in data["sha256"])


def test_whole_repo_is_clean() -> None:
    """The repo itself must always pass its own scanner."""
    assert phi_scan.main([]) == 0
