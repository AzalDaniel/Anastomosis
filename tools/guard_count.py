"""A pinned floor on ``tests/``'s regression-guard count, independent of
``prose_gate.py --write-baseline``.

``prose_gate.py`` already fails a commit that drops the count of distinct
``#NNN`` references under ``tests/`` below its OWN baseline — but that
baseline is regenerated wholesale by ``--write-baseline``, which would
silently accept a drop as the new normal. ``tools/guard_baseline.txt`` (one
integer) is a second, separately-edited floor: lowering it is a deliberate
line in a diff, never a side effect of a routine baseline regeneration.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from prose_gate import measure

BASELINE = Path(__file__).resolve().parent / "guard_baseline.txt"


def main(argv: list[str] | None = None) -> int:
    del argv  # no arguments; kept for the same main(argv) shape as the other gates
    if not BASELINE.is_file():
        print(f"guard_count: no baseline at {BASELINE}")
        return 1
    floor = int(BASELINE.read_text(encoding="utf-8").strip())
    current = int(measure()["guard_count"])  # type: ignore[arg-type]
    if current < floor:
        print(f"guard_count: DROPPED — was {floor}, now {current} (see {BASELINE.name})")
        return 1
    print(f"guard_count: {current} (floor {floor})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
