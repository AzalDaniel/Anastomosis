"""Every path a person types reaches `Path` through the same cleaner.

Windows Explorer's "Copy as path" wraps the path in double quotes, which is the
ordinary way to get a path on Windows 11. `core.output.clean_typed_path` handles
that, and #131 applied it — to the Charts and Migrate console only. Uploads and
Teach kept building `Path(arg)` straight from the bridge argument, so the same
paste worked on two screens and failed on the other two.

Fixing the four sites is not enough on its own: the next field added to any
console is one `Path(out_dir)` away from re-introducing it, and nothing would
say so. So this walks each console's syntax tree and refuses a `Path()` built
directly out of a parameter — the boundary where a typed string arrives.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

CONSOLES = Path(__file__).resolve().parents[2] / "src" / "anastomosis" / "gui" / "consoles"

#: What a typed string must go through instead. `require_output_dir` adds the
#: blank-value refusal on top of the same cleaning.
CLEANERS = frozenset({"typed_path", "require_output_dir", "clean_typed_path"})


def _parameters(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    args = node.args
    named = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    if args.vararg:
        named.append(args.vararg)
    if args.kwarg:
        named.append(args.kwarg)
    return {a.arg for a in named}


def _raw_path_calls(source: str) -> list[tuple[str, int, str]]:
    """`Path(<parameter>)` calls, with the function and line they sit in."""
    tree = ast.parse(source)
    found: list[tuple[str, int, str]] = []

    class Walker(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope: list[tuple[str, set[str]]] = []

        def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            # A nested worker closes over the enclosing signature, so parameter
            # names accumulate down the stack rather than replacing each other.
            inherited = self.scope[-1][1] if self.scope else set()
            self.scope.append((node.name, inherited | _parameters(node)))
            self.generic_visit(node)
            self.scope.pop()

        visit_FunctionDef = _visit_function
        visit_AsyncFunctionDef = _visit_function

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            name = func.id if isinstance(func, ast.Name) else None
            if name == "Path" and self.scope:
                where, params = self.scope[-1]
                for arg in node.args:
                    if isinstance(arg, ast.Name) and arg.id in params:
                        found.append((where, node.lineno, arg.id))
            self.generic_visit(node)

    Walker().visit(tree)
    return found


@pytest.mark.parametrize("module", sorted(p.name for p in CONSOLES.glob("*.py")))
def test_a_console_never_builds_a_path_straight_from_a_typed_argument(module: str) -> None:
    raw = _raw_path_calls((CONSOLES / module).read_text(encoding="utf-8"))
    assert not raw, (
        f"{module} builds Path() directly from a bridge argument at "
        + ", ".join(f"{where}():{line} (Path({arg}))" for where, line, arg in raw)
        + f" — route it through one of {sorted(CLEANERS)} so a pasted, "
        "quote-wrapped Windows path works here too"
    )


def test_the_check_would_catch_the_defect_it_was_written_for() -> None:
    """The guard is only worth having if it fails on the shape it forbids."""
    offending = "def run(self, out_dir: str) -> None:\n    out = Path(out_dir)\n"
    assert _raw_path_calls(offending) == [("run", 2, "out_dir")]

    # A literal, a cleaned value and a local are all fine.
    for allowed in (
        'def run(self, out_dir: str) -> None:\n    out = Path("packs")\n',
        "def run(self, out_dir: str) -> None:\n    out = Path(clean_typed_path(out_dir))\n",
        "def run(self) -> None:\n    here = 'x'\n    out = Path(here)\n",
    ):
        assert _raw_path_calls(allowed) == []

    # And on a nested worker that closes over the signature — the shape three of
    # the four real sites actually had.
    nested = (
        "def start(self, db_path: str) -> None:\n"
        "    def _worker() -> None:\n"
        "        path = Path(db_path)\n"
    )
    assert _raw_path_calls(nested) == [("_worker", 3, "db_path")]
