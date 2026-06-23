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
