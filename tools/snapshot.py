"""Deliverable snapshots: prove a refactor changed no output.

Drives the real ``anast`` CLI (subprocess, never an import) over the five
committed fixtures, capturing every file each run produces. Write a baseline
on ``main``; diff against it on a refactor branch — any difference fails
loudly. ``--help`` lists every mode; the normalizer's per-format rules are
documented at each function below. PHI: fixtures are this repo's own synthetic
data.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
_TOOLS_DIR = Path(__file__).resolve().parent
_SRC_DIR = REPO_ROOT / "src"
for _p in (_TOOLS_DIR, _SRC_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

BASELINE = _TOOLS_DIR / "snapshot_baseline.json"

#: Fixture directory name -> the ``--source``/``--from`` adapter name it is
#: read as. Order matches the task brief; iteration order of the baseline's
#: own ``runs`` key is alphabetical regardless (``sort_keys=True`` on write).
FIXTURES: dict[str, str] = {
    "pf_tebra_v9": "pf-tebra",
    "ccda": "ccda",
    "synthea": "ccda",
    "oracle_ehi_v500": "oracle-ehi",
    "fhir_r4": "fhir-r4",
}

#: The commands captured per fixture. ``migrate`` is additionally gated on the
#: fixture's source being ``ccda`` (see :func:`capture_fixture`).
COMMANDS = ("pipeline", "migrate")

#: The five deliverable file names whose full NORMALIZED content is captured
#: verbatim (not just digested) for a human-readable diff. ``quarantine.json``
#: is written only when an adapter held rows back, hence "if present" in the
#: brief — this set makes every one of the five equally optional in practice.
JSON_DELIVERABLE_NAMES = frozenset(
    {
        "qa_report.json",
        "upload_manifest.json",
        "loss_ledger.json",
        "run_manifest.json",
        "quarantine.json",
    }
)

#: Process-local marker files a lock leaves beside its target (see
#: ``core/locking.py``): the content is the CURRENT run's own pid, so it is
#: never comparable across runs and carries nothing about the deliverable.
EXCLUDE_NAMES = frozenset({".anast.lock"})

#: Pinned per https://reproducible-builds.org/specs/source-date-epoch/ — every
#: stamping site in ``src/`` reads this through ``core.clock`` instead of the
#: wall clock, so two captures made under it are directly comparable.
SOURCE_DATE_EPOCH = "1700000000"

#: The only two GUID prefixes this repo's fixtures are allowed to use for
#: SYNTHETIC, stable ids (``tools/phi_scan.py`` enforces this repo-wide) — any
#: OTHER UUID-shaped token found in captured output is therefore a runtime
#: assignment (``uuid4()``), never fixture data, and is safe to rewrite.
FIXTURE_GUID_PREFIXES = ("feedface-", "00000000-")

_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_VERSION_KEY_RE = re.compile(r"(^version$|_version$)", re.IGNORECASE)
_SEMVER_RE = re.compile(r"^\d+\.\d+(\.\d+)?([.\-+][0-9A-Za-z.\-]+)?$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$", re.IGNORECASE)

#: Keys whose VALUE is path-derived (a digest OF an absolute path, not the
#: path itself — see ``core/runmanifest.py:export_dir_id``) and therefore only
#: stable between two captures made from the same checkout location.
_PATH_DERIVED_KEYS = frozenset({"export_dir_id"})


class SnapshotError(Exception):
    """A capture could not be completed — a subprocess failed, an argument was
    malformed. Loud, never silently downgraded to a skip."""


# --- JSON normalization ------------------------------------------------------


def _looks_like_absolute_path(value: str) -> bool:
    if not value:
        return False
    if value.startswith("/"):
        return True
    return bool(re.fullmatch(r"[A-Za-z]:\\.*", value))


def _looks_like_version(value: str) -> bool:
    return bool(_SEMVER_RE.match(value) or _GIT_SHA_RE.match(value))


def _blank_special_values(obj: Any) -> Any:
    """Blank a ``run_id`` key, a path-derived key, an absolute-path STRING
    VALUE (the whole value, never a substring — a partial match would risk
    mangling an unrelated string that merely contains a slash), and a
    ``*version`` key whose value looks like a semver or a git sha."""
    if isinstance(obj, dict):
        result: dict[str, Any] = {}
        for key, value in obj.items():
            if key == "run_id":
                result[key] = "<run_id>"
            elif key in _PATH_DERIVED_KEYS:
                result[key] = "<path-id>"
            elif (
                _VERSION_KEY_RE.search(key)
                and isinstance(value, str)
                and _looks_like_version(value)
            ):
                result[key] = "<version>"
            else:
                result[key] = _blank_special_values(value)
        return result
    if isinstance(obj, list):
        return [_blank_special_values(v) for v in obj]
    if isinstance(obj, str) and _looks_like_absolute_path(obj):
        return "<path>"
    return obj


def _canonicalize_random_ids(obj: Any, order: dict[str, str]) -> Any:
    """Rewrite every non-fixture UUID-shaped token to ``<id-N>`` by first
    appearance, ``order`` shared across the whole call so the SAME token gets
    the SAME placeholder everywhere it recurs (a resource id and every
    ``reference``/``fullUrl`` pointing at it stay linked after the rewrite)."""
    if isinstance(obj, dict):
        return {k: _canonicalize_random_ids(v, order) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_canonicalize_random_ids(v, order) for v in obj]
    if isinstance(obj, str):
        return _UUID_RE.sub(lambda m: _placeholder_for(m.group(0), order), obj)
    return obj


def _placeholder_for(token: str, order: dict[str, str]) -> str:
    low = token.lower()
    if low.startswith(FIXTURE_GUID_PREFIXES):
        return token
    if low not in order:
        order[low] = f"<id-{len(order)}>"
    return order[low]


def _substitute_rendered_pdf_hashes(obj: Any, base_dir: Path) -> Any:
    """Contract: any object with sibling string keys ``file_path`` (ending
    ``.pdf``, resolving under ``base_dir`` to a real file) and ``sha256`` gets
    both that ``sha256`` and ``item_key``'s hash suffix replaced by
    :func:`pdf_digest` — Chromium's own PDF bytes are never hash-comparable
    across runs (a fresh internal ``/ID`` on every render), so raw sha256 in
    ``upload_manifest.json`` would fail the identity test on unchanged input."""
    if isinstance(obj, dict):
        result = {k: _substitute_rendered_pdf_hashes(v, base_dir) for k, v in obj.items()}
        file_path = obj.get("file_path")
        sha256 = obj.get("sha256")
        if (
            isinstance(file_path, str)
            and isinstance(sha256, str)
            and file_path.lower().endswith(".pdf")
        ):
            resolved = (base_dir / file_path).resolve()
            if resolved.is_file():
                digest = pdf_digest(resolved)
                result["sha256"] = digest
                item_key = obj.get("item_key")
                if isinstance(item_key, str) and ":" in item_key:
                    prefix, _, _old_suffix = item_key.rpartition(":")
                    result["item_key"] = f"{prefix}:{digest[:12]}"
        return result
    if isinstance(obj, list):
        return [_substitute_rendered_pdf_hashes(v, base_dir) for v in obj]
    return obj


def normalize_json_value(obj: Any) -> Any:
    """Key/path/version blanking, then UUID canonicalization, over an
    already-loaded JSON value. Excludes the PDF-hash substitution, which needs
    the file's own directory on disk: see :func:`load_normalized_json`."""
    blanked = _blank_special_values(obj)
    return _canonicalize_random_ids(blanked, {})


