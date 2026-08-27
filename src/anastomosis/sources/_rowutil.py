"""Row-cell helpers shared by the table-mapper adapters (oracle_ehi, pf_tebra).

Both adapters map flat rows — ``dict[str, str | None]``, aliased ``Row`` in
each loader (``oracle_ehi.loader.Row``, ``pf_tebra.loader.Row``) — into the
canonical model under the same lossless discipline: every column a mapping
doesn't explicitly consume rides ``extensions`` under a ``{source}:``
namespace. These five functions were maintained as separate, byte-identical
(or near-identical) copies in both mapper modules; this is the one
definition. Each mapper keeps its own ``SOURCE`` constant and passes it to
:func:`residual`.
"""

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
    """Everything ``row`` carries that ``mapped`` didn't consume — the lossless catch-all.

    ``prefix`` qualifies the namespaced key for a row that is not the mapping's
    own primary row (a side row folded onto another record), so several
    tables' surplus columns can share one ``extensions`` dict without
    colliding. A prefix opens its own sub-namespace rather than starting with
    a table name, so no prefixed key can ever be spelled by an unprefixed
    column name.
    """
    return {
        f"{source}:{prefix}{col}": value
        for col, value in row.items()
        if col is not None and col not in mapped and clean_cell(value) is not None
    }
