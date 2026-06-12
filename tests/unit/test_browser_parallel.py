"""Parallel runner tests: patient partitioning, abort propagation, worker crash.

Synthetic data only — ``feedface-`` GUIDs, neutral file names in ``tmp_path``.
Each worker builds its own destination via the factory; an instrumented
factory records which destination instance uploaded which patient so the
"one patient, one worker" guarantee is checked directly. Timing is made
deterministic with cooperative events and the engine's injected ``sleeper``;
no real sleep exceeds a small bound.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from anastomosis.core.model import Patient
from anastomosis.deliver.browser.fake import FakeCrash, FakeDestination
from anastomosis.deliver.browser.manifest import build_manifest
from anastomosis.deliver.browser.parallel import run_parallel
from anastomosis.deliver.browser.states import UploadState
from anastomosis.deliver.browser.tracking import TrackingDB
from anastomosis.destinations.base import (
    DestinationPatient,
    UploadItem,
    UploadReceipt,
)
from anastomosis.reconstruct.engine import RenderedDoc


def _patient(pid: str) -> Patient:
    return Patient(id=pid, given_name="Given", family_name="Family")


def _three_patients_three_items(
    tmp_path: Path,
) -> tuple[list[UploadItem], dict[str, Patient], dict[str, str]]:
    """3 patients x 3 items each = 9 items; distinct destination charts."""
    docs: list[RenderedDoc] = []
    patients: dict[str, Patient] = {}
    known: dict[str, str] = {}
    for p in range(3):
        pid = f"feedface-0000-0000-0000-00000000020{p}"
        patients[pid] = _patient(pid)
        known[pid] = f"dest-{p}"
        for d in range(3):
            enc = f"enc-{p}-{d}"
            path = tmp_path / f"note-{p}-{d}.pdf"
            path.write_bytes(f"chart-{p}-{d}".encode())
            docs.append(RenderedDoc(path, enc, pid))
    return build_manifest(docs), patients, known


# --- partitioning: each patient on exactly one worker ---


def test_partition_keeps_each_patient_on_one_worker(tmp_path: Path) -> None:
    items, patients, known = _three_patients_three_items(tmp_path)
    db_path = tmp_path / "out" / "ledger.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Each created destination shares the same backing store but records its
    # own uploads, so we can see which destination handled which patient.
    shared_existing: dict[str, set[str]] = {}
    created: list[FakeDestination] = []
    lock = threading.Lock()

    def factory() -> FakeDestination:
        dest = FakeDestination(known, existing=shared_existing)
        with lock:
            created.append(dest)
        return dest

    result = run_parallel(
        factory,
        db_path,
        items,
        patients,
        workers=2,
        sleeper=lambda _s: None,
    )

    # 9 items, all completed; counts come from the single ledger read.
    assert result.counts == {UploadState.COMPLETED.value: 9}
    assert result.aborted_reason is None

    # The coordinator (and only the coordinator) stamped the run row: a
    # dropped finish_run would leave the run perpetually "in progress".
    tracking = TrackingDB(db_path)
    try:
        info = tracking.run_info(result.run_id)
        assert info["finished_at"] is not None
        assert info["aborted_reason"] is None
    finally:
        tracking.close()

    # No patient was uploaded by two different destination instances.
    patient_to_dest: dict[str, int] = {}
    for dest_idx, dest in enumerate(created):
        for _key, dest_pid in dest.uploads:
            owner = patient_to_dest.setdefault(dest_pid, dest_idx)
            assert owner == dest_idx, "a patient was navigated by two destinations"

    # Greedy balance: partitions sum to 9 and are well balanced (max-min <= 3).
    assert sum(result.partitions) == 9
    assert max(result.partitions) - min(result.partitions) <= 3
    # Two workers each processed their whole partition.
    assert sum(result.processed_per_worker) == 9


def test_partition_is_deterministic() -> None:
    # Pure partition determinism: same items -> same split, every time.
    from anastomosis.deliver.browser.parallel import _partition

    items: list[UploadItem] = []
    for p in range(3):
        pid = f"feedface-0000-0000-0000-00000000030{p}"
        for d in range(3):
            items.append(
                UploadItem(
                    item_key=f"enc-{p}-{d}:abc",
                    encounter_id=f"enc-{p}-{d}",
                    patient_id=pid,
                    file_path=Path(f"/dev/null/{p}-{d}.pdf"),
                    sha256="0" * 64,
                    size_bytes=1,
                )
            )
    first = [[i.item_key for i in b] for b in _partition(items, 2)]
    second = [[i.item_key for i in b] for b in _partition(items, 2)]
    assert first == second
    # Each patient's three items land together in one bucket.
    for bucket in _partition(items, 2):
        pids = {i.patient_id for i in bucket}
        # A bucket holds whole patients only (multiples of 3 items).
        assert len(bucket) % 3 == 0
        assert len(pids) == len(bucket) // 3


# --- abort propagation: wrong patient stops the other worker promptly ---


class _AbortingDestination:
    """A destination double whose banner readback fails for ``wrong_patient``.

    A non-aborting worker's banner check blocks until the abort has been
    reached (the shared event), then a tiny bounded pause lets the aborting
    worker's engine set the run-wide ``stop`` flag before this worker reaches
    its next item boundary — so the other worker stops with items still
    PENDING rather than draining its whole queue.
    """

    def __init__(
        self,
        known: dict[str, str],
        *,
        wrong_patient: str,
        abort_reached: threading.Event,
    ) -> None:
        self._known = known
        self._wrong = wrong_patient
        self._abort_reached = abort_reached
        self.uploads: list[tuple[str, str]] = []

    @property
    def name(self) -> str:
        return "aborting"

    @property
    def session(self) -> FakeDestination:
        return self  # type: ignore[return-value]

    def open(self) -> None:
        return None

    def close(self) -> None:
        return None

    def is_alive(self) -> bool:
        return True

    @property
    def resolver(self) -> _AbortingDestination:
        return self

    @property
    def banner(self) -> _AbortingDestination:
        return self

    @property
    def scanner(self) -> _AbortingDestination:
        return self

    @property
    def driver(self) -> _AbortingDestination:
        return self

    def resolve(self, patient: Patient) -> DestinationPatient | None:
        dest = self._known.get(patient.id)
        return None if dest is None else DestinationPatient(dest, matched_on=("id",))

    def current_patient_matches(self, expected: Patient) -> bool:
        if expected.id == self._wrong:
            # Signal the abort is happening, then return False to trigger it.
            self._abort_reached.set()
            return False
        # A non-aborting worker waits here until the abort is under way, then a
        # small bounded pause lets the aborting engine set the stop flag.
        self._abort_reached.wait(timeout=2.0)
        time.sleep(0.02)
        return True

    def existing_fingerprints(self, patient: DestinationPatient) -> set[str]:
        return set()

    def upload(self, item: UploadItem, patient: DestinationPatient) -> UploadReceipt:
        self.uploads.append((item.item_key, patient.destination_patient_id))
        return UploadReceipt(
            destination_doc_id=f"doc-{item.item_key}",
            echoed_size_bytes=item.size_bytes,
        )


def test_wrong_patient_aborts_and_stops_other_worker(tmp_path: Path) -> None:
    # Patient A (worker 0) is the wrong-patient; patient B (worker 1) has many
    # items. The B worker must NOT drain its whole queue once A aborts.
    docs: list[RenderedDoc] = []
    patients: dict[str, Patient] = {}
    known: dict[str, str] = {}

    pid_a = "feedface-0000-0000-0000-0000000004a0"
    # A: one item, sorts first so worker 0 gets it.
    docs.append(RenderedDoc(tmp_path / "a.pdf", "enc-a", pid_a))
    (tmp_path / "a.pdf").write_bytes(b"a")
    patients[pid_a] = _patient(pid_a)
    known[pid_a] = "dest-a"

    pid_b = "feedface-0000-0000-0000-0000000004b0"
    for d in range(5):
        path = tmp_path / f"b-{d}.pdf"
        path.write_bytes(f"b{d}".encode())
        docs.append(RenderedDoc(path, f"enc-b-{d}", pid_b))
    patients[pid_b] = _patient(pid_b)
    known[pid_b] = "dest-b"

    items = build_manifest(docs)
    db_path = tmp_path / "out" / "ledger.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    abort_reached = threading.Event()

    def factory() -> _AbortingDestination:
        return _AbortingDestination(known, wrong_patient=pid_a, abort_reached=abort_reached)

    result = run_parallel(
        factory,
        db_path,
        items,
        patients,
        workers=2,
        sleeper=lambda _s: None,
    )

    # The run aborted for patient safety.
    assert result.aborted_reason == "WrongPatientError"

    tracking = TrackingDB(db_path)
    try:
        # The durable abort reason reached the run row (the coordinator's
        # finish_run) — reports and resumes read it from here, not memory.
        info = tracking.run_info(result.run_id)
        assert info["finished_at"] is not None
        assert info["aborted_reason"] == "WrongPatientError"
        # The wrong-patient item failed pre-verify.
        a_key = next(i.item_key for i in items if i.patient_id == pid_a)
        assert tracking.state_of(a_key) is UploadState.PRE_VERIFY_FAILED
        # The OTHER worker did not drain its queue: some B items remain PENDING.
        b_states = [tracking.state_of(i.item_key) for i in items if i.patient_id == pid_b]
        assert UploadState.PENDING in b_states, "the other worker drained its whole queue"
    finally:
        tracking.close()


# --- worker crash: re-raised after joins, ledger consistent, resumable ---


def test_worker_crash_reraised_and_resumable(tmp_path: Path) -> None:
    items, patients, known = _three_patients_three_items(tmp_path)
    db_path = tmp_path / "out" / "ledger.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    shared_existing: dict[str, set[str]] = {}

    # One worker's destination crashes after a couple of uploads (a kill).
    crash_done = threading.Event()
    lock = threading.Lock()

    def factory() -> FakeDestination:
        with lock:
            # Only the FIRST destination built is crash-prone; the rest are
            # ordinary. This guarantees exactly one worker crashes.
            if not crash_done.is_set():
                crash_done.set()
                return FakeDestination(known, existing=shared_existing, crash_after=1)
        return FakeDestination(known, existing=shared_existing)

    # The coordinator must re-raise the worker's BaseException after joins.
    with pytest.raises(FakeCrash):
        run_parallel(
            factory,
            db_path,
            items,
            patients,
            workers=2,
            sleeper=lambda _s: None,
        )

    # Ledger is consistent and resumable: recover the in-flight item, then a
    # fresh SEQUENTIAL engine run completes everything that still owes work.
    from anastomosis.deliver.browser.engine import UploadEngine

    tracking = TrackingDB(db_path)
    try:
        resume_run = tracking.begin_run("fake")
        tracking.recover(resume_run)
        dest = FakeDestination(known, existing=shared_existing)
        result = UploadEngine(dest, tracking, sleeper=lambda _s: None).run(
            items, patients, resume_run
        )
        tracking.finish_run(resume_run)
        counts = result.counts
        # Every item now terminal; all nine accounted for, none failed.
        assert sum(counts.values()) == 9
        assert counts.get(UploadState.FAILED.value, 0) == 0
        completed = counts.get(UploadState.COMPLETED.value, 0)
        dup = counts.get(UploadState.DUPLICATE_AT_DESTINATION.value, 0)
        assert completed + dup == 9
    finally:
        tracking.close()


def test_workers_validation() -> None:
    with pytest.raises(ValueError, match="workers"):
        run_parallel(
            lambda: FakeDestination({}),
            Path("/dev/null/x.sqlite"),
            [],
            {},
            workers=0,
        )
