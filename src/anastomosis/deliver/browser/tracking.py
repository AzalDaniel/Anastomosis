"""The crash-resumable upload ledger (M2 item 10): WAL-mode SQLite.

A browser upload run is long and crash-prone, so the ground truth for "what
has happened to every item" lives in a durable ledger, not in process
memory. Kill the process at any point — a power cut, an OOM, a Ctrl-C — and
:meth:`TrackingDB.recover` rewinds the few mid-flight items to a safe state
and the run continues exactly where it stopped, never re-filing a chart.

Two tables carry the truth and one carries history:

* ``runs`` — one row per invocation (so an abort reason is recorded).
* ``items`` — one row per :class:`UploadItem`, holding its current state.
* ``transitions`` — append-only audit trail; every state change writes a
  row, so the full history of an item is reconstructable after the fact.

Durability: ``journal_mode=WAL`` survives a hard kill mid-write, and
``synchronous=FULL`` is chosen deliberately over the faster NORMAL — this is
a safety-critical ledger and its volume is tiny (a few rows per uploaded
file), so paying for every fsync is the right trade.

Concurrency: the parallel workers (a later PR) drive this from several
threads. SQLite connection objects are not safe to share across threads, so
each thread gets its own connection via :class:`threading.local`; the small
``busy_timeout`` covers the brief lock contention WAL still allows on write.

PHI rule (enforced by schema, not just discipline): there is NO column for a
patient name, DOB, or address anywhere in this schema. ``file_path`` may
embed a name-derived filename, which is why the DB file MUST live inside the
same hardened ``0o700`` directory (``secure_output_dir``) as the files it
tracks — that directory's parent is the PHI boundary. ``last_error_type``
and ``error_type`` store exception *type* names only (``exc_tag``), never
exception messages.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
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

# States an item can be picked up from on a resumed run (work still owed,
# nothing in flight). UPLOAD_INTERRUPTED is included so resume drives it back
# through the duplicate scan; RETRY_WAIT so a backed-off item is retried.
_PENDING_STATES: tuple[UploadState, ...] = (
    UploadState.PENDING,
    UploadState.UPLOAD_INTERRUPTED,
    UploadState.RETRY_WAIT,
)

# The error_type stamped on audit rows written by the privileged recovery
# path (which intentionally bypasses validate_transition — see ``recover``).
_RECOVERY_TAG = "CrashRecovery"

_SCHEMA = """
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
    state             TEXT NOT NULL,
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
"""


def _now() -> str:
    """A timezone-aware UTC timestamp (DTZ rule: never a naive datetime)."""
    return datetime.now(tz=UTC).isoformat()


class TrackingDB:
    """The resumable upload ledger backed by one WAL-mode SQLite file.

    ``db_path``'s parent is the PHI boundary: the caller places it inside a
    :func:`anastomosis.core.output.secure_output_dir` (``0o700``), alongside
    the files it tracks. Connections are per-thread; close with
    :meth:`close` or use the instance as a context manager.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._local = threading.local()
        # One-time schema setup. executescript() manages its own transaction
        # (it COMMITs any pending one first), so it runs outside the explicit
        # BEGIN/COMMIT bracket _connection() provides for normal writes.
        self._conn().executescript(_SCHEMA)

    # --- connection management (one connection per thread) ---

    def _conn(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Run a body inside one explicit transaction on this thread's conn.

        ``isolation_level=None`` puts the driver in autocommit mode, so we
        bracket multi-statement writes with explicit BEGIN/COMMIT and roll
        back on any error — a transition's UPDATE and its audit INSERT are
        all-or-nothing.
        """
        conn = self._conn()
        # BEGIN IMMEDIATE takes the write lock up front, so concurrent writers
        # queue on busy_timeout instead of deadlocking on a lock upgrade (the
        # classic WAL multi-writer "database is locked" trap).
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

    def attempts_of(self, item_key: str) -> int:
        """Return the retry-attempt count of ``item_key`` (raises ``KeyError``).

        The count is bumped by the ledger itself on every RETRY_WAIT write,
        so it stays durable across resumed runs — the engine's retry budget
        survives a crash.
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
        """Move ``item_key`` to ``new_state`` and append an audit row.

        Validates the transition (loud failure on an illegal move), then
        performs the UPDATE and the ``transitions`` INSERT in one
        transaction. Bumps ``attempts`` when ``new_state`` is RETRY_WAIT;
        sets ``claimed_by = run_id`` for non-terminal states and NULL for
        terminal ones. Raises ``KeyError`` for an unknown ``item_key``.

        Does NOT fence on ``claimed_by``: two runs may legally advance the
        same item in sequence. Same-item exclusivity is the scheduler's
        job (batch/parallel PR), not the ledger's.
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
        """Rewind every mid-flight item to a safe state on a resumed run.

        Applies :data:`CRASH_RECOVERY` to each item currently in a
        recoverable active state and returns counts keyed by the recovered-to
        state value.

        Two of these recovery edges (RESOLVING_PATIENT -> PENDING and
        UPLOADING -> UPLOAD_INTERRUPTED) are deliberately NOT in
        :data:`LEGAL_TRANSITIONS`: the forward graph never lets work flow
        backward, but recovery must. So recovery is a distinct, privileged
        code path that bypasses :func:`validate_transition` while STILL
        writing the same audit rows (stamped ``error_type='CrashRecovery'``),
        so the rewind is fully traceable.
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

    def counts(self) -> dict[str, int]:
        """Item counts per state (for reports and logging — counts only,
        never patient-derived values)."""
        rows = (
            self._conn().execute("SELECT state, COUNT(*) AS n FROM items GROUP BY state").fetchall()
        )
        return {row["state"]: row["n"] for row in rows}

    # --- read accessors (for reports — counts/ids/type names only) ---

    def run_info(self, run_id: str) -> dict[str, str | None]:
        """Return the ``runs`` row for ``run_id`` (raises ``KeyError`` if absent).

        Exposes the run's destination, timestamps, and abort reason for the
        report writer without reaching into the ledger's privates. Every value
        is log-safe: a destination name, ISO timestamps, and an abort *type*
        name (never a patient value).
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
        """Count audit transitions by ``error_type`` for one run.

        Reads the append-only ``transitions`` table and tallies the non-null
        ``error_type`` values (exception *type* names only — the schema stores
        nothing else there) for ``run_id``. Surfaces the failure-shape mix in
        a run report without exposing any item detail.
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
        """Count items by their durable ``attempts`` value (counts only).

        Keyed by attempt count, valued by how many items have it — a histogram
        of how hard each item was to land, for the run report.
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
