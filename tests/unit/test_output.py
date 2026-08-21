"""Tests for output-directory hygiene."""

import logging
import os
import re
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from anastomosis.core import output as output_module
from anastomosis.core.output import (
    _ADMINISTRATORS_SID,
    _README_NAME,
    _SYSTEM_SID,
    _harden_windows_acl,
    _windows_dacl_aces,
    _windows_user_sid,
    secure_output_dir,
)

# Synthetic domain-account SID (never a real machine's): safe to bake into
# recorded-command assertions.
_FAKE_USER_SID = "S-1-5-21-1111111111-2222222222-3333333333-1001"


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


# ---------------------------------------------------------------------------
# Windows ACL hardening — helpers take Path args, so they are exercised on any
# platform by recording the subprocess calls they would issue on Windows.
# ---------------------------------------------------------------------------


def _recording_run(
    calls: list[tuple[list[str], dict[str, object]]],
    *,
    user_sid: str = _FAKE_USER_SID,
    fail_on: str | None = None,
    fail_on_arg: str | None = None,
    exc: Exception | None = None,
    sddl: str | None = None,
) -> object:
    """Build a fake ``subprocess.run`` that records commands and returns SIDs.

    ``whoami.exe`` calls yield stdout containing ``user_sid``; ``icacls.exe``
    calls succeed, and a ``/save`` call writes a compliant SDDL file (override
    the descriptor with ``sddl`` to simulate a DACL the hardening did NOT
    produce). ``fail_on`` matches an executable's basename, ``fail_on_arg`` a
    specific argument (``"/reset"``, ``"/grant:r"``); either raises ``exc``
    for that call.
    """

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((list(cmd), dict(kwargs)))
        exe = cmd[0]
        if exc is not None and fail_on is not None and exe.endswith(fail_on):
            raise exc
        if exc is not None and fail_on_arg is not None and fail_on_arg in cmd:
            raise exc
        if exe.endswith("whoami.exe"):
            return subprocess.CompletedProcess(cmd, 0, stdout=f"USER INFORMATION\n\n{user_sid}\n")
        if "/save" in cmd:
            descriptor = sddl or (f"D:PAI(A;OICI;FA;;;{user_sid})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)")
            # icacls writes UTF-16-LE with no BOM: path line, then the SDDL.
            Path(cmd[-1]).write_bytes(f"out\n{descriptor}\n".encode("utf-16-le"))
        return subprocess.CompletedProcess(cmd, 0, stdout="")

    return fake_run


def test_harden_acl_exact_call_sequence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr(subprocess, "run", _recording_run(calls))

    root = tmp_path / "out"
    root.mkdir()
    assert _harden_windows_acl(root) is True

    # whoami (SID lookup), then reset -> grant -> strip -> save(verify).
    assert len(calls) == 5
    whoami_cmd, _ = calls[0]
    assert whoami_cmd[0].endswith("whoami.exe")
    assert whoami_cmd[1] == "/user"

    reset_cmd, _ = calls[1]
    grant_cmd, _ = calls[2]
    strip_cmd, _ = calls[3]
    save_cmd, _ = calls[4]
    # /reset FIRST: clear pre-existing EXPLICIT entries (neither /grant:r nor
    # /inheritance:r touches those) before granting the allowlist.
    assert reset_cmd == [reset_cmd[0], str(root), "/reset"]
    assert grant_cmd[0].endswith("icacls.exe")
    assert grant_cmd[1] == str(root)
    # /grant:r comes before /inheritance:r — explicit ACEs added while the
    # inherited ones still exist, so nobody is ever locked out.
    assert grant_cmd[2] == "/grant:r"
    assert grant_cmd[3:] == [
        f"*{_FAKE_USER_SID}:(OI)(CI)F",
        f"*{_SYSTEM_SID}:(OI)(CI)F",
        f"*{_ADMINISTRATORS_SID}:(OI)(CI)F",
    ]
    assert _SYSTEM_SID == "S-1-5-18"
    assert _ADMINISTRATORS_SID == "S-1-5-32-544"
    assert strip_cmd == [strip_cmd[0], str(root), "/inheritance:r"]
    # The post-verify reads the DACL back — the return value is a promise.
    assert save_cmd[2] == "/save"

    # Never a shell; always captured, timed out, and check=True.
    for _, kwargs in calls[1:]:
        assert kwargs.get("shell") is not True
        assert kwargs.get("capture_output") is True
        assert kwargs.get("check") is True
        assert "timeout" in kwargs


