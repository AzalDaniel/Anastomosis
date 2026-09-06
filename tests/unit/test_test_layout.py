"""Guard: no two test files anywhere under ``tests/`` share a basename.

pytest imports a test module by its basename unless the directory is a
package, so two files called ``test_migrate.py`` are one module name:
collecting both in a single run stops with an import-file mismatch — not
a failing test, a refusal to start. The default lanes never collect both
directories at once, so this guards ``pytest tests/`` itself, typed by
someone who deserves a clean run, not an error about `__file__`."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_no_two_test_files_share_a_basename() -> None:
    by_name: defaultdict[str, list[str]] = defaultdict(list)
    for path in (REPO_ROOT / "tests").rglob("test_*.py"):
        by_name[path.name].append(str(path.relative_to(REPO_ROOT)))

    collisions = {name: sorted(paths) for name, paths in by_name.items() if len(paths) > 1}
    assert not collisions, (
        "two test files share a module name, so collecting both in one run is an "
        f"import-file mismatch rather than a test result: {collisions}"
    )


def test_the_whole_tests_tree_is_collectable() -> None:
    """A basename collision is not the only way to make ``pytest tests/``
    refuse to start, so this asks pytest itself: honest about the
    purpose (the tree collects) rather than the one mechanism above."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "--collect-only", "-p", "no:randomly"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, "`pytest tests/` cannot collect the tree:\n" + "\n".join(
        result.stdout.strip().splitlines()[-12:]
    )
