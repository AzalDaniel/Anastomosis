"""The comment ratchet: known prose may shrink or hold, never grow.

A file already in the baseline may not raise its ratio; a docstring already
over-long may not get longer; a history phrase or over-long docstring new to
the tree fails outright; a file the baseline has never seen must already be
lean (:data:`NEW_FILE_RATIO_LIMIT`, zero of either violation). Same shape as
``complexity_gate.py`` otherwise: a checked-in baseline
(``tools/prose_baseline.json``), ``--write-baseline`` to regenerate — refused
if that would admit a new violation unless ``--allow-new-violations`` says
so. Exemptions: ``tools/prose_allowlist.txt``, one path per line, a reason
after a ``#``.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE = Path(__file__).resolve().parent / "prose_baseline.json"
ALLOWLIST = Path(__file__).resolve().parent / "prose_allowlist.txt"
WALKED_DIRS = ("src", "tests")

#: A docstring longer than this many physical lines is reported, per scope.
OVER_LONG_LIMITS = {"module": 10, "class": 5, "function": 5}

#: The absolute ceiling a file with NO baseline entry must already meet — a
#: brand-new file gets no "nothing to compare against yet" grace period.
NEW_FILE_RATIO_LIMIT = 0.15

#: Narrating what code WAS instead of documenting what it IS.
HISTORY_RE = re.compile(
    r"\b(used to|before this|previously|until #\d|we changed|this was|originally|"
    r"no longer|historically|the old behavio[u]r)\b",
    re.IGNORECASE,
)

#: An issue reference this repo's own comments write as ``(#164)``, ``#374``,
#: or inline in a raised message's text (``"Merging #310 proved this"``) — so
#: the guard count scans whole files, not just comment/docstring text.
#: 2-4 digits with a trailing word boundary excludes both a bare ordinal
#: (``sample #1``, ``page #0``) and a hex colour: a boundary can never fall
#: between two digits, so ``#701a14``/``#171310`` never match at all — the
#: run either continues past 4 digits or runs into a hex letter, and either
#: way backtracking to 2-4 digits still fails the boundary check right after.
ISSUE_REF_RE = re.compile(r"#(\d{2,4})\b")

#: Floating-point ratio comparisons tolerate this much noise.
_EPS = 1e-9


@dataclass
class Totals:
    code: int = 0
    docstring: int = 0
    comment: int = 0
    blank: int = 0

    def add(self, other: Totals) -> None:
        self.code += other.code
        self.docstring += other.docstring
        self.comment += other.comment
        self.blank += other.blank

    @property
    def total(self) -> int:
        return self.code + self.docstring + self.comment + self.blank

    @property
    def ratio(self) -> float:
        denom = self.total - self.blank
        return (self.docstring + self.comment) / denom if denom else 0.0

    def as_json(self) -> dict[str, object]:
        return {
            "code": self.code,
            "docstring": self.docstring,
            "comment": self.comment,
            "blank": self.blank,
            "total": self.total,
            "ratio": round(self.ratio, 6),
        }


@dataclass
class FileFindings:
    totals: Totals
    over_long: list[dict[str, object]] = field(default_factory=list)
    history_hits: list[dict[str, object]] = field(default_factory=list)
    issue_refs: set[str] = field(default_factory=set)


def _docstring_owners(tree: ast.Module) -> list[tuple[ast.stmt, str]]:
    """Every ``(docstring-expr-node, scope)`` in ``tree`` — ``scope`` one of
    ``module``/``class``/``function``, matching :data:`OVER_LONG_LIMITS`."""
    owners: list[tuple[ast.stmt, str]] = []

    def _first_stmt_docstring(body: list[ast.stmt], scope: str) -> None:
        if not body:
            return
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            owners.append((first, scope))

    _first_stmt_docstring(tree.body, "module")
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            _first_stmt_docstring(node.body, "class")
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            _first_stmt_docstring(node.body, "function")
    return owners


def _classify_lines(path: Path, source_lines: list[str]) -> tuple[list[str], list[tuple[int, str]]]:
    """Contract: returns ``(kind per 1-indexed line, [(line, comment), ...])``.
    Any code token makes a line "code"; a lone ``#`` token makes it "comment"
    unless code already claimed it (comments always trail code, never precede
    it); everything else is "blank". Docstrings are reclassified separately in
    :func:`analyze_file` — ``tokenize`` sees only a STRING token, not a
    docstring."""
    n = len(source_lines)
    kind = ["blank"] * (n + 1)
    comments: list[tuple[int, str]] = []
    with tokenize.open(path) as handle:
        tokens = list(tokenize.generate_tokens(handle.readline))
    for tok in tokens:
        if tok.type == tokenize.COMMENT:
            comments.append((tok.start[0], tok.string))
            if kind[tok.start[0]] == "blank":
                kind[tok.start[0]] = "comment"
        elif tok.type in (
            tokenize.NEWLINE,
            tokenize.NL,
            tokenize.INDENT,
            tokenize.DEDENT,
            tokenize.ENDMARKER,
            tokenize.ENCODING,
        ):
            continue
        else:
            for row in range(tok.start[0], tok.end[0] + 1):
                if 0 < row <= n:
                    kind[row] = "code"
    return kind, comments


def analyze_file(path: Path, root: Path = REPO_ROOT) -> FileFindings:
    """One file's line totals, over-long docstrings, and history hits. Raises
    whatever ``tokenize``/``ast.parse`` raise on a file that will not parse —
    a defect, never something to skip quietly."""
    relpath = _relpath(path, root)
    source = path.read_text(encoding="utf-8")
    source_lines = source.splitlines()
    kind, comments = _classify_lines(path, source_lines)
    tree = ast.parse(source, filename=str(path))

    findings = FileFindings(totals=Totals())
    for owner, scope in _docstring_owners(tree):
        start, end = owner.lineno, owner.end_lineno or owner.lineno
        for row in range(start, end + 1):
            if 0 < row < len(kind):
                kind[row] = "docstring"
        length = end - start + 1
        limit = OVER_LONG_LIMITS[scope]
        if length > limit:
            findings.over_long.append(
                {"file": relpath, "line": start, "length": length, "limit": limit, "scope": scope}
            )
        for row in range(start, end + 1):
            text = source_lines[row - 1] if 0 < row <= len(source_lines) else ""
            if HISTORY_RE.search(text):
                findings.history_hits.append(
                    {"file": relpath, "line": row, "kind": "docstring", "text": text.strip()}
                )

    for row, text in comments:
        if HISTORY_RE.search(text):
            findings.history_hits.append(
                {"file": relpath, "line": row, "kind": "comment", "text": text.strip()}
            )

    # The guard count scans the WHOLE file, not just comment/docstring text: a
    # regression reference sometimes lives in a raised message's own string
    # literal ("Merging #310 proved this"), which is neither.
    findings.issue_refs.update(m.group(1) for m in ISSUE_REF_RE.finditer(source))

    totals = Totals()
    for row in range(1, len(kind)):
        setattr(totals, kind[row], getattr(totals, kind[row]) + 1)
    findings.totals = totals
    return findings


def _relpath(path: Path, root: Path = REPO_ROOT) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def load_allowlist() -> set[str]:
    """Repo-relative paths exempted from every measurement in this file —
    ``tools/prose_allowlist.txt``, one path per line, a ``#`` reason after it
    or alone on its own comment line."""
    if not ALLOWLIST.is_file():
        return set()
    exempt: set[str] = set()
    for raw in ALLOWLIST.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            exempt.add(line)
    return exempt


def package_of(path: Path, root: Path = REPO_ROOT) -> str:
    """The top-level package a file belongs to: ``anastomosis`` for anything
    under ``src`` (the layout directory itself is not a package), or
    ``tests.<subdir>`` for anything under ``tests`` — ``tests.unit``,
    ``tests.e2e``, and so on, so the four lanes are judged separately."""
    parts = _relpath(path, root).split("/")
    if parts[0] == "src" and len(parts) > 1:
        return parts[1]
    if parts[0] == "tests" and len(parts) > 1:
        return f"tests.{parts[1]}"
    return parts[0]


def iter_py_files(exempt: set[str], root: Path = REPO_ROOT) -> list[Path]:
    files: list[Path] = []
    for top in WALKED_DIRS:
        base = root / top
        if not base.is_dir():
            continue
        files.extend(p for p in base.rglob("*.py") if _relpath(p, root) not in exempt)
    return sorted(files)


def measure(root: Path = REPO_ROOT) -> dict[str, object]:
    exempt = load_allowlist()
    files: dict[str, object] = {}
    packages: dict[str, Totals] = {}
    repo = Totals()
    over_long: list[dict[str, object]] = []
    history_hits: list[dict[str, object]] = []
    guard_refs: set[str] = set()

    for path in iter_py_files(exempt, root):
        relpath = _relpath(path, root)
        findings = analyze_file(path, root)
        files[relpath] = findings.totals.as_json()
        repo.add(findings.totals)
        packages.setdefault(package_of(path, root), Totals()).add(findings.totals)
        over_long.extend(findings.over_long)
        history_hits.extend(findings.history_hits)
        if relpath.startswith("tests/"):
            guard_refs.update(findings.issue_refs)

    return {
        "files": files,
        "packages": {name: totals.as_json() for name, totals in sorted(packages.items())},
        "repo": repo.as_json(),
        "over_long_docstrings": sorted(over_long, key=lambda d: (str(d["file"]), int(d["line"]))),
        "history_hits": sorted(history_hits, key=lambda d: (str(d["file"]), int(d["line"]))),
        "guard_count": len(guard_refs),
    }


def _locations(entries: list[dict[str, object]]) -> set[tuple[str, int]]:
    return {(str(e["file"]), int(e["line"])) for e in entries}


def _lengths(entries: list[dict[str, object]]) -> dict[tuple[str, int], int]:
    return {(str(e["file"]), int(e["line"])): int(e["length"]) for e in entries}


def compare(current: dict[str, object], baseline: dict[str, object]) -> tuple[list[str], int]:
    """Every way ``current`` is worse than ``baseline``, plus how many FILES
    improved (a lower ratio, a resolved over-long docstring, a resolved
    history hit — any one of those is enough to count the file once) — the
    burn-down worth nudging a baseline regeneration over."""
    failures: list[str] = []
    improved_files: set[str] = set()

    base_files: dict[str, dict[str, object]] = baseline["files"]  # type: ignore[assignment]
    cur_files: dict[str, dict[str, object]] = current["files"]  # type: ignore[assignment]
    for relpath, cur in sorted(cur_files.items()):
        base = base_files.get(relpath)
        cur_ratio = float(cur["ratio"])
        if base is None:
            # No baseline entry to compare against — an absolute cap instead
            # of a free pass, so a new file cannot simply arrive already
            # over whatever it likes.
            if cur_ratio > NEW_FILE_RATIO_LIMIT + _EPS:
                failures.append(
                    f"new file exceeds the absolute prose cap: {relpath} is "
                    f"{cur_ratio:.4f} (limit {NEW_FILE_RATIO_LIMIT})"
                )
            continue
        base_ratio = float(base["ratio"])
        if cur_ratio > base_ratio + _EPS:
            failures.append(f"file ratio rose: {relpath} was {base_ratio:.4f}, now {cur_ratio:.4f}")
        elif cur_ratio < base_ratio - _EPS:
            improved_files.add(relpath)

    cur_repo_ratio = float(current["repo"]["ratio"])  # type: ignore[index]
    base_repo_ratio = float(baseline["repo"]["ratio"])  # type: ignore[index]
    if cur_repo_ratio > base_repo_ratio + _EPS:
        failures.append(
            f"repo-wide prose ratio rose: was {base_repo_ratio:.4f}, now {cur_repo_ratio:.4f}"
        )

    base_overlong = _locations(baseline["over_long_docstrings"])  # type: ignore[arg-type]
    base_overlong_lengths = _lengths(baseline["over_long_docstrings"])  # type: ignore[arg-type]
    cur_overlong = current["over_long_docstrings"]
    for entry in cur_overlong:  # type: ignore[union-attr]
        loc = (str(entry["file"]), int(entry["line"]))
        length = int(entry["length"])
        if loc not in base_overlong:
            failures.append(
                f"NEW over-long docstring: {entry['file']}:{entry['line']} "
                f"({length} lines, {entry['scope']} limit {entry['limit']})"
            )
        elif length > base_overlong_lengths[loc]:
            failures.append(
                f"over-long docstring GREW: {entry['file']}:{entry['line']} was "
                f"{base_overlong_lengths[loc]} lines, now {length}"
            )
        elif length < base_overlong_lengths[loc]:
            improved_files.add(str(entry["file"]))
    for resolved_file, _line in base_overlong - _locations(cur_overlong):  # type: ignore[arg-type]
        improved_files.add(resolved_file)

    base_history = _locations(baseline["history_hits"])  # type: ignore[arg-type]
    cur_history = current["history_hits"]
    for entry in cur_history:  # type: ignore[union-attr]
        loc = (str(entry["file"]), int(entry["line"]))
        if loc not in base_history:
            failures.append(
                f"NEW history phrase: {entry['file']}:{entry['line']}: {entry['text']!r}"
            )
    for resolved_file, _line in base_history - _locations(cur_history):  # type: ignore[arg-type]
        improved_files.add(resolved_file)

    cur_guard, base_guard = int(current["guard_count"]), int(baseline["guard_count"])  # type: ignore[arg-type]
    if cur_guard < base_guard:
        failures.append(f"regression-guard count DROPPED: was {base_guard}, now {cur_guard}")

    return failures, len(improved_files)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="Regenerate tools/prose_baseline.json from the working tree.",
    )
    parser.add_argument(
        "--allow-new-violations",
        action="store_true",
        help="Let --write-baseline admit a new/grown violation it would otherwise refuse.",
    )
    args = parser.parse_args(argv)

    current = measure(REPO_ROOT)

    if args.write_baseline:
        if BASELINE.is_file():
            old_baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
            new_violations, _ = compare(current, old_baseline)
            if new_violations and not args.allow_new_violations:
                print("prose gate: --write-baseline would admit new violations:")
                for violation in new_violations:
                    print(f"  {violation}")
                print(
                    "prose gate: refusing — fix the source, or pass --allow-new-violations "
                    "to admit them deliberately."
                )
                return 1
            for violation in new_violations:
                print(f"prose gate: admitting: {violation}")
        payload = {
            "_comment": (
                "Prose ratchet baseline - see tools/prose_gate.py. Regenerate ONLY in a commit "
                "that reduces prose or documents why it grew (and never lets the guard count drop)."
            ),
            "tool": "tools/prose_gate.py",
            **current,
        }
        BASELINE.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(
            f"baseline written: {len(current['files'])} file(s), "  # type: ignore[arg-type]
            f"repo ratio {current['repo']['ratio']:.4f}, "  # type: ignore[index]
            f"guard count {current['guard_count']}"
        )
        return 0

    if not BASELINE.is_file():
        print(f"prose gate: no baseline at {BASELINE} — run with --write-baseline first")
        return 1

    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    failures, improved = compare(current, baseline)
    for failure in failures:
        print(f"prose gate: {failure}")
    if failures:
        print(f"prose gate: FAILED ({len(failures)} regression(s))")
        return 1

    print(f"prose gate: PASSED ({improved} files improved)")
    if improved:
        print("prose gate: tighten the ratchet: python tools/prose_gate.py --write-baseline")
    return 0


if __name__ == "__main__":
    sys.exit(main())
