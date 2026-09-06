"""Output-directory hygiene: creates and hardens every directory the
pipeline writes into (18)."""

from __future__ import annotations

import logging
import os
import re
import stat
import subprocess
from pathlib import Path

__all__ = [
    "OutputPathError",
    "clean_typed_path",
    "require_output_dir",
    "secure_output_dir",
    "typed_path",
    "validate_output_target",
]

logger = logging.getLogger(__name__)


class OutputPathError(Exception):
    """An output path (or an ancestor) is a file, not a directory. Raised
    before any backend work, so the operator gets a clean message instead
    of a raw ``FileExistsError``/``NotADirectoryError``; callers map it to
    exit code 2."""


def clean_typed_path(raw: str) -> str:
    """Strip the quotes and stray spaces a pasted path carries (Windows
    Explorer's "Copy as path" wraps it in double quotes); left alone, both
    produce a folder that does not exist."""
    cleaned = raw.strip()
    for quote in ('"', "'"):
        if len(cleaned) >= 2 and cleaned[0] == quote and cleaned[-1] == quote:
            cleaned = cleaned[1:-1].strip()
            break
    return cleaned


def typed_path(raw: str) -> Path:
    """A Path from a string a person typed or pasted into a field: every
    such frontend field must go through here, never a bare ``Path(arg)``."""
    return Path(clean_typed_path(raw))


def require_output_dir(raw: str) -> Path:
    """A user-supplied output location as a Path; refuses a blank one.
    ``Path("")`` is ``Path(".")``, so unrefused, a blank value would
    silently write patient charts into the program's own working
    directory."""
    raw = clean_typed_path(raw)
    if not raw:
        raise OutputPathError(
            "No output folder was given. Choose the folder Anastomosis should write "
            "the finished charts into."
        )
    return Path(raw)


def validate_output_target(path: str | Path) -> None:
    """Contract: raises :class:`OutputPathError` if ``path`` cannot become a
    directory — the nearest existing path component is a file, whether
    that is ``path`` itself or an ancestor."""
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
    """Absolute ``System32`` path for ``name``, via ``%SystemRoot%`` — never
    a PATH lookup, so a hijack binary dropped in the working directory can
    never be selected."""
    system_root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    return str(Path(system_root, "System32", name))


def _windows_user_sid() -> str | None:
    """The current user's SID via ``whoami /user`` (absolute exe path, no
    PATH lookup), or ``None`` on failure. Only the first ``S-1-...``
    literal in stdout is parsed; column-header localisation is
    irrelevant."""
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


# SDDL trustee tokens icacls may emit for the granted principals (SYSTEM,
# Administrators, the RID-500 admin, owner-rights); anything else (Everyone,
# Users, a foreign SID) is outside the promise and fails the verify.
_ALLOWED_SDDL_ALIASES = frozenset({"SY", "BA", "LA", "OW"})

#: Flag letters an SDDL ACL header may carry between ``D:`` and its first ACE
#: (``P`` protected, ``AI`` auto-inherited, ``AR`` auto-inherit-req, and the
#: word forms like ``NO_ACCESS_CONTROL``).
_SDDL_ACL_FLAGS_RE = re.compile(r"[A-Za-z_]*")
#: Fields in a plain (non-conditional) ACE:
#: ``type;flags;rights;object_guid;inherit_object_guid;trustee``.
_SDDL_ACE_FIELDS = 6


def _dacl_section(text: str) -> str | None:
    """The ``D:`` section body of an SDDL string, or ``None`` if none starts
    at paren depth 0. Depth-tracked, not a naive ``split("S:")``: a
    conditional ACE can embed ``S:`` inside its own condition, and a naive
    split would silently drop every ACE after the cut while still reporting
    success. ``None`` means unverifiable; the caller fails closed."""
    depth = 0
    start: int | None = None
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == ":" and depth == 0 and index and text[index - 1] in "OGDS":
            if start is None:
                if text[index - 1] == "D":
                    start = index + 1
            else:
                # The next top-level section ends the DACL.
                return text[start : index - 1]
    return None if start is None else text[start:]


