"""Run report tests: JSON content, determinism, PHI exclusion, summary line.

Synthetic data only. A name-shaped path is deliberately placed in the ledger
to prove the JSON report never copies ``file_path`` values out of it.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from anastomosis.core.model import Patient
from anastomosis.deliver.browser.engine import UploadEngine
from anastomosis.deliver.browser.fake import FakeDestination
from anastomosis.deliver.browser.manifest import build_manifest
from anastomosis.deliver.browser.reports import summary_line, write_run_report
from anastomosis.deliver.browser.states import UploadState
from anastomosis.deliver.browser.tracking import TrackingDB
from anastomosis.reconstruct.engine import RenderedDoc

PAT_OK = "feedface-0000-0000-0000-0000000000c0"
PAT_FAIL = "feedface-0000-0000-0000-0000000000c1"
PAT_RETRY = "feedface-0000-0000-0000-0000000000c2"

# A name-shaped basename to prove the report never copies file_path values.
NAME_SHAPED = "Featherstonehaugh_Aloysius_03-14-1980.pdf"


def _patient(pid: str) -> Patient:
    return Patient(id=pid, given_name="Given", family_name="Family")


def _mixed_run(tmp_path: Path) -> tuple[TrackingDB, str]:
    """A run that ends with a completed, a permanently failed, and a retried-
    then-completed item — exercising the counts and both histograms."""
    docs = []
    patients = {}
    specs = [
        (PAT_OK, "enc-ok", "ok.pdf"),
        (PAT_FAIL, "enc-fail", "fail.pdf"),
        # The retried item carries a name-shaped filename in the ledger.
        (PAT_RETRY, "enc-retry", NAME_SHAPED),
    ]
    for pid, enc, name in specs:
        path = tmp_path / name
        path.write_bytes(f"chart-{enc}".encode())
        docs.append(RenderedDoc(path, enc, pid))
        patients[pid] = _patient(pid)
    items = build_manifest(docs)

    fail_key = next(i.item_key for i in items if i.encounter_id == "enc-fail")
    retry_key = next(i.item_key for i in items if i.encounter_id == "enc-retry")

    dest = FakeDestination(
        {PAT_OK: "d-ok", PAT_FAIL: "d-fail", PAT_RETRY: "d-retry"},
        permanent_failures={fail_key},
        transient_failures={retry_key: 1},
    )
    db_path = tmp_path / "out" / "ledger.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tracking = TrackingDB(db_path)
    run_id = tracking.begin_run(dest.name)
    UploadEngine(dest, tracking, max_attempts=3, sleeper=lambda _s: None).run(
        items, patients, run_id
    )
    tracking.finish_run(run_id)
    return tracking, run_id


# --- JSON content ---


def test_report_matches_ledger(tmp_path: Path) -> None:
    tracking, run_id = _mixed_run(tmp_path)
    out_dir = tmp_path / "report-out"

    path = write_run_report(tracking, run_id, out_dir)
    report = json.loads(path.read_text(encoding="utf-8"))

    assert report["run_id"] == run_id
    assert report["destination"] == "fake"
    assert report["counts"] == {
        UploadState.COMPLETED.value: 2,
        UploadState.FAILED.value: 1,
    }
    # Error-type histogram from the audit trail: the permanent failure routed
    # to FAILED (PermanentDeliveryError) and the retry recorded a transient.
    errors = report["error_type_histogram"]
    assert errors["PermanentDeliveryError"] == 1
    assert errors["TransientDeliveryError"] == 1
    # Attempts histogram: two items at 0 attempts, one at 1 (the retried one).
    assert report["attempts_histogram"] == {"0": 2, "1": 1}


def test_report_is_deterministic_byte_identical(tmp_path: Path) -> None:
    tracking, run_id = _mixed_run(tmp_path)
    out_dir = tmp_path / "report-out"

    first = write_run_report(tracking, run_id, out_dir).read_bytes()
    second = write_run_report(tracking, run_id, out_dir).read_bytes()
    assert first == second


def test_report_contains_no_file_path_values(tmp_path: Path) -> None:
    tracking, run_id = _mixed_run(tmp_path)
    out_dir = tmp_path / "report-out"
    blob = write_run_report(tracking, run_id, out_dir).read_text(encoding="utf-8")

    # The name-shaped basename lives in the ledger's file_path column but must
    # NOT appear anywhere in the report.
    assert NAME_SHAPED not in blob
    assert "Featherstonehaugh" not in blob
    assert ".pdf" not in blob


def test_report_written_under_hardened_dir(tmp_path: Path) -> None:
    tracking, run_id = _mixed_run(tmp_path)
    out_dir = tmp_path / "report-out"
    path = write_run_report(tracking, run_id, out_dir)

    assert path.parent == out_dir
    assert path.exists()
    if os.name == "posix":
        mode = stat.S_IMODE(out_dir.stat().st_mode)
        assert mode == 0o700


def test_report_records_abort_reason(tmp_path: Path) -> None:
    # A run finished with an abort reason surfaces it in the report.
    db_path = tmp_path / "out" / "ledger.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tracking = TrackingDB(db_path)
    run_id = tracking.begin_run("fake")
    tracking.finish_run(run_id, aborted_reason="WrongPatientError")

    report = json.loads(
        write_run_report(tracking, run_id, tmp_path / "rep").read_text(encoding="utf-8")
    )
    assert report["aborted_reason"] == "WrongPatientError"
    assert report["finished_at"] is not None


# --- summary line ---


def test_summary_line_counts_only_fixed_order() -> None:
    counts = {
        UploadState.COMPLETED.value: 5,
        UploadState.FAILED.value: 1,
        UploadState.PENDING.value: 2,
    }
    line = summary_line(counts)
    # Fixed order: PENDING comes before FAILED comes before COMPLETED.
    assert line == "pending=2 failed=1 completed=5"


def test_summary_line_omits_zero_count_states() -> None:
    line = summary_line({UploadState.COMPLETED.value: 3})
    assert line == "completed=3"


def test_summary_line_empty_counts() -> None:
    assert summary_line({}) == ""


def test_summary_line_has_no_paths_or_keys() -> None:
    # Even if a stray non-state key sneaks into counts, it is ignored.
    line = summary_line({UploadState.COMPLETED.value: 1, "enc-secret:abc123": 9, "/tmp/x.pdf": 4})
    assert line == "completed=1"
    assert "enc-secret" not in line
    assert ".pdf" not in line


# --- verification coverage ---


def test_report_embeds_verification_coverage_when_supplied(tmp_path: Path) -> None:
    """The report's new ``verification_coverage`` field tells the truth about
    which L-levels actually ran for this run. The README/CLI no longer over-
    claim a 'full L0-L6 ladder' — the report names every skipped level.
    """
    tracking, run_id = _mixed_run(tmp_path)
    out_dir = tmp_path / "report-out"

    coverage = {
        "L0": {"pass_count": 3, "fail_count": 0, "skip_count": 0, "skip_reasons": []},
        "L1": {"pass_count": 3, "fail_count": 0, "skip_count": 0, "skip_reasons": []},
        "L2": {"pass_count": 3, "fail_count": 0, "skip_count": 0, "skip_reasons": []},
        # The exact contract: L3 SKIP with the verifier's own
        # level-shape reason string (no patient values, no paths).
        "L3": {
            "pass_count": 0,
            "fail_count": 0,
            "skip_count": 3,
            "skip_reasons": ["no pack provided"],
        },
        "L4": {"pass_count": 3, "fail_count": 0, "skip_count": 0, "skip_reasons": []},
        "L5": {
            "pass_count": 0,
            "fail_count": 0,
            "skip_count": 3,
            "skip_reasons": ["destination has no MetadataReader"],
        },
        "L6": {
            "pass_count": 0,
            "fail_count": 0,
            "skip_count": 3,
            "skip_reasons": ["destination has no DocumentReader"],
        },
    }
    path = write_run_report(tracking, run_id, out_dir, verification_coverage=coverage)
    report = json.loads(path.read_text(encoding="utf-8"))

    assert "verification_coverage" in report
    assert set(report["verification_coverage"].keys()) == {f"L{i}" for i in range(7)}
    assert report["verification_coverage"]["L0"]["pass_count"] == 3
    assert report["verification_coverage"]["L3"]["skip_count"] == 3
    assert report["verification_coverage"]["L3"]["skip_reasons"] == ["no pack provided"]
    assert report["verification_coverage"]["L5"]["skip_reasons"] == [
        "destination has no MetadataReader"
    ]


def test_report_coverage_omitted_when_none(tmp_path: Path) -> None:
    """``--no-verify`` runs supply None — the field must NOT appear."""
    tracking, run_id = _mixed_run(tmp_path)
    out_dir = tmp_path / "report-out"
    path = write_run_report(tracking, run_id, out_dir, verification_coverage=None)
    report = json.loads(path.read_text(encoding="utf-8"))
    assert "verification_coverage" not in report


def test_report_coverage_round_trip_is_deterministic(tmp_path: Path) -> None:
    """Same coverage → byte-identical bytes (sort_keys + sorted reasons)."""
    tracking, run_id = _mixed_run(tmp_path)
    out_dir = tmp_path / "report-out"

    coverage = {
        "L3": {
            "pass_count": 1,
            "fail_count": 0,
            "skip_count": 2,
            # Reasons supplied unsorted — the writer must canonicalize.
            "skip_reasons": ["no header fields declared", "no pack provided"],
        },
    }
    first = write_run_report(tracking, run_id, out_dir, verification_coverage=coverage).read_bytes()
    second = write_run_report(
        tracking, run_id, out_dir, verification_coverage=coverage
    ).read_bytes()
    assert first == second
    blob = first.decode("utf-8")
    # Sorted alphabetically.
    assert blob.index("no header fields declared") < blob.index("no pack provided")


def test_layered_verifier_coverage_summary_aggregates_by_level() -> None:
    """``LayeredVerifier.coverage_summary()`` collects per-level counts and
    dedup'd skip-reason strings across all items the verifier saw — no item
    keys, no paths, only PHI-safe aggregate counts and detail strings.
    """
    from anastomosis.deliver.verify.composite import LayeredVerifier
    from anastomosis.deliver.verify.levels import LevelResult, LevelStatus

    verifier = LayeredVerifier()
    # Two items hand-fed into the verifier's results dict — bypasses
    # verify_pre/verify_post which need real UploadItem fixtures. We are
    # testing the aggregator, not the level runners.
    verifier._results = {  # type: ignore[attr-defined]
        "item-A": [
            LevelResult("L0", LevelStatus.PASS, "sha256 and size match"),
            LevelResult("L3", LevelStatus.SKIP, "no pack provided"),
            LevelResult("L5", LevelStatus.SKIP, "destination has no MetadataReader"),
        ],
        "item-B": [
            LevelResult("L0", LevelStatus.PASS, "sha256 and size match"),
            LevelResult("L3", LevelStatus.SKIP, "no pack provided"),  # dedup target
            LevelResult("L4", LevelStatus.FAIL, "wrong patient on banner"),
        ],
    }
    summary = verifier.coverage_summary()

    assert summary["L0"] == {
        "pass_count": 2,
        "fail_count": 0,
        "skip_count": 0,
        "skip_reasons": [],
    }
    assert summary["L3"]["skip_count"] == 2
    # Identical reasons collapse to one — the dedup contract.
    assert summary["L3"]["skip_reasons"] == ["no pack provided"]
    assert summary["L4"]["fail_count"] == 1
    # PHI-safety: nothing in the summary should carry an item key.
    flat = json.dumps(summary)
    assert "item-A" not in flat and "item-B" not in flat
