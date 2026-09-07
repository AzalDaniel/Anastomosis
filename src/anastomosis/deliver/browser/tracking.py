"""The crash-resumable upload ledger (M2 item 10): WAL-mode SQLite.

:meth:`TrackingDB.recover` rewinds mid-flight items to a safe state on a
killed run, so it continues without re-filing a chart. ``runs``, ``items``
and an append-only ``transitions`` audit trail; ``synchronous=FULL`` (45)
and one SQLite connection per thread are both deliberate.
PHI: no column for a name, DOB or address; ``file_path`` may embed one,
so the DB lives inside the hardened dir it tracks files in (45), and
``last_error_type``/``error_type`` store exception type names only.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from uuid import uuid4

from anastomosis.destinations.base import UploadItem

from .states import (
    CRASH_RECOVERY,
    TERMINAL_STATES,
    UploadState,
    validate_transition,
)

__all__ = ["TrackingDB"]

# States an item can be picked up from on resume: UPLOAD_INTERRUPTED
# re-enters the duplicate scan; RETRY_WAIT retries after backoff.
#: Keys per ``IN (...)`` clause: SQLite's default bound-parameter ceiling
#: is 999, and a real run offers thousands of items.
_IN_CHUNK = 500

_PENDING_STATES: tuple[UploadState, ...] = (
    UploadState.PENDING,
    UploadState.UPLOAD_INTERRUPTED,
    UploadState.RETRY_WAIT,
)

# error_type stamped by the privileged recovery path, which bypasses
# validate_transition (see recover()).
_RECOVERY_TAG = "CrashRecovery"

# Derived from UploadState so schema and enum can never drift: an unknown
# state is refused at the DB boundary, not later on read.
_STATE_LITERALS = ", ".join(f"'{s.value}'" for s in UploadState)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    destination   TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    aborted_reason TEXT
);

CREATE TABLE IF NOT EXISTS items (
    item_key          TEXT PRIMARY KEY,
    encounter_id      TEXT NOT NULL,
    patient_id        TEXT NOT NULL,
    file_path         TEXT NOT NULL,
    sha256            TEXT NOT NULL,
    size_bytes        INTEGER NOT NULL,
    state             TEXT NOT NULL CHECK (state IN ({_STATE_LITERALS})),
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_error_type   TEXT,
    destination_doc_id TEXT,
    claimed_by        TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transitions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_key    TEXT NOT NULL REFERENCES items(item_key),
    run_id      TEXT NOT NULL,
    from_state  TEXT NOT NULL,
    to_state    TEXT NOT NULL,
    error_type  TEXT,
    at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_transitions_item ON transitions(item_key);
-- A covering index for single-state scans (recover()'s per-source-state sweep,
-- WHERE state = ?), which fall back to a full table scan without it. For
-- pending_items()'s multi-value WHERE state IN (...) ORDER BY item_key the
-- planner prefers the item_key primary-key index (already ordered, no sort), so
-- this mainly accelerates equality scans and is otherwise a harmless defensive
-- index.
CREATE INDEX IF NOT EXISTS idx_items_state ON items(state, item_key);

-- transitions is an append-only audit trail (the module contract, and the basis
-- for HIPAA-style integrity). SQLite does not enforce that, so guard it: any
-- DELETE or UPDATE aborts; only INSERT is allowed. Added via CREATE TRIGGER IF
-- NOT EXISTS, so an existing ledger gains the protection on next open.
CREATE TRIGGER IF NOT EXISTS transitions_no_delete
    BEFORE DELETE ON transitions
    BEGIN SELECT RAISE(ABORT, 'transitions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS transitions_no_update
    BEFORE UPDATE ON transitions
    BEGIN SELECT RAISE(ABORT, 'transitions is append-only'); END;
"""


def _now() -> str:
    """A timezone-aware UTC timestamp (DTZ rule: never a naive datetime);
    the one exception to routing through :mod:`core.clock`, because this
    ordering key must stay monotonic across runs, not reproducible (20).
    """
    return datetime.now(tz=UTC).isoformat()


