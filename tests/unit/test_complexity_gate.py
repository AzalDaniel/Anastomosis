"""The complexity ratchet's rules, held against synthetic radon reports.

The live half — the checked-in baseline actually matching HEAD — is asserted
too, because a baseline that drifts from the tree turns every unrelated PR red
and teaches people to regenerate it reflexively, which unratchets the ratchet.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tools.complexity_gate import compare, measure

REPO_ROOT = Path(__file__).resolve().parents[2]


def _report(*blocks: tuple[str, str, int | None, int]) -> dict[str, list[dict[str, object]]]:
    """A radon-shaped report from (path, name, classname-or-None, cc) tuples."""
    out: dict[str, list[dict[str, object]]] = {}
    for path, name, classname, cc in blocks:
        entry: dict[str, object] = {"name": name, "complexity": cc}
        if classname:
            entry["classname"] = classname
        out.setdefault(path, []).append(entry)
    return out


def test_healthy_code_is_not_in_the_violation_tables() -> None:
    """The gate governs debt, not health — an A-rank function gaining a branch
    must never appear, or every trivial edit fails the build."""
    # Two blocks under the ceiling whose AVERAGE also stays under A — the
    # module table has its own threshold and its own test below.
    measured = measure(_report(("a.py", "tidy", None, 3), ("a.py", "fine", None, 5)))
    assert measured["blocks"] == {}
    assert measured["modules"] == {}


def test_a_new_violation_fails_and_names_the_block() -> None:
    baseline = {"blocks": {}, "modules": {}}
    current = measure(_report(("a.py", "sprawl", "Mapper", 14)))
    failures, _ = compare(current, baseline)
    assert any("NEW block" in f and "a.py::Mapper.sprawl" in f for f in failures)
    # The module average trips its own threshold alongside — both are named.
    assert any("NEW module" in f and "a.py" in f for f in failures)


def test_a_worsened_violation_fails_even_within_its_rank() -> None:
    """E/38 -> E/39 is one branch and still a failure — the burn-down cannot
    lose ground quietly."""
    baseline = {"blocks": {"a.py::big": {"rank": "E", "cc": 38}}, "modules": {}}
    current = measure(_report(("a.py", "big", None, 39)))
    failures, _ = compare(current, baseline)
    assert any("WORSENED block" in f and "was E/38, now E/39" in f for f in failures)


def test_grandfathered_debt_at_its_exact_level_passes() -> None:
    baseline = {
        "blocks": {"a.py::big": {"rank": "E", "cc": 38}},
        "modules": {"a.py": {"rank": "E", "avg": 38.0}},
    }
    current = measure(_report(("a.py", "big", None, 38)))
    failures, improved = compare(current, baseline)
    assert failures == []
    assert improved == 0


def test_improvement_passes_and_is_counted_for_the_regen_prompt() -> None:
    baseline = {
        "blocks": {"a.py::big": {"rank": "E", "cc": 38}},
        "modules": {"a.py": {"rank": "E", "avg": 38.0}},
    }
    current = measure(_report(("a.py", "big", None, 25)))
    failures, improved = compare(current, baseline)
    assert failures == []
    assert improved == 2  # the block and its module average both burned down


def test_a_healed_block_may_not_quietly_regress_back() -> None:
    """Once a block improves under the ceiling and the baseline is regenerated,
    a later slide back over B reads as NEW and fails — got healthy, stays
    healthy."""
    baseline_after_regen = {"blocks": {}, "modules": {}}
    current = measure(_report(("a.py", "healed", None, 12)))
    failures, _ = compare(current, baseline_after_regen)
    assert any("NEW block" in f for f in failures)


def test_two_same_named_blocks_keep_the_worst() -> None:
    measured = measure(_report(("a.py", "twin", None, 12), ("a.py", "twin", None, 18)))
    assert measured["blocks"]["a.py::twin"] == {"rank": "C", "cc": 18}


def test_a_module_average_just_over_the_bound_is_ranked_like_xenon_ranks_it() -> None:
    """5.2 is over A. Rounding it back to 5 would grandfather debt xenon
    counts, and the two tools would disagree forever after."""
    measured = measure(_report(("a.py", "f1", None, 5), ("a.py", "f2", None, 6)))
    assert measured["modules"]["a.py"]["rank"] == "B"


def test_the_checked_in_baseline_matches_head() -> None:
    """The live self-test: the gate, run for real over src against the
    committed baseline, passes. A drifted baseline fails here first — in the
    PR that drifted it — instead of in every PR after."""
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "complexity_gate.py")],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_baseline_file_is_violations_only() -> None:
    """9 KB of debt is reviewable; 170 KB of every block in the tree is not —
    and a full-tree baseline is what made an A->A edit a build failure."""
    baseline = json.loads((REPO_ROOT / "tools" / "complexity_baseline.json").read_text())
    for entry in baseline["blocks"].values():
        assert entry["rank"] not in ("A", "B"), entry
    for entry in baseline["modules"].values():
        assert entry["rank"] != "A", entry
    # ``totals`` is the reference a deletion is judged against, one int per
    # module, never a violation.
    assert all(isinstance(v, int) for v in baseline["totals"].values())


def test_windows_paths_measure_to_the_same_keys_as_posix_paths() -> None:
    """Radon reports OS-native separators. The baseline is written once, on
    whichever OS regenerated it, and read on all four CI legs — so the keys
    must agree. The day they did not, every Windows leg saw the entire
    baseline as missing and reported 93 phantom regressions."""
    windows = measure(_report(("src\\pkg\\mod.py", "sprawl", "Mapper", 14)))
    posix = measure(_report(("src/pkg/mod.py", "sprawl", "Mapper", 14)))
    assert windows == posix
    assert set(windows["blocks"]) == {"src/pkg/mod.py::Mapper.sprawl"}
    assert set(windows["modules"]) == {"src/pkg/mod.py"}


def test_deleting_a_simple_block_never_fails_even_when_the_average_rises() -> None:
    # Three blocks at 6 and one at 1: average 4.75, rank A, total 19. Delete
    # the simple one: average 6.0, rank B, total 18. Less code, higher average.
    baseline = measure(
        _report(
            ("src/m.py", "a", None, 6),
            ("src/m.py", "b", None, 6),
            ("src/m.py", "c", None, 6),
            ("src/m.py", "d", None, 1),
        )
    )
    assert "src/m.py" not in baseline["modules"]  # type: ignore[operator]
    current = measure(
        _report(("src/m.py", "a", None, 6), ("src/m.py", "b", None, 6), ("src/m.py", "c", None, 6))
    )
    assert "src/m.py" in current["modules"]  # type: ignore[operator]
    failures, improved = compare(current, baseline)
    assert failures == []
    assert improved == 1


def test_adding_a_block_to_a_module_over_the_line_fails() -> None:
    baseline = measure(_report(("src/m.py", "a", None, 6), ("src/m.py", "b", None, 6)))
    current = measure(
        _report(("src/m.py", "a", None, 6), ("src/m.py", "b", None, 6), ("src/m.py", "c", None, 6))
    )
    failures, _ = compare(current, baseline)
    assert any("WORSENED module" in f and "12 to 18" in f for f in failures)


def test_a_baseline_without_totals_still_judges_a_known_module() -> None:
    baseline = measure(_report(("src/m.py", "a", None, 6), ("src/m.py", "b", None, 6)))
    del baseline["totals"]  # type: ignore[union-attr]
    current = measure(_report(("src/m.py", "a", None, 6), ("src/m.py", "b", None, 5)))
    failures, improved = compare(current, baseline)
    assert failures == []
    assert improved == 1
