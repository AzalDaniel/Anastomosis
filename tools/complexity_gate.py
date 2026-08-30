"""The complexity ratchet: existing debt may shrink, but never grow.

An external audit measured the mapping and rendering hotspots — the exact code
that lossless attribution and clinical rendering keep having to change — at
E-rank complexity, with no gate anywhere. A plain threshold cannot be turned on
over a codebase that already exceeds it: it either fails every commit or gets
set so loose it guards nothing. So this gate compares against a checked-in
BASELINE instead. What was already complex stays allowed, exactly as complex as
it was; anything new, or anything that got worse, fails the build.

The rules, applied to radon's cyclomatic-complexity report over ``src``:

* the baseline holds only the VIOLATIONS — blocks over rank B, module
  averages over rank A. Healthy code is not in it and stays free to move
  anywhere under those ceilings; a gate that fails an A-rank function for
  gaining one branch is a gate people learn to bypass;
* a violating block absent from the baseline fails — new code is born simple,
  or it does not merge;
* a baselined violation may not worsen, in letter rank or raw CC. Adding one
  branch to an E/38 function makes it E/39 and fails, which is the point: the
  burn-down cannot lose ground quietly;
* the same rules for per-module averages, at rank A;
* improvement never fails, and when the working tree is strictly better than
  the baseline the gate says so and asks for a regeneration, so the ratchet
  actually tightens instead of remembering debt that no longer exists.

A RENAMED complex function reads as a new block and fails. Deliberate: the
rename must regenerate the baseline in the same commit, so the debt's paper
trail follows the code instead of silently re-attaching under a new name.

Regenerate with ``python tools/complexity_gate.py --write-baseline`` — in a
commit that either reduces debt or consciously documents why it moved.

PHI: the report contains file paths, function names, and integers. Nothing
patient-derived can appear here.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE = Path(__file__).resolve().parent / "complexity_baseline.json"
SRC = "src"

#: Ranks in worsening order, so comparisons are index comparisons.
RANKS = "ABCDEF"

#: A block NOT in the baseline must rank B or better.
NEW_BLOCK_LIMIT = "B"
#: A module average NOT in the baseline must rank A.
NEW_MODULE_LIMIT = "A"


def _radon_json() -> dict[str, list[dict[str, object]]]:
    """Radon's cyclomatic-complexity report for ``src``, as parsed JSON.

    A subprocess rather than radon's API: the command line is what the issue
    pins, what a human reruns, and what stays comparable across environments.
    """
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "radon", "cc", "-j", SRC],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    if proc.returncode != 0:
        # Echo radon's own stderr: "No module named radon" is a one-line
        # diagnosis, and a swallowed CalledProcessError already cost one CI
        # round to un-swallow.
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"complexity gate: radon exited {proc.returncode}")
    return json.loads(proc.stdout)


def _rank(cc: float) -> str:
    """Radon's CC-to-rank mapping, restated so the gate needs only the number.

    Takes a float so module AVERAGES rank the way xenon ranks them: 5.2 is
    over the A bound, not rounded back under it.
    """
    for bound, letter in ((5, "A"), (10, "B"), (20, "C"), (30, "D"), (40, "E")):
        if cc <= bound:
            return letter
    return "F"


def measure(report: dict[str, list[dict[str, object]]]) -> dict[str, object]:
    """Collapse a radon report into the ratchet's two violation tables.

    Only blocks over rank ``B`` and modules over rank ``A`` are kept — the
    gate governs debt, not health. Blocks are keyed ``path::Class.method``
    (or ``path::function``). Two same-named blocks in one file — a nested
    helper shadowing, say — collapse to the WORST of them, which can only
    make the gate stricter, never blinder.
    """
    blocks: dict[str, dict[str, object]] = {}
    modules: dict[str, dict[str, object]] = {}
    for raw_path, entries in sorted(report.items()):
        # Radon reports OS-native separators; the baseline is written once and
        # read on every platform, so keys are normalized to forward slashes —
        # a Windows leg comparing src\a\b.py against src/a/b.py saw the whole
        # baseline as missing and every standing violation as new (93 phantom
        # regressions).
        path = raw_path.replace("\\", "/")
        total = 0
        count = 0
        for entry in entries:
            if "complexity" not in entry:  # a parse error entry, not a block
                continue
            cc = int(entry["complexity"])  # type: ignore[arg-type]
            total += cc
            count += 1
            name = str(entry.get("name", "?"))
            classname = entry.get("classname")
            qual = f"{classname}.{name}" if classname else name
            key = f"{path}::{qual}"
            known = blocks.get(key)
            if _worse(_rank(cc), NEW_BLOCK_LIMIT) and (
                known is None or cc > int(known["cc"])  # type: ignore[arg-type]
            ):
                blocks[key] = {"rank": _rank(cc), "cc": cc}
        if count:
            avg = total / count
            if _worse(_rank(avg), NEW_MODULE_LIMIT):
                modules[path] = {"rank": _rank(avg), "avg": round(avg, 4)}
    return {"blocks": blocks, "modules": modules}


def _worse(rank: str, than: str) -> bool:
    return RANKS.index(rank) > RANKS.index(than)


def compare(current: dict[str, object], baseline: dict[str, object]) -> tuple[list[str], int]:
    """Every way the working tree is worse than the baseline, plus how many
    baseline entries improved (the burn-down worth announcing)."""
    failures: list[str] = []
    improved = 0

    base_blocks: dict[str, dict[str, object]] = baseline["blocks"]  # type: ignore[assignment]
    for key, now in sorted(current["blocks"].items()):  # type: ignore[union-attr]
        was = base_blocks.get(key)
        rank, cc = str(now["rank"]), int(now["cc"])
        if was is None:
            failures.append(
                f"NEW block over rank {NEW_BLOCK_LIMIT}: {key} is {rank}/{cc}. "
                "New code is born simple; split it before it merges."
            )
            continue
        was_rank, was_cc = str(was["rank"]), int(was["cc"])
        if _worse(rank, was_rank) or (rank == was_rank and cc > was_cc):
            failures.append(
                f"WORSENED block: {key} was {was_rank}/{was_cc}, now {rank}/{cc}. "
                "The ratchet only turns one way — simplify, or regenerate the "
                "baseline in this commit and say why in its message."
            )
        elif cc < was_cc:
            improved += 1

    base_modules: dict[str, dict[str, object]] = baseline["modules"]  # type: ignore[assignment]
    for path, now in sorted(current["modules"].items()):  # type: ignore[union-attr]
        was = base_modules.get(path)
        rank, avg = str(now["rank"]), float(now["avg"])
        if was is None:
            failures.append(
                f"NEW module over rank {NEW_MODULE_LIMIT}: {path} averages {rank}/{avg}"
            )
            continue
        was_rank, was_avg = str(was["rank"]), float(was["avg"])
        if _worse(rank, was_rank) or (rank == was_rank and avg > was_avg + 1e-9):
            failures.append(
                f"WORSENED module average: {path} was {was_rank}/{was_avg}, now {rank}/{avg}"
            )
        elif avg < was_avg - 1e-9:
            improved += 1

    return failures, improved


def _stale(current: dict[str, object], baseline: dict[str, object]) -> int:
    """Baseline entries the tree no longer has (deleted or already improved out
    of relevance) — debt the baseline remembers that no longer exists."""
    gone = sum(1 for k in baseline["blocks"] if k not in current["blocks"])  # type: ignore[union-attr,operator]
    gone += sum(1 for k in baseline["modules"] if k not in current["modules"])  # type: ignore[union-attr,operator]
    return gone


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="Regenerate tools/complexity_baseline.json from the working tree.",
    )
    args = parser.parse_args(argv)

    current = measure(_radon_json())

    if args.write_baseline:
        payload = {
            "_comment": (
                "Complexity ratchet baseline - see tools/complexity_gate.py. "
                "Regenerate ONLY in a commit that reduces debt or documents why it moved."
            ),
            "tool": "radon cc -j src",
            **current,
        }
        BASELINE.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        blocks = current["blocks"]
        over = sum(1 for v in blocks.values() if _worse(str(v["rank"]), NEW_BLOCK_LIMIT))  # type: ignore[union-attr]
        print(f"baseline written: {len(blocks)} block(s), {over} over rank {NEW_BLOCK_LIMIT}")  # type: ignore[arg-type]
        return 0

    if not BASELINE.is_file():
        print(f"complexity gate: no baseline at {BASELINE} — run with --write-baseline first")
        return 1

    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    failures, improved = compare(current, baseline)
    for failure in failures:
        print(f"complexity gate: {failure}")
    if failures:
        print(f"complexity gate: FAILED ({len(failures)} regression(s))")
        return 1

    freed = _stale(current, baseline)
    if improved or freed:
        print(
            f"complexity gate: PASSED — and {improved + freed} baseline entr(y/ies) improved. "
            "Tighten the ratchet: python tools/complexity_gate.py --write-baseline"
        )
    else:
        print("complexity gate: PASSED (no new or worsened complexity)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
