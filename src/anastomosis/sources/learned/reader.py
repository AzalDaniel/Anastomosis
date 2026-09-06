"""Reading a single-file structured export, and recognizing it again.

The dumb IO half (the :mod:`pf_tebra.loader` analogue) for a flat,
single-file export (CSV/TSV/JSON/NDJSON) — read rows into mapping-authored
header-keyed dicts, nothing semantic. :func:`header_fingerprint` gives a
file's column *set* a stable identity so :func:`find_source_file` can tell
"no candidate", "columns changed" (loud), and "matched" apart. JSON/NDJSON
records flatten to dotted keys (a non-scalar becomes its JSON text; a path
collision is malformed input). PHI: row VALUES are never logged."""

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


class _DuplicateJsonKey(ValueError):
    """A JSON object repeated a key; deliberately carries no key or value."""


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build one JSON object without accepting last-key-wins data loss."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            # JSON keys can themselves contain identifiers. Keep the exception
            # content-free; callers report only its type (and NDJSON line).
            raise _DuplicateJsonKey
        result[key] = value
    return result


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
    """Canonicalize a column name for fingerprinting and fuzzy matching:
    splits camelCase, lowercases, and collapses non-alphanumeric runs to a
    single space, so ``"PatientFirstName"``, ``"first_name"``, and
    ``"First Name"`` all normalize toward the same words."""
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
    """Flatten one JSON value into dotted-key cells on ``out``: scalars
    become strings, ``None`` stays ``None``, nested objects recurse, and
    anything else (a list) is preserved as its JSON text so ``extensions``
    can still keep it."""
    if isinstance(value, dict):
        for key, child in value.items():
            _flatten(child, f"{prefix}.{key}" if prefix else str(key), out)
    else:
        if prefix in out:
            # A literal ``a.b`` and ``a: {b: ...}`` address the same cell;
            # keep the diagnostic structural since JSON values may be PHI.
            raise MappingError(f"flattened JSON path collision at {prefix!r}")
        if value is None:
            out[prefix] = None
        elif isinstance(value, (str, int, float, bool)):
            out[prefix] = str(value)
        else:
            out[prefix] = json.dumps(value, ensure_ascii=False, sort_keys=True)


def _validate_csv_header(header: list[str], path: Path) -> None:
    """Reject CSV headers that ``DictReader`` would silently conflate."""
    exact: dict[str, int] = {}
    normalized: dict[str, int] = {}
    for position, column in enumerate(header, start=1):
        if not column.strip() or not normalize_column(column):
            raise MappingError(f"CSV source {path} has a blank header at position {position}")
        if column in exact:
            raise MappingError(
                f"CSV source {path} has duplicate headers at positions "
                f"{exact[column]} and {position}"
            )
        exact[column] = position
        normalized_column = normalize_column(column)
        if normalized_column in normalized:
            raise MappingError(
                f"CSV source {path} has normalization-colliding headers at positions "
                f"{normalized[normalized_column]} and {position}"
            )
        normalized[normalized_column] = position


def _authored_aliases(actual_columns: list[str], fmt: SourceFormat, path: Path) -> dict[str, str]:
    """Bind runtime columns to reviewed names, failing closed on ambiguity."""
    authored: dict[str, str] = {}
    for position, column in enumerate(fmt.columns, start=1):
        normalized = normalize_column(column)
        if not normalized:
            raise MappingError(f"mapping format has a blank authored column at position {position}")
        if normalized in authored:
            raise MappingError(
                "mapping format has normalization-colliding authored columns at "
                f"position {position}"
            )
        authored[normalized] = column

    actual: dict[str, str] = {}
    for position, column in enumerate(actual_columns, start=1):
        normalized = normalize_column(column)
        if not normalized:
            raise MappingError(f"source {path} has a blank column at position {position}")
        if normalized in actual:
            raise MappingError(
                f"source {path} has normalization-colliding columns at position {position}"
            )
        actual[normalized] = column

    if set(actual) != set(authored):
        raise MappingError(
            f"source {path} columns do not uniquely match the {len(authored)} authored columns "
            f"({len(actual)} runtime columns)"
        )
    return {column: authored[normalized] for normalized, column in actual.items()}


def _bound_rows(rows: list[Row], columns: list[str], fmt: SourceFormat, path: Path) -> list[Row]:
    """Rewrite runtime keys to their authored mapping keys by normalized alias."""
    aliases = _authored_aliases(columns, fmt, path)
    return [{aliases[column]: value for column, value in row.items()} for row in rows]


