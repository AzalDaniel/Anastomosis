"""Tests for output-directory hygiene."""

import logging
import os
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
    exc: Exception | None = None,
) -> object:
    """Build a fake ``subprocess.run`` that records commands and returns SIDs.

    ``whoami.exe`` calls yield stdout containing ``user_sid``; ``icacls.exe``
    calls succeed. When ``fail_on`` matches an executable's basename, ``exc``
    is raised for that call instead.
    """

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((list(cmd), dict(kwargs)))
        exe = cmd[0]
        if exc is not None and fail_on is not None and exe.endswith(fail_on):
            raise exc
        if exe.endswith("whoami.exe"):
            return subprocess.CompletedProcess(cmd, 0, stdout=f"USER INFORMATION\n\n{user_sid}\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="")

    return fake_run


def test_harden_acl_exact_two_call_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr(subprocess, "run", _recording_run(calls))

    root = Path("Z:/phi/out")
    assert _harden_windows_acl(root) is True

    # whoami (SID lookup) then the two icacls calls, in order.
    assert len(calls) == 3
    whoami_cmd, _ = calls[0]
    assert whoami_cmd[0].endswith("whoami.exe")
    assert whoami_cmd[1] == "/user"

    grant_cmd, _ = calls[1]
    strip_cmd, _ = calls[2]
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

    # Never a shell; always captured, timed out, and check=True.
    for _, kwargs in (calls[1], calls[2]):
        assert kwargs.get("shell") is not True
        assert kwargs.get("capture_output") is True
        assert kwargs.get("check") is True
        assert "timeout" in kwargs


def test_harden_acl_grant_failure_short_circuits_inheritance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    fail = _recording_run(
        calls, fail_on="icacls.exe", exc=subprocess.CalledProcessError(5, "icacls")
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


@pytest.mark.skipif(os.name != "nt", reason="real NTFS ACL inspection (Windows CI lane)")
def test_windows_real_dacl_is_protected(tmp_path: Path) -> None:
    out = secure_output_dir(tmp_path / "out")
    sddl_file = tmp_path / "acl.sddl"
    subprocess.run(
        ["icacls", str(out), "/save", str(sddl_file)],
        check=True,
        capture_output=True,
    )
    sddl = sddl_file.read_text(encoding="utf-16")

    # Inheritance stripped → DACL is protected (typically ``D:PAI``).
    assert "D:P" in sddl
    # The current user retains access...
    user_sid = _windows_user_sid()
    assert user_sid is not None
    assert user_sid in sddl
    # ...but broad principals do not: no BUILTIN\Users, no Everyone ACE.
    assert ";BU)" not in sddl
    assert ";WD)" not in sddl
