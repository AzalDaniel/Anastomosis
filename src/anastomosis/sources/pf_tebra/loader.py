# AI-assisted: written with Claude agents under the author's direction and review; see DESIGN.md.
"""TSV loading for PF/Tebra EHI exports.

Kept dumb on purpose: read every TSV in the export into header-keyed rows and
nothing else. All semantics (sentinels, joins, type parsing) live in the
mapper, so a future column rename is a mapper diff, not a loader rewrite.

Losslessness boundary: the loader discovers EVERY ``*.tsv`` in the export — not
only :data:`KNOWN_TABLES` — so the mapper can account for all of them. Tables the
mapper does not map are preserved (patient-keyed rows into each patient's
``extensions``) or, when they cannot be attributed to a patient, the run is
refused (:class:`UnsupportedTablesError`) rather than the data being dropped.
"""

from __future__ import annotations

import csv
from pathlib import Path

__all__ = [
    "KNOWN_TABLES",
    "Export",
    "MalformedExportError",
    "Row",
    "UnsupportedTablesError",
    "read_export",
    "read_table",
]

Row = dict[str, str | None]
Export = dict[str, list[Row]]


class UnsupportedTablesError(Exception):
    """An export carries tables the adapter can neither map nor losslessly keep.

    Raised by the mapper when an unmapped table has no patient key to attribute
    its rows to — failing closed beats silently discarding clinical data. The
    message names the offending table(s) only (schema names, never row values).
    """

    def __init__(self, tables: list[str]) -> None:
        self.tables = tables
        super().__init__(
            "export contains unmapped tables that cannot be attributed to a patient "
            f"(no PatientPracticeGuid column): {tables}. Map them in the adapter, or "
            "remove them from the export, before migrating."
        )


# Tables the mapper consumes today. Everything else found in the export is still
# READ (see read_export) and preserved by the mapper — a real v9 export has ~85
# tables, of which these are the mapped subset.
KNOWN_TABLES = (
    "patient-demographics",
    "patient-race",
    "patient-ethnicity",
    "patient-gender-identity-sexual-orientation",
    "patient-smokingstatus",
    "occupation-industry",
    "patient-education",
    "patient-financial-resources",
    "tribal-affiliation",
    "patient-encounters",
    "patient-encounter-addendums",
    "patient-encounter-observations",
    "patient-diagnoses",
    "patient-encounter-diagnoses",
    "patient-allergy",
    "patient-allergy-reactions",
    "patient-medications",
    "patient-prescriptions",
    "prescription-transactions",
    "patient-insurances",
    "superbill-insurances",
    "patient-guarantor",
    "patient-med-history",
    "patient-family-medical-history",
    "patient-family-history-diagnoses",
    "patient-immunizations",
    "patient-advance-directives",
    "patient-documents",
    "providers",
    "facilities",
    "pinned-notes",
)


# csv.DictReader collects fields beyond the header count under this key. The name
# is a deliberate non-column sentinel — v9 columns are PascalCase (PatientPractice
# Guid), so it cannot collide with a real header.
_OVERFLOW_KEY = "__overflow__"


class MalformedExportError(Exception):
    """A TSV row does not line up with its header (likely an unquoted tab in a
    cell). The message names the file and physical line only — never row values."""

    def __init__(self, table: str, line: int) -> None:
        self.table = table
        self.line = line
        super().__init__(
            f"{table}.tsv line {line} has more columns than its header — an unquoted "
            "tab in a cell would misalign the row. Fix the export before migrating."
        )


def read_table(root: Path, name: str) -> list[Row]:
    """Read one TSV into dict rows; a missing table is an empty list.

    Raises :class:`MalformedExportError` on a row whose column count exceeds the
    header's — a corrupted row must not pass silently with shifted values.
    """
    path = root / f"{name}.tsv"
    if not path.is_file():
        return []
    # utf-8-sig: tolerate a BOM, which Windows-produced exports may carry.
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh, delimiter="\t", restkey=_OVERFLOW_KEY)
        rows: list[Row] = []
        for row in reader:
            # Raise only when a surplus cell carries DATA — an unquoted tab split a
            # real cell and shifted every later column, so a NAMED column now holds
            # the wrong value. A purely trailing-empty surplus (some exporters append
            # a delimiter to each data row) carries nothing, misaligns no named
            # column, and is dropped. (The learned-source reader in sources/learned/
            # reader.py is intentionally more lenient and drops all surplus; pf_tebra
            # knows v9 has no embedded tabs, so a data-bearing surplus is corruption.)
            surplus = row.pop(_OVERFLOW_KEY, None)
            if surplus and any(v and v.strip() for v in surplus):
                raise MalformedExportError(name, reader.line_num)
            rows.append(dict(row))
        return rows


def read_export(root: Path) -> Export:
    """Read EVERY ``*.tsv`` in the export, keyed by filename stem.

    Every :data:`KNOWN_TABLES` key is always present (absent file → empty list, so
    the mapper's ``export[...]`` lookups never KeyError), plus every other TSV
    discovered on disk. Discovering all of them — not just the mapped subset — is
    what lets the mapper preserve or refuse unmapped tables instead of silently
    skipping them.
    """
    discovered = sorted(p.stem for p in root.glob("*.tsv"))
    known = set(KNOWN_TABLES)
    names = list(KNOWN_TABLES) + [stem for stem in discovered if stem not in known]
    return {name: read_table(root, name) for name in names}
