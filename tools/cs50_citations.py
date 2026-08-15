#!/usr/bin/env python3
"""Re-applies per-file AI-assistance citations for an academic-submission branch.

The product repo carries attribution in DESIGN.md instead: one place to read,
one place to keep accurate. Some academic submissions, however, ask for the
disclosure to appear in the comments of each authored file. This script
re-applies that per-file form mechanically so the submission branch and the
product branch differ only in comment headers.

Usage:
    python tools/cs50_citations.py              # apply to src/tests/tools/packaging
    python tools/cs50_citations.py --check      # report what is missing, change nothing
    python tools/cs50_citations.py src/anastomosis/cli.py   # explicit paths

Exit status: 0 nothing left to do, 1 files are missing the citation
(``--check`` only), 2 usage error.

The script is idempotent: a file that already carries the citation is left
byte-identical, so it is safe to re-run after every rebase.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The trees whose authored files carry the citation. Generated and vendored
# content is excluded below; everything else here is authored for this repo.
DEFAULT_ROOTS: tuple[str, ...] = ("src", "tests", "tools", "packaging")

# The sentence itself. It names no vendor and no model: tools change, the
# disclosure does not, and a stale vendor name would be worse than none.
# LENGTH INVARIANT: every rendered form must fit the repo's 100-column limit,
# or the stamped tree fails `ruff check`/`ruff format --check` on line length.
# `_MAX_LINE` pins that; `test_cs50_citations.py` proves it for every style.
CITATION = "AI-assistance: written in part with AI tools under the author's direction and review."

_MAX_LINE = 100

# A short, stable substring used to decide "already cited". Matching on this
# rather than the whole rendered line keeps the check working if the sentence
# is ever reworded, so a reword never double-stamps a file.
MARKER = "AI-assistance:"

# How each file kind spells a comment. The tuple is (prefix, suffix).
COMMENT_STYLES: dict[str, tuple[str, str]] = {
    ".py": ("# ", ""),
    ".js": ("// ", ""),
    ".css": ("/* ", " */"),
    ".html": ("<!-- ", " -->"),
}

# Path components that mark content this project did not author: third-party
# vendored trees and interpreter/tool caches.
SKIP_COMPONENTS = frozenset({"vendor", "__pycache__", "node_modules", ".git"})

# Lines that must stay first in their file. The citation is inserted directly
# after a run of these, never above one.
_PY_PROLOGUE_PREFIXES = ("#!", "# -*- coding", "# coding")


def _is_candidate(path: Path) -> bool:
    """True for an authored file of a kind that can carry a comment header."""
    if path.suffix not in COMMENT_STYLES:
        return False
    return not SKIP_COMPONENTS.intersection(path.parts)


def _iter_files(roots: list[Path]) -> list[Path]:
    """Every candidate file under ``roots``, sorted for deterministic output.

    An explicitly named file is honored even if it is not under one of the
    default roots; a named directory is walked.
    """
    found: set[Path] = set()
    for root in roots:
        if root.is_file():
            if _is_candidate(root):
                found.add(root)
            continue
        for path in root.rglob("*"):
            if path.is_file() and _is_candidate(path):
                found.add(path)
    return sorted(found)


def _citation_line(suffix: str) -> str:
    prefix, closer = COMMENT_STYLES[suffix]
    return f"{prefix}{CITATION}{closer}"


def _insert_at(lines: list[str], suffix: str) -> int:
    """The index the citation line belongs at, given a file's existing lines.

    Two constructs must keep their position: a Python shebang/encoding
    prologue, and an HTML doctype. Everything else takes the citation on
    line 1 — including a Python module docstring, which stays the module's
    first *statement* with a comment above it.
    """
    if suffix == ".py":
        index = 0
        while index < len(lines) and lines[index].startswith(_PY_PROLOGUE_PREFIXES):
            index += 1
        return index
    if suffix == ".html" and lines and lines[0].lstrip().lower().startswith("<!doctype"):
        return 1
    return 0


def _apply(path: Path) -> bool:
    """Insert the citation into ``path`` unless it is already there.

    Returns True if the file was (or, in check mode, would be) modified.
    """
    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        return False
    lines = text.splitlines(keepends=True)
    index = _insert_at(lines, path.suffix)
    lines.insert(index, _citation_line(path.suffix) + "\n")
    path.write_text("".join(lines), encoding="utf-8")
    return True


def _needs_citation(path: Path) -> bool:
    return MARKER not in path.read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help=f"files or directories to process (default: {', '.join(DEFAULT_ROOTS)})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report files missing the citation and exit 1; write nothing",
    )
    args = parser.parse_args(argv)

    roots = args.paths or [REPO_ROOT / name for name in DEFAULT_ROOTS]
    roots = [root for root in roots if root.exists()]
    if not roots:
        sys.stderr.write("cs50_citations: no existing paths to process\n")
        return 2

    files = _iter_files(roots)
    if args.check:
        missing = [path for path in files if _needs_citation(path)]
        for path in missing:
            sys.stdout.write(f"missing citation: {path}\n")
        sys.stdout.write(f"cs50_citations: {len(missing)} of {len(files)} files missing\n")
        return 1 if missing else 0

    changed = [path for path in files if _apply(path)]
    sys.stdout.write(f"cs50_citations: cited {len(changed)} of {len(files)} files\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
