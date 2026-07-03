# AI-assisted: written with Claude agents under the author's direction and review; see DESIGN.md.
"""Output-directory hygiene (security backlog: output hygiene, M1).

Everything the pipeline writes — archives, rendered PDFs, QA reports,
delivery manifests — lands in a directory created here, so two guarantees
hold everywhere:

* **Restricted permissions**, tightened per platform to the smallest set of
  principals that can still run the tool. Reconstructed charts are PHI; a
  world-readable archive directory is a breach waiting to happen.

  * POSIX: ``chmod 0o700`` — owner-only.
  * Windows NTFS: inheritance is removed and access is restricted to the
    current user + SYSTEM + Administrators, mirroring the semantics CPython
    gives ``mkdir(0o700)`` on Windows (and the OpenSSH-for-Windows
    convention). Applied via ``icacls`` with literal SIDs, so no localisation
    of account names and no ``pywin32`` dependency.
  * Filesystems without ACLs (FAT32/exFAT) cannot be hardened; the operator
    gets a one-line warning and must rely on profile ACLs / disk encryption.

* **A PHI warning README** in every output root, so a folder found on disk
  months later explains itself before someone syncs it to a cloud drive. The
  README lands regardless of whether permission hardening succeeded.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import subprocess
from pathlib import Path

__all__ = ["OutputPathError", "secure_output_dir", "validate_output_target"]

logger = logging.getLogger(__name__)


class OutputPathError(Exception):
    """An output path cannot become a directory (it, or an ancestor, is a file).

    Raised *before* any backend work so the operator gets a clean message
    instead of a ``FileExistsError``/``NotADirectoryError`` traceback from deep
    in the render/delivery code. Callers map it to a clean exit (code 2).
    """


def validate_output_target(path: str | Path) -> None:
    """Raise :class:`OutputPathError` if ``path`` can't be created as a directory.

    ``Path.mkdir(parents=True)`` fails with ``FileExistsError`` when the target
    itself is a file, and ``NotADirectoryError`` when an ancestor is a file.
    Both reduce to: the nearest *existing* path component must be a directory.
    """
    target = Path(path)
    for component in (target, *target.parents):
        if component.exists():
            if not component.is_dir():
                where = "" if component == target else f" (ancestor of {target})"
                raise OutputPathError(f"Output path {component} is a file, not a directory{where}.")
            return  # nearest existing component is a directory — mkdir will succeed


_README_NAME = "_PHI_WARNING_README.txt"

_README_TEXT = """\
THIS FOLDER MAY CONTAIN PROTECTED HEALTH INFORMATION (PHI)
===========================================================

