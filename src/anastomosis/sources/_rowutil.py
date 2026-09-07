"""Row-cell helpers shared by the table-mapper adapters (oracle_ehi, pf_tebra).

Both map flat rows — ``dict[str, str | None]``, aliased ``Row`` in each
loader — into the canonical model under the extensions-namespace discipline
(rule 63); this is the one definition (rule 84). Each mapper keeps its own
``SOURCE`` constant and passes it to :func:`residual`."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from anastomosis.core.textutil import clean_cell
from anastomosis.core.timeutil import parse_date, parse_dt

__all__ = ["Row", "clean_date", "clean_dt", "clean_str", "group_by", "residual"]

# Matches ``oracle_ehi.loader.Row`` / ``pf_tebra.loader.Row`` exactly (both are
# this same alias) — a cell-keyed row as the loader produced it.
Row = dict[str, str | None]


def clean_str(row: Row, col: str) -> str | None:
    return clean_cell(row.get(col))


def clean_dt(row: Row, col: str) -> datetime | None:
    return parse_dt(clean_str(row, col))


def clean_date(row: Row, col: str) -> date | None:
    return parse_date(clean_str(row, col))


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