def _parse_dacl_aces(section: str) -> list[tuple[str, str]] | None:
    """Contract: ``(ace type, trustee)`` pairs from a DACL section, or
    ``None`` for any shape this parser does not fully understand — a
    conditional ACE, an extra (resource-attribute) field, stray text
    between groups. ``None`` becomes a failed verify, never "looks fine"."""
    body = section.strip()
    flags = _SDDL_ACL_FLAGS_RE.match(body)
    rest = body[flags.end() :] if flags else body
    aces: list[tuple[str, str]] = []
    while rest:
        if not rest.startswith("("):
            return None
        end = rest.find(")")
        if end == -1:
            return None
        ace = rest[1:end]
        if "(" in ace:
            return None  # conditional ACE — its body is not this shape
        fields = ace.split(";")
        if len(fields) != _SDDL_ACE_FIELDS:
            return None
        aces.append((fields[0], fields[5]))
        rest = rest[end + 1 :]
    return aces


def _windows_dacl_aces(root: Path) -> list[tuple[str, str]] | None:
    """``root``'s DACL as ``(ace type, trustee token)`` pairs via
    ``icacls /save`` (SDDL, never localized names), or ``None`` when
    unreadable/unparsable — the caller must fail closed."""
    icacls = _system32_exe("icacls.exe")
    sddl_path = root / ".anast-acl-verify.sddl"
    try:
        subprocess.run(  # noqa: S603 — absolute exe, shell=False, fixed args
            [icacls, str(root), "/save", str(sddl_path)],
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT,
            check=True,
        )
        raw = sddl_path.read_bytes()
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        try:
            sddl_path.unlink(missing_ok=True)
        except OSError:  # a leftover verify file is cosmetic, never a failure
            pass
    # ``icacls /save`` writes UTF-16-LE with NO BOM; decode explicitly and
    # drop a BOM if a Windows version ever adds one.
    text = raw.decode("utf-16-le", errors="replace").lstrip("\ufeff")
    section = _dacl_section(text)
    if section is None:
        return None
    return _parse_dacl_aces(section)


def _harden_windows_acl(root: Path) -> bool:
    """Contract: restrict ``root`` to the current user + SYSTEM +
    Administrators (18) — reset, grant, strip inheritance, then read the
    DACL back and verify every ACE is an Allow for one of those three.
    Fails safe at each step (never locks the owner out); ``True`` only
    when the read-back proves it, not just that the calls exited 0."""
    user_sid = _windows_user_sid()
    if user_sid is None:
        return False
    icacls = _system32_exe("icacls.exe")
    reset = [icacls, str(root), "/reset"]
    grant = [
        icacls,
        str(root),
        "/grant:r",
        f"*{user_sid}:(OI)(CI)F",
        f"*{_SYSTEM_SID}:(OI)(CI)F",
        f"*{_ADMINISTRATORS_SID}:(OI)(CI)F",
    ]
    strip = [icacls, str(root), "/inheritance:r"]
    for step in (reset, grant, strip):
        try:
            subprocess.run(  # noqa: S603 — absolute exe, shell=False, literal SIDs
                step, capture_output=True, timeout=_SUBPROCESS_TIMEOUT, check=True
            )
        except (OSError, subprocess.SubprocessError):
            return False  # earlier steps left owner access intact — no lockout
    aces = _windows_dacl_aces(root)
    if not aces:
        return False  # unreadable or empty DACL — the promise is unproven
    allowed = {user_sid, _SYSTEM_SID, _ADMINISTRATORS_SID} | _ALLOWED_SDDL_ALIASES
    return all(kind == "A" and trustee in allowed for kind, trustee in aces)


def secure_output_dir(path: str | Path) -> Path:
    """Contract: create (or re-harden) ``path`` and return it (18) —
    idempotent, so re-running self-heals against a sync service resetting
    ACLs. The PHI warning README is written regardless of whether
    hardening succeeded."""
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
