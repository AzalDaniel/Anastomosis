#!/usr/bin/env python3
"""PHI scanner: blocks protected health information from entering this repo.

Anastomosis was generalized from a private production system that handled
real patient data. This scanner enforces the project's first rule:
**no real PHI ever enters this repository** — not in code, not in docs,
not in fixtures, not in git history.

Two complementary mechanisms:

1. Hashed deny-list (``tools/phi_hashes.json``): SHA-256 hashes of tokens
   known to be PHI from the private predecessor (patient/provider names,
   real GUIDs). The plaintext never appears here; the hashes were generated
   locally against the private repo. Any token in a scanned file whose
   hash appears in the deny-list fails the scan.

2. Generic patterns: SSN-shaped strings, GUIDs outside the synthetic
   fixture prefixes, phone numbers outside the 555 exchange, and dates
   adjacent to DOB markers.

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
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HASHES = Path(__file__).resolve().parent / "phi_hashes.json"
ALLOWLIST = Path(__file__).resolve().parent / "phi_allowlist.txt"

# Files the scanner never inspects.
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".pyc"}
SKIP_NAMES = {"phi_hashes.json"}

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
    if not ALLOWLIST.exists():
        return set()
    lines = ALLOWLIST.read_text(encoding="utf-8").splitlines()
    return {ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")}


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


def iter_target_files(args_paths: list[str]) -> list[Path]:
    if args_paths:
        return [Path(p) for p in args_paths if Path(p).is_file()]
    out = subprocess.run(
        ["git", "ls-files"],  # noqa: S607 — fixed argv, repo-local
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [REPO_ROOT / line for line in out.stdout.splitlines() if line]


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

    all_findings: list[str] = []
    for path in iter_target_files(args.paths):
        if path.suffix.lower() in SKIP_SUFFIXES or path.name in SKIP_NAMES:
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:8192]:  # binary
            continue
        text = raw.decode("utf-8", errors="ignore")
        all_findings.extend(
            scan_text(
                path.relative_to(REPO_ROOT)
                if path.is_absolute() and path.is_relative_to(REPO_ROOT)
                else path,
                text,
                deny,
                allow,
            )
        )

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
