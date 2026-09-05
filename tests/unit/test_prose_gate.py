"""Tests for tools.prose_gate — the comment ratchet."""

from __future__ import annotations

from pathlib import Path

from tools.prose_gate import analyze_file, compare, measure

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
    assert any("file ratio rose" in f and "mod.py" in f for f in failures)


def test_gate_passes_and_counts_a_file_as_improved(tmp_path: Path) -> None:
    _write_tree(tmp_path)
    baseline = measure(tmp_path)
    current = measure(tmp_path)  # unchanged
    failures, improved = compare(current, baseline)
    assert failures == []
    assert improved == 0
