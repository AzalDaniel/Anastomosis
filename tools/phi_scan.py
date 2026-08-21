#!/usr/bin/env python3
"""PHI scanner: blocks protected health information from entering this repo.

Anastomosis was generalized from a private production system that handled
real patient data. This scanner enforces the project's first rule:
**no real PHI ever enters this repository** — not in code, not in docs,
not in fixtures, not in git history.

Three complementary mechanisms:

1. Hashed deny-list (``tools/phi_hashes.json``): SHA-256 hashes of tokens
   known to be PHI from the private predecessor (patient/provider names,
   real GUIDs). The plaintext never appears here; the hashes were generated
   locally against the private repo. Any token in a scanned file whose
   hash appears in the deny-list fails the scan.

2. Generic patterns: SSN-shaped strings, GUIDs outside the synthetic
   fixture prefixes, phone numbers outside the 555 exchange, and dates
   adjacent to DOB markers.

3. Binary default-deny: a file the text passes cannot inspect (a binary
   suffix, or NUL bytes in the first 8 KiB) passes ONLY when its sha256
   appears as a ``sha256:<hex>`` entry in ``tools/phi_allowlist.txt`` with a
   provenance comment. The scanner is the repository's no-unapproved-binaries
   enforcer; skipping what it cannot read would let a media file carry
   sensitive content past it unread.

Synthetic-data conventions enforced repo-wide:
  * fixture GUIDs must start with ``feedface-`` or ``00000000-``
  * fixture SSNs must use never-issued ranges (area 000, 666, or >= 900)
  * fixture phone numbers must use the 555 exchange

Usage:
    python tools/phi_scan.py [paths...]      # default: all git-tracked files
    python tools/phi_scan.py --hashes FILE   # override deny-list (tests)

Exit status: 0 clean, 1 findings, 2 usage error.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HASHES = Path(__file__).resolve().parent / "phi_hashes.json"
ALLOWLIST = Path(__file__).resolve().parent / "phi_allowlist.txt"

# Suffixes classified as binary/media without sniffing. NOT a skip list: a
# file with one of these suffixes is checked against the binary allowlist
# (default-deny), exactly like a NUL-sniffed binary.
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".pyc"}
SKIP_NAMES = {"phi_hashes.json"}

# The prefix marking an approved-binary hash entry in tools/phi_allowlist.txt.
_BINARY_ALLOW_PREFIX = "sha256:"

# Directories the filesystem-walk fallback prunes: version-control metadata,
# tool caches, and virtualenvs — intrinsic machine state, never project
# content. Deliberately NOT pruned: ``dist``/``build`` and other
# conventionally-gitignored output dirs, because git skips those only via
# .gitignore and the walk runs precisely when there is no git to consult —
# scanning extra files is safe (an unlisted binary FAILS the scan rather
# than slipping through), silently skipping a committable file is not. Any
# directory whose name ends in ``.egg-info`` is pruned too.
WALK_PRUNE_DIRS = frozenset(
    {
        ".git",
        "__pycache__",
        ".venv",
        "venv",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".eggs",
        "node_modules",
    }
)

WORD_RE = re.compile(r"[A-Za-z]{2,}")
GUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
OPAQUE_RE = re.compile(r"\b[A-Za-z0-9_-]{24,}\b")
SSN_RE = re.compile(r"\b(\d{3})-(\d{2})-(\d{4})\b")
PHONE_RE = re.compile(r"\(?\b(\d{3})\)?[ .-](\d{3})[ .-]\d{4}\b")
DOB_RE = re.compile(r"(?:dob|birth)\W{0,40}?(\d{1,2}/\d{1,2}/(?:19|20)\d{2})", re.IGNORECASE)

FIXTURE_GUID_PREFIXES = ("feedface-", "00000000-")
FIXTURE_PATH_MARKERS = ("tests/fixtures/", "tests\\fixtures\\")


def sha(token: str) -> str:
    return hashlib.sha256(token.lower().encode()).hexdigest()


def candidate_tokens(text: str) -> set[str]:
    """Tokens to test against the hashed deny-list (mirrors the generator)."""
    words = [w.lower() for w in WORD_RE.findall(text)]
    tokens: set[str] = {w for w in words if len(w) >= 4}
    tokens.update(f"{a} {b}" for a, b in itertools.pairwise(words))
    tokens.update(g.lower() for g in GUID_RE.findall(text))
    tokens.update(o.lower() for o in OPAQUE_RE.findall(text))
    return tokens


def load_allowlist() -> set[str]:
    """Token false-positive entries (every non-comment line that is not a hash)."""
    if not ALLOWLIST.exists():
        return set()
    lines = ALLOWLIST.read_text(encoding="utf-8").splitlines()
    return {
        ln.strip()
        for ln in lines
        if ln.strip() and not ln.startswith("#") and not ln.strip().startswith(_BINARY_ALLOW_PREFIX)
    }


def load_binary_allowlist() -> set[str]:
    """The sha256 hex digests of APPROVED binary/media files.

    ``sha256:<hex>`` lines in ``tools/phi_allowlist.txt``; each needs a
    preceding provenance/license comment. Any binary file whose digest is not
    in this set fails the scan — default-deny for content the text passes
    cannot read.
    """
    if not ALLOWLIST.exists():
        return set()
    lines = ALLOWLIST.read_text(encoding="utf-8").splitlines()
    return {
        ln.strip()[len(_BINARY_ALLOW_PREFIX) :].lower()
        for ln in lines
        if ln.strip().startswith(_BINARY_ALLOW_PREFIX)
    }


def is_fixture_path(path: Path) -> bool:
    posix = path.as_posix()
    return any(marker.replace("\\", "/") in posix for marker in FIXTURE_PATH_MARKERS)


def scan_text(path: Path, text: str, deny: set[str], allow: set[str]) -> list[str]:
    findings: list[str] = []

    for token in candidate_tokens(text):
        if token in allow:
            continue
        if sha(token) in deny:
            # Never echo the token itself — that would re-leak it into logs.
            findings.append(f"{path}: token matching PHI deny-list (sha256={sha(token)[:12]}…)")

    for m in GUID_RE.finditer(text):
        guid = m.group(0).lower()
        if not guid.startswith(FIXTURE_GUID_PREFIXES) and guid not in allow:
            findings.append(f"{path}: non-fixture GUID '{guid}' (use feedface-/00000000- prefixes)")

    for m in SSN_RE.finditer(text):
        area = int(m.group(1))
        if area not in (0, 666) and area < 900 and m.group(0) not in allow:
            findings.append(f"{path}: SSN-shaped value (use area 000/666/9xx for synthetic data)")

    if not is_fixture_path(path):
        for m in PHONE_RE.finditer(text):
            if m.group(2) != "555" and m.group(0) not in allow:
                findings.append(f"{path}: phone-shaped value '{m.group(0)}' (use 555 exchange)")
        for m in DOB_RE.finditer(text):
            if m.group(1) not in allow:
                findings.append(f"{path}: date adjacent to DOB marker '{m.group(1)}'")

    return findings


def _walk_all_files(root: Path) -> list[Path]:
    """Enumerate every committable file under ``root`` without git.

    The scanner must cover everything committable whether or not a ``.git``
    checkout is present, so a source ZIP/sdist unpacked outside version
    control is scanned exactly as a clone would be. Version-control metadata,
    tool caches, virtualenvs, and build artifacts are pruned (see
    ``WALK_PRUNE_DIRS``); every remaining file is returned as an absolute path
    under ``root`` to match the shape produced by the git enumeration, and the
    list is sorted for a deterministic order.
    """
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames if d not in WALK_PRUNE_DIRS and not d.endswith(".egg-info")
        ]
        found.extend(Path(dirpath) / name for name in filenames)
    return sorted(found)


def iter_target_files(args_paths: list[str]) -> list[Path]:
    """Enumerate the files to scan: everything committable, git or not.

    With explicit paths, scan exactly those. Otherwise scan the whole tree —
    tracked files PLUS untracked-but-not-ignored files, so a "clean" run
    before ``git add`` cannot miss brand-new files. When git is unavailable
    (no checkout, git not installed), fall back to a filesystem walk so the
    scanner never silently stops for users who obtained the code as a source
    ZIP/sdist rather than a clone.
    """
    if args_paths:
        return [Path(p) for p in args_paths if Path(p).is_file()]
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],  # noqa: S607
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        print(
            "phi_scan: no git checkout detected; scanning via filesystem walk",
            file=sys.stderr,
        )
        return _walk_all_files(REPO_ROOT)
    files = [REPO_ROOT / line for line in out.stdout.splitlines() if line]
    return [f for f in files if f.is_file()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="files to scan (default: git-tracked files)")
    parser.add_argument("--hashes", default=str(DEFAULT_HASHES), help="deny-list JSON path")
    args = parser.parse_args(argv)

    hashes_path = Path(args.hashes)
    if not hashes_path.exists():
        print(f"phi_scan: deny-list not found at {hashes_path}", file=sys.stderr)
        return 2
    deny: set[str] = set(json.loads(hashes_path.read_text(encoding="utf-8"))["sha256"])
    allow = load_allowlist()
    approved_binaries = load_binary_allowlist()

    all_findings: list[str] = []
    for path in iter_target_files(args.paths):
        if path.name in SKIP_NAMES:
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        display = (
            path.relative_to(REPO_ROOT)
            if path.is_absolute() and path.is_relative_to(REPO_ROOT)
            else path
        )
        if path.suffix.lower() in BINARY_SUFFIXES or b"\x00" in raw[:8192]:
            # Default-deny for what the text passes cannot read: a binary or
            # media file passes only by provenance — its sha256 must be
            # approved in tools/phi_allowlist.txt. The digest (not the
            # content) is echoed so approving a legitimate file is one
            # copy-paste with a provenance comment.
            digest = hashlib.sha256(raw).hexdigest()
            if digest not in approved_binaries:
                all_findings.append(
                    f"{display}: unapproved binary/media file (sha256:{digest}) — the "
                    "scanner cannot inspect it; add a provenance comment + "
                    "'sha256:<hex>' entry to tools/phi_allowlist.txt to approve"
                )
            continue
        text = raw.decode("utf-8", errors="ignore")
        all_findings.extend(scan_text(display, text, deny, allow))

    if all_findings:
        print("PHI scan FAILED:", file=sys.stderr)
        for finding in all_findings:
            print(f"  {finding}", file=sys.stderr)
        print(
            f"\n{len(all_findings)} finding(s). If a match is a false positive, add the "
            "literal value to tools/phi_allowlist.txt with a justification comment.",
            file=sys.stderr,
        )
        return 1
    print("PHI scan clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
