"""Row-cell readers and field tables shared by the table-mapper adapters.

``oracle_ehi`` and ``pf_tebra`` map flat rows into the canonical model under
the extensions-namespace discipline (rule 63); this is the one definition
(rule 84). A :class:`RowTable` names each consumed column once and builds the
record, so the declaration, the lift and the residual cannot drift apart."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from anastomosis.core.model import Provenance
from anastomosis.core.textutil import clean_cell
from anastomosis.core.timeutil import parse_date, parse_dt

__all__ = [
    "FieldMap",
    "Row",
    "RowTable",
    "cell_date",
    "cell_dt",
    "cell_key",
    "clean_date",
    "clean_dt",
    "clean_str",
    "group_by",
    "residual",
]

# Matches ``oracle_ehi.loader.Row`` / ``pf_tebra.loader.Row`` exactly (both are
# this same alias) — a cell-keyed row as the loader produced it.
Row = dict[str, str | None]

_M = TypeVar("_M", bound=BaseModel)


def cell_date(value: str | None) -> date | None:
    return parse_date(clean_cell(value))


def cell_dt(value: str | None) -> datetime | None:
    return parse_dt(clean_cell(value))


def cell_key(value: str | None) -> str:
    """A required id: blank reads as ``""``, never a freshly minted uuid."""
    return clean_cell(value) or ""


def clean_str(row: Row, col: str) -> str | None:
    return clean_cell(row.get(col))


def clean_dt(row: Row, col: str) -> datetime | None:
    return cell_dt(row.get(col))


def clean_date(row: Row, col: str) -> date | None:
    return cell_date(row.get(col))


@dataclass(frozen=True, slots=True)
class FieldMap:
    """One structural rule: source column -> model field, via a transform —
    :class:`anastomosis.sources.learned.spec.FieldMapping`'s shape for the
    built-in adapters, whose targets are the whole canonical model."""

    source_path: str
    target_path: str
    transform: Callable[[str | None], Any] = clean_cell


class RowTable(Generic[_M]):
    """One source table: the model it builds, the source and file its
    provenance names, and every column it consumes (rule 63) — a
    :class:`FieldMap` per field filled, a bare name per column read into
    something no field holds. ``provenance_key`` reads a blank id as ``""``."""

    __slots__ = ("consumed", "fields", "file", "model", "provenance_id", "provenance_key", "source")

    def __init__(
        self,
        model: type[_M],
        source: str,
        file: str,
        *columns: FieldMap | str,
        provenance_id: str | None = None,
        provenance_key: bool = False,
    ) -> None:
        self.model = model
        self.source = source
        self.file = file
        self.fields = tuple(c for c in columns if isinstance(c, FieldMap))
        self.consumed = frozenset(c.source_path if isinstance(c, FieldMap) else c for c in columns)
        self.provenance_id = provenance_id
        self.provenance_key = provenance_key

    def build(self, row: Row, *, extensions: dict[str, Any] | None = None, **fields: Any) -> _M:
        """One row built: every declared field through its own transform, every
        other valued column into ``extensions`` (rule 63), provenance naming the
        file and the row. ``fields`` and ``extensions`` add to those."""
        kwargs: dict[str, Any] = {
            f.target_path: f.transform(row.get(f.source_path)) for f in self.fields
        }
        if self.provenance_id is not None:
            read = cell_key if self.provenance_key else clean_cell
            kwargs["provenance"] = Provenance(
                source_system=self.source,
                source_file=self.file,
                source_id=read(row.get(self.provenance_id)),
            )
        kwargs |= fields
        kwargs["extensions"] = residual(row, self.consumed, self.source) | (extensions or {})
        return self.model(**kwargs)

    def build_all(self, rows: list[Row]) -> list[_M]:
        return [self.build(row) for row in rows]


def group_by(rows: list[Row], col: str) -> dict[str, list[Row]]:
    """Group ``rows`` by the value of ``col``; a row with no value there is dropped."""
    grouped: dict[str, list[Row]] = {}
    for row in rows:
        key = clean_str(row, col)
        if key is not None:
            grouped.setdefault(key, []).append(row)
    return grouped


def residual(row: Row, mapped: frozenset[str], source: str, prefix: str = "") -> dict[str, Any]:
    """Everything ``row`` carries that ``mapped`` didn't consume (rule 63).
    ``prefix`` opens its own sub-namespace for a side row folded onto another
    record, so no prefixed key can ever collide with an unprefixed column
    name."""
    return {
        f"{source}:{prefix}{col}": value
        for col, value in row.items()
        if col is not None and col not in mapped and clean_cell(value) is not None
    }
