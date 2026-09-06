"""Tests for tools.prose_gate — the comment ratchet."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tools.prose_gate import NEW_FILE_RATIO_LIMIT, analyze_file, compare, main, measure

# Exactly 12 physical lines: opening quote, 10 body lines, closing quote.
_TWELVE_LINE_DOCSTRING = '"""\n' + "\n".join(f"Line {i}." for i in range(1, 11)) + '\n"""\n'


def _write_tree(root: Path) -> None:
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "pkg" / "mod.py").write_text(
        f"{_TWELVE_LINE_DOCSTRING}\n"
        "def add(a, b):\n"
        '    """This was previously computed differently."""\n'
        "    return a + b\n",
        encoding="utf-8",
    )
    (root / "tests").mkdir()
    (root / "tests" / "test_mod.py").write_text(
        '"""Guards regression #123."""\n\ndef test_add():\n    assert 1 + 1 == 2\n',
        encoding="utf-8",
    )


def test_analyze_file_reports_the_over_long_docstring_and_the_history_phrase(
    tmp_path: Path,
) -> None:
    _write_tree(tmp_path)
    findings = analyze_file(tmp_path / "src" / "pkg" / "mod.py", tmp_path)
    assert any(o["length"] == 12 and o["scope"] == "module" for o in findings.over_long)
    assert any("previously" in h["text"] for h in findings.history_hits)


def test_measure_computes_a_known_ratio_and_guard_count(tmp_path: Path) -> None:
    _write_tree(tmp_path)
    current = measure(tmp_path)
    assert current["files"]["tests/test_mod.py"]["ratio"] > 0
    assert current["guard_count"] == 1
    assert len(current["over_long_docstrings"]) == 1
    assert len(current["history_hits"]) == 1


def test_gate_fails_when_guard_count_drops(tmp_path: Path) -> None:
    _write_tree(tmp_path)
    baseline = measure(tmp_path)

    # The regression-guard comment is deleted: the guard count drops to 0.
    (tmp_path / "tests" / "test_mod.py").write_text(
        "def test_add():\n    assert 1 + 1 == 2\n", encoding="utf-8"
    )
    current = measure(tmp_path)
    failures, _ = compare(current, baseline)
    assert any("DROPPED" in f for f in failures)


def test_gate_fails_on_a_new_over_long_docstring_or_history_phrase(tmp_path: Path) -> None:
    _write_tree(tmp_path)
    baseline = measure(tmp_path)

    # A brand-new file with its own over-long docstring and history phrase.
    (tmp_path / "src" / "pkg" / "extra.py").write_text(
        f"{_TWELVE_LINE_DOCSTRING}\n"
        "def f():\n"
        '    """We changed this to a longer contract than it used to be here."""\n'
        "    return 1\n",
        encoding="utf-8",
    )
    current = measure(tmp_path)
    failures, _ = compare(current, baseline)
    assert any("NEW over-long docstring" in f and "extra.py" in f for f in failures)
    assert any("NEW history phrase" in f and "extra.py" in f for f in failures)


def test_gate_fails_when_a_files_ratio_rises_above_baseline(tmp_path: Path) -> None:
    _write_tree(tmp_path)
    baseline = measure(tmp_path)

    # Pile on comments (no new code) so this file's OWN ratio rises.
    mod = tmp_path / "src" / "pkg" / "mod.py"
    mod.write_text("# a\n# b\n# c\n# d\n# e\n" + mod.read_text(encoding="utf-8"), encoding="utf-8")
    current = measure(tmp_path)
    failures, _ = compare(current, baseline)
    assert any("file prose grew" in f and "mod.py" in f for f in failures)


def test_gate_passes_and_counts_a_file_as_improved(tmp_path: Path) -> None:
    _write_tree(tmp_path)
    baseline = measure(tmp_path)
    current = measure(tmp_path)  # unchanged
    failures, improved = compare(current, baseline)
    assert failures == []
    assert improved == 0


def test_a_new_file_over_the_absolute_ratio_cap_fails_with_no_baseline_entry(
    tmp_path: Path,
) -> None:
    _write_tree(tmp_path)
    baseline = measure(tmp_path)

    # No over-long docstring, no history phrase — ratio alone is over the cap.
    (tmp_path / "src" / "pkg" / "commenty.py").write_text(
        "# one\n# two\n# three\n# four\nx = 1\n", encoding="utf-8"
    )
    current = measure(tmp_path)
    assert current["files"]["src/pkg/commenty.py"]["ratio"] > NEW_FILE_RATIO_LIMIT
    failures, _ = compare(current, baseline)
    assert any(
        "new file exceeds the absolute prose cap" in f and "commenty.py" in f for f in failures
    )


def test_a_new_file_within_the_cap_and_clean_passes(tmp_path: Path) -> None:
    _write_tree(tmp_path)
    baseline = measure(tmp_path)

    (tmp_path / "src" / "pkg" / "lean.py").write_text(
        "def g():\n    return 1\n\n\ndef h():\n    return 2\n", encoding="utf-8"
    )
    current = measure(tmp_path)
    failures, _ = compare(current, baseline)
    assert failures == []


