"""TSV loading for PF/Tebra EHI exports.

Kept dumb on purpose: read every known table into header-keyed rows and
nothing else. All semantics (sentinels, joins, type parsing) live in the
mapper, so a future column rename is a mapper diff, not a loader rewrite.
"""

from __future__ import annotations

import csv
from pathlib import Path

__all__ = ["KNOWN_TABLES", "Export", "Row", "read_export", "read_table"]

Row = dict[str, str | None]
Export = dict[str, list[Row]]

# Tables this adapter consumes today. Unknown TSVs in an export are fine —
# they're simply not read yet (and a real v9 export has 85 of them).
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
    "patient-family-medical-history",
    "patient-family-history-diagnoses",
    "patient-immunizations",
    "patient-advance-directives",
    "patient-documents",
    "providers",
    "facilities",
    "pinned-notes",
)


def read_table(root: Path, name: str) -> list[Row]:
    """Read one TSV into dict rows; a missing table is an empty list."""
    path = root / f"{name}.tsv"
    if not path.is_file():
        return []
    # utf-8-sig: tolerate a BOM, which Windows-produced exports may carry.
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return [dict(row) for row in csv.DictReader(fh, delimiter="\t")]


def read_export(root: Path) -> Export:
    """Read every known table from an export directory."""
    return {name: read_table(root, name) for name in KNOWN_TABLES}
