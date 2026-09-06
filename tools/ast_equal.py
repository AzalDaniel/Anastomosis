"""Prove a change is comment/docstring-only: the CODE ast did not move.

Strips every docstring (module/class/function, its ``Expr(Constant(str))``
first statement) from both sides, then compares ``ast.dump(...,
include_attributes=False)`` per file — a rename or a moved branch differs, a
reworded comment or docstring never does. Each side is a directory (its
``src/``) or a git ref, told apart by ``Path(value).is_dir()``. A ``.py`` file
on only one side is a difference too. Not part of ``tools/check.sh`` — run
once, deliberately, on the prose-sweep PR this proves touched no logic.

    python tools/ast_equal.py <a> <b> [--src src]
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: AST node kinds whose first statement, if a bare string-constant
#: expression, is a docstring per the language's own rule (``ast.get_docstring``
#: recognizes exactly these four).
_DOCSTRING_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _is_docstring_expr(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def strip_docstrings(tree: ast.AST) -> None:
    """Drop the leading docstring statement from every scope in ``tree``, in
    place — reassigning ``body`` is all a later ``ast.dump`` needs to see it."""
    for node in ast.walk(tree):
        if isinstance(node, _DOCSTRING_OWNERS) and node.body and _is_docstring_expr(node.body[0]):
            node.body = node.body[1:]


def stripped_dump(relpath: str, source: str) -> str:
    """The docstring-free AST dump of ``source``, or a sentinel string for a
    file that will not parse — a syntax error is a difference from anything
    that DOES parse, and two files failing to parse for different reasons
    still compare as different (their messages differ)."""
    try:
        tree = ast.parse(source, filename=relpath)
    except SyntaxError as exc:
        return f"<unparseable: {exc}>"
    strip_docstrings(tree)
    return ast.dump(tree, include_attributes=False)


def _tree_files(root: Path, subdir: str) -> dict[str, str]:
    """Every ``.py`` file under ``root/subdir``, keyed by its path relative to
    ``root`` (POSIX separators, so a tree and a git ref compare by the same
    keys on every platform)."""
    base = root / subdir
    return {
        p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(base.rglob("*.py"))
    }


def _git_show(ref: str, relpath: str) -> str:
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "show", f"{ref}:{relpath}"],  # noqa: S607 - git is the whole point here
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def _git_files(ref: str, subdir: str) -> dict[str, str]:
    """Every ``.py`` blob ``git`` holds for ``ref`` under ``subdir``, by path."""
    listing = subprocess.run(  # noqa: S603
        ["git", "ls-tree", "-r", "--name-only", ref, "--", subdir],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return {
        relpath: _git_show(ref, relpath)
        for relpath in listing.stdout.splitlines()
        if relpath.endswith(".py")
    }


def resolve(value: str, subdir: str) -> dict[str, str]:
    """``value`` as a source tree: a directory's files if it IS one, else a
    git ref resolved through ``git show``/``git ls-tree``."""
    path = Path(value)
    if path.is_dir():
        return _tree_files(path, subdir)
    return _git_files(value, subdir)


def compare(files_a: dict[str, str], files_b: dict[str, str]) -> list[str]:
    """Every path whose code AST differs between the two sides, as a
    human-readable line each — empty when every file's structure agrees."""
    differences: list[str] = []
    for relpath in sorted(set(files_a) | set(files_b)):
        in_a, in_b = relpath in files_a, relpath in files_b
        if not in_a:
            differences.append(f"{relpath}: only on the second side")
        elif not in_b:
            differences.append(f"{relpath}: only on the first side")
        elif stripped_dump(relpath, files_a[relpath]) != stripped_dump(relpath, files_b[relpath]):
            differences.append(f"{relpath}: code AST differs")
    return differences


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("a", help="First tree (a directory) or git ref")
    parser.add_argument("b", help="Second tree (a directory) or git ref")
    parser.add_argument(
        "--src", default="src", help="Subdirectory to compare, relative to each side (default: src)"
    )
    args = parser.parse_args(argv)

    files_a = resolve(args.a, args.src)
    files_b = resolve(args.b, args.src)
    if not files_a or not files_b:
        # Both arguments are repository roots; ``<root>/src`` is the default
        # target. An empty side is a wrong path, never a pass.
        print(f"ast_equal: FAILED (no .py files under {args.src!r} on one side; pass repo roots)")
        return 2
    differences = compare(files_a, files_b)

    for line in differences:
        print(f"ast_equal: {line}")
    if differences:
        print(f"ast_equal: FAILED ({len(differences)} file(s) with a code-AST difference)")
        return 1
    print(f"ast_equal: PASSED ({len(set(files_a) | set(files_b))} file(s), code AST identical)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
