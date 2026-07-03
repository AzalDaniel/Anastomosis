# AI-assisted: written with Claude agents under the author's direction and review; see DESIGN.md.
"""Guard: source comments state invariants, never review history.

A comment must pin the PROPERTY the code or test enforces, never the
review, reviewer, or audit round that once requested it. History lives in
CHANGELOG.md and docs/reviews/ — not scattered through the tree, where it
goes stale and turns every file into an excavation site.

This test walks the shipped source (``src/``, ``tests/``, ``tools/``,
``.github/``) and fails if any line carries one of the review-archaeology
tokens. Each banned pattern is assembled from concatenated fragments so
this guard file cannot match itself; if the tokens ever creep back in, add
the invariant to the comment and move the history to the changelog/docs.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# The trees whose comments must stay invariant-only. docs/ and CHANGELOG.md
# are the sanctioned home for history and are deliberately NOT scanned.
_SCAN_DIRS: tuple[str, ...] = ("src", "tests", "tools", ".github")

# Text file kinds that carry comments/docstrings we care about.
_SCAN_SUFFIXES: frozenset[str] = frozenset({".py", ".yml", ".yaml", ".sh", ".toml"})

# A path with a ``vendor`` component is third-party and out of our comment
# discipline — skip it wholesale.
_SKIP_COMPONENT = "vendor"

# Banned tokens, each compiled case-insensitively. Every pattern is built by
# concatenating fragments so no single physical line in THIS file contains a
# whole token — the guard therefore never trips on its own source. The human
# names deliberately share no substring with any banned token.
_BANNED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("external tool name", re.compile("cod" + "ex", re.IGNORECASE)),
    ("numbered audit item", re.compile("find" + "ing #", re.IGNORECASE)),
    ("release gate tag", re.compile("release" + "-review", re.IGNORECASE)),
    ("reviewer-raised note", re.compile("review" + " flagged", re.IGNORECASE)),
    ("prior-pass tag", re.compile("re" + "-audit", re.IGNORECASE)),
    ("review-round marker", re.compile("round-" + r"[0-9] p[0-9]", re.IGNORECASE)),
    ("worklog PR tag", re.compile("w" + r"[0-9]/" + "pr-" + r"[0-9]+[a-z]?\b", re.IGNORECASE)),
)


def _scanned_files() -> list[Path]:
    """Every file under the scanned trees with a comment-bearing suffix,
    minus any ``vendor`` subtree."""
    files: list[Path] = []
    for top in _SCAN_DIRS:
        root = REPO_ROOT / top
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in _SCAN_SUFFIXES:
                continue
            if _SKIP_COMPONENT in path.parts:
                continue
            files.append(path)
    return files


def test_no_review_archaeology_in_source() -> None:
    violations: list[str] = []
    for path in _scanned_files():
        rel = path.relative_to(REPO_ROOT)
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for name, pattern in _BANNED_PATTERNS:
                if pattern.search(line):
                    violations.append(f"{rel}:{lineno} ({name})")
    assert not violations, (
        "Review-history archaeology found — comments must state the invariant the "
        "code/test pins, not the review round that requested it. Rewrite these lines "
        "to name the property, and move any history to CHANGELOG.md / docs/reviews/:\n  "
        + "\n  ".join(sorted(violations))
    )


def test_guard_patterns_actually_fire() -> None:
    """The scanner is not a silent no-op: a synthetic string carrying a
    banned token matches at least one pattern. The sample is assembled from
    fragments so this file's own source stays clean."""
    sample = "a comment that mentions " + "cod" + "ex"
    assert any(pattern.search(sample) for _name, pattern in _BANNED_PATTERNS)
