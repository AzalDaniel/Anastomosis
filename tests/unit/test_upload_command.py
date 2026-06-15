"""The shared browser-upload orchestration core (one engine drive, two frontends).

These pin codex P1-3/P1-4: a SINGLE retry-budget default for both frontends, the
manifest read INSIDE the output lock (lock-then-read — no TOCTOU), and skiplist
support routed through the one ``UploadCommand``. The Playwright touch is the
``attach`` callable, here a :class:`FakeDestination`, so the whole drive runs with
no browser. Synthetic data only (``feedface-`` ids, neutral file names).
"""

from __future__ import annotations

import datetime
import threading
from pathlib import Path

import pytest

from anastomosis.core.locking import OutputLockedError, output_lock
from anastomosis.core.model import Patient, PatientRecord
from anastomosis.core.upload_command import (
    DEFAULT_MAX_ATTEMPTS,
    LEDGER_NAME,
    UploadCommand,
    resolve_manifest_root,
    run_upload_command,
)
from anastomosis.deliver.browser.fake import FakeCrash, FakeDestination
from anastomosis.deliver.browser.persist import MANIFEST_NAME, write_upload_manifest
from anastomosis.deliver.browser.states import UploadState
from anastomosis.deliver.browser.tracking import TrackingDB
from anastomosis.reconstruct.engine import RenderedDoc

PATS = [f"feedface-0000-0000-0000-00000000030{i}" for i in range(3)]


def _write_manifest(root: Path, n: int = 3) -> Path:
    """Write a manifest of ``n`` charts INTO ``root``; return ``root``."""
    root.mkdir(parents=True, exist_ok=True)
    docs: list[RenderedDoc] = []
    records: list[PatientRecord] = []
    for i in range(n):
        pid = PATS[i]
        path = root / f"note-{i}.pdf"
        path.write_bytes(f"chart-{i}".encode())
        docs.append(RenderedDoc(path=path, encounter_id=f"enc-{i}", patient_id=pid))
        records.append(
            PatientRecord(
                id=pid,
                patient=Patient(
                    id=pid,
                    family_name="Family",
                    given_name="Given",
                    birth_date=datetime.date(1980, 1, 1 + i),
                ),
            )
        )
    write_upload_manifest(docs, records, root)
    return root


def _known(n: int = 3) -> dict[str, str]:
    return {PATS[i]: f"dest-{i}" for i in range(n)}


def _counts(out_dir: Path) -> dict[str, int]:
    tracking = TrackingDB(out_dir / LEDGER_NAME)
    try:
        return dict(tracking.counts())
    finally:
        tracking.close()


def test_default_max_attempts_is_three() -> None:
    # The single retry budget both frontends share (they previously diverged 3/4).
    assert DEFAULT_MAX_ATTEMPTS == 3


def test_resolve_manifest_root_prefers_out_dir_then_charts(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    # No manifest yet -> falls back to the <out>/charts (migrate) layout.
    assert resolve_manifest_root(out) == out / "charts"
    # A manifest in the dir itself -> that dir.
    (out / MANIFEST_NAME).write_text("{}", encoding="utf-8")
    assert resolve_manifest_root(out) == out


def test_run_upload_command_drives_to_completed(tmp_path: Path) -> None:
    out = _write_manifest(tmp_path / "out")
    result = run_upload_command(UploadCommand(out_dir=out), lambda: FakeDestination(_known()))
    assert result.aborted_reason is None
    assert result.counts.get(UploadState.COMPLETED.value) == 3
    assert sum(result.counts.values()) == 3
    assert result.report_path.is_file()
    assert result.report_path.parent == out  # report lands inside the output dir


def test_run_upload_command_honors_skiplist(tmp_path: Path) -> None:
    out = _write_manifest(tmp_path / "out")
    dest = FakeDestination(_known())
    result = run_upload_command(
        UploadCommand(out_dir=out, skiplist=frozenset({"enc-1"})),
        lambda: dest,
    )
    assert result.counts.get(UploadState.SKIPPED_SKIPLIST.value) == 1
    assert result.counts.get(UploadState.COMPLETED.value) == 2
    # The skiplisted encounter was never physically uploaded.
    uploaded = {k for (k, _d) in dest.uploads}
    assert not any(k.startswith("enc-1:") for k in uploaded)


def test_run_upload_command_locks_before_reading_or_attaching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lock-then-read: when another run holds the output lock, the command refuses
    BEFORE the manifest is read AND before the browser is attached (codex P1-4
    TOCTOU). Instrumenting BOTH sides proves the ordering — a regression that
    moved ONLY the manifest read above the lock would raise OutputLockedError with
    the attach untouched, and would survive an attach-only assertion."""
    import anastomosis.deliver.browser.persist as persist

    out = _write_manifest(tmp_path / "out")
    calls = {"read": 0, "attach": 0}

    real_read = persist.read_upload_manifest

    def _counting_read(root: Path) -> object:
        calls["read"] += 1
        return real_read(root)

    # run_upload_command imports read_upload_manifest lazily FROM persist, so the
    # patch must live on persist (the source), not on upload_command's namespace.
    monkeypatch.setattr(persist, "read_upload_manifest", _counting_read)

    def _attach() -> FakeDestination:
        calls["attach"] += 1
        return FakeDestination(_known())

    with output_lock(out):  # a sibling run holds the dir
        with pytest.raises(OutputLockedError):
            run_upload_command(UploadCommand(out_dir=out), _attach)
    # NEITHER the manifest read NOR the attach was reached — both live INSIDE the
    # lock, so a locked dir refuses before touching the manifest or the browser.
    assert calls == {"read": 0, "attach": 0}
    # No ledger run was begun either (nothing drove past the lock).
    assert not (out / LEDGER_NAME).exists() or sum(_counts(out).values()) == 0


def test_run_upload_command_resumes_after_a_crash(tmp_path: Path) -> None:
    """A process-kill (BaseException) sails out; a re-run resumes with no double-file."""
    out = _write_manifest(tmp_path / "out")
    shared: dict[str, set[str]] = {}

    # First run crashes after one successful upload (FakeCrash is a BaseException
    # — a KeyboardInterrupt subclass — so it sails out exactly like a process kill).
    with pytest.raises(FakeCrash):
        run_upload_command(
            UploadCommand(out_dir=out),
            lambda: FakeDestination(_known(), existing=shared, crash_after=1),
        )

    # Second run resumes against the SAME ledger + destination store.
    result = run_upload_command(
        UploadCommand(out_dir=out),
        lambda: FakeDestination(_known(), existing=shared),
    )
    assert result.aborted_reason is None
    counts = _counts(out)
    completed = counts.get(UploadState.COMPLETED.value, 0)
    dupes = counts.get(UploadState.DUPLICATE_AT_DESTINATION.value, 0)
    assert counts.get(UploadState.FAILED.value, 0) == 0
    assert completed + dupes == 3
    assert sum(counts.values()) == 3


def test_run_upload_command_passes_the_stop_flag(tmp_path: Path) -> None:
    """A pre-set stop flag halts the run at the first item boundary (cooperative)."""
    out = _write_manifest(tmp_path / "out")
    stop = threading.Event()
    stop.set()  # already requested -> nothing should be driven
    result = run_upload_command(
        UploadCommand(out_dir=out), lambda: FakeDestination(_known()), stop=stop
    )
    assert result.aborted_reason is None
    # Everything stays PENDING (no item was driven past the boundary check).
    counts = _counts(out)
    assert counts.get(UploadState.COMPLETED.value, 0) == 0
    assert counts.get(UploadState.PENDING.value, 0) == 3