def _read_text(path: Path, encoding: str) -> str:
    try:
        return path.read_text(encoding=encoding)
    except (OSError, UnicodeError) as exc:
        raise MappingError(f"cannot read source file {path}: {type(exc).__name__}") from exc


def _json_records(path: Path, encoding: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(
            _read_text(path, encoding), object_pairs_hook=_object_without_duplicate_keys
        )
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
            obj = json.loads(line, object_pairs_hook=_object_without_duplicate_keys)
        except ValueError as exc:
            raise MappingError(f"NDJSON source {path} line {lineno}: {type(exc).__name__}") from exc
        if not isinstance(obj, dict):
            raise MappingError(f"NDJSON source {path} line {lineno} is not an object")
        records.append(obj)
    return records


def _clean_csv_row(row: dict[str | None, object], header: list[str], path: Path, line: int) -> Row:
    """One DictReader row as a plain str->str mapping, or a loud refusal.
    ``DictReader`` puts surplus cells under the ``None`` key as a list, so a
    row wider than its header looks ordinary with one odd entry — reading
    that is how a shifted cell becomes a value under the wrong column."""
    if None in row:
        overflow = row[None]
        overflow_count = len(overflow) if isinstance(overflow, list) else 1
        raise MappingError(
            f"CSV source {path} row {line} has {overflow_count} cells "
            f"beyond its {len(header)} headers"
        )
    clean: Row = {}
    for column, value in row.items():
        if column is None or isinstance(value, list):
            raise MappingError(f"CSV source {path} row {line} has malformed cells")
        clean[column] = value  # type: ignore[assignment]
    return clean


def _read_delimited(path: Path, fmt: SourceFormat) -> list[Row]:
    """Every row of a CSV/TSV source, header-validated and shape-checked."""
    try:
        with path.open(encoding=fmt.encoding, newline="") as handle:
            header = next(csv.reader(handle, delimiter=_delimiter(fmt)), None)
            if header is None:
                raise MappingError(f"source file {path} is empty (no header row)")
            _validate_csv_header(header, path)
            reader = csv.DictReader(handle, fieldnames=header, delimiter=_delimiter(fmt))
            rows = [
                _clean_csv_row(row, header, path, line) for line, row in enumerate(reader, start=2)
            ]
            return _bound_rows(rows, header, fmt, path)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise MappingError(f"cannot read source file {path}: {type(exc).__name__}") from exc


def read_rows(path: Path, fmt: SourceFormat) -> list[Row]:
    """Read every row into authored-header dicts (loud on malformed input)."""
    if fmt.type in ("csv", "tsv"):
        return _read_delimited(path, fmt)
    records = (
        _json_records(path, fmt.encoding)
        if fmt.type == "json"
        else _ndjson_records(path, fmt.encoding)
    )
    rows: list[Row] = []
    columns: list[str] = []
    seen: set[str] = set()
    for record in records:
        flat: Row = {}
        _flatten(record, "", flat)
        rows.append(flat)
        for column in flat:
            if column not in seen:
                seen.add(column)
                columns.append(column)
    return _bound_rows(rows, columns, fmt, path)


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
        _validate_csv_header(header, path)
        return header
    # JSON/NDJSON: union of flattened keys across records, first-seen order.
    ordered: list[str] = []
    seen: set[str] = set()
    records = (
        _json_records(path, fmt.encoding)
        if fmt.type == "json"
        else _ndjson_records(path, fmt.encoding)
    )
    for record in records:
        row: Row = {}
        _flatten(record, "", row)
        for key in row:
            if key not in seen:
                seen.add(key)
                ordered.append(key)
    return ordered


def find_source_file(export_dir: Path, fmt: SourceFormat) -> Path:
    """Locate the file in ``export_dir`` whose columns match ``fmt``'s
    fingerprint. Raises :class:`MappingError` when no candidate of the
    right type exists, or when one exists but its columns don't match the
    fingerprint (the "stale" case — re-run ``source init``)."""
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
            columns = read_columns(candidate, fmt)
            if header_fingerprint(columns) == fmt.header_fingerprint:
                # A normalized fingerprint is only a coarse discovery key; a
                # candidate is executable only when columns bind one-to-one.
                _authored_aliases(columns, fmt, candidate)
                return candidate
        except MappingError:
            continue  # an unreadable candidate is simply not a match
    raise MappingError(
        f"a {fmt.type} file is present in {export_dir} but its columns no longer match "
        "this learned mapping — re-run 'anast source init' to relearn the format"
    )
