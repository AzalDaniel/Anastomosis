"""TrackingDB tests: idempotent enqueue, validated transitions, crash
recovery, WAL persistence across a reopen, thread safety, and the PHI
schema guarantee."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from anastomosis.deliver.browser.errors import IllegalTransitionError
from anastomosis.deliver.browser.states import UploadState
from anastomosis.deliver.browser.tracking import TrackingDB
from anastomosis.destinations.base import UploadItem

# Synthetic identities only: feedface- GUIDs, never real PHI.
_PATIENT = "feedface-0000-0000-0000-0000000000aa"


def _item(n: int) -> UploadItem:
    encounter = f"feedface-e000-0000-0000-{n:012x}"
    sha = f"{n:064x}"
    return UploadItem(
        item_key=f"{encounter}:{sha[:12]}",
        encounter_id=encounter,
        patient_id=_PATIENT,
        file_path=Path(f"/synthetic/out/{encounter}.pdf"),
        sha256=sha,
        size_bytes=1024 + n,
    )


@pytest.fixture
def db(tmp_path: Path) -> TrackingDB:
    return TrackingDB(tmp_path / "tracking.sqlite3")


# --- enqueue idempotence ---


def test_enqueue_new_item_returns_true_and_is_pending(db: TrackingDB) -> None:
    run = db.begin_run("fake")
    item = _item(1)
    assert db.enqueue(item) is True
    assert db.state_of(item.item_key) is UploadState.PENDING
    assert run  # uuid4 hex
    assert len(run) == 32


def test_enqueue_is_idempotent_and_preserves_state(db: TrackingDB) -> None:
    run = db.begin_run("fake")
    item = _item(1)
    assert db.enqueue(item) is True
    db.transition(item.item_key, UploadState.RESOLVING_PATIENT, run_id=run)
    # Re-enqueue: must not touch the advanced state, must report "already known".
    assert db.enqueue(item) is False
    assert db.state_of(item.item_key) is UploadState.RESOLVING_PATIENT


# --- transitions ---


def test_transition_happy_path_writes_audit_row(db: TrackingDB) -> None:
    run = db.begin_run("fake")
    item = _item(1)
    db.enqueue(item)
    db.transition(item.item_key, UploadState.RESOLVING_PATIENT, run_id=run)
    rows = _transitions(db, item.item_key)
    assert len(rows) == 1
    assert rows[0]["from_state"] == UploadState.PENDING.value
    assert rows[0]["to_state"] == UploadState.RESOLVING_PATIENT.value
    assert rows[0]["run_id"] == run
    assert rows[0]["at"]


def test_transition_bumps_updated_at(db: TrackingDB) -> None:
    run = db.begin_run("fake")
    item = _item(1)
    db.enqueue(item)
    before = _item_row(db, item.item_key)["updated_at"]
    db.transition(item.item_key, UploadState.RESOLVING_PATIENT, run_id=run)
    after = _item_row(db, item.item_key)["updated_at"]
    assert after >= before


def test_attempts_increments_only_via_retry_wait(db: TrackingDB) -> None:
    run = db.begin_run("fake")
    item = _item(1)
    db.enqueue(item)
    db.transition(item.item_key, UploadState.RESOLVING_PATIENT, run_id=run)
    assert _item_row(db, item.item_key)["attempts"] == 0
    db.transition(
        item.item_key,
        UploadState.RETRY_WAIT,
        run_id=run,
        error_type="TransientDeliveryError",
    )
    assert _item_row(db, item.item_key)["attempts"] == 1
    # A non-RETRY_WAIT transition leaves attempts where it was.
    db.transition(item.item_key, UploadState.RESOLVING_PATIENT, run_id=run)
    assert _item_row(db, item.item_key)["attempts"] == 1


def test_terminal_transition_clears_claimed_by(db: TrackingDB) -> None:
    run = db.begin_run("fake")
    item = _item(1)
    db.enqueue(item)
    db.transition(item.item_key, UploadState.RESOLVING_PATIENT, run_id=run)
    assert _item_row(db, item.item_key)["claimed_by"] == run
    db.transition(item.item_key, UploadState.PATIENT_NOT_FOUND, run_id=run)
    assert _item_row(db, item.item_key)["claimed_by"] is None


def test_transition_persists_destination_doc_id(db: TrackingDB) -> None:
    run = db.begin_run("fake")
    item = _item(1)
    db.enqueue(item)
    db.transition(item.item_key, UploadState.RESOLVING_PATIENT, run_id=run)
    db.transition(item.item_key, UploadState.VERIFYING_PRE, run_id=run)
    db.transition(item.item_key, UploadState.UPLOADING, run_id=run)
    db.transition(item.item_key, UploadState.VERIFYING_POST, run_id=run)
    db.transition(
        item.item_key,
        UploadState.COMPLETED,
        run_id=run,
        destination_doc_id="dest-doc-777",
    )
    assert _item_row(db, item.item_key)["destination_doc_id"] == "dest-doc-777"


def test_illegal_transition_raises_and_does_not_mutate(db: TrackingDB) -> None:
    run = db.begin_run("fake")
    item = _item(1)
    db.enqueue(item)
    with pytest.raises(IllegalTransitionError):
        db.transition(item.item_key, UploadState.COMPLETED, run_id=run)
    assert db.state_of(item.item_key) is UploadState.PENDING
    assert _transitions(db, item.item_key) == []


def test_unknown_item_key_raises_key_error(db: TrackingDB) -> None:
    run = db.begin_run("fake")
    with pytest.raises(KeyError):
        db.transition("no-such-item", UploadState.RESOLVING_PATIENT, run_id=run)
    with pytest.raises(KeyError):
        db.state_of("no-such-item")


# --- counts & pending ---


def test_counts_reflect_states(db: TrackingDB) -> None:
    run = db.begin_run("fake")
    for n in range(3):
        db.enqueue(_item(n))
    db.transition(_item(0).item_key, UploadState.RESOLVING_PATIENT, run_id=run)
    counts = db.counts()
    assert counts[UploadState.PENDING.value] == 2
    assert counts[UploadState.RESOLVING_PATIENT.value] == 1


def test_pending_items_deterministic_order_and_states(db: TrackingDB) -> None:
    run = db.begin_run("fake")
    for n in (5, 1, 3):
        db.enqueue(_item(n))
    # Move one to a terminal state — it must drop out of pending.
    done = _item(1)
    db.transition(done.item_key, UploadState.SKIPPED_SKIPLIST, run_id=run)
    pending = db.pending_items()
    keys = [p.item_key for p in pending]
    assert keys == sorted(keys)
    assert done.item_key not in keys
    assert len(pending) == 2


def test_pending_items_limit(db: TrackingDB) -> None:
    db.begin_run("fake")
    for n in range(5):
        db.enqueue(_item(n))
    assert len(db.pending_items(limit=2)) == 2


# --- crash recovery ---


def test_recover_rewinds_each_active_state(db: TrackingDB) -> None:
    run = db.begin_run("fake")

    # Seed one item in each recoverable active state plus controls.
    resolving = _drive(db, _item(1), run, UploadState.RESOLVING_PATIENT)
    verifying_pre = _drive(db, _item(2), run, UploadState.VERIFYING_PRE)
    uploading = _drive(db, _item(3), run, UploadState.UPLOADING)
    verifying_post = _drive(db, _item(4), run, UploadState.VERIFYING_POST)
    # Controls that recovery must NOT touch:
    retry = _drive(db, _item(5), run, UploadState.RETRY_WAIT)
    pending = _item(6)
    db.enqueue(pending)
    completed = _drive(db, _item(7), run, UploadState.COMPLETED)

    resume = db.begin_run("fake")
    counts = db.recover(resume)

    assert db.state_of(resolving.item_key) is UploadState.PENDING
    assert db.state_of(verifying_pre.item_key) is UploadState.PENDING
    assert db.state_of(uploading.item_key) is UploadState.UPLOAD_INTERRUPTED
    assert db.state_of(verifying_post.item_key) is UploadState.UPLOAD_INTERRUPTED
    # Untouched controls:
    assert db.state_of(retry.item_key) is UploadState.RETRY_WAIT
    assert db.state_of(pending.item_key) is UploadState.PENDING
    assert db.state_of(completed.item_key) is UploadState.COMPLETED

    # Exact returned counts (keyed by recovered-to state value).
    assert counts == {
        UploadState.PENDING.value: 2,
        UploadState.UPLOAD_INTERRUPTED.value: 2,
    }


def test_recover_writes_audit_rows_tagged_crash_recovery(db: TrackingDB) -> None:
    run = db.begin_run("fake")
    uploading = _drive(db, _item(3), run, UploadState.UPLOADING)
    resume = db.begin_run("fake")
    db.recover(resume)
    rows = _transitions(db, uploading.item_key)
    recovery = [r for r in rows if r["error_type"] == "CrashRecovery"]
    assert len(recovery) == 1
    assert recovery[0]["from_state"] == UploadState.UPLOADING.value
    assert recovery[0]["to_state"] == UploadState.UPLOAD_INTERRUPTED.value
    assert recovery[0]["run_id"] == resume


def test_recover_on_clean_ledger_is_noop(db: TrackingDB) -> None:
    db.begin_run("fake")
    db.enqueue(_item(1))
    assert db.recover(db.begin_run("fake")) == {}


# --- reopen after kill (WAL persistence) ---


def test_reopen_after_close_preserves_state_and_recovers(tmp_path: Path) -> None:
    path = tmp_path / "tracking.sqlite3"
    first = TrackingDB(path)
    run = first.begin_run("fake")
    resolving = _drive(first, _item(1), run, UploadState.RESOLVING_PATIENT)
    uploading = _drive(first, _item(2), run, UploadState.UPLOADING)
    done = _drive(first, _item(3), run, UploadState.COMPLETED)
    first.close()  # simulate process death after a clean close

    # A brand-new TrackingDB on the same path — the resumed process.
    second = TrackingDB(path)
    assert second.state_of(resolving.item_key) is UploadState.RESOLVING_PATIENT
    assert second.state_of(uploading.item_key) is UploadState.UPLOADING
    assert second.state_of(done.item_key) is UploadState.COMPLETED

    resume = second.begin_run("fake")
    counts = second.recover(resume)
    assert counts == {
        UploadState.PENDING.value: 1,
        UploadState.UPLOAD_INTERRUPTED.value: 1,
    }
    assert second.state_of(resolving.item_key) is UploadState.PENDING
    assert second.state_of(uploading.item_key) is UploadState.UPLOAD_INTERRUPTED
    second.close()


def test_context_manager_closes(tmp_path: Path) -> None:
    path = tmp_path / "tracking.sqlite3"
    with TrackingDB(path) as db:
        db.begin_run("fake")
        db.enqueue(_item(1))
    # Reopen confirms the write survived the context-manager close.
    with TrackingDB(path) as db2:
        assert db2.state_of(_item(1).item_key) is UploadState.PENDING


# --- thread-safety smoke ---


def test_concurrent_threads_disjoint_items(tmp_path: Path) -> None:
    db = TrackingDB(tmp_path / "tracking.sqlite3")
    run = db.begin_run("fake")
    threads_n = 4
    per_thread = 25
    errors: list[BaseException] = []
    barrier = threading.Barrier(threads_n)

    def worker(worker_id: int) -> None:
        try:
            barrier.wait()
            for i in range(per_thread):
                item = _item(worker_id * 1000 + i)
                db.enqueue(item)
                db.transition(item.item_key, UploadState.RESOLVING_PATIENT, run_id=run)
                db.transition(item.item_key, UploadState.VERIFYING_PRE, run_id=run)
        except BaseException as exc:  # surfaced via the assert below
            errors.append(exc)

    workers = [threading.Thread(target=worker, args=(w,)) for w in range(threads_n)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()

    assert errors == []
    counts = db.counts()
    assert counts.get(UploadState.VERIFYING_PRE.value) == threads_n * per_thread
    db.close()


# --- PHI probe ---


def test_schema_has_no_patient_demographic_columns(db: TrackingDB) -> None:
    forbidden = ("name", "dob", "birth", "address")
    for table in ("items", "transitions"):
        columns = _columns(db, table)
        for column in columns:
            assert not any(token in column.lower() for token in forbidden), (
                f"{table}.{column} looks PHI-shaped"
            )


def test_error_paths_store_only_type_names(db: TrackingDB) -> None:
    run = db.begin_run("fake")
    item = _item(1)
    db.enqueue(item)
    db.transition(item.item_key, UploadState.RESOLVING_PATIENT, run_id=run)
    db.transition(
        item.item_key,
        UploadState.RETRY_WAIT,
        run_id=run,
        error_type="TransientDeliveryError",
    )
    assert _item_row(db, item.item_key)["last_error_type"] == "TransientDeliveryError"
    rows = _transitions(db, item.item_key)
    types = {r["error_type"] for r in rows if r["error_type"]}
    # Only ever exception TYPE names (no spaces, no message text).
    for value in types:
        assert " " not in value


def test_last_error_type_survives_later_clean_transition(db: TrackingDB) -> None:
    """A clean (error_type=None) transition must not wipe the recorded
    error history — COALESCE keeps the last known failure cause for triage."""
    run = db.begin_run("fake")
    item = _item(1)
    db.enqueue(item)
    db.transition(item.item_key, UploadState.RESOLVING_PATIENT, run_id=run)
    db.transition(
        item.item_key,
        UploadState.RETRY_WAIT,
        run_id=run,
        error_type="TransientDeliveryError",
    )
    db.transition(item.item_key, UploadState.RESOLVING_PATIENT, run_id=run)
    assert _item_row(db, item.item_key)["last_error_type"] == "TransientDeliveryError"


# --- helpers ---


def _drive(db: TrackingDB, item: UploadItem, run: str, target: UploadState) -> UploadItem:
    """Enqueue ``item`` and walk it to ``target`` over legal transitions."""
    paths = {
        UploadState.RESOLVING_PATIENT: [UploadState.RESOLVING_PATIENT],
        UploadState.VERIFYING_PRE: [
            UploadState.RESOLVING_PATIENT,
            UploadState.VERIFYING_PRE,
        ],
        UploadState.UPLOADING: [
            UploadState.RESOLVING_PATIENT,
            UploadState.VERIFYING_PRE,
            UploadState.UPLOADING,
        ],
        UploadState.VERIFYING_POST: [
            UploadState.RESOLVING_PATIENT,
            UploadState.VERIFYING_PRE,
            UploadState.UPLOADING,
            UploadState.VERIFYING_POST,
        ],
        UploadState.RETRY_WAIT: [
            UploadState.RESOLVING_PATIENT,
            UploadState.RETRY_WAIT,
        ],
        UploadState.COMPLETED: [
            UploadState.RESOLVING_PATIENT,
            UploadState.VERIFYING_PRE,
            UploadState.UPLOADING,
            UploadState.VERIFYING_POST,
            UploadState.COMPLETED,
        ],
    }
    db.enqueue(item)
    for state in paths[target]:
        db.transition(item.item_key, state, run_id=run)
    return item


def _raw(db: TrackingDB) -> sqlite3.Connection:
    conn = sqlite3.connect(db._db_path)  # test inspects the raw ledger
    conn.row_factory = sqlite3.Row
    return conn


def _item_row(db: TrackingDB, item_key: str) -> sqlite3.Row:
    conn = _raw(db)
    try:
        row = conn.execute("SELECT * FROM items WHERE item_key = ?", (item_key,)).fetchone()
    finally:
        conn.close()
    assert row is not None
    return row


def _transitions(db: TrackingDB, item_key: str) -> list[sqlite3.Row]:
    conn = _raw(db)
    try:
        return conn.execute(
            "SELECT * FROM transitions WHERE item_key = ? ORDER BY id", (item_key,)
        ).fetchall()
    finally:
        conn.close()


def _columns(db: TrackingDB, table: str) -> list[str]:
    conn = _raw(db)
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        conn.close()
    return [row["name"] for row in rows]
