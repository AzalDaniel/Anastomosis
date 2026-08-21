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
from typing import NoReturn

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


# Negative-test payloads are assembled at runtime so the forbidden patterns
# never appear contiguously in this (committed, scanned) source file.
def test_non_fixture_guid_fails(tmp_path: Path, canary_denylist: Path) -> None:
    f = tmp_path / "code.py"
    guid = "dead" + "beef-1234-5678-9abc-def012345678"
    f.write_text(f'OWNER = "{guid}"\n')
    assert run_scan([f], canary_denylist) == 1


def test_fixture_guid_passes(tmp_path: Path, canary_denylist: Path) -> None:
    f = tmp_path / "fixture.py"
    f.write_text(
        'A = "feedface-0000-0000-0000-000000000001"\nB = "00000000-1111-2222-3333-444444444444"\n'
    )
    assert run_scan([f], canary_denylist) == 0


def test_real_looking_ssn_fails(tmp_path: Path, canary_denylist: Path) -> None:
    f = tmp_path / "data.txt"
    f.write_text("ssn: 123-45-" + "6789\n")
    assert run_scan([f], canary_denylist) == 1


def test_synthetic_ssn_ranges_pass(tmp_path: Path, canary_denylist: Path) -> None:
    f = tmp_path / "data.txt"
    f.write_text("ssn: 000-12-3456 or 666-12-3456 or 987-65-4321\n")
    assert run_scan([f], canary_denylist) == 0


def test_phone_outside_555_fails(tmp_path: Path, canary_denylist: Path) -> None:
    f = tmp_path / "notes.md"
    f.write_text("call (212) 867-" + "1234\n")
    assert run_scan([f], canary_denylist) == 1


def test_555_phone_passes(tmp_path: Path, canary_denylist: Path) -> None:
    f = tmp_path / "notes.md"
    f.write_text("call (212) 555-0142\n")
    assert run_scan([f], canary_denylist) == 0


def test_dob_adjacent_date_fails(tmp_path: Path, canary_denylist: Path) -> None:
    f = tmp_path / "notes.md"
    f.write_text("DOB: 4/12/" + "1957\n")
    assert run_scan([f], canary_denylist) == 1


def test_unapproved_nul_binary_fails(tmp_path: Path, canary_denylist: Path) -> None:
    """Default-deny: a binary the text passes cannot read must fail, not skip.

    Before this gate, any file with a NUL in its first 8 KiB (or a media
    suffix) was silently skipped — an unapproved media file could carry
    sensitive content past the scanner unread.
    """
    f = tmp_path / "mystery.pdf"
    f.write_bytes(b"%PDF-1.4\x00" + b"opaque payload the scanner cannot read")
    assert run_scan([f], canary_denylist) == 1


def test_unapproved_media_suffix_fails_even_without_nul(
    tmp_path: Path, canary_denylist: Path
) -> None:
    # A media suffix is classified binary even when its bytes happen to look
    # like text (a mislabeled or crafted file must not dodge the gate).
    f = tmp_path / "image.png"
    f.write_bytes(b"not really an image, but the suffix says media")
    assert run_scan([f], canary_denylist) == 1