def test_harden_acl_reset_failure_short_circuits_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    fail = _recording_run(
        calls, fail_on_arg="/reset", exc=subprocess.CalledProcessError(5, "icacls")
    )
    monkeypatch.setattr(subprocess, "run", fail)

    assert _harden_windows_acl(Path("Z:/phi/out")) is False
    # The reset was attempted; neither the grant nor the strip may follow.
    assert not any("/grant:r" in cmd for cmd, _ in calls)
    assert not any("/inheritance:r" in cmd for cmd, _ in calls)


def test_harden_acl_grant_failure_short_circuits_inheritance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    fail = _recording_run(
        calls, fail_on_arg="/grant:r", exc=subprocess.CalledProcessError(5, "icacls")
    )
    monkeypatch.setattr(subprocess, "run", fail)

    assert _harden_windows_acl(Path("Z:/phi/out")) is False
    # The grant was attempted; /inheritance:r must never run after it fails.
    assert not any("/inheritance:r" in cmd for cmd, _ in calls)


def test_harden_acl_grant_timeout_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    fail = _recording_run(
        calls, fail_on="icacls.exe", exc=subprocess.TimeoutExpired("icacls", 10.0)
    )
    monkeypatch.setattr(subprocess, "run", fail)

    assert _harden_windows_acl(Path("Z:/phi/out")) is False


