"""A pinned floor on ``tests/``'s regression-guard count.

Independent of ``prose_gate.py`` on purpose — its own regex, its own walk of
``tests/**/*.py`` — so the two floors cannot be moved by one edit to a shared
pattern. ``tools/guard_baseline.txt`` (one integer) is also a SEPARATE file
from ``prose_gate.py``'s own baseline: lowering it takes a deliberate line in
a diff, never a side effect of a routine ``prose_gate.py --write-baseline``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE = Path(__file__).resolve().parent / "guard_baseline.txt"
TESTS_DIR = REPO_ROOT / "tests"

#: 2-4 digits with a trailing word boundary: excludes both a bare ordinal
#: ("sample #1") and a hex colour ("#701a14", "#171310") — a boundary can
#: never fall between two digits, so a longer digit or hex run never matches
#: at any backtracked length.
ISSUE_REF_RE = re.compile(r"#(\d{2,4})\b")


def count_guard_refs() -> int:
    refs: set[str] = set()
    for path in sorted(TESTS_DIR.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        refs.update(m.group(1) for m in ISSUE_REF_RE.finditer(text))
    return len(refs)


def main(argv: list[str] | None = None) -> int:
    del argv  # no arguments; kept for the same main(argv) shape as the other gates
    if not BASELINE.is_file():
        print(f"guard_count: no baseline at {BASELINE}")
        return 1
    floor = int(BASELINE.read_text(encoding="utf-8").strip())
    current = count_guard_refs()
    if current < floor:
        print(f"guard_count: DROPPED — was {floor}, now {current} (see {BASELINE.name})")
        return 1
    print(f"guard_count: {current} (floor {floor})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