def test_gate_fails_when_an_existing_over_long_docstring_grows(tmp_path: Path) -> None:
    _write_tree(tmp_path)
    baseline = measure(tmp_path)

    # Grow the SAME docstring (still starts at line 1) by two more lines —
    # a location match with a bigger length, not a new location.
    mod = tmp_path / "src" / "pkg" / "mod.py"
    grown = mod.read_text(encoding="utf-8").replace(
        "Line 10.\n", "Line 10.\nLine 11.\nLine 12.\n", 1
    )
    mod.write_text(grown, encoding="utf-8")
    current = measure(tmp_path)
    failures, _ = compare(current, baseline)
    assert any("over-long docstring GREW" in f and "mod.py" in f for f in failures)


def test_write_baseline_refuses_new_violations_without_the_escape_hatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_tree(tmp_path)
    baseline_path = tmp_path / "baseline.json"
    import tools.prose_gate as prose_gate

    monkeypatch.setattr(prose_gate, "BASELINE", baseline_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(prose_gate, "REPO_ROOT", tmp_path)

    assert main(["--write-baseline"]) == 0
    original = json.loads(baseline_path.read_text(encoding="utf-8"))

    # Introduce a new history phrase without --allow-new-violations.
    (tmp_path / "tests" / "test_mod.py").write_text(
        '"""Guards regression #123. This was previously flaky."""\n\n'
        "def test_add():\n    assert 1 + 1 == 2\n",
        encoding="utf-8",
    )
    capsys.readouterr()
    assert main(["--write-baseline"]) == 1
    assert "refusing" in capsys.readouterr().out
    assert json.loads(baseline_path.read_text(encoding="utf-8")) == original

    assert main(["--write-baseline", "--allow-new-violations"]) == 0
    assert json.loads(baseline_path.read_text(encoding="utf-8")) != original


def test_an_over_long_docstring_that_only_moved_is_not_new(tmp_path: Path) -> None:
    _write_tree(tmp_path)
    mod = tmp_path / "src" / "pkg" / "mod.py"
    mod.write_text(
        f"{_TWELVE_LINE_DOCSTRING}\n"
        "def add(a, b):\n"
        '    """Adds."""\n'
        "    return a + b\n"
        "\n"
        "def sub(a, b):\n"
        '    """One.\n    Two.\n    Three.\n    Four.\n    Five.\n    Six.\n    """\n'
        "    return a - b\n",
        encoding="utf-8",
    )
    baseline = measure(tmp_path)
    # The module docstring shrinks to one line: ``sub``'s over-long docstring
    # keeps its length but lands eleven lines earlier.
    mod.write_text(
        '"""Short."""\n\n'
        "def add(a, b):\n"
        '    """Adds."""\n'
        "    return a + b\n"
        "\n"
        "def sub(a, b):\n"
        '    """One.\n    Two.\n    Three.\n    Four.\n    Five.\n    Six.\n    """\n'
        "    return a - b\n",
        encoding="utf-8",
    )
    failures, improved = compare(measure(tmp_path), baseline)
    assert failures == []
    assert improved == 1


def test_a_baseline_without_owner_names_still_matches_by_line(tmp_path: Path) -> None:
    _write_tree(tmp_path)
    baseline = measure(tmp_path)
    for entry in baseline["over_long_docstrings"]:  # type: ignore[union-attr]
        del entry["name"]
    failures, _ = compare(measure(tmp_path), baseline)
    assert failures == []


def test_a_history_phrase_that_only_moved_is_not_new(tmp_path: Path) -> None:
    _write_tree(tmp_path)
    baseline = measure(tmp_path)
    mod = tmp_path / "src" / "pkg" / "mod.py"
    mod.write_text(
        '"""Short."""\n\n'
        "def add(a, b):\n"
        '    """This was previously computed differently."""\n'
        "    return a + b\n",
        encoding="utf-8",
    )
    failures, _ = compare(measure(tmp_path), baseline)
    assert failures == []


def test_deleting_code_from_a_file_never_fails_even_though_its_ratio_rises() -> None:
    _write_tree(tmp := Path(__import__("tempfile").mkdtemp()))
    baseline = measure(tmp)
    mod = tmp / "src" / "pkg" / "mod.py"
    mod.write_text(
        mod.read_text(encoding="utf-8") + "\n\ndef sub(a, b):\n    return a - b\n", encoding="utf-8"
    )
    baseline = measure(tmp)
    mod.write_text(
        mod.read_text(encoding="utf-8").replace("\n\ndef sub(a, b):\n    return a - b\n", ""),
        encoding="utf-8",
    )
    current = measure(tmp)
    assert float(current["files"]["src/pkg/mod.py"]["ratio"]) > float(
        baseline["files"]["src/pkg/mod.py"]["ratio"]
    )  # type: ignore[index]
    failures, _ = compare(current, baseline)
    assert failures == []
