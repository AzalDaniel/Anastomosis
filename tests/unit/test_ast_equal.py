"""Tests for tools.ast_equal — the mechanical proof a change is prose-only."""

from __future__ import annotations

from pathlib import Path

import pytest
from tools.ast_equal import compare, main, resolve, strip_docstrings, stripped_dump

_MODULE_DOCSTRING = '"""A module about widgets."""\n\n'
_FUNCTION = 'def add(a, b):\n    """Add two numbers."""\n    return a + b\n'


def _write_tree(root: Path, *, module_doc: str, function_body: str) -> None:
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "pkg" / "mod.py").write_text(module_doc + function_body, encoding="utf-8")


def test_strip_docstrings_drops_module_class_and_function_docstrings() -> None:
    import ast

    source = (
        '"""Module doc."""\n\n'
        "class Widget:\n"
        '    """Class doc."""\n\n'
        "    def spin(self):\n"
        '        """Function doc."""\n'
        "        return 1\n"
    )
    tree = ast.parse(source)
    strip_docstrings(tree)
    dumped = ast.dump(tree, include_attributes=False)
    assert "Module doc" not in dumped
    assert "Class doc" not in dumped
    assert "Function doc" not in dumped
    assert "return 1" in dumped or "Constant(value=1)" in dumped


def test_same_code_different_docstrings_is_equal(tmp_path: Path) -> None:
    """A prose-only rewrite — the whole point of the tool."""
    tree_a = tmp_path / "a"
    tree_b = tmp_path / "b"
    _write_tree(tree_a, module_doc=_MODULE_DOCSTRING, function_body=_FUNCTION)
    _write_tree(
        tree_b,
        module_doc='"""A rewritten module about the same widgets, at length."""\n\n',
        function_body=_FUNCTION.replace("Add two numbers.", "Sum a and b together."),
    )
    differences = compare(resolve(str(tree_a), "src"), resolve(str(tree_b), "src"))
    assert differences == []


def test_one_changed_token_is_not_equal(tmp_path: Path) -> None:
    tree_a = tmp_path / "a"
    tree_b = tmp_path / "b"
    _write_tree(tree_a, module_doc=_MODULE_DOCSTRING, function_body=_FUNCTION)
    subtracted = _FUNCTION.replace("a + b", "a - b")
    _write_tree(tree_b, module_doc=_MODULE_DOCSTRING, function_body=subtracted)
    differences = compare(resolve(str(tree_a), "src"), resolve(str(tree_b), "src"))
    assert len(differences) == 1
    assert "pkg/mod.py" in differences[0]
    assert "code AST differs" in differences[0]


def test_a_file_only_on_one_side_is_a_difference(tmp_path: Path) -> None:
    tree_a = tmp_path / "a"
    tree_b = tmp_path / "b"
    _write_tree(tree_a, module_doc=_MODULE_DOCSTRING, function_body=_FUNCTION)
    (tree_b / "src" / "pkg").mkdir(parents=True)
    (tree_b / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    differences = compare(resolve(str(tree_a), "src"), resolve(str(tree_b), "src"))
    assert any("mod.py" in d and "only on the first side" in d for d in differences)


def test_stripped_dump_of_unparseable_source_is_a_sentinel_not_a_crash() -> None:
    dumped = stripped_dump("broken.py", "def broken(:\n")
    assert "unparseable" in dumped


def test_main_exits_zero_when_equal(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tree_a = tmp_path / "a"
    tree_b = tmp_path / "b"
    _write_tree(tree_a, module_doc=_MODULE_DOCSTRING, function_body=_FUNCTION)
    _write_tree(tree_b, module_doc='"""Different words entirely."""\n\n', function_body=_FUNCTION)
    assert main([str(tree_a), str(tree_b)]) == 0
    assert "PASSED" in capsys.readouterr().out


def test_main_exits_one_when_code_differs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tree_a = tmp_path / "a"
    tree_b = tmp_path / "b"
    _write_tree(tree_a, module_doc=_MODULE_DOCSTRING, function_body=_FUNCTION)
    multiplied = _FUNCTION.replace("a + b", "a * b")
    _write_tree(tree_b, module_doc=_MODULE_DOCSTRING, function_body=multiplied)
    assert main([str(tree_a), str(tree_b)]) == 1
    assert "FAILED" in capsys.readouterr().out
