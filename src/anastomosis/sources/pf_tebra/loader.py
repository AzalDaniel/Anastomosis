"""TSV loading for PF/Tebra EHI exports.

Kept dumb on purpose: reads every TSV into header-keyed rows and nothing
else — sentinels, joins and type parsing all live in the mapper.

The loader discovers EVERY ``*.tsv``, not only :data:`KNOWN_TABLES` (rule
63): an unmapped table is preserved into ``extensions`` or, with no path to
a patient, refused (:class:`UnsupportedTablesError`); a mapped row whose
foreign key names no record is refused the same way
(:class:`OrphanRowsError`)."""

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
    """An unmapped table with no path to a patient — no ``PatientPracticeGuid``
    column, no declared indirect join, no practice-level identity of its own.
    Raised rather than silently discarding clinical data. Schema names only,
    never row values."""

    def __init__(self, tables: list[str]) -> None:
        self.tables = tables
        super().__init__(
            "export contains unmapped tables with no path to a patient (no "
            f"PatientPracticeGuid column and no declared join): {tables}. Map "
            "them in the adapter, or remove them from the export, before migrating."
        )


class OrphanRowsError(SourceDataError):
    """A KNOWN table's foreign key names no record in the export. The mapper
    groups rows once by the owning guid, so an orphan would otherwise vanish
    with no sentinel and no extension; raised instead, fail-closed like
    :class:`UnsupportedTablesError`. Table names and row COUNTS only."""

    def __init__(self, orphans: dict[str, int]) -> None:
        self.orphans = dict(sorted(orphans.items()))
        detail = ", ".join(f"{table} ({count} row(s))" for table, count in self.orphans.items())
        super().__init__(
            "export contains rows whose foreign key names no record in the export "
            f"(a missing or dangling key): {detail}. Repair the export, or remove "
            "the orphaned rows, before migrating."
        )


# Tables the mapper consumes today; everything else is still read (read_export)
# and preserved by the mapper — a real v9 export has ~85 tables total.
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


# csv.DictReader collects fields beyond the header count under this key; the
# name can't collide with a real header (v9 columns are all PascalCase).
_OVERFLOW_KEY = "__overflow__"


@cache
def v9_reference_columns() -> dict[str, tuple[str, ...]]:
    """The vendor's own v9 column dictionary, shipped with the adapter —
    names only, nothing patient-derived. Cached: the loader may consult it
    once per table read."""
    raw = resources.files("anastomosis.sources.pf_tebra").joinpath("pf_v9_columns.json")
    tables: dict[str, list[str]] = json.loads(raw.read_text(encoding="utf-8"))
    return {name: tuple(cols) for name, cols in tables.items()}


#: Alias kept for discoverability from the package surface.
V9_REFERENCE_COLUMNS = v9_reference_columns


#: Header defects the VENDOR ships, verified against real exports: two
#: independent v9 exports carry ``patient-contacts``'s exact five-column
#: header — another table's schema pasted over it — while every data row
#: still has the vendor-documented 15 cells. Repaired only when the table
#: name, the exact anomalous header, and a uniform row width equal to the
#: reference column count all hold; any other surplus still refuses as
#: corruption. Recovery logs table name and row count only, never a value.
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
            # Raise only when a surplus cell carries DATA (an unquoted tab shifted a
            # named column); a purely trailing-empty surplus is dropped. The learned
            # reader is intentionally more lenient (drops all surplus): only pf_tebra
            # knows v9 has no embedded tabs.
            surplus = row.pop(_OVERFLOW_KEY, None)
            if surplus and any(v and v.strip() for v in surplus):
                raise MalformedExportError(name, reader.line_num)
            rows.append(dict(row))
        return rows


def _read_with_repaired_header(name: str, path: Path, columns: tuple[str, ...]) -> list[Row]:
    """Read a table whose header is a registered vendor defect, under the
    reference schema instead. Fail-closed at every step: any row not exactly
    the reference width, or the header repeated mid-file, refuses the whole
    table as corruption rather than widening into a general tolerance."""
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
    """Read EVERY ``*.tsv`` in the export, keyed by filename stem. Every
    :data:`KNOWN_TABLES` key is always present (absent file → empty list, so
    the mapper's lookups never KeyError), plus every other TSV on disk —
    discovering all of them is what lets the mapper preserve or refuse
    unmapped tables instead of silently skipping them."""
    discovered = sorted(p.stem for p in root.glob("*.tsv"))
    known = set(KNOWN_TABLES)
    names = list(KNOWN_TABLES) + [stem for stem in discovered if stem not in known]
    return {name: read_table(root, name) for name in names}


@dataclass(frozen=True)
class Attachments:
    """Files an export carries beside its tables, indexed by storage id.
    Keeps the export root too: a document row needs an absolute path to
    READ a file and a root-relative one to RECORD it, so a chart says
    ``binary-content/<id>.pdf``, never the machine's own layout."""

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
    """Index every non-table file in the export, keyed by filename stem —
    exports disagree about where attachment files live, so this finds them
    wherever they are rather than assuming a layout. A stem matching more
    than one file is dropped from the index rather than guessed at:
    unresolved is recoverable, wrong is not."""
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