def load_normalized_json(path: Path) -> Any:
    """Load ``path`` and return its fully normalized value."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    substituted = _substitute_rendered_pdf_hashes(raw, path.parent)
    return normalize_json_value(substituted)


def json_digest(path: Path) -> str:
    normalized = load_normalized_json(path)
    canonical = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- XML normalization --------------------------------------------------------


def xml_digest(path: Path) -> str:
    """Canonicalized (c14n) sha256 of a C-CDA document, EVERY element
    included — the document header's own ``effectiveTime`` is derived from
    the record deterministically (see ``deliver/ccda_export/builder.py``) and
    stamped under the clock seam like everything else, so scrubbing it before
    hashing would hide a real regression in that derivation instead of
    tolerating a harmless one."""
    from lxml import etree

    tree = etree.parse(str(path))
    canonical = etree.tostring(tree.getroot(), method="c14n")
    return hashlib.sha256(canonical).hexdigest()


# --- HTML normalization --------------------------------------------------------

_WHITESPACE_RE = re.compile(r"\s+")


def html_digest(path: Path) -> str:
    collapsed = _WHITESPACE_RE.sub(" ", path.read_text(encoding="utf-8")).strip()
    return hashlib.sha256(collapsed.encode("utf-8")).hexdigest()


# --- PDF normalization ---------------------------------------------------------


def pdf_digest(path: Path) -> str:
    """Structural digest of a rendered PDF, NEVER the raw file bytes (see
    :func:`_substitute_rendered_pdf_hashes`): page count/geometry/text and
    every page's word boxes, reusing ``regen_goldens``'s own extraction —
    plus, per page, a sha256 of its content stream(s) (``read_contents()``,
    the drawing operators themselves: a CSS-only change with no visible text
    move, e.g. a colour, changes nothing PyMuPDF's word-box/text layer
    reports) and a sha256 of a low-DPI rendered pixmap (a second, independent
    check the same colour change also fails)."""
    import pymupdf

    import regen_goldens

    props = dict(regen_goldens.extract_pdf_props(path))
    boxes = regen_goldens.extract_word_boxes(path)
    with pymupdf.open(str(path)) as doc:
        content_streams = [hashlib.sha256(page.read_contents()).hexdigest() for page in doc]
        pixmaps = [hashlib.sha256(page.get_pixmap(dpi=36).samples).hexdigest() for page in doc]
    payload = json.dumps(
        {"props": props, "boxes": boxes, "content_streams": content_streams, "pixmaps": pixmaps},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --- file/tree capture ----------------------------------------------------------


def file_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return "json"
    if suffix == ".xml":
        return "xml"
    if suffix in (".html", ".htm"):
        return "html"
    if suffix == ".pdf":
        return "pdf"
    return "other"


def digest_for(path: Path, kind: str) -> str:
    if kind == "json":
        return json_digest(path)
    if kind == "xml":
        return xml_digest(path)
    if kind == "html":
        return html_digest(path)
    if kind == "pdf":
        return pdf_digest(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def capture_tree(root: Path) -> list[dict[str, str]]:
    """Every file under ``root`` (recursively), sorted by relative path, as
    ``{relpath, kind, digest}`` — the digest already normalized per
    :func:`digest_for`."""
    entries: list[dict[str, str]] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if path.name in EXCLUDE_NAMES:
            continue
        relpath = path.relative_to(root).as_posix()
        kind = file_kind(path)
        entries.append({"relpath": relpath, "kind": kind, "digest": digest_for(path, kind)})
    return entries


def capture_json_deliverables(root: Path) -> dict[str, Any]:
    """The full normalized content of every :data:`JSON_DELIVERABLE_NAMES`
    file found anywhere under ``root``, keyed by its path relative to
    ``root`` (a run may write more than one, e.g. ``qa_report.json`` inside
    ``charts/`` alongside a root-level ``loss_ledger.json``)."""
    found: dict[str, Any] = {}
    for path in sorted(root.rglob("*.json")):
        if path.name in JSON_DELIVERABLE_NAMES:
            found[path.relative_to(root).as_posix()] = load_normalized_json(path)
    return found


# --- driving the real CLI -------------------------------------------------------


def _anast_env() -> dict[str, str]:
    """The subprocess environment, PINNED to this checkout's own ``src/`` —
    prepended ahead of any ambient ``PYTHONPATH`` so a stray one (a different
    worktree's) can never shadow it. Without this, ``anast`` resolved off
    ``PATH`` and an in-process ``import anastomosis`` (``--cli-surface``) can
    silently measure two different trees at once."""
    env = dict(os.environ)
    env["SOURCE_DATE_EPOCH"] = SOURCE_DATE_EPOCH
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(_SRC_DIR) if not existing else f"{_SRC_DIR}{os.pathsep}{existing}"
    return env


def _run_anast(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed prefix, args are our own tempdir/fixture paths
        [sys.executable, "-m", "anastomosis.cli", *args],
        cwd=REPO_ROOT,
        env=_anast_env(),
        capture_output=True,
        text=True,
        check=False,
    )


def resolved_anastomosis_module() -> str:
    """Where a subprocess run under :func:`_anast_env` actually resolves
    ``anastomosis`` from — repo-relative when it is this checkout's own
    ``src/``, absolute otherwise. Captured once per run and compared like any
    other field, so a wrong-tree measurement (an ambient ``PYTHONPATH``
    that won, a stale editable install) fails the baseline instead of
    silently passing."""
    proc = subprocess.run(
        [sys.executable, "-c", "import anastomosis; print(anastomosis.__file__)"],
        cwd=REPO_ROOT,
        env=_anast_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SnapshotError(f"could not resolve anastomosis in the subprocess env:\n{proc.stderr}")
    resolved = Path(proc.stdout.strip()).resolve()
    try:
        return str(resolved.relative_to(REPO_ROOT))
    except ValueError:
        return str(resolved)


def _renderer_unavailable_reason() -> str | None:
    import regen_goldens

    return regen_goldens._renderer_available()


def _display_input_path(fixture_dir: Path) -> str:
    """A repo-relative path for the recorded ``cmd`` when ``fixture_dir`` is
    one of the committed fixtures; a placeholder for an ``--extra-input``
    directory, which by definition lives outside this checkout and, on
    principle, never gets its real filesystem location written down even into
    that input's own (uncommitted) side file."""
    try:
        return str(fixture_dir.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return "<extra-input>"


def _raw_export_dir_id(out: Path) -> str | None:
    """The UNBLANKED ``run.export_dir_id`` from ``out/run_manifest.json``, if
    one was written — read before :func:`load_normalized_json` would blank
    it, so :func:`capture_all` can check the ids across every captured run
    are pairwise distinct without ever persisting a path-derived value."""
    manifest = out / "run_manifest.json"
    if not manifest.is_file():
        return None
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    value = raw.get("run", {}).get("export_dir_id")
    return str(value) if value is not None else None


def run_pipeline_command(fixture_dir: Path, source: str, out: Path) -> dict[str, Any]:
    # No --no-qa: a QA regression (every check silently downgraded to PASS)
    # must be visible to this net, not hidden by skipping the stage that
    # would catch it. KNOWN RESIDUAL GAP: every one of the five fixtures'
    # generic_soap pipeline runs verifies clean (all pass) — generic_soap's
    # pack.yaml declares `omits` for all five CHARTABLE_KINDS, so
    # RecordCoverageCheck can never WARN/FAIL through it, and no `--section`
    # override changes that (it reads pack.yaml, not runtime section flags);
    # DateStalenessCheck never finds the frozen SOURCE_DATE_EPOCH day printed
    # on a chart dated from a fixture's own (different) dates. No CLI-reachable
    # route through the five committed fixtures was found that produces a
    # non-pass verdict; tests/unit/test_qa.py's mutation tests remain the only
    # guard on a verdict actually flipping, a known and accepted residual.
    cmd = [
        "pipeline",
        "run",
        str(fixture_dir),
        "--source",
        source,
        "--out",
        str(out),
        "--archive",
        str(out / "arch"),
        "--bundle",
        str(out / "bundle"),
        "--ccda",
        str(out / "ccda"),
        "--upload-manifest",
    ]
    proc = _run_anast(cmd)
    if proc.returncode != 0:
        raise SnapshotError(
            f"anast pipeline run over {fixture_dir.name} exited {proc.returncode}:\n{proc.stderr}"
        )
    return {
        "cmd": [
            "anast",
            "pipeline",
            "run",
            _display_input_path(fixture_dir),
            "--source",
            source,
            "--out",
            "<out>",
            "--archive",
            "<out>/arch",
            "--bundle",
            "<out>/bundle",
            "--ccda",
            "<out>/ccda",
            "--upload-manifest",
        ],
        "files": capture_tree(out),
        "json": capture_json_deliverables(out),
        "_export_dir_id": _raw_export_dir_id(out),
    }


def run_migrate_command(fixture_dir: Path, out: Path) -> dict[str, Any]:
    cmd = [
        "migrate",
        str(fixture_dir),
        "--out",
        str(out),
        "--from",
        "ccda",
        "--to",
        "tebra",
        "--render",
        "ccda-standard",
    ]
    proc = _run_anast(cmd)
    if proc.returncode != 0:
        raise SnapshotError(
            f"anast migrate over {fixture_dir.name} exited {proc.returncode}:\n{proc.stderr}"
        )
    return {
        "cmd": [
            "anast",
            "migrate",
            _display_input_path(fixture_dir),
            "--out",
            "<out>",
            "--from",
            "ccda",
            "--to",
            "tebra",
            "--render",
            "ccda-standard",
        ],
        "files": capture_tree(out),
        "json": capture_json_deliverables(out),
        "_export_dir_id": _raw_export_dir_id(out),
    }


def capture_fixture(
    name: str, source: str, *, commands: set[str], renderer_reason: str | None
) -> dict[str, Any]:
    """Every requested command's capture for one fixture — ``{command: result}``,
    a result being either a full capture or ``{"skipped": "<reason>"}``."""
    fixture_dir = REPO_ROOT / "tests" / "fixtures" / name
    result: dict[str, Any] = {}
    if not fixture_dir.is_dir():
        return {cmd: {"skipped": f"fixture directory not found: {fixture_dir}"} for cmd in commands}
    for cmd in sorted(commands):
        if cmd == "migrate" and source != "ccda":
            result[cmd] = {"skipped": "migrate only runs for ccda-sourced fixtures (task spec)"}
            continue
        if renderer_reason is not None:
            result[cmd] = {"skipped": f"renderer unavailable: {renderer_reason}"}
            continue
        with tempfile.TemporaryDirectory(prefix=f"anast-snap-{name}-{cmd}-") as tmp:
            if cmd == "pipeline":
                result[cmd] = run_pipeline_command(fixture_dir, source, Path(tmp))
            elif cmd == "migrate":
                result[cmd] = run_migrate_command(fixture_dir, Path(tmp))
    return result


def _parse_only(value: str | None) -> dict[str, set[str]] | None:
    """``--only`` syntax: ``NAME`` or ``NAME:COMMAND``, comma-separated.
    ``None`` (the flag was not given) means every fixture and command."""
    if value is None:
        return None
    selection: dict[str, set[str]] = {}
    for token in value.split(","):
        token = token.strip()
        if not token:
            continue
        name, _, command = token.partition(":")
        if name not in FIXTURES:
            raise SnapshotError(f"--only: unknown fixture {name!r} (have: {', '.join(FIXTURES)})")
        if command and command not in COMMANDS:
            raise SnapshotError(
                f"--only: unknown command {command!r} (have: {', '.join(COMMANDS)})"
            )
        selection.setdefault(name, set()).update([command] if command else COMMANDS)
    return selection


def _pop_export_dir_ids(runs: dict[str, Any]) -> list[str]:
    """Strip the transient ``_export_dir_id`` field every command result
    carries, returning the raw (never-persisted) values collected — see
    :func:`_raw_export_dir_id`."""
    ids: list[str] = []
    for fixture_result in runs.values():
        for command_result in fixture_result.values():
            found = command_result.pop("_export_dir_id", None)
            if found is not None:
                ids.append(found)
    return ids


def capture_all(selection: dict[str, set[str]] | None) -> tuple[dict[str, Any], bool]:
    """``(runs, export_dir_ids_distinct)`` — the second element is whether
    every captured run's ``export_dir_id`` (a hash of its export path, so
    never comparable verbatim across checkouts) was pairwise DISTINCT from
    every other one. A recipe that collapsed to a constant would flip this to
    ``False`` without needing to persist a single path-derived value."""
    renderer_reason = _renderer_unavailable_reason()
    names = list(FIXTURES) if selection is None else [n for n in FIXTURES if n in selection]
    runs: dict[str, Any] = {}
    for name in names:
        commands = set(COMMANDS) if selection is None else selection[name]
        runs[name] = capture_fixture(
            name, FIXTURES[name], commands=commands, renderer_reason=renderer_reason
        )
    ids = _pop_export_dir_ids(runs)
    return runs, len(ids) == len(set(ids))


def parse_extra_input(spec: str) -> tuple[str, Path, str]:
    name, sep, rest = spec.partition("=")
    dir_part, sep2, source = rest.rpartition(":")
    if not sep or not sep2 or not name or not dir_part or not source:
        raise SnapshotError(f"--extra-input must be NAME=DIR:SOURCE, got {spec!r}")
    return name, Path(dir_part), source


def capture_extra_input(name: str, directory: Path, source: str) -> dict[str, Any]:
    """The same capture :func:`capture_fixture` does, for an input that lives
    OUTSIDE this repo and must never reach the committed baseline."""
    if not directory.is_dir():
        return {"pipeline": {"skipped": f"directory not found: {directory}"}}
    renderer_reason = _renderer_unavailable_reason()
    result: dict[str, Any] = {}
    commands = set(COMMANDS)
    for cmd in sorted(commands):
        if cmd == "migrate" and source != "ccda":
            result[cmd] = {"skipped": "migrate only runs for ccda-sourced inputs (task spec)"}
            continue
        if renderer_reason is not None:
            result[cmd] = {"skipped": f"renderer unavailable: {renderer_reason}"}
            continue
        with tempfile.TemporaryDirectory(prefix=f"anast-snap-{name}-{cmd}-") as tmp:
            if cmd == "pipeline":
                result[cmd] = run_pipeline_command(directory, source, Path(tmp))
            elif cmd == "migrate":
                result[cmd] = run_migrate_command(directory, Path(tmp))
        result[cmd].pop("_export_dir_id", None)
    return {"name": name, "source": source, **result}


# --- CLI surface --------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return str(value)


def _dump_click_command(cmd: Any) -> dict[str, Any]:
    """One Click command's shape, recursively for a group.

    Duck-typed on ``commands`` (a group's subcommand mapping): Typer vendors
    Click as ``typer._click``, an internal module with no importable top-level
    ``click`` package to run ``isinstance(cmd, click.Group)`` against."""
    entry: dict[str, Any] = {"help": (cmd.help or cmd.short_help or "").strip()}
    if hasattr(cmd, "commands"):
        entry["subcommands"] = {
            sub_name: _dump_click_command(sub) for sub_name, sub in sorted(cmd.commands.items())
        }
        return entry
    params = []
    for param in cmd.params:
        opts = sorted({*getattr(param, "opts", []), *getattr(param, "secondary_opts", [])})
        params.append(
            {
                "name": param.name,
                "opts": opts,
                "required": bool(getattr(param, "required", False)),
                "default": _jsonable(getattr(param, "default", None)),
                "help": getattr(param, "help", None) or "",
                "is_flag": bool(getattr(param, "is_flag", False)),
            }
        )
    entry["params"] = sorted(params, key=lambda p: str(p["name"]))
    return entry


def dump_cli_surface() -> dict[str, Any]:
    """Every ``anast`` command/subcommand's options, defaults and help text,
    walked off the live Typer app object — the only way to see what
    ``--help`` would show without re-parsing rendered text."""
    import typer

    from anastomosis.cli import app

    return _dump_click_command(typer.main.get_command(app))


# --- comparison -----------------------------------------------------------------


def _json_diff_lines(baseline: Any, current: Any, *, limit: int = 20) -> list[str]:
    base_lines = json.dumps(baseline, indent=2, sort_keys=True).splitlines()
    cur_lines = json.dumps(current, indent=2, sort_keys=True).splitlines()
    diff = list(
        difflib.unified_diff(
            base_lines, cur_lines, fromfile="baseline", tofile="current", lineterm=""
        )
    )
    if len(diff) > limit:
        return [*diff[:limit], f"... ({len(diff) - limit} more line(s) not shown)"]
    return diff


def _compare_command(label: str, current: Any, baseline: Any) -> list[str]:
    if current is None:
        return [f"{label}: captured by the baseline, missing from this run"]
    if baseline is None:
        return [f"{label}: new capture, not in the baseline (run --write-baseline to add it)"]
    current_skipped = "skipped" in current
    baseline_skipped = "skipped" in baseline
    if current_skipped != baseline_skipped:
        return [
            f"{label}: skip status changed (baseline skipped={baseline_skipped!r}, "
            f"now skipped={current_skipped!r})"
        ]
    if current_skipped:
        return []
    diffs: list[str] = []
    cur_files = {f["relpath"]: f for f in current.get("files", [])}
    base_files = {f["relpath"]: f for f in baseline.get("files", [])}
    for relpath in sorted(set(cur_files) | set(base_files)):
        cur_entry = cur_files.get(relpath)
        base_entry = base_files.get(relpath)
        if base_entry is None:
            diffs.append(f"{label}: {relpath}: new file, not in the baseline")
        elif cur_entry is None:
            diffs.append(f"{label}: {relpath}: baseline file is now missing")
        elif cur_entry["kind"] != base_entry["kind"]:
            diffs.append(
                f"{label}: {relpath}: kind changed ({base_entry['kind']} -> {cur_entry['kind']})"
            )
        elif cur_entry["digest"] != base_entry["digest"]:
            diffs.append(f"{label}: {relpath}: content changed")
    cur_json = current.get("json", {})
    base_json = baseline.get("json", {})
    for relpath in sorted(set(cur_json) | set(base_json)):
        cur_value = cur_json.get(relpath)
        base_value = base_json.get(relpath)
        if relpath not in base_json:
            diffs.append(f"{label}: {relpath}: new JSON deliverable, not in the baseline")
        elif relpath not in cur_json:
            diffs.append(f"{label}: {relpath}: baseline JSON deliverable is now missing")
        elif cur_value != base_value:
            diffs.append(f"{label}: {relpath}: JSON content differs")
            diffs.extend(f"    {line}" for line in _json_diff_lines(base_value, cur_value))
    return diffs


def compare_baseline(current: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    """Every way ``current`` differs from ``baseline``, as human-readable
    lines — empty when every captured file and JSON deliverable matches."""
    diffs: list[str] = []
    cur_runs: dict[str, Any] = current.get("runs", {})
    base_runs: dict[str, Any] = baseline.get("runs", {})
    for name in sorted(set(cur_runs) | set(base_runs)):
        cur_fixture = cur_runs.get(name)
        base_fixture = base_runs.get(name)
        if cur_fixture is None:
            diffs.append(f"{name}: captured by the baseline, missing from this run")
            continue
        if base_fixture is None:
            diffs.append(f"{name}: new fixture, not in the baseline")
            continue
        for command in sorted(set(cur_fixture) | set(base_fixture)):
            diffs.extend(
                _compare_command(
                    f"{name}:{command}", cur_fixture.get(command), base_fixture.get(command)
                )
            )
    if "cli_surface" in baseline:
        if "cli_surface" not in current:
            diffs.append("cli surface missing from this capture (baseline has one)")
        elif current["cli_surface"] != baseline["cli_surface"]:
            diffs.append("cli surface changed:")
            diffs.extend(
                f"  {line}"
                for line in _json_diff_lines(
                    baseline["cli_surface"], current["cli_surface"], limit=60
                )
            )
    if "export_dir_ids_distinct" in baseline:
        cur_distinct = current.get("export_dir_ids_distinct")
        base_distinct = baseline["export_dir_ids_distinct"]
        if cur_distinct != base_distinct:
            diffs.append(
                f"export_dir_ids_distinct flipped: baseline {base_distinct!r}, now {cur_distinct!r}"
            )
    if "anastomosis_module" in baseline:
        cur_module = current.get("anastomosis_module")
        if cur_module != baseline["anastomosis_module"]:
            diffs.append(
                f"anastomosis resolved from a different tree: baseline "
                f"{baseline['anastomosis_module']!r}, now {cur_module!r}"
            )
    return diffs


def _restrict_baseline(baseline: dict[str, Any], selection: dict[str, set[str]]) -> dict[str, Any]:
    """The baseline narrowed to exactly what ``--only`` selected, so a
    restricted capture is compared against what it actually captured rather
    than scored against fixtures/commands it never touched."""
    restricted = dict(baseline)
    restricted_runs: dict[str, Any] = {}
    for name, commands in selection.items():
        fixture = baseline.get("runs", {}).get(name)
        if fixture is not None:
            restricted_runs[name] = {cmd: r for cmd, r in fixture.items() if cmd in commands}
    restricted["runs"] = restricted_runs
    return restricted


def _compare_extra_input(current: dict[str, Any], prior: dict[str, Any]) -> list[str]:
    """Diff two ``--extra-input`` side captures the same way a fixture's
    commands are compared — this input has no committed baseline, only
    whatever the previous run at this same ``--out`` path left behind."""
    diffs: list[str] = []
    for command in sorted(set(current) | set(prior)):
        if command in ("name", "source"):
            was, now = prior.get(command), current.get(command)
            if now != was:
                diffs.append(f"{command} changed: {was!r} -> {now!r}")
            continue
        diffs.extend(_compare_command(command, current.get(command), prior.get(command)))
    return diffs


# --- entry point ----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="Regenerate the baseline from a fresh capture.",
    )
    parser.add_argument(
        "--baseline-path",
        default=str(BASELINE),
        help="Baseline file to write/compare against (default: tools/snapshot_baseline.json).",
    )
    parser.add_argument(
        "--cli-surface",
        action="store_true",
        help="No-op: the CLI surface is always captured now.",
    )
    parser.add_argument(
        "--only",
        default=None,
        metavar="NAME[:COMMAND][,...]",
        help="Restrict to these fixtures (optionally :pipeline or :migrate). Default: all five.",
    )
    parser.add_argument(
        "--extra-input",
        action="append",
        default=[],
        metavar="NAME=DIR:SOURCE",
        help="Capture an uncommitted input too; written under --out, never into the baseline.",
    )
    parser.add_argument("--out", default=None, help="Output directory for --extra-input captures.")
    args = parser.parse_args(argv)

    try:
        if args.extra_input:
            if not args.out:
                print("snapshot: --extra-input requires --out DIR", file=sys.stderr)
                return 2
            out_dir = Path(args.out)
            out_dir.mkdir(parents=True, exist_ok=True)
            any_drift = False
            for spec in args.extra_input:
                name, directory, source = parse_extra_input(spec)
                result = capture_extra_input(name, directory, source)
                target = out_dir / f"{name}.snapshot.json"
                if target.is_file():
                    prior = json.loads(target.read_text(encoding="utf-8"))
                    diffs = _compare_extra_input(result, prior)
                    for diff in diffs:
                        print(f"snapshot: {name}: {diff}")
                    if diffs:
                        any_drift = True
                        print(f"snapshot: {name}: FAILED ({len(diffs)} difference(s))")
                    else:
                        print(f"snapshot: {name}: PASSED (matches the prior capture at {target})")
                else:
                    print(f"snapshot: {name}: baseline written, nothing compared")
                target.write_text(
                    json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
                )
            return 1 if any_drift else 0

        selection = _parse_only(args.only)
        if args.write_baseline and selection is not None and args.baseline_path == str(BASELINE):
            print(
                "snapshot: --write-baseline with --only requires --baseline-path "
                "(refusing to overwrite the full baseline with a partial capture)",
                file=sys.stderr,
            )
            return 2
    except SnapshotError as exc:
        print(f"snapshot: {exc}", file=sys.stderr)
        return 2

    baseline_path = Path(args.baseline_path)

    try:
        runs, export_dir_ids_distinct = capture_all(selection)
        current: dict[str, Any] = {
            "runs": runs,
            "export_dir_ids_distinct": export_dir_ids_distinct,
            "cli_surface": dump_cli_surface(),
            "anastomosis_module": resolved_anastomosis_module(),
        }
    except SnapshotError as exc:
        print(f"snapshot: {exc}", file=sys.stderr)
        return 1

    if args.write_baseline:
        payload = {
            "_comment": (
                "Deliverable snapshot baseline - see tools/snapshot.py. Regenerate ONLY in a "
                "commit that intentionally changes rendered/exported output, and say what and why."
            ),
            "tool": "tools/snapshot.py",
            "source_date_epoch": SOURCE_DATE_EPOCH,
            **current,
        }
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        commands_captured = sum(len(v) for v in current["runs"].values())
        print(
            f"snapshot: baseline written to {baseline_path} "
            f"({commands_captured} command capture(s))"
        )
        return 0

    if not baseline_path.is_file():
        print(f"snapshot: no baseline at {baseline_path} — run with --write-baseline first")
        return 1
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    if selection is not None:
        baseline = _restrict_baseline(baseline, selection)
    diffs = compare_baseline(current, baseline)
    for diff in diffs:
        print(f"snapshot: {diff}")
    if diffs:
        print(f"snapshot: FAILED ({len(diffs)} difference(s))")
        return 1
    print("snapshot: PASSED (every captured file and JSON deliverable matches the baseline)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