def test_allowlisted_binary_hash_passes(
    tmp_path: Path, canary_denylist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"\x00approved synthetic binary"
    f = tmp_path / "approved.woff2"
    f.write_bytes(payload)
    allowlist = tmp_path / "allow.txt"
    allowlist.write_text(
        "# synthetic test binary, approved for this test only\n"
        f"sha256:{hashlib.sha256(payload).hexdigest()}\n"
    )
    monkeypatch.setattr(phi_scan, "ALLOWLIST", allowlist)
    assert run_scan([f], canary_denylist) == 0
    # The approval is the exact bytes: any change to the file re-fails.
    f.write_bytes(payload + b"!")
    assert run_scan([f], canary_denylist) == 1


def test_base64_armored_payload_in_a_text_file_fails(tmp_path: Path, canary_denylist: Path) -> None:
    """A base64 blob inside a readable file is opaque content, and must fail.

    The binary gate keys on a suffix or a NUL byte, so a text file carrying a
    ``data:...;base64,...`` payload sails past it — and the token splitter
    shreds the blob at every ``+``/``/``/``=``, so no pattern inspects it
    either. That is a chart, a scan, or a spreadsheet of PHI hiding in plain
    sight inside a .md or .html the reviewer skims.
    """
    f = tmp_path / "page.html"
    payload = "QUJDRA" * 60  # > BASE64_ARMOR_MIN_CHARS of base64 alphabet
    f.write_text(f'<img src="data:image/png;base64,{payload}">\n')
    assert run_scan([f], canary_denylist) == 1


def test_hash_approved_file_may_carry_a_base64_payload(
    tmp_path: Path, canary_denylist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The escape hatch is the SAME one binaries use: approve the whole file.

    This is how the vendored HL7 stylesheet (whose two payloads are its own
    toolbar icons) passes — by provenance, recorded once, invalidated by any
    change to the file.
    """
    f = tmp_path / "stylesheet.xsl"
    f.write_text(f"<xsl:text>data:image/png;base64,{'QUJDRA' * 60}</xsl:text>\n")
    allowlist = tmp_path / "allow.txt"
    allowlist.write_text(
        "# synthetic stylesheet, approved for this test only\n"
        f"sha256:{hashlib.sha256(f.read_bytes()).hexdigest()}\n"
    )
    monkeypatch.setattr(phi_scan, "ALLOWLIST", allowlist)
    assert run_scan([f], canary_denylist) == 0
    # The approval is the exact bytes: any change to the file re-fails.
    f.write_text(f.read_text() + "<!-- edited -->\n")
    assert run_scan([f], canary_denylist) == 1


def test_armor_approval_is_line_ending_independent(
    tmp_path: Path, canary_denylist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One approval hash covers the LF and CRLF checkouts of a text file.

    git checks text files out with CRLF on Windows runners (autocrlf), so the
    raw bytes of the SAME committed file hash differently per platform; the
    armor gate therefore hashes the LF form. Without this, an approval
    computed on Linux failed every Windows run of the whole-repo scan.
    """
    lf_content = f"<xsl:text>data:image/png;base64,{'QUJDRA' * 60}</xsl:text>\n"
    allowlist = tmp_path / "allow.txt"
    allowlist.write_text(
        "# synthetic stylesheet, approved for this test only\n"
        f"sha256:{hashlib.sha256(lf_content.encode()).hexdigest()}\n"
    )
    monkeypatch.setattr(phi_scan, "ALLOWLIST", allowlist)
    lf = tmp_path / "lf.xsl"
    lf.write_bytes(lf_content.encode())
    crlf = tmp_path / "crlf.xsl"
    crlf.write_bytes(lf_content.replace("\n", "\r\n").encode())
    assert run_scan([lf], canary_denylist) == 0
    assert run_scan([crlf], canary_denylist) == 0


def test_short_base64_runs_do_not_trip_the_armor_gate(
    tmp_path: Path, canary_denylist: Path
) -> None:
    """A tracking-pixel-sized data URI is not opaque content — it stays clean.

    The floor exists so ordinary inline snippets (and the base64 alphabet
    appearing by accident) never need an approval entry.
    """
    f = tmp_path / "small.md"
    short = "A" * (phi_scan.BASE64_ARMOR_MIN_CHARS - 1)
    f.write_text(f"data:image/gif;base64,{short}\n")
    assert run_scan([f], canary_denylist) == 0


def test_indented_comment_never_becomes_an_allowlist_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An INDENTED ``#`` line is a comment, not an allowlist entry.

    Testing the comment marker before stripping let an indented comment enter
    the token allowlist verbatim — every word of a justification note would
    then excuse itself, silently widening the ledger.
    """
    allowlist = tmp_path / "allow.txt"
    allowlist.write_text("    # an indented justification comment\nrealentry\n")
    monkeypatch.setattr(phi_scan, "ALLOWLIST", allowlist)
    assert phi_scan.load_allowlist() == {"realentry"}


def test_repo_binaries_are_all_hash_approved() -> None:
    """Every tracked binary's current hash is in the allowlist (drift guard).

    A binary changed without re-approval fails here BEFORE the whole-repo
    scan does, with a message naming this discipline.
    """
    approved = phi_scan.load_approved_file_hashes()
    assert approved, "the approved-file ledger must not be empty while binaries ship"
    for entry in approved:
        assert len(entry) == 64 and all(c in "0123456789abcdef" for c in entry)


def test_repo_denylist_exists_and_is_hashes_only() -> None:
    data = json.loads(phi_scan.DEFAULT_HASHES.read_text())
    assert data["sha256"], "deny-list must not be empty"
    assert all(len(h) == 64 and int(h, 16) >= 0 for h in data["sha256"])


def test_whole_repo_is_clean() -> None:
    """The repo itself must always pass its own scanner."""
    assert phi_scan.main([]) == 0


def _raise_no_git(*_args: object, **_kwargs: object) -> NoReturn:
    """Stand in for ``subprocess.run`` when git is unavailable."""
    raise FileNotFoundError("git not installed")


def test_walk_fallback_catches_canary(
    tmp_path: Path,
    canary_denylist: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With no git checkout, the scanner walks the tree and still catches PHI."""
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "clean.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "pkg" / "leak.md").write_text("The patient Zzq Phantomson was seen.\n")

    monkeypatch.setattr(phi_scan, "REPO_ROOT", root)
    monkeypatch.setattr(phi_scan.subprocess, "run", _raise_no_git)

    rc = phi_scan.main(["--hashes", str(canary_denylist)])

    assert "scanning via filesystem walk" in capsys.readouterr().err
    assert rc == 1


def test_walk_fallback_prunes_cache_dirs(
    tmp_path: Path,
    canary_denylist: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Files under pruned dirs are never enumerated, hence never scanned."""
    root = tmp_path / "repo"
    (root / "__pycache__").mkdir(parents=True)
    (root / ".venv").mkdir(parents=True)
    (root / "leak.egg-info").mkdir(parents=True)
    # Canaries hidden inside pruned directories must NOT be flagged.
    (root / "__pycache__" / "hidden.md").write_text("Zzq Phantomson\n")
    (root / ".venv" / "buried.md").write_text("Zzq Phantomson\n")
    (root / "leak.egg-info" / "meta.md").write_text("Zzq Phantomson\n")
    (root / "clean.py").write_text("ok = 1\n")

    walked_names = {p.name for p in phi_scan._walk_all_files(root)}
    assert "clean.py" in walked_names
    assert "hidden.md" not in walked_names
    assert "buried.md" not in walked_names
    assert "meta.md" not in walked_names

    monkeypatch.setattr(phi_scan, "REPO_ROOT", root)
    monkeypatch.setattr(phi_scan.subprocess, "run", _raise_no_git)
    assert phi_scan.main(["--hashes", str(canary_denylist)]) == 0


def test_walk_enumeration_is_deterministic(tmp_path: Path) -> None:
    """Two walks of the same tree return identical, sorted, ordered lists."""
    for rel in ("a.py", "b/c.py", "b/d.py", "e/f/g.py"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x = 1\n")

    first = phi_scan._walk_all_files(tmp_path)
    second = phi_scan._walk_all_files(tmp_path)
    assert first == second
    assert first == sorted(first)