class TrackingDB:
    """The resumable upload ledger backed by one WAL-mode SQLite file.
    ``db_path``'s parent is the PHI boundary (45). Connections are
    per-thread; close with :meth:`close` or use as a context manager.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._local = threading.local()
        # executescript() manages its own transaction (COMMITs any pending
        # one first), outside _connection()'s explicit BEGIN/COMMIT.
        self._conn().executescript(_SCHEMA)

    # --- connection management (one connection per thread) ---

    def _conn(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path, isolation_level=None, timeout=30.0)
            conn.row_factory = sqlite3.Row
            # Set FIRST (journal_mode takes a file lock); sized for the
            # worst case, not the average — synchronous=FULL fsyncs every
            # commit, and a slow CI disk under contention needs headroom.
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Run a body inside one explicit transaction on this thread's
        connection (autocommit mode otherwise): a transition's UPDATE and
        its audit INSERT are all-or-nothing.
        """
        conn = self._conn()
        # BEGIN IMMEDIATE takes the write lock up front, so writers queue
        # on busy_timeout instead of deadlocking on a lock upgrade.
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    # --- runs ---

    def begin_run(self, destination: str) -> str:
        """Record the start of a run and return its (uuid4 hex) ``run_id``."""
        run_id = uuid4().hex
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, destination, started_at) VALUES (?, ?, ?)",
                (run_id, destination, _now()),
            )
        return run_id

    def finish_run(self, run_id: str, aborted_reason: str | None = None) -> None:
        """Mark a run finished, optionally with an abort reason (type name)."""
        with self._connection() as conn:
            conn.execute(
                "UPDATE runs SET finished_at = ?, aborted_reason = ? WHERE run_id = ?",
                (_now(), aborted_reason, run_id),
            )

    # --- items ---

    def enqueue(self, item: UploadItem) -> bool:
        """Idempotent upsert. Insert a new item as PENDING and return ``True``;
        leave an already-known item untouched (state preserved — that is the
        resumability) and return ``False``."""
        now = _now()
        with self._connection() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO items (
                    item_key, encounter_id, patient_id, file_path, sha256,
                    size_bytes, state, attempts, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    item.item_key,
                    item.encounter_id,
                    item.patient_id,
                    str(item.file_path),
                    item.sha256,
                    item.size_bytes,
                    UploadState.PENDING.value,
                    now,
                    now,
                ),
            )
            return cursor.rowcount == 1

    def state_of(self, item_key: str) -> UploadState:
        """Return the current state of ``item_key`` (raises ``KeyError``)."""
        return self._require_state(self._conn(), item_key)

    def count_known(self, item_keys: Sequence[str]) -> int:
        """How many of the DISTINCT ``item_keys`` the ledger holds a row
        for — an item enqueued and then absent has left the accounting.
        Chunked: SQLite caps bound parameters per statement.
        """
        keys = sorted(set(item_keys))
        if not keys:
            return 0
        conn = self._conn()
        found = 0
        for start in range(0, len(keys), _IN_CHUNK):
            chunk = keys[start : start + _IN_CHUNK]
            # The only interpolation is literal "?" placeholders; the keys
            # themselves bind as parameters (same shape as pending_items).
            placeholders = ", ".join("?" for _ in chunk)
            sql = f"SELECT COUNT(*) AS n FROM items WHERE item_key IN ({placeholders})"  # noqa: S608
            row = conn.execute(sql, chunk).fetchone()
            found += int(row["n"])
        return found

    def attempts_of(self, item_key: str) -> int:
        """Return the retry-attempt count of ``item_key`` (raises
        ``KeyError``); durable across resumed runs.
        """
        row = (
            self._conn()
            .execute("SELECT attempts FROM items WHERE item_key = ?", (item_key,))
            .fetchone()
        )
        if row is None:
            raise KeyError(item_key)
        return int(row["attempts"])

    def transition(
        self,
        item_key: str,
        new_state: UploadState,
        *,
        run_id: str,
        error_type: str | None = None,
        destination_doc_id: str | None = None,
    ) -> None:
        """Contract: validates the transition, then writes the UPDATE and
        the audit INSERT in one transaction; raises ``KeyError`` for an
        unknown ``item_key``. Does not fence on ``claimed_by`` —
        same-item exclusivity is the scheduler's job, not the ledger's.
        """
        with self._connection() as conn:
            current = self._require_state(conn, item_key)
            validate_transition(current, new_state)
            claimed_by = None if new_state in TERMINAL_STATES else run_id
            attempts_bump = 1 if new_state is UploadState.RETRY_WAIT else 0
            conn.execute(
                """
                UPDATE items SET
                    state = ?,
                    attempts = attempts + ?,
                    last_error_type = COALESCE(?, last_error_type),
                    destination_doc_id = COALESCE(?, destination_doc_id),
                    claimed_by = ?,
                    updated_at = ?
                WHERE item_key = ?
                """,
                (
                    new_state.value,
                    attempts_bump,
                    error_type,
                    destination_doc_id,
                    claimed_by,
                    _now(),
                    item_key,
                ),
            )
            self._record_transition(conn, item_key, run_id, current, new_state, error_type)

    def recover(self, run_id: str) -> dict[str, int]:
        """Rewind every mid-flight item via :data:`CRASH_RECOVERY`, counts
        keyed by the recovered-to state; bypasses :func:`validate_transition`
        deliberately (51) but still writes the audit row for traceability.
        """
        counts: dict[str, int] = {}
        with self._connection() as conn:
            for source_state, target_state in CRASH_RECOVERY.items():
                rows = conn.execute(
                    "SELECT item_key FROM items WHERE state = ?",
                    (source_state.value,),
                ).fetchall()
                for row in rows:
                    item_key = row["item_key"]
                    claimed_by = None if target_state in TERMINAL_STATES else run_id
                    conn.execute(
                        """
                        UPDATE items SET state = ?, claimed_by = ?, updated_at = ?
                        WHERE item_key = ?
                        """,
                        (target_state.value, claimed_by, _now(), item_key),
                    )
                    self._record_transition(
                        conn,
                        item_key,
                        run_id,
                        source_state,
                        target_state,
                        _RECOVERY_TAG,
                    )
                if rows:
                    counts[target_state.value] = counts.get(target_state.value, 0) + len(rows)
        return counts

    def pending_items(self, limit: int | None = None) -> list[UploadItem]:
        """Items still owing work (PENDING/UPLOAD_INTERRUPTED/RETRY_WAIT),
        ordered by ``item_key`` for deterministic, resumable iteration."""
        # The only interpolation is a run of literal "?" placeholders, one per
        # fixed _PENDING_STATES entry; the values bind as parameters below.
        placeholders = ", ".join("?" for _ in _PENDING_STATES)
        sql = (
            "SELECT item_key, encounter_id, patient_id, file_path, sha256, size_bytes "  # noqa: S608
            f"FROM items WHERE state IN ({placeholders}) ORDER BY item_key"
        )
        params: list[object] = [s.value for s in _PENDING_STATES]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self._conn().execute(sql, params).fetchall()
        return [
            UploadItem(
                item_key=row["item_key"],
                encounter_id=row["encounter_id"],
                patient_id=row["patient_id"],
                file_path=Path(row["file_path"]),
                sha256=row["sha256"],
                size_bytes=row["size_bytes"],
            )
            for row in rows
        ]

    def pending_count(self) -> int:
        """How many items still owe work — :meth:`pending_items`'s states,
        counted so a truncated list can report its true total honestly.
        """
        placeholders = ", ".join("?" for _ in _PENDING_STATES)
        row = (
            self._conn()
            .execute(
                f"SELECT COUNT(*) AS n FROM items WHERE state IN ({placeholders})",  # noqa: S608
                [state.value for state in _PENDING_STATES],
            )
            .fetchone()
        )
        return int(row["n"])

    def counts(self) -> dict[str, int]:
        """Item counts per state (for reports and logging — counts only,
        never patient-derived values)."""
        rows = (
            self._conn().execute("SELECT state, COUNT(*) AS n FROM items GROUP BY state").fetchall()
        )
        return {row["state"]: row["n"] for row in rows}

    # --- read accessors (for reports — counts/ids/type names only) ---

    def latest_run_id(self) -> str | None:
        """The most-recent run id (by ``started_at``), or ``None``. The
        upload console shows one current run; log-safe (a run id only).
        """
        row = (
            self._conn()
            .execute("SELECT run_id FROM runs ORDER BY started_at DESC, run_id DESC LIMIT 1")
            .fetchone()
        )
        return row["run_id"] if row is not None else None

    def run_info(self, run_id: str) -> dict[str, str | None]:
        """The ``runs`` row for ``run_id`` (raises ``KeyError`` if
        absent): destination, timestamps, and abort *type* name — every
        value log-safe.
        """
        row = (
            self._conn()
            .execute(
                "SELECT destination, started_at, finished_at, aborted_reason "
                "FROM runs WHERE run_id = ?",
                (run_id,),
            )
            .fetchone()
        )
        if row is None:
            raise KeyError(run_id)
        return {
            "destination": row["destination"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "aborted_reason": row["aborted_reason"],
        }

    def error_type_histogram(self, run_id: str) -> dict[str, int]:
        """Count audit transitions by ``error_type`` (exception type names
        only) for one run — the failure-shape mix, with no item detail.
        """
        rows = (
            self._conn()
            .execute(
                "SELECT error_type, COUNT(*) AS n FROM transitions "
                "WHERE run_id = ? AND error_type IS NOT NULL GROUP BY error_type",
                (run_id,),
            )
            .fetchall()
        )
        return {row["error_type"]: row["n"] for row in rows}

    def attempts_histogram(self) -> dict[int, int]:
        """Count items by their durable ``attempts`` value: how hard each
        item was to land, for the run report.
        """
        rows = (
            self._conn()
            .execute("SELECT attempts, COUNT(*) AS n FROM items GROUP BY attempts")
            .fetchall()
        )
        return {int(row["attempts"]): row["n"] for row in rows}

    # --- internals ---

    @staticmethod
    def _require_state(conn: sqlite3.Connection, item_key: str) -> UploadState:
        row = conn.execute("SELECT state FROM items WHERE item_key = ?", (item_key,)).fetchone()
        if row is None:
            raise KeyError(item_key)
        return UploadState(row["state"])

    @staticmethod
    def _record_transition(
        conn: sqlite3.Connection,
        item_key: str,
        run_id: str,
        from_state: UploadState,
        to_state: UploadState,
        error_type: str | None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO transitions (item_key, run_id, from_state, to_state, error_type, at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                item_key,
                run_id,
                from_state.value,
                to_state.value,
                error_type,
                _now(),
            ),
        )

    # --- lifecycle ---

    def close(self) -> None:
        """Close this thread's connection (idempotent)."""
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def __enter__(self) -> TrackingDB:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
