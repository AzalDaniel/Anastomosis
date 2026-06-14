"""Reading a single-file structured export, and recognizing it again.

A learned source targets the long tail of *flat, single-file* exports — one
CSV/TSV/JSON/NDJSON file of rows. This module is the dumb IO half (the
:mod:`pf_tebra.loader` analogue): read rows into header-keyed dicts, and nothing
semantic. All mapping meaning lives in the interpreter.

Two ideas live here because both the reader and the authoring/matcher layer
need them, and putting them at the lowest level avoids an import cycle:

* :func:`normalize_column` / :func:`header_fingerprint` — a stable identity for
  a file's *column set* (order-independent, case/spacing/camelCase-insensitive).
  The fingerprint is how a learned mapping auto-recognizes its file (and detects
  that the columns changed — "stale" — instead of mis-reading a different file).
* :func:`find_source_file` — locate THE file in an export dir whose column set
  matches a mapping's fingerprint, distinguishing "no candidate", "a candidate
  whose columns changed" (loud), and "matched".

JSON/NDJSON records are flattened to dotted keys (``name.first``); a nested list
or object that can't be a scalar cell is preserved as its JSON text, so nothing
is lost before the interpreter's ``extensions`` catch-all even sees it.

PHI: row VALUES are patient data — this module never logs them. It logs/raises
with file paths, column names, and counts only.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from anastomosis.sources.learned.spec import MappingError, SourceFormat

__all__ = [
    "Row",
    "find_source_file",
    "header_fingerprint",
    "normalize_column",
    "read_columns",
    "read_rows",
]

Row = dict[str, str | None]

# Extensions for each format, used when globbing an export dir for candidates.
_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "csv": (".csv",),
    "tsv": (".tsv",),
    "json": (".json",),
    "ndjson": (".ndjson", ".jsonl"),
}

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_column(name: str) -> str:
    """Canonicalize a column name for fingerprinting and fuzzy matching.

    Splits camelCase, lowercases, and collapses any run of non-alphanumeric
    characters to a single space, so ``"PatientFirstName"``, ``"first_name"``,
    and ``"First Name"`` all normalize toward the same words.
    """
    spaced = _CAMEL_BOUNDARY.sub(" ", name)
    return _NON_ALNUM.sub(" ", spaced.lower()).strip()


def header_fingerprint(columns: Iterable[str]) -> str:
    """A stable sha256 over the SET of normalized column names (order-free)."""
    normalized = sorted({normalize_column(c) for c in columns if c})
    digest = hashlib.sha256("\n".join(normalized).encode("utf-8"))
    return digest.hexdigest()


def _delimiter(fmt: SourceFormat) -> str:
    if fmt.type == "tsv":
        return "\t"
    return fmt.delimiter or ","


def _flatten(value: Any, prefix: str, out: Row) -> None:
    """Flatten one JSON value into dotted-key cells on ``out``.

    Scalars become strings; ``None`` stays ``None``; nested objects recurse;
    anything else (a list, say) is preserved as its JSON text so the
    interpreter's ``extensions`` can still keep it.
    """
    if isinstance(value, dict):
        for key, child in value.items():
            _flatten(child, f"{prefix}.{key}" if prefix else str(key), out)
    elif value is None:
        out[prefix] = None
    elif isinstance(value, (str, int, float, bool)):
        out[prefix] = str(value)
    else:
        out[prefix] = json.dumps(value, ensure_ascii=False, sort_keys=True)


def _read_text(path: Path, encoding: str) -> str:
    try:
        return path.read_text(encoding=encoding)
    except (OSError, UnicodeError) as exc:
        raise MappingError(f"cannot read source file {path}: {type(exc).__name__}") from exc


def _json_records(path: Path, encoding: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(_read_text(path, encoding))
    except ValueError as exc:
        raise MappingError(f"JSON source {path} is not valid JSON: {type(exc).__name__}") from exc
    if isinstance(data, list):
        records = data
    elif isinstance(data, dict):
        arrays = [v for v in data.values() if isinstance(v, list)]
        if len(arrays) != 1:
            raise MappingError(
                f"JSON source {path} must be an array of records, or an object with "
                "exactly one array of records"
            )
        records = arrays[0]
    else:
        raise MappingError(
            f"JSON source {path} must be an array or object, got {type(data).__name__}"
        )
    if not all(isinstance(r, dict) for r in records):
        raise MappingError(f"JSON source {path} records must all be objects")
    return records


def _ndjson_records(path: Path, encoding: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for lineno, line in enumerate(_read_text(path, encoding).splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError as exc:
            raise MappingError(f"NDJSON source {path} line {lineno}: {type(exc).__name__}") from exc
        if not isinstance(obj, dict):
            raise MappingError(f"NDJSON source {path} line {lineno} is not an object")
        records.append(obj)
    return records


def read_rows(path: Path, fmt: SourceFormat) -> list[Row]:
    """Read every row of ``path`` into header-keyed dicts (loud on malformed)."""
    if fmt.type in ("csv", "tsv"):
        try:
            with path.open(encoding=fmt.encoding, newline="") as handle:
                reader = csv.DictReader(handle, delimiter=_delimiter(fmt))
                if reader.fieldnames is None:
                    raise MappingError(f"source file {path} is empty (no header row)")
                # Drop the restkey/None column DictReader uses for ragged rows;
                # a ragged extra cell is preserved under its real header only.
                return [{k: v for k, v in row.items() if k is not None} for row in reader]
        except (OSError, UnicodeError, csv.Error) as exc:
            raise MappingError(f"cannot read source file {path}: {type(exc).__name__}") from exc
    records = (
        _json_records(path, fmt.encoding)
        if fmt.type == "json"
        else _ndjson_records(path, fmt.encoding)
    )
    rows: list[Row] = []
    for record in records:
        flat: Row = {}
        _flatten(record, "", flat)
        rows.append(flat)
    return rows


def read_columns(path: Path, fmt: SourceFormat) -> list[str]:
    """The column names of ``path``, in first-seen order (for fingerprinting)."""
    if fmt.type in ("csv", "tsv"):
        try:
            with path.open(encoding=fmt.encoding, newline="") as handle:
                reader = csv.reader(handle, delimiter=_delimiter(fmt))
                header = next(reader, None)
        except (OSError, UnicodeError, csv.Error) as exc:
            raise MappingError(f"cannot read source file {path}: {type(exc).__name__}") from exc
        if header is None:
            raise MappingError(f"source file {path} is empty (no header row)")
        return [c for c in header if c]
    # JSON/NDJSON: union of flattened keys across records, first-seen order.
    ordered: list[str] = []
    seen: set[str] = set()
    for row in read_rows(path, fmt):
        for key in row:
            if key not in seen:
                seen.add(key)
                ordered.append(key)
    return ordered


def find_source_file(export_dir: Path, fmt: SourceFormat) -> Path:
    """Locate the file in ``export_dir`` whose columns match ``fmt``'s fingerprint.

    Raises :class:`MappingError` when no candidate of the right type exists, or
    when a candidate exists but its columns no longer match the fingerprint the
    mapping was learned against (the "stale" case — re-run ``source init``).
    """
    if export_dir.is_file():
        candidates = [export_dir]
    else:
        suffixes = _EXTENSIONS[fmt.type]
        candidates = sorted(
            p for p in export_dir.iterdir() if p.is_file() and p.suffix.lower() in suffixes
        )
    if not candidates:
        raise MappingError(f"no {fmt.type} file found in {export_dir} for this learned mapping")
    for candidate in candidates:
        try:
            if header_fingerprint(read_columns(candidate, fmt)) == fmt.header_fingerprint:
                return candidate
        except MappingError:
            continue  # an unreadable candidate is simply not a match
    raise MappingError(
        f"a {fmt.type} file is present in {export_dir} but its columns no longer match "
        "this learned mapping — re-run 'anast source init' to relearn the format"
    )
