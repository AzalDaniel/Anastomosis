"""Upload engine tests: the full lifecycle, every terminal, and kill-and-resume.

Synthetic data only — ``feedface-`` GUIDs for patient ids, neutral file names
in ``tmp_path``, no patient-derived values. The destination is the in-memory
:class:`FakeDestination`, so no browser and no I/O are involved.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from anastomosis.core.model import Patient
from anastomosis.deliver.browser.engine import UploadEngine
from anastomosis.deliver.browser.fake import FakeCrash, FakeDestination
from anastomosis.deliver.browser.manifest import build_manifest
from anastomosis.deliver.browser.states import UploadState
from anastomosis.deliver.browser.tracking import TrackingDB
from anastomosis.destinations.base import UploadItem
from anastomosis.reconstruct.engine import RenderedDoc

# --- synthetic patients (feedface- GUIDs, never real) ---

PAT_A = "feedface-0000-0000-0000-00000000000a"
PAT_B = "feedface-0000-0000-0000-00000000000b"

DEST_A = "dest-a"
DEST_B = "dest-b"


def _patient(pid: str) -> Patient:
    return Patient(id=pid, given_name="Given", family_name="Family")


def _tracking(tmp_path: Path) -> TrackingDB:
    return TrackingDB(tmp_path / "ledger.sqlite")


def _doc(tmp_path: Path, name: str, encounter_id: str, patient_id: str, content: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(content)
    return path


def _audit_trail(tracking: TrackingDB, item_key: str) -> list[str]:
    rows = (
        tracking._conn()
        .execute(
            "SELECT to_state FROM transitions WHERE item_key = ? ORDER BY id",
            (item_key,),
        )
        .fetchall()
    )
    return [row["to_state"] for row in rows]


def _attempts(tracking: TrackingDB, item_key: str) -> int:
    row = (
        tracking._conn()
        .execute("SELECT attempts FROM items WHERE item_key = ?", (item_key,))
        .fetchone()
    )
    return int(row["attempts"])


def _single_manifest(tmp_path: Path, *, content: bytes = b"chart-bytes") -> list[UploadItem]:
    path = _doc(tmp_path, "note.pdf", "enc-a", PAT_A, content)
    docs = [RenderedDoc(path=path, encounter_id="enc-a", patient_id=PAT_A)]
    return build_manifest(docs)


# --- happy path ---


def test_happy_path_completes_with_exact_audit_trail(tmp_path: Path) -> None:
    items = _single_manifest(tmp_path)
    dest = FakeDestination({PAT_A: DEST_A})
    tracking = _tracking(tmp_path)
    run_id = tracking.begin_run(dest.name)

    engine = UploadEngine(dest, tracking)
    result = engine.run(items, {PAT_A: _patient(PAT_A)}, run_id)

    assert result.aborted_reason is None
    assert result.processed == 1
    assert result.counts == {UploadState.COMPLETED.value: 1}

    key = items[0].item_key
    assert _audit_trail(tracking, key) == [
        UploadState.RESOLVING_PATIENT.value,
        UploadState.VERIFYING_PRE.value,
        UploadState.UPLOADING.value,
        UploadState.VERIFYING_POST.value,
        UploadState.COMPLETED.value,
    ]
    # Receipt doc id persisted on the item.
    row = (
        tracking._conn()
        .execute("SELECT destination_doc_id FROM items WHERE item_key = ?", (key,))
        .fetchone()
    )
    assert row["destination_doc_id"] == f"doc-{key}"
    # The fake recorded exactly one upload to the resolved destination patient.
    assert dest.uploads == [(key, DEST_A)]


# --- skiplist ---


def test_skiplisted_item_skips_before_any_destination_call(tmp_path: Path) -> None:
    items = _single_manifest(tmp_path)
    dest = FakeDestination({PAT_A: DEST_A})
    tracking = _tracking(tmp_path)
    run_id = tracking.begin_run(dest.name)

    engine = UploadEngine(dest, tracking)
    result = engine.run(items, {PAT_A: _patient(PAT_A)}, run_id, skiplist=frozenset({"enc-a"}))

    assert result.counts == {UploadState.SKIPPED_SKIPLIST.value: 1}
    # Zero interaction with the destination.
    assert dest.uploads == []
    assert _audit_trail(tracking, items[0].item_key) == [UploadState.SKIPPED_SKIPLIST.value]


# --- preflight ---


def test_corrupted_file_fails_preflight_without_upload(tmp_path: Path) -> None:
    items = _single_manifest(tmp_path)
    # Corrupt the file after the manifest was built (hash now mismatches).
    items[0].file_path.write_bytes(b"tampered-different-bytes")
    dest = FakeDestination({PAT_A: DEST_A})
    tracking = _tracking(tmp_path)
    run_id = tracking.begin_run(dest.name)

    result = UploadEngine(dest, tracking).run(items, {PAT_A: _patient(PAT_A)}, run_id)

    assert result.counts == {UploadState.PREFLIGHT_FAILED.value: 1}
    assert dest.uploads == []


def test_missing_file_fails_preflight(tmp_path: Path) -> None:
    items = _single_manifest(tmp_path)
    items[0].file_path.unlink()
    dest = FakeDestination({PAT_A: DEST_A})
    tracking = _tracking(tmp_path)
    run_id = tracking.begin_run(dest.name)

    result = UploadEngine(dest, tracking).run(items, {PAT_A: _patient(PAT_A)}, run_id)

    assert result.counts == {UploadState.PREFLIGHT_FAILED.value: 1}
    assert dest.uploads == []


# --- patient not found ---


def test_patient_not_found(tmp_path: Path) -> None:
    items = _single_manifest(tmp_path)
    dest = FakeDestination({})  # PAT_A unknown at the destination
    tracking = _tracking(tmp_path)
    run_id = tracking.begin_run(dest.name)

    result = UploadEngine(dest, tracking).run(items, {PAT_A: _patient(PAT_A)}, run_id)

    assert result.counts == {UploadState.PATIENT_NOT_FOUND.value: 1}
    assert dest.uploads == []


def test_missing_patient_mapping_raises_keyerror(tmp_path: Path) -> None:
    items = _single_manifest(tmp_path)
    dest = FakeDestination({PAT_A: DEST_A})
    tracking = _tracking(tmp_path)
    run_id = tracking.begin_run(dest.name)
    with pytest.raises(KeyError):
        UploadEngine(dest, tracking).run(items, {}, run_id)


# --- duplicate at destination ---


def test_duplicate_at_destination(tmp_path: Path) -> None:
    items = _single_manifest(tmp_path)
    # Pre-seed the destination chart with this item's fingerprint.
    dest = FakeDestination({PAT_A: DEST_A}, existing={DEST_A: {items[0].fingerprint}})
    tracking = _tracking(tmp_path)
    run_id = tracking.begin_run(dest.name)

    result = UploadEngine(dest, tracking).run(items, {PAT_A: _patient(PAT_A)}, run_id)

    assert result.counts == {UploadState.DUPLICATE_AT_DESTINATION.value: 1}
    assert dest.uploads == []


# --- wrong patient (the headline safety abort) ---


def test_wrong_patient_aborts_run_and_leaves_remaining_unprocessed(tmp_path: Path) -> None:
    # Two items; item ordering in pending_items() is by item_key, so build keys
    # that sort with the wrong-patient one first.
    p1 = _doc(tmp_path, "one.pdf", "aaa-enc", PAT_A, b"one")
    p2 = _doc(tmp_path, "two.pdf", "zzz-enc", PAT_B, b"two")
    items = build_manifest(
        [
            RenderedDoc(p1, "aaa-enc", PAT_A),
            RenderedDoc(p2, "zzz-enc", PAT_B),
        ]
    )
    dest = FakeDestination({PAT_A: DEST_A, PAT_B: DEST_B}, wrong_patient_ids={PAT_A})
    tracking = _tracking(tmp_path)
    run_id = tracking.begin_run(dest.name)

    result = UploadEngine(dest, tracking).run(
        items, {PAT_A: _patient(PAT_A), PAT_B: _patient(PAT_B)}, run_id
    )

    # 1. wrong-patient item -> PRE_VERIFY_FAILED with the right error type.
    a_key = next(i.item_key for i in items if i.encounter_id == "aaa-enc")
    b_key = next(i.item_key for i in items if i.encounter_id == "zzz-enc")
    assert tracking.state_of(a_key) is UploadState.PRE_VERIFY_FAILED
    # 2. run aborted with the patient-safety reason.
    assert result.aborted_reason == "WrongPatientError"
    # 3. the remaining item is untouched — still PENDING, no upload.
    assert tracking.state_of(b_key) is UploadState.PENDING
    assert dest.uploads == []
    # Abort reason was persisted on the run row too.
    run_row = (
        tracking._conn()
        .execute("SELECT aborted_reason FROM runs WHERE run_id = ?", (run_id,))
        .fetchone()
    )
    assert run_row["aborted_reason"] == "WrongPatientError"


# --- transient retry ---


def test_transient_retry_succeeds_with_backoff(tmp_path: Path) -> None:
    items = _single_manifest(tmp_path)
    key = items[0].item_key
    dest = FakeDestination({PAT_A: DEST_A}, transient_failures={key: 2})
    tracking = _tracking(tmp_path)
    run_id = tracking.begin_run(dest.name)
    sleeps: list[float] = []

    engine = UploadEngine(dest, tracking, max_attempts=3, sleeper=sleeps.append)
    result = engine.run(items, {PAT_A: _patient(PAT_A)}, run_id)

    assert result.counts == {UploadState.COMPLETED.value: 1}
    assert _attempts(tracking, key) == 2
    assert sleeps == [2.0, 4.0]
    assert dest.uploads == [(key, DEST_A)]


def test_transient_exhausted_fails(tmp_path: Path) -> None:
    items = _single_manifest(tmp_path)
    key = items[0].item_key
    dest = FakeDestination({PAT_A: DEST_A}, transient_failures={key: 5})
    tracking = _tracking(tmp_path)
    run_id = tracking.begin_run(dest.name)
    sleeps: list[float] = []

    engine = UploadEngine(dest, tracking, max_attempts=3, sleeper=sleeps.append)
    result = engine.run(items, {PAT_A: _patient(PAT_A)}, run_id)

    assert result.counts == {UploadState.FAILED.value: 1}
    assert _attempts(tracking, key) == 3
    assert dest.uploads == []


# --- permanent failure ---


def test_permanent_failure_no_retries(tmp_path: Path) -> None:
    items = _single_manifest(tmp_path)
    key = items[0].item_key
    dest = FakeDestination({PAT_A: DEST_A}, permanent_failures={key})
    tracking = _tracking(tmp_path)
    run_id = tracking.begin_run(dest.name)
    sleeps: list[float] = []

    engine = UploadEngine(dest, tracking, sleeper=sleeps.append)
    result = engine.run(items, {PAT_A: _patient(PAT_A)}, run_id)

    assert result.counts == {UploadState.FAILED.value: 1}
    assert sleeps == []  # no backoff for a permanent failure
    assert dest.uploads == []


# --- echoed-size mismatch ---


def test_echoed_size_mismatch_fails_post_verify(tmp_path: Path) -> None:
    items = _single_manifest(tmp_path)
    key = items[0].item_key
    dest = FakeDestination({PAT_A: DEST_A}, echo_wrong_size_keys={key})
    tracking = _tracking(tmp_path)
    run_id = tracking.begin_run(dest.name)

    result = UploadEngine(dest, tracking).run(items, {PAT_A: _patient(PAT_A)}, run_id)

    assert result.counts == {UploadState.POST_VERIFY_FAILED.value: 1}
    # The upload itself did happen (the fake recorded it) — post-verify caught it.
    assert dest.uploads == [(key, DEST_A)]
    assert _audit_trail(tracking, key)[-1] == UploadState.POST_VERIFY_FAILED.value


# --- kill and resume (the headline) ---


def _five_item_manifest(tmp_path: Path) -> tuple[list[UploadItem], dict[str, Patient]]:
    docs: list[RenderedDoc] = []
    patients: dict[str, Patient] = {}
    # Distinct patients so each item resolves to its own destination chart;
    # item_keys sort deterministically by encounter id 0..4.
    for i in range(5):
        pid = f"feedface-0000-0000-0000-00000000010{i}"
        enc = f"enc-{i}"
        path = _doc(tmp_path, f"note-{i}.pdf", enc, pid, f"chart-{i}".encode())
        docs.append(RenderedDoc(path, enc, pid))
        patients[pid] = _patient(pid)
    return build_manifest(docs), patients


def test_kill_and_resume_no_double_filing(tmp_path: Path) -> None:
    items, patients = _five_item_manifest(tmp_path)
    db_path = tmp_path / "ledger.sqlite"
    # Shared destination store: uploads that land before the crash are visible
    # to the resumed run's scanner (simulating the destination's persistence).
    shared_existing: dict[str, set[str]] = {}
    known = {item.patient_id: f"dest-{item.encounter_id}" for item in items}

    # --- first run: crash after 2 successful uploads ---
    tracking1 = TrackingDB(db_path)
    run1 = tracking1.begin_run("fake")
    dest1 = FakeDestination(known, existing=shared_existing, crash_after=2)
    with pytest.raises(FakeCrash):
        UploadEngine(dest1, tracking1).run(items, patients, run1)
    landed_keys = [k for (k, _dest) in dest1.uploads]
    assert len(landed_keys) == 2  # two charts filed before the crash
    tracking1.close()

    # --- recover on a fresh ledger over the same DB file ---
    tracking2 = TrackingDB(db_path)
    run2 = tracking2.begin_run("fake")
    recovered = tracking2.recover(run2)
    # The in-flight item (the one whose UPLOADING was cut off) is now
    # UPLOAD_INTERRUPTED so it re-enters through the duplicate scan.
    assert recovered.get(UploadState.UPLOAD_INTERRUPTED.value) == 1
    interrupted = [
        item.item_key
        for item in items
        if tracking2.state_of(item.item_key) is UploadState.UPLOAD_INTERRUPTED
    ]
    assert len(interrupted) == 1

    # --- second run: resume; destination shares the same existing store ---
    dest2 = FakeDestination(known, existing=shared_existing)
    result = UploadEngine(dest2, tracking2).run(items, patients, run2)

    # The two charts that landed pre-crash were NOT re-uploaded by the resume.
    resumed_uploaded = {k for (k, _d) in dest2.uploads}
    assert not (set(landed_keys) & resumed_uploaded), "no double-filing of completed items"

    # The interrupted item: its bytes landed before the crash (crash fired
    # AFTER the upload was recorded), so the duplicate scan catches it.
    assert tracking2.state_of(interrupted[0]) is UploadState.DUPLICATE_AT_DESTINATION

    # Final state: every item terminal, none failed, all five accounted for.
    counts = result.counts
    assert sum(counts.values()) == 5
    assert counts.get(UploadState.COMPLETED.value, 0) == 4
    assert counts.get(UploadState.DUPLICATE_AT_DESTINATION.value, 0) == 1
    assert UploadState.FAILED.value not in counts
    # The headline property: across BOTH runs, no chart was uploaded twice.
    # All five charts were physically uploaded once (enc-0/enc-1 pre-crash,
    # enc-2/3/4 on resume); enc-1's pre-crash upload landed and the resume
    # caught it as a duplicate instead of re-filing it.
    all_uploads = landed_keys + [k for (k, _d) in dest2.uploads]
    assert len(all_uploads) == len(set(all_uploads)) == 5


def test_kill_and_resume_reuploads_when_crash_landed_nothing(tmp_path: Path) -> None:
    """The not-landed crash variant: the kill fires BEFORE the destination
    commits the bytes, so the resumed run must RE-UPLOAD the interrupted
    document — and still exactly once across both runs."""
    items, patients = _five_item_manifest(tmp_path)
    db_path = tmp_path / "ledger.sqlite"
    shared_existing: dict[str, set[str]] = {}
    known = {item.patient_id: f"dest-{item.encounter_id}" for item in items}

    # --- first run: the third upload dies before anything is recorded ---
    tracking1 = TrackingDB(db_path)
    run1 = tracking1.begin_run("fake")
    dest1 = FakeDestination(known, existing=shared_existing, crash_before=3)
    with pytest.raises(FakeCrash):
        UploadEngine(dest1, tracking1).run(items, patients, run1)
    landed_keys = [k for (k, _dest) in dest1.uploads]
    assert len(landed_keys) == 2  # the third never reached the destination
    tracking1.close()

    # --- recover and resume ---
    tracking2 = TrackingDB(db_path)
    run2 = tracking2.begin_run("fake")
    recovered = tracking2.recover(run2)
    assert recovered.get(UploadState.UPLOAD_INTERRUPTED.value) == 1
    interrupted = [
        item.item_key
        for item in items
        if tracking2.state_of(item.item_key) is UploadState.UPLOAD_INTERRUPTED
    ]
    assert interrupted[0] not in landed_keys  # truly not at the destination

    dest2 = FakeDestination(known, existing=shared_existing)
    result = UploadEngine(dest2, tracking2).run(items, patients, run2)

    # The interrupted document was re-uploaded (not treated as a duplicate)
    # and completed; every chart was physically uploaded exactly once.
    assert tracking2.state_of(interrupted[0]) is UploadState.COMPLETED
    resumed_keys = [k for (k, _d) in dest2.uploads]
    assert interrupted[0] in resumed_keys
    counts = result.counts
    assert counts.get(UploadState.COMPLETED.value, 0) == 5
    assert UploadState.DUPLICATE_AT_DESTINATION.value not in counts
    all_uploads = landed_keys + resumed_keys
    assert len(all_uploads) == len(set(all_uploads)) == 5


# --- PHI discipline ---


def test_no_phi_in_logs_across_a_failing_run(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A run that exercises failure + abort logging paths.
    p1 = _doc(tmp_path, "one.pdf", "aaa-enc", PAT_A, b"one")
    items = build_manifest([RenderedDoc(p1, "aaa-enc", PAT_A)])
    dest = FakeDestination({PAT_A: DEST_A}, wrong_patient_ids={PAT_A})
    tracking = _tracking(tmp_path)
    run_id = tracking.begin_run(dest.name)

    patient = Patient(
        id=PAT_A,
        given_name="Aloysius",
        family_name="Featherstonehaugh",
    )
    with caplog.at_level(logging.DEBUG, logger="anastomosis.deliver.browser.engine"):
        UploadEngine(dest, tracking).run(items, {PAT_A: patient}, run_id)

    blob = "\n".join(record.getMessage() for record in caplog.records)
    # No patient-derived values leak into any log line.
    assert "Aloysius" not in blob
    assert "Featherstonehaugh" not in blob
    # No file-system path leaks (paths can embed patient names).
    assert str(p1) not in blob
    assert "one.pdf" not in blob
    # What IS allowed: item keys, state names, and exc_tag type names.
    assert "WrongPatientError" in blob
    assert items[0].item_key in blob
