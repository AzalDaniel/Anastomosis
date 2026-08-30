"""TSV loading for PF/Tebra EHI exports.

Kept dumb on purpose: read every TSV in the export into header-keyed rows and
nothing else. All semantics (sentinels, joins, type parsing) live in the
mapper, so a future column rename is a mapper diff, not a loader rewrite.

Losslessness boundary: the loader discovers EVERY ``*.tsv`` in the export — not
only :data:`KNOWN_TABLES` — so the mapper can account for all of them. Tables the
mapper does not map are preserved (patient-keyed rows into each patient's
``extensions``) or, when they cannot be attributed to a patient, the run is
refused (:class:`UnsupportedTablesError`) rather than the data being dropped.
A row in a table the mapper DOES map is refused the same way when its foreign
key names no record in the export (:class:`OrphanRowsError`).
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass
from functools import cache
from importlib import resources
from pathlib import Path

from anastomosis.sources.base import SourceDataError

__all__ = [
    "KNOWN_TABLES",
    "V9_REFERENCE_COLUMNS",
    "Attachments",
    "Export",
    "MalformedExportError",
    "OrphanRowsError",
    "Row",
    "UnsupportedTablesError",
    "find_attachments",
    "read_export",
    "read_table",
]

Row = dict[str, str | None]
Export = dict[str, list[Row]]

#: The export's own tables. Everything else on disk is a candidate attachment.
_TABLE_SUFFIX = ".tsv"


class UnsupportedTablesError(SourceDataError):
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


class OrphanRowsError(SourceDataError):
    """A KNOWN table carries rows whose foreign key names no record in the export.

    The mapper reads these tables by slicing a per-key grouping with the owning
    record's guid, so a row keyed to a patient (encounter, allergy,
    prescription, relative) that is not in the export is grouped once and never
    read again — it would vanish with no sentinel and no extension. Raised
    instead, the same fail-closed stance :class:`UnsupportedTablesError` takes
    for an orphan table. The message names the table(s) and the orphan row
    COUNTS only — schema names and counts, never row values.
    """

    def __init__(self, orphans: dict[str, int]) -> None:
        self.orphans = dict(sorted(orphans.items()))
        detail = ", ".join(f"{table} ({count} row(s))" for table, count in self.orphans.items())
        super().__init__(
            "export contains rows whose foreign key names no record in the export "
            f"(a missing or dangling key): {detail}. Repair the export, or remove "
            "the orphaned rows, before migrating."
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
    "patient-encounter-events",
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
    "patient-goals",
    "patient-health-concerns",
    "patient-documents",
    "providers",
    "facilities",
    "pinned-notes",
)


# csv.DictReader collects fields beyond the header count under this key. The name
# is a deliberate non-column sentinel — v9 columns are PascalCase (PatientPractice
# Guid), so it cannot collide with a real header.
_OVERFLOW_KEY = "__overflow__"


@cache
def v9_reference_columns() -> dict[str, tuple[str, ...]]:
    """The vendor's own v9 column dictionary, shipped with the adapter.

    One copy, used by the loader's vendor-defect recovery below and by the
    schema-reference tests — extracted from the vendor documentation, names
    only, nothing patient-derived. Cached because the loader may consult it
    once per table read.
    """
    raw = resources.files("anastomosis.sources.pf_tebra").joinpath("pf_v9_columns.json")
    tables: dict[str, list[str]] = json.loads(raw.read_text(encoding="utf-8"))
    return {name: tuple(cols) for name, cols in tables.items()}


#: Alias kept for discoverability from the package surface.
V9_REFERENCE_COLUMNS = v9_reference_columns


#: Header defects the VENDOR ships, verified against real exports and repaired
#: only when everything about the observed shape agrees with the repair.
#:
#: ``patient-contacts``: two independent real v9 exports carry this exact
#: five-column header — it is another table's schema pasted over the contacts
#: table, ending in the two modification columns in the wrong order — while
#: every data row underneath has the 15 cells the vendor's own dictionary
#: documents for the table. The header is the anomaly; the rows are not. An
#: export with rows under that header is unloadable without this, and "fix
#: the export before migrating" is not an instruction an operator can follow
#: by hand against PHI they must not edit.
#:
#: The repair is deliberately narrow. It applies only when the table name AND
#: the exact anomalous header AND a uniform row width equal to the reference
#: column count all hold; any other surplus — mixed widths, one extra cell, a
#: different header — still refuses as corruption. Recovery logs the table
#: name and row count only, never a value.
_VENDOR_HEADER_DEFECTS: dict[tuple[str, tuple[str, ...]], str] = {
    (
        "patient-contacts",
        (
            "PatientPracticeGuid",
            "NoteType",
            "NoteText",
            "LastModifiedDateTimeUtc",
            "LastModifiedByUserGuid",
        ),
    ): "patient-contacts",
}

logger = logging.getLogger(__name__)


class MalformedExportError(SourceDataError):
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
        header = tuple(reader.fieldnames or ())
        reference = _VENDOR_HEADER_DEFECTS.get((name, header))
        if reference is not None:
            return _read_with_repaired_header(name, path, v9_reference_columns()[reference])
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


def _read_with_repaired_header(name: str, path: Path, columns: tuple[str, ...]) -> list[Row]:
    """Read a table whose header is a registered vendor defect, under the
    reference schema instead.

    Fail-closed at every step: EVERY data row must have exactly the reference
    width. One row wider, narrower, or the header repeated mid-file, and the
    whole table refuses as corruption — the repair never widens into a general
    tolerance for misshapen rows.
    """
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh, delimiter="\t")
        next(reader)  # the anomalous header, already matched exactly by the caller
        rows: list[Row] = []
        for cells in reader:
            if not cells:  # a blank line, as DictReader would also skip
                continue
            if len(cells) != len(columns):
                raise MalformedExportError(name, reader.line_num)
            rows.append(dict(zip(columns, cells, strict=True)))
    # Counts and schema only — a contacts row is a person's name and address.
    logger.warning(
        "%s.tsv: vendor header defect repaired from the v9 reference schema "
        "(%d column(s) in the shipped header, %d in the reference; %d row(s) read)",
        name,
        len(_anomalous_header_for(name)),
        len(columns),
        len(rows),
    )
    return rows


def _anomalous_header_for(name: str) -> tuple[str, ...]:
    """The registered anomalous header for ``name`` (exists by construction
    when called from the repair path)."""
    for (table, header), _reference in _VENDOR_HEADER_DEFECTS.items():
        if table == name:
            return header
    raise KeyError(name)


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


@dataclass(frozen=True)
class Attachments:
    """The files an export carries beside its tables, found by storage id.

    Holds the export root as well as the index because a document row needs two
    different answers about the same file: an absolute path to READ it (to hash
    it, to count its pages) and a root-relative one to RECORD, so the chart says
    ``binary-content/<id>.pdf`` and not the operator's home directory. An
    exported bundle travels to another EHR; it has no business carrying the
    layout of the machine that made it.
    """

    root: Path
    by_id: dict[str, Path]

    def find(self, storage_id: str) -> Path | None:
        """The absolute path for a storage id, or None if the export lacks it."""
        return self.by_id.get(storage_id.lower()) if storage_id else None

    def relative(self, path: Path) -> str:
        """How to name that file in a chart: relative to the export root."""
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:  # not under the root — the name alone still locates it
            return path.name


def find_attachments(root: Path) -> Attachments:
    """Index every non-table file in the export, keyed by its filename stem.

    A ``patient-documents`` row names its file by a storage GUID, and exports
    disagree about where those files go — a ``binary-content/`` folder in one,
    a flat directory beside the tables in another. Indexing by stem instead of
    by an assumed path means the mapper finds the file wherever the export
    happened to put it, without the adapter having to encode a layout it would
    then be wrong about.

    A stem naming more than one file is left OUT of the index rather than
    resolved to whichever the walk reached first. Two candidates for one
    document is an ambiguity, and choosing between them would be a guess about
    which file belongs in a patient's chart. Unresolved is recoverable; wrong
    is not.
    """
    found: dict[str, Path] = {}
    ambiguous: set[str] = set()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() == _TABLE_SUFFIX:
            continue
        stem = path.stem.lower()
        if stem in found:
            ambiguous.add(stem)
        found[stem] = path
    for stem in ambiguous:
        del found[stem]
    return Attachments(root=root, by_id=found)
