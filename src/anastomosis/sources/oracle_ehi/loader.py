"""SQL-dump loading for Oracle Health (Cerner Millennium) EHI exports.

The single-patient Millennium export ships the V500 data model as MySQL
``INSERT`` dumps split across three subdirectories — ``v500/{schema,activity,
reference}`` — per the brief's §5.1 packaging table. The DDL under
``v500/schema`` is irrelevant to ingest; the data we need lives in the
``activity`` and ``reference`` INSERT dumps as ``<table_name><_##>.sql``
(1..N files per table, §5.1).

This module is deliberately dumb: a dependency-free reader that turns the
``INSERT`` statements for *only the tables the mapper consumes* into
header-keyed rows. The brief (§3.1) documents ~6,590 tables; loading all of
them would be absurd, so the loader is told which tables to read and ignores
the rest. All semantics (sentinels, joins, code resolution, blob decoding)
live in the mapper, mirroring the PF/Tebra loader/mapper split.

The INSERT parser handles what real MySQL dumps contain — multi-row
``INSERT`` statements, single-quoted strings with backslash and doubled-quote
escapes, ``NULL``, and bare numbers — and raises a loud :exc:`ValueError`
naming the offending file on malformed SQL. It is **not** a general SQL
engine: DDL, comments, and ``INSERT`` targets we did not ask for are skipped.
Column names come from each table's DDL in ``v500/schema`` so value tuples can
be keyed without guessing.
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = [
    "ACTIVITY_TABLES",
    "REFERENCE_TABLES",
    "Export",
    "Row",
    "iter_insert_rows",
    "parse_column_names",
    "read_export",
    "read_table",
]

# A cell is the raw lexeme as it appeared in the dump: a decoded string for
# quoted/number literals, or ``None`` for SQL ``NULL``. The mapper applies all
# sentinel and type semantics.
Row = dict[str, str | None]
Export = dict[str, list[Row]]

# Tables the mapper consumes today, grouped by the V500 classification that
# decides which subdirectory they ship in (§3.1, §5.1). A real export carries
# thousands more; they are simply not read. Names are verified against the
# brief's cited data-model reports (§3.2, §4).
ACTIVITY_TABLES = (
    "PERSON",  # §3.2 dms_person3.html — identity spine
    "PERSON_ALIAS",  # task scope (MRN); columns undocumented in brief → extensions
    "ENCOUNTER",  # §3.2 dms_encounter17.html
    "CLINICAL_EVENT",  # §3.2 dms_clinical_events10.html — clinical spine
    "CE_BLOB",  # §4.1 dms_clinical_events1.html — local document text
    "CE_BLOB_RESULT",  # §4.2 dms_clinical_events1.html — remote document refs
    "ENCNTR_PRSNL_RELTN",  # supporting; columns → extensions if present
)
# Reference (dictionary) tables. CODE_VALUE resolves every ``*_CD`` numeric
# key (§3.2 dms_code_sets2.html).
REFERENCE_TABLES = ("CODE_VALUE",)

KNOWN_TABLES = ACTIVITY_TABLES + REFERENCE_TABLES

# V500 subdirectories that carry INSERT dumps (§5.1).
_DATA_DIRS = ("activity", "reference")
# ``<table_name>`` or ``<table_name>_<digits>`` (.sql), per §5.1 file naming.
_DUMP_STEM_RE = re.compile(r"^(?P<table>[A-Za-z0-9_]+?)(?:_\d+)?$")

# DDL column extraction: ``CREATE TABLE `FOO` ( `COL` TYPE, ... )``. MySQL
# quotes identifiers in backticks; we only need the ordered column names.
_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+`?(?P<table>\w+)`?\s*\((?P<body>.*?)\)\s*(?:ENGINE|;|$)",
    re.IGNORECASE | re.DOTALL,
)
_COLUMN_DEF_RE = re.compile(r"^\s*`(?P<col>\w+)`\s", re.MULTILINE)
# Constraint lines that are not column definitions.
_NON_COLUMN_RE = re.compile(r"^\s*(?:PRIMARY|UNIQUE|KEY|INDEX|CONSTRAINT|FOREIGN)\b", re.IGNORECASE)


def _table_of(stem: str) -> str | None:
    """Map a dump-file stem to its table name, upper-cased for matching."""
    match = _DUMP_STEM_RE.match(stem)
    return match.group("table").upper() if match else None


def parse_column_names(ddl: str, table: str) -> list[str] | None:
    """Return the ordered column list for ``table`` from a schema DDL string.

    ``None`` when the DDL has no ``CREATE TABLE`` for that table — the loader
    then cannot key that table's rows and raises with a clear message.
    """
    for match in _CREATE_TABLE_RE.finditer(ddl):
        if match.group("table").upper() != table.upper():
            continue
        body = match.group("body")
        columns = [
            col.group("col")
            for line in body.splitlines()
            if not _NON_COLUMN_RE.match(line) and (col := _COLUMN_DEF_RE.match(line))
        ]
        return columns or None
    return None


class _InsertLexer:
    """A single-pass MySQL lexer over a whole dump file.

    Scope is deliberately narrow: the literals that appear in Millennium EHI
    INSERT dumps — single-quoted strings (with ``\\`` escapes and ``''``
    doubling), ``NULL``, and bare numeric tokens. Anything else is malformed
    for our purposes and raises, naming the source file.

    The pass is *single*: string literals are consumed as opaque units before
    any ``INSERT ... VALUES`` head is matched, so a quoted cell whose text
    happens to contain a literal ``INSERT INTO ... VALUES`` shape can never be
    mistaken for a statement head (a regex ``finditer`` over the raw text
    cannot make that distinction — it is the false-collision this lexer
    closes). The head regex is only ever tried anchored at the current scan
    position, which the scanner only reaches outside a string literal.
    """

    def __init__(self, text: str, source: str) -> None:
        self._text = text
        self._source = source
        self._pos = 0
        self._len = len(text)

    def _fail(self, detail: str) -> None:
        # PHI-safe: report position and structural detail, never cell content.
        raise ValueError(f"malformed INSERT in {self._source}: {detail} at offset {self._pos}")

    def _skip_ws(self) -> None:
        while self._pos < self._len and self._text[self._pos] in " \t\r\n":
            self._pos += 1

    def _read_string(self) -> str:
        # Assumes current char is the opening quote.
        self._pos += 1
        chars: list[str] = []
        while self._pos < self._len:
            ch = self._text[self._pos]
            if ch == "\\":  # backslash escape: take the next char literally
                if self._pos + 1 >= self._len:
                    self._fail("unterminated escape")
                nxt = self._text[self._pos + 1]
                chars.append(_UNESCAPE.get(nxt, nxt))
                self._pos += 2
                continue
            if ch == "'":
                if self._pos + 1 < self._len and self._text[self._pos + 1] == "'":
                    chars.append("'")  # doubled-quote escape
                    self._pos += 2
                    continue
                self._pos += 1  # closing quote
                return "".join(chars)
            chars.append(ch)
            self._pos += 1
        self._fail("unterminated string literal")
        raise AssertionError  # pragma: no cover — _fail always raises

    def _read_bare(self) -> str | None:
        start = self._pos
        while self._pos < self._len and self._text[self._pos] not in ",)":
            self._pos += 1
        token = self._text[start : self._pos].strip()
        if not token:
            self._fail("empty value token")
        if token.upper() == "NULL":
            return None
        # Numbers (and the occasional DDL keyword like DEFAULT we never expect
        # inside a value tuple) pass through verbatim; the mapper types them.
        return token

    def read_statements(self, table: str, columns: list[str]) -> list[Row]:
        """Scan the whole file once, returning rows from matching INSERTs.

        The head regex is tried only anchored at the current position; when it
        matches, the statement's value tuples are read by :meth:`read_tuples`,
        which consumes each quoted cell *whole* via :meth:`_read_string`. So a
        cell whose text contains a literal ``INSERT INTO ... VALUES`` shape is
        swallowed as opaque string content and can never be re-scanned as a
        head — the false-collision the old ``finditer`` had. Between statements
        (comments, DDL, whitespace) the scanner just steps one char at a time;
        a stray apostrophe there is harmless filler, never a string start (real
        string literals live only inside value tuples, handled above).
        """
        rows: list[Row] = []
        while self._pos < self._len:
            head = _INSERT_HEAD_RE.match(self._text, self._pos)
            if head is None:
                self._pos += 1
                continue
            self._pos = head.end()
            cols = (
                [c.strip().strip("`") for c in head.group("cols").split(",")]
                if head.group("cols")
                else columns
            )
            tuples = self.read_tuples()
            if head.group("table").upper() != table.upper():
                continue  # a different table's INSERT: parsed past, not kept
            for values in tuples:
                if len(values) != len(cols):
                    # Not a SQL query: a parse-error message about a malformed
                    # dump file. The arity mismatch means we would misalign
                    # every field, so we raise rather than silently drop one.
                    self._fail(
                        f"tuple has {len(values)} values but {table} declares {len(cols)} columns"
                    )
                rows.append(dict(zip(cols, values, strict=True)))
        return rows

    def read_tuples(self) -> list[list[str | None]]:
        rows: list[list[str | None]] = []
        while True:
            self._skip_ws()
            if self._pos >= self._len:
                break
            if self._text[self._pos] != "(":
                self._fail("expected '(' starting a value tuple")
            self._pos += 1
            rows.append(self._read_tuple())
            self._skip_ws()
            if self._pos < self._len and self._text[self._pos] == ",":
                self._pos += 1  # between tuples
                continue
            if self._pos < self._len and self._text[self._pos] == ";":
                self._pos += 1  # consume the terminator; resume scanning after it
                break
        return rows

    def _read_tuple(self) -> list[str | None]:
        values: list[str | None] = []
        while True:
            self._skip_ws()
            if self._pos >= self._len:
                self._fail("unterminated value tuple")
            ch = self._text[self._pos]
            if ch == ")":
                self._pos += 1
                return values
            if ch == "'":
                values.append(self._read_string())
            else:
                values.append(self._read_bare())
            self._skip_ws()
            if self._pos >= self._len:
                self._fail("unterminated value tuple")
            ch = self._text[self._pos]
            if ch == ",":
                self._pos += 1
                continue
            if ch == ")":
                self._pos += 1
                return values
            self._fail("expected ',' or ')' after value")


# MySQL backslash escapes that do not map to themselves.
_UNESCAPE = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "b": "\b", "Z": "\x1a"}

# ``INSERT [IGNORE] INTO `TABLE` [(...)] VALUES`` up to the first value tuple.
# Matched only anchored at the lexer's current (outside-a-string) position via
# ``re.Pattern.match(text, pos)`` — never ``finditer`` over the raw text, which
# would false-collide with an ``INSERT ... VALUES`` shape inside a quoted cell.
_INSERT_HEAD_RE = re.compile(
    r"INSERT\s+(?:IGNORE\s+)?INTO\s+`?(?P<table>\w+)`?\s*"
    r"(?:\((?P<cols>[^)]*)\)\s*)?VALUES",
    re.IGNORECASE,
)


def iter_insert_rows(sql: str, table: str, columns: list[str], source: str) -> list[Row]:
    """Parse every ``INSERT INTO <table>`` in ``sql`` into header-keyed rows.

    A single lexer pass consumes string literals as opaque units before any
    statement head is matched, so a quoted cell containing a literal
    ``INSERT INTO ... VALUES`` shape can never be read as a head (the bug the
    old ``finditer``-over-the-whole-file approach had).

    ``columns`` is the DDL column order for the table. When an INSERT names
    its own column list, that ordering wins (some dumps reorder); otherwise
    the DDL order applies. A value tuple whose arity does not match the column
    count is malformed and raises (lossless rule: a mismatch means we would
    misalign every field, never silently drop one).
    """
    return _InsertLexer(sql, source).read_statements(table, columns)


def _read_schema(root: Path) -> str:
    """Concatenate every ``v500/schema`` DDL file into one string.

    Column names come from the ``V500TableSchema*.sql`` DDL (§5.1); reading
    all schema files is cheap (DDL is tiny next to the data dumps) and avoids
    guessing which file holds which table.
    """
    schema_dir = root / "v500" / "schema"
    if not schema_dir.is_dir():
        return ""
    parts: list[str] = []
    for path in sorted(schema_dir.glob("*.sql")):
        parts.append(path.read_text(encoding="latin-1"))  # §5.1: latin1 charset
    return "\n".join(parts)


def read_table(root: Path, table: str, columns: list[str]) -> list[Row]:
    """Read every dump file for one table across the V500 data directories."""
    rows: list[Row] = []
    for subdir in _DATA_DIRS:
        data_dir = root / "v500" / subdir
        if not data_dir.is_dir():
            continue
        for path in sorted(data_dir.glob("*.sql")):
            if _table_of(path.stem) != table.upper():
                continue
            sql = path.read_text(encoding="latin-1")  # §5.1: database charset latin1
            rows.extend(iter_insert_rows(sql, table, columns, path.name))
    return rows


def read_export(root: Path) -> Export:
    """Read every known table from a single-patient V500 export directory."""
    ddl = _read_schema(root)
    export: Export = {}
    for table in KNOWN_TABLES:
        columns = parse_column_names(ddl, table)
        if columns is None:
            # No DDL for a table we ask for means either the table is absent
            # from this export (fine — empty) or the schema files are missing.
            # We cannot key rows without column names, so a present-but-unkeyed
            # data file would be silent loss; guard that below.
            if _has_data_file(root, table):
                raise ValueError(
                    f"oracle_ehi: data dump for {table} present but no CREATE TABLE "
                    f"found in v500/schema — cannot key rows without column names"
                )
            export[table] = []
            continue
        export[table] = read_table(root, table, columns)
    return export


def _has_data_file(root: Path, table: str) -> bool:
    for subdir in _DATA_DIRS:
        data_dir = root / "v500" / subdir
        if data_dir.is_dir():
            for path in data_dir.glob("*.sql"):
                if _table_of(path.stem) == table.upper():
                    return True
    return False
