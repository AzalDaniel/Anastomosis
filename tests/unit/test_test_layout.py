"""Guard: no two test files anywhere under ``tests/`` share a basename.

pytest imports a test module by its basename unless the directory is a
package, so two files called ``test_migrate.py`` are one module name. Collect
both in a single run and the second one stops the run with an import-file
mismatch — not a failing test, a refusal to start, whose message is about
module identity and sends the reader looking for a packaging problem.

The default lanes never collect both directories at once, so the trap is
only for someone who types ``pytest tests/`` themselves. That is the obvious
command, and the answer it deserves is either a clean run or an honest
"needs a browser", not an error about `__file__`.
"""

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
    """And the guard above is the reason, checked from the other side.

    A basename is the collision this repo actually hit; it is not the only way
    to make ``pytest tests/`` refuse to start. Asking pytest itself keeps the
    guard honest about its purpose rather than about its mechanism.
    """
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