def test_harden_acl_fails_closed_when_a_foreign_ace_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The return value is a PROMISE: if the read-back DACL still carries an
    ACE outside the granted set (here Everyone, ``WD``), the function must
    report failure even though every icacls step exited 0."""
    calls: list[tuple[list[str], dict[str, object]]] = []
    survived = (
        f"D:PAI(A;OICI;FA;;;{_FAKE_USER_SID})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1200a9;;;WD)"
    )
    monkeypatch.setattr(subprocess, "run", _recording_run(calls, sddl=survived))

    root = tmp_path / "out"
    root.mkdir()
    assert _harden_windows_acl(root) is False


def test_harden_acl_fails_closed_when_the_dacl_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    fail = _recording_run(
        calls, fail_on_arg="/save", exc=subprocess.CalledProcessError(5, "icacls")
    )
    monkeypatch.setattr(subprocess, "run", fail)

    # An unverifiable DACL cannot be reported as hardened.
    assert _harden_windows_acl(Path("Z:/phi/out")) is False


def test_harden_acl_sid_lookup_failure_skips_icacls(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    fail = _recording_run(calls, fail_on="whoami.exe", exc=OSError("boom"))
    monkeypatch.setattr(subprocess, "run", fail)

    assert _harden_windows_acl(Path("Z:/phi/out")) is False
    # SID lookup failed, so no icacls call is ever issued.
    assert len(calls) == 1
    assert calls[0][0][0].endswith("whoami.exe")


def test_windows_user_sid_none_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired("whoami", 10.0)

    monkeypatch.setattr(subprocess, "run", boom)
    assert _windows_user_sid() is None


def test_secure_output_dir_warns_and_writes_readme_when_hardening_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Force the Windows code path on a POSIX test host and make hardening fail
    # (as it would on a FAT32/exFAT volume with no ACLs). Patch os *as the
    # module sees it* — flipping the real ``os.name`` would make ``pathlib``
    # hand out WindowsPath and break the test host.
    monkeypatch.setattr(output_module, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(output_module, "_harden_windows_acl", lambda root: False)

    target = tmp_path / "out"
    with caplog.at_level(logging.WARNING, logger="anastomosis.core.output"):
        result = secure_output_dir(target)

    assert result == target
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "harden" in message
    # PHI-safe: the warning must never carry the output path.
    assert str(target) not in message
    # README lands regardless of the hardening outcome.
    assert (target / _README_NAME).read_text(encoding="utf-8")


@pytest.mark.skipif(os.name != "nt", reason="real NTFS ACL mutation (Windows CI lane)")
def test_windows_real_dacl_survives_a_seeded_broad_ace(tmp_path: Path) -> None:
    """A pre-existing broad EXPLICIT ACE must not survive the hardening.

    ``/grant:r`` replaces only NAMED trustees' entries and ``/inheritance:r``
    removes only INHERITED ones, so an explicit Everyone ACE seeded before the
    call (a sync tool, a helpful admin) used to ride through both steps while
    the function reported success. The ``/reset`` + post-verify close that:
    after ``secure_output_dir`` the DACL matches the allowlist exactly.
    """
    target = tmp_path / "out"
    target.mkdir()
    # Seed the broad explicit ACE by SID (S-1-1-0 = Everyone; locale-safe).
    subprocess.run(
        ["icacls", str(target), "/grant", "*S-1-1-0:(OI)(CI)RX"],
        check=True,
        capture_output=True,
    )

    out = secure_output_dir(target)

    aces = _windows_dacl_aces(out)
    assert aces is not None
    trustees = {trustee for _kind, trustee in aces}
    user_sid = _windows_user_sid()
    assert user_sid is not None
    allowed = {user_sid, "LA", "OW", "SY", "BA", _SYSTEM_SID, _ADMINISTRATORS_SID}
    assert trustees <= allowed, f"unexpected ACE trustees: {sorted(trustees - allowed)}"
    # Everyone (WD / S-1-1-0) is the seeded entry and must be gone.
    assert not trustees & {"WD", "S-1-1-0", "BU", "AU", "IU"}
    assert all(kind == "A" for kind, _trustee in aces)


@pytest.mark.skipif(os.name != "nt", reason="real NTFS ACL inspection (Windows CI lane)")
def test_windows_real_dacl_is_protected(tmp_path: Path) -> None:
    out = secure_output_dir(tmp_path / "out")
    sddl_file = tmp_path / "acl.sddl"
    subprocess.run(
        ["icacls", str(out), "/save", str(sddl_file)],
        check=True,
        capture_output=True,
    )
    # ``icacls /save`` writes UTF-16-LE with NO BOM; the plain ``utf-16``
    # codec refuses BOM-less input, so decode explicitly (and drop a BOM if
    # a Windows version ever adds one).
    sddl = sddl_file.read_bytes().decode("utf-16-le").lstrip("\ufeff")

    # Inheritance stripped → DACL is protected (typically ``D:PAI``).
    assert "D:P" in sddl
    user_sid = _windows_user_sid()
    assert user_sid is not None
    # Every ACE trustee must be within the granted set — the current user
    # (as a literal SID, or the well-known alias SDDL abbreviates it to:
    # ``LA`` for a RID-500 admin account, ``OW`` owner-rights), SYSTEM
    # (``SY``), and Administrators (``BA``). Broad principals (BU Users,
    # WD Everyone, AU Authenticated Users, IU Interactive) must be absent.
    dacl = sddl.split("D:", 1)[1]
    if "S:" in dacl:
        dacl = dacl.split("S:", 1)[0]
    trustees = set(re.findall(r";([A-Z0-9-]+)\)", dacl))
    assert trustees, f"no ACE trustees parsed from SDDL: {sddl!r}"
    allowed = {user_sid, "LA", "OW", "SY", "BA"}
    assert trustees <= allowed, f"unexpected ACE trustees: {sorted(trustees - allowed)}"
    assert not trustees & {"BU", "WD", "AU", "IU"}
