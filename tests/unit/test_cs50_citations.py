"""The per-file citation stamper is idempotent and never breaks a stamped file.

``tools/cs50_citations.py`` re-applies the per-file AI-assistance disclosure
for an academic-submission branch (the product repo carries attribution in
DESIGN.md). Three properties matter, because the script rewrites every
authored file in the tree:

* **Idempotence** — a second run leaves every file byte-identical, so it is
  safe to re-run after a rebase.
* **Position** — a Python shebang and an HTML doctype must stay on line 1,
  and a module docstring must remain the module's first *statement* (a
  comment above it does not displace it).
* **Line length** — every rendered comment form must fit the repo's
  100-column limit, or the stamped tree fails its own lint gate.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "tools" / "cs50_citations.py"


def _load() -> ModuleType:
    """Import the script by path (``tools/`` is not an installed package)."""
    spec = importlib.util.spec_from_file_location("cs50_citations", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


citations = _load()


@pytest.mark.parametrize("suffix", sorted(citations.COMMENT_STYLES))
def test_every_rendered_citation_fits_the_line_limit(suffix: str) -> None:
    line = citations._citation_line(suffix)
    assert citations.MARKER in line
    assert len(line) <= citations._MAX_LINE, f"{suffix}: {len(line)} columns"


@pytest.mark.parametrize(
    ("name", "body", "expected_first_line"),
    [
        ("plain.py", '"""Doc."""\n\nX = 1\n', "# "),
        ("shebang.py", '#!/usr/bin/env python3\n"""Doc."""\n', "#!/usr/bin/env python3"),
        ("page.html", "<!doctype html>\n<title>t</title>\n", "<!doctype html>"),
        ("bare.html", "<title>t</title>\n", "<!-- "),
        ("style.css", "body { color: red; }\n", "/* "),
        ("app.js", "const a = 1;\n", "// "),
    ],
)
def test_citation_lands_without_displacing_a_prologue(
    tmp_path: Path, name: str, body: str, expected_first_line: str
) -> None:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")

    assert citations._apply(path) is True
    text = path.read_text(encoding="utf-8")

    assert text.splitlines()[0].startswith(expected_first_line)
    assert citations.MARKER in text
    assert body.splitlines()[-1] in text  # the original content survived


def test_python_docstring_survives_the_stamp(tmp_path: Path) -> None:
    """A comment above a docstring must not demote it out of ``__doc__``."""
    path = tmp_path / "mod.py"
    path.write_text('#!/usr/bin/env python3\n"""Module summary."""\n\nX = 1\n', encoding="utf-8")

    citations._apply(path)

    module = ast.parse(path.read_text(encoding="utf-8"))
    assert ast.get_docstring(module) == "Module summary."


def test_second_run_is_a_no_op(tmp_path: Path) -> None:
    path = tmp_path / "mod.py"
    path.write_text('"""Doc."""\n', encoding="utf-8")

    assert citations._apply(path) is True
    once = path.read_text(encoding="utf-8")
    assert citations._apply(path) is False
    assert path.read_text(encoding="utf-8") == once


def test_vendored_and_cache_paths_are_skipped(tmp_path: Path) -> None:
    """Third-party trees are not ours to annotate — and a cache is not source."""
    for parts in (("vendor", "lib.py"), ("__pycache__", "mod.py"), ("node_modules", "a.js")):
        path = tmp_path.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("X = 1\n", encoding="utf-8")
    assert citations._iter_files([tmp_path]) == []


def test_unknown_suffixes_are_never_touched(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text("# heading\n", encoding="utf-8")
    (tmp_path / "data.json").write_text("{}\n", encoding="utf-8")
    assert citations._iter_files([tmp_path]) == []


def test_check_mode_reports_without_writing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "mod.py"
    path.write_text('"""Doc."""\n', encoding="utf-8")

    assert citations.main([str(tmp_path), "--check"]) == 1
    assert path.read_text(encoding="utf-8") == '"""Doc."""\n'
    assert "missing citation" in capsys.readouterr().out

    assert citations.main([str(tmp_path)]) == 0
    assert citations.main([str(tmp_path), "--check"]) == 0


def test_missing_paths_are_a_usage_error(tmp_path: Path) -> None:
    assert citations.main([str(tmp_path / "nope")]) == 2