It was created by Anastomosis (https://github.com/AzalDaniel/Anastomosis)
while reconstructing or delivering clinical records.

Handle accordingly:
* Do NOT sync this folder to consumer cloud storage or share it by email.
* Do NOT commit it to version control.
* Store on encrypted media; delete securely when your retention need ends.
* Access is restricted to this user account — owner-only on POSIX; on
  Windows, this user plus SYSTEM and Administrators. Keep it that way.

If you found this folder and don't know why it exists, contact the practice
or person who ran the export before opening anything else in it.
"""

# Well-known SIDs granted alongside the current user, kept as literals so the
# grants survive on non-English installs (no BUILTIN\Administrators lookup).
# This is the same principal set CPython uses for mkdir(0o700) on Windows and
# that OpenSSH-for-Windows applies to its key directories; OWNER-RIGHTS
# (S-1-3-4) is deliberately never granted — it is known to break interop.
_SYSTEM_SID = "S-1-5-18"  # NT AUTHORITY\SYSTEM
_ADMINISTRATORS_SID = "S-1-5-32-544"  # BUILTIN\Administrators

_SID_RE = re.compile(r"S-1-[0-9-]+")
_SUBPROCESS_TIMEOUT = 10.0  # seconds; whoami/icacls are local and near-instant


def _system32_exe(name: str) -> str:
    """Absolute path to a ``System32`` executable, resolved via ``%SystemRoot%``.

    Locating the interpreter's own system directory instead of trusting PATH
    is what keeps these calls immune to executable-hijack: an attacker who
    drops ``whoami.exe`` in the working directory can never be selected.
    """
    system_root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    return str(Path(system_root, "System32", name))


def _windows_user_sid() -> str | None:
    """Return the current user's SID via ``whoami /user``, or ``None`` on failure.

    Uses the absolute ``System32\\whoami.exe`` path (no PATH lookup) and reads
    the first ``S-1-...`` literal out of stdout. Column-header localisation is
    irrelevant — only the SID token is parsed.
    """
    try:
        completed = subprocess.run(  # noqa: S603 — absolute exe, shell=False, fixed args
            [_system32_exe("whoami.exe"), "/user"],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = _SID_RE.search(completed.stdout)
    return match.group(0) if match else None


def _harden_windows_acl(root: Path) -> bool:
    """Restrict ``root`` to the current user + SYSTEM + Administrators (NTFS).

    Fail-safe ordering — the explicit grants are added *before* inheritance is
    stripped, so a failure at any step leaves the directory readable by its
    owner rather than locking everyone out:

    #. ``icacls <root> /grant:r *<user>:(OI)(CI)F *SYSTEM:(OI)(CI)F
       *Administrators:(OI)(CI)F`` — add the three explicit full-control ACEs
       while inherited ACEs still exist.
    #. ``icacls <root> /inheritance:r`` — strip inherited ACEs, leaving only
       the explicit grants.

    Returns ``True`` only when both calls succeed. If the SID lookup or the
    grant fails, the inheritance strip is never attempted (directory keeps its
    inherited permissions — weaker, but never broken). If the strip fails, the
    explicit grants remain alongside inheritance — still no lockout.
    """
    user_sid = _windows_user_sid()
    if user_sid is None:
        return False
    icacls = _system32_exe("icacls.exe")
    grant = [
        icacls,
        str(root),
        "/grant:r",
        f"*{user_sid}:(OI)(CI)F",
        f"*{_SYSTEM_SID}:(OI)(CI)F",
        f"*{_ADMINISTRATORS_SID}:(OI)(CI)F",
    ]
    strip = [icacls, str(root), "/inheritance:r"]
    try:
        subprocess.run(  # noqa: S603 — absolute exe, shell=False, literal SIDs
            grant, capture_output=True, timeout=_SUBPROCESS_TIMEOUT, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return False  # grants not applied — leave inheritance in place
    try:
        subprocess.run(  # noqa: S603 — absolute exe, shell=False, fixed args
            strip, capture_output=True, timeout=_SUBPROCESS_TIMEOUT, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return False  # explicit grants remain alongside inheritance — no lockout
    return True


def secure_output_dir(path: str | Path) -> Path:
    """Create (or harden) an output directory and return it.

    Idempotent: safe to call on every run, and re-running re-applies the
    permission hardening (self-healing against sync services that reset ACLs).
    Permissions are tightened per platform:

    * POSIX — ``chmod 0o700`` (owner-only).
    * Windows NTFS — inheritance removed, access restricted to the current
      user + SYSTEM + Administrators (mirrors ``mkdir(0o700)`` on Windows).
    * Filesystems without ACLs — a single path-free WARNING is logged and the
      operator must fall back to profile ACLs / full-disk encryption.

    The PHI warning README is written regardless of the hardening outcome.
    """
    root = Path(path)
    validate_output_target(root)  # clean OutputPathError instead of a raw OSError
    root.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        root.chmod(stat.S_IRWXU)  # 0o700 — owner only
    elif os.name == "nt" and not _harden_windows_acl(root):
        logger.warning(
            "could not harden output directory permissions; the filesystem may lack "
            "ACLs (FAT32/exFAT). Rely on user-profile ACLs and full-disk encryption "
            "to keep PHI protected at rest."
        )
    readme = root / _README_NAME
    if not readme.exists():
        readme.write_text(_README_TEXT, encoding="utf-8")
    return root
