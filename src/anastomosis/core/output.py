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

__all__ = [
    "OutputPathError",
    "require_output_dir",
    "secure_output_dir",
    "validate_output_target",
]

logger = logging.getLogger(__name__)


class OutputPathError(Exception):
    """An output path cannot become a directory (it, or an ancestor, is a file).

    Raised *before* any backend work so the operator gets a clean message
    instead of a ``FileExistsError``/``NotADirectoryError`` traceback from deep
    in the render/delivery code. Callers map it to a clean exit (code 2).
    """


def require_output_dir(raw: str) -> Path:
    """Turn a user-supplied output location into a Path, refusing a blank one.

    ``Path("")`` is ``Path(".")``, so a blank value does not fail — it silently
    means "here", and charts named after patients land in whatever directory the
    program happens to be running from, with the run reporting success. The
    blankness only exists before ``Path()`` sees it, so this has to be called on
    the raw value at the boundary where the person typed it.
    """
    if not raw.strip():
        raise OutputPathError(
            "No output folder was given. Choose the folder Anastomosis should write "
            "the finished charts into."
        )
    return Path(raw)


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


# SDDL trustee tokens ``icacls /save`` may emit for principals inside the
# granted set: SYSTEM (``SY``), Administrators (``BA``), and the current user
# when it is the RID-500 admin account (``LA``) or abbreviated to owner-rights
# (``OW``). Anything else — ``WD`` Everyone, ``BU`` Users, ``AU`` Authenticated
# Users, a foreign literal SID — is outside the promise and fails the verify.
_ALLOWED_SDDL_ALIASES = frozenset({"SY", "BA", "LA", "OW"})

#: Flag letters an SDDL ACL header may carry between ``D:`` and its first ACE
#: (``P`` protected, ``AI`` auto-inherited, ``AR`` auto-inherit-req, and the
#: word forms like ``NO_ACCESS_CONTROL``).
_SDDL_ACL_FLAGS_RE = re.compile(r"[A-Za-z_]*")
#: Fields in a plain (non-conditional) ACE:
#: ``type;flags;rights;object_guid;inherit_object_guid;trustee``.
_SDDL_ACE_FIELDS = 6


def _dacl_section(text: str) -> str | None:
    """The body of the ``D:`` section of an SDDL string, or ``None``.

    Section markers (``O:`` ``G:`` ``D:`` ``S:``) are located at PAREN DEPTH 0
    only. A conditional ACE carries its condition inside its own parentheses —
    ``(XA;;FA;;;WD;(@User.Title=="S:x"))`` — so a naive ``split("S:")`` cuts the
    DACL in half at a marker that is not a section at all, silently discarding
    every ACE after the cut while the parse still returns the clean ones. That
    is a verify that reports ``True`` with an Everyone ACE still on the
    directory, which is why the depth is tracked rather than assumed.

    ``None`` means no ``D:`` section starts at depth 0 — unverifiable, and the
    caller fails closed.
    """
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
    """``(ace type, trustee)`` pairs from a DACL section body, or ``None``.

    The section must be a flag header followed by a plain sequence of
    ``(...)`` groups and nothing else, each group holding exactly
    :data:`_SDDL_ACE_FIELDS` semicolon-separated fields. Anything else —
    stray text between groups, an unterminated group, a group whose body
    contains ``(`` (a conditional ACE, whose condition may embed ``;`` and
    quotes and cannot be split on field boundaries), or an extra field (a
    resource-attribute ACE) — returns ``None``. Refusing to interpret an ACE
    shape this parser does not fully understand is the point: the caller turns
    ``None`` into a failed verify, never into "no ACEs found, looks fine".
    """
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
    """Read ``root``'s DACL as ``(ace type, trustee token)`` pairs, or ``None``.

    Uses ``icacls /save`` (SDDL — literal SIDs and locale-independent alias
    tokens, never localized account names) and parses the ``D:`` section only.
    ``None`` means the ACL could not be read or parsed; the caller must fail
    closed — an unverifiable DACL cannot be reported as hardened.
    """
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
    """Restrict ``root`` to the current user + SYSTEM + Administrators (NTFS).

    Reset first, then grant, then strip, then PROVE it — and fail-safe at
    every step (a failure leaves the directory readable by its owner, never
    locked out):

    #. ``icacls <root> /reset`` — replace the DACL with the inherited default,
       clearing any pre-existing EXPLICIT entry. ``/grant:r`` alone replaces
       only the named trustees' entries and ``/inheritance:r`` removes only
       inherited ones, so a broad explicit ACE someone (or some sync tool)
       added earlier would silently survive both.
    #. ``icacls <root> /grant:r *<user>:(OI)(CI)F *SYSTEM:(OI)(CI)F
       *Administrators:(OI)(CI)F`` — add the three explicit full-control ACEs
       while inherited ACEs still exist.
    #. ``icacls <root> /inheritance:r`` — strip inherited ACEs, leaving only
       the explicit grants.
    #. Read the DACL back (:func:`_windows_dacl_aces`) and verify every ACE is
       an Allow for a trustee inside the granted set. The function's return
       value is a PROMISE the caller repeats to the operator, so it reflects
       the directory's actual state, never just the exit codes of the steps.

    Returns ``True`` only when all four steps succeed. A failed reset stops
    before the grant (inherited permissions remain); a failed grant stops
    before the strip; a failed strip leaves the grants alongside inheritance;
    a failed or mismatched verify returns ``False`` even though the steps ran,
    because the promised state could not be proven.
    """
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
