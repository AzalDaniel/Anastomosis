"""`anast upload` CLI driver tests — exercisable with NO browser/Chromium.

The Playwright touch is the single ``cli._make_destination`` seam, monkeypatched
to a :class:`FakeDestination`. The CDP loopback validation runs for real on a
loopback URL. A fixture pack dir ships a ready ``selectors.yaml`` so the
``.ready`` gate passes without the discovery wizard. Synthetic data only
(``feedface-`` ids, neutral file names).
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

import anastomosis.cli as cli
from anastomosis.cli import app
from anastomosis.core.model import Patient, PatientRecord
from anastomosis.deliver.browser.fake import FakeDestination
from anastomosis.deliver.browser.persist import write_upload_manifest
from anastomosis.deliver.browser.states import UploadState
from anastomosis.deliver.browser.tracking import TrackingDB
from anastomosis.destinations.browserpack import SelectorMap
from anastomosis.reconstruct.engine import RenderedDoc

runner = CliRunner()

LOOPBACK = "http://127.0.0.1:9222"
DEST = "testdest"

# Three patients, three charts — distinct so each resolves to its own chart.
PATS = [f"feedface-0000-0000-0000-00000000010{i}" for i in range(3)]


def _pack_dir(tmp_path: Path) -> Path:
    """A ready destination pack dir (real selectors, no DISCOVER placeholders)."""
    root = tmp_path / "packs"
    pack = root / DEST
    pack.mkdir(parents=True)
    selectors = {slot: f"#{slot}" for slot in SelectorMap.required_slots()}
    lines = [f"name: {DEST}", "display: Test Destination", "selectors:"]
    lines += [f"  {slot}: '{sel}'" for slot, sel in selectors.items()]
    (pack / "pack.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


def _scaffold_pack_dir(tmp_path: Path) -> Path:
    """A NOT-ready pack dir: required selectors left at the DISCOVER placeholder."""
    root = tmp_path / "packs"
    pack = root / DEST
    pack.mkdir(parents=True)
    lines = [f"name: {DEST}", "display: Test Destination", "selectors:"]
    lines += [f"  {slot}: 'DISCOVER — run the wizard'" for slot in SelectorMap.required_slots()]
    (pack / "pack.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


def _write_manifest(tmp_path: Path, n: int = 3) -> Path:
    """Write a manifest of ``n`` charts directly into ``out_dir``; return out_dir.

    The chart PDFs are written INTO ``out_dir`` (where a real render lands them),
    because the manifest stores basenames re-absolutized against the manifest
    root — so the engine's preflight (existence + re-hash) resolves them there.
    """
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    docs: list[RenderedDoc] = []
    records: list[PatientRecord] = []
    for i in range(n):
        pid = PATS[i]
        path = out_dir / f"note-{i}.pdf"
        path.write_bytes(f"chart-{i}".encode())
        docs.append(RenderedDoc(path=path, encounter_id=f"enc-{i}", patient_id=pid))
        patient = Patient(
            id=pid,
            family_name="Family",
            given_name="Given",
            birth_date=datetime.date(1980, 1, 1 + i),
        )
        records.append(PatientRecord(id=pid, patient=patient))
    write_upload_manifest(docs, records, out_dir)
    return out_dir


def _known(n: int = 3) -> dict[str, str]:
    return {PATS[i]: f"dest-{i}" for i in range(n)}


def _invoke(out_dir: Path, pack_root: Path, *extra: str) -> object:
    return runner.invoke(
        app,
        [
            "upload",
            str(out_dir),
            "--to",
            DEST,
            "--cdp",
            LOOPBACK,
            "--pack-dir",
            str(pack_root),
            "--yes",
            *extra,
        ],
    )


def _ledger_states(out_dir: Path) -> dict[str, int]:
    tracking = TrackingDB(out_dir / "upload_ledger.sqlite")
    try:
        return dict(tracking.counts())
    finally:
        tracking.close()


# --- (2) happy path ---------------------------------------------------------


def test_happy_path_all_completed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out_dir = _write_manifest(tmp_path)
    pack_root = _pack_dir(tmp_path)
    monkeypatch.setattr(cli, "_make_destination", lambda cdp, loaded: FakeDestination(_known()))

    result = _invoke(out_dir, pack_root)

    assert result.exit_code == 0, result.output
    counts = _ledger_states(out_dir)
    assert counts.get(UploadState.COMPLETED.value) == 3
    assert sum(counts.values()) == 3
    # The summary line and the report path printed.
    assert "completed=3" in result.output
    assert "run report" in result.output
    # The run report lives under the 0700 out dir.
    reports = list(out_dir.glob("run-report-*.json"))
    assert len(reports) == 1
    import os
    import stat

    if os.name == "posix":
        assert stat.S_IMODE(out_dir.stat().st_mode) == 0o700


# --- (3) resume / no double-file --------------------------------------------


def test_resume_no_double_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out_dir = _write_manifest(tmp_path)
    pack_root = _pack_dir(tmp_path)
    # A destination store shared across the crash so pre-crash uploads are
    # visible to the resumed run's scanner (the double-file defense).
    shared: dict[str, set[str]] = {}

    # First run: crash after 1 successful upload (FakeCrash is a BaseException
    # that sails out of the engine — exactly a process kill).
    monkeypatch.setattr(
        cli,
        "_make_destination",
        lambda cdp, loaded: FakeDestination(_known(), existing=shared, crash_after=1),
    )
    first = _invoke(out_dir, pack_root)
    # The crash propagated out of the run (no clean exit).
    assert first.exit_code != 0

    # Second run: resume against the SAME ledger + shared destination store.
    monkeypatch.setattr(
        cli,
        "_make_destination",
        lambda cdp, loaded: FakeDestination(_known(), existing=shared),
    )
    second = _invoke(out_dir, pack_root)

    counts = _ledger_states(out_dir)
    # Every item terminal, none failed; completed + duplicate == N.
    assert counts.get(UploadState.FAILED.value, 0) == 0
    completed = counts.get(UploadState.COMPLETED.value, 0)
    dupes = counts.get(UploadState.DUPLICATE_AT_DESTINATION.value, 0)
    assert completed + dupes == 3
    assert sum(counts.values()) == 3
    assert second.exit_code == 0, second.output


# --- (4) skiplist -----------------------------------------------------------


def test_skiplist_item_skipped_never_uploaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_dir = _write_manifest(tmp_path)
    pack_root = _pack_dir(tmp_path)
    skip = tmp_path / "skip.txt"
    skip.write_text("enc-1\n", encoding="utf-8")  # exclude the second encounter

    dest = FakeDestination(_known())
    monkeypatch.setattr(cli, "_make_destination", lambda cdp, loaded: dest)

    result = _invoke(out_dir, pack_root, "--skiplist", str(skip))

    assert result.exit_code == 0, result.output
    counts = _ledger_states(out_dir)
    assert counts.get(UploadState.SKIPPED_SKIPLIST.value) == 1
    assert counts.get(UploadState.COMPLETED.value) == 2
    # The skiplisted encounter was never physically uploaded.
    uploaded_keys = {k for (k, _d) in dest.uploads}
    assert not any(k.startswith("enc-1:") for k in uploaded_keys)


# --- (5) wrong-patient ------------------------------------------------------


def test_wrong_patient_aborts_exit_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out_dir = _write_manifest(tmp_path)
    pack_root = _pack_dir(tmp_path)
    # The FIRST patient triggers a banner mismatch -> the run aborts.
    monkeypatch.setattr(
        cli,
        "_make_destination",
        lambda cdp, loaded: FakeDestination(_known(), wrong_patient_ids={PATS[0]}),
    )

    result = _invoke(out_dir, pack_root)

    assert result.exit_code == 1, result.output
    counts = _ledger_states(out_dir)
    # The offending item failed pre-verify; later items remain PENDING.
    assert counts.get(UploadState.PRE_VERIFY_FAILED.value) == 1
    assert counts.get(UploadState.PENDING.value, 0) >= 1
    # The abort reason is recorded in the run report.
    import json

    report = json.loads(next(out_dir.glob("run-report-*.json")).read_text(encoding="utf-8"))
    assert report["aborted_reason"] == "WrongPatientError"


# --- (6) non-loopback cdp ---------------------------------------------------


def test_non_loopback_cdp_exit_2_seam_never_called(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_dir = _write_manifest(tmp_path)
    pack_root = _pack_dir(tmp_path)
    called = {"n": 0}

    def _spy(cdp: str, loaded: object) -> object:
        called["n"] += 1
        return FakeDestination(_known())

    monkeypatch.setattr(cli, "_make_destination", _spy)
    result = runner.invoke(
        app,
        [
            "upload",
            str(out_dir),
            "--to",
            DEST,
            "--cdp",
            "http://10.0.0.5:9222",  # routable, non-loopback
            "--pack-dir",
            str(pack_root),
            "--yes",
        ],
    )
    assert result.exit_code == 2, result.output
    assert called["n"] == 0  # the seam is never reached past the loopback gate
    assert "loopback" in result.output.lower()


# --- (7) missing-port cdp ---------------------------------------------------


def test_missing_port_cdp_exit_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out_dir = _write_manifest(tmp_path)
    pack_root = _pack_dir(tmp_path)
    monkeypatch.setattr(cli, "_make_destination", lambda cdp, loaded: FakeDestination(_known()))
    result = runner.invoke(
        app,
        [
            "upload",
            str(out_dir),
            "--to",
            DEST,
            "--cdp",
            "http://127.0.0.1",  # no explicit port
            "--pack-dir",
            str(pack_root),
            "--yes",
        ],
    )
    assert result.exit_code == 2, result.output
    assert "port" in result.output.lower()


# --- (8) warning surfaced + prompt-no aborts --------------------------------


def test_warning_surfaced_and_prompt_no_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_dir = _write_manifest(tmp_path)
    pack_root = _pack_dir(tmp_path)
    called = {"n": 0}

    def _spy(cdp: str, loaded: object) -> object:
        called["n"] += 1
        return FakeDestination(_known())

    monkeypatch.setattr(cli, "_make_destination", _spy)
    # NO --yes; answer "n" to the attach confirmation.
    result = runner.invoke(
        app,
        ["upload", str(out_dir), "--to", DEST, "--cdp", LOOPBACK, "--pack-dir", str(pack_root)],
        input="n\n",
    )
    assert result.exit_code == 0, result.output
    assert "multi-user" in result.output.lower()  # the shared-machine warning
    assert "aborted" in result.output.lower()
    assert called["n"] == 0  # declined before the seam


# --- (9) not-ready pack -----------------------------------------------------


def test_not_ready_pack_exit_2_names_wizard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_dir = _write_manifest(tmp_path)
    pack_root = _scaffold_pack_dir(tmp_path)
    monkeypatch.setattr(cli, "_make_destination", lambda cdp, loaded: FakeDestination(_known()))

    result = _invoke(out_dir, pack_root)

    assert result.exit_code == 2, result.output
    # Normalize whitespace: Rich soft-wraps the guidance line at the terminal
    # width (which differs across platforms), so the wizard command can span a
    # newline ("anast \ndestination init") — match on the collapsed text.
    assert "anast destination init" in " ".join(result.output.split())


# --- (10) missing / malformed manifest --------------------------------------


def test_missing_manifest_exit_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pack_root = _pack_dir(tmp_path)
    out_dir = tmp_path / "empty"
    out_dir.mkdir()
    monkeypatch.setattr(cli, "_make_destination", lambda cdp, loaded: FakeDestination(_known()))

    result = _invoke(out_dir, pack_root)
    assert result.exit_code == 2, result.output


def test_malformed_manifest_exit_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pack_root = _pack_dir(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "upload_manifest.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(cli, "_make_destination", lambda cdp, loaded: FakeDestination(_known()))

    result = _invoke(out_dir, pack_root)
    assert result.exit_code == 2, result.output


# --- manifest discovered under <out>/charts (migrate layout) ----------------


def test_manifest_found_under_charts_subdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Migrate writes the manifest into <out>/charts; the upload command must
    # find it there when the top-level dir has none.
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    charts = out_dir / "charts"
    charts.mkdir()
    docs: list[RenderedDoc] = []
    records: list[PatientRecord] = []
    for i in range(2):
        pid = PATS[i]
        path = charts / f"c-{i}.pdf"
        path.write_bytes(f"x{i}".encode())
        docs.append(RenderedDoc(path=path, encounter_id=f"enc-{i}", patient_id=pid))
        records.append(
            PatientRecord(id=pid, patient=Patient(id=pid, family_name="F", given_name="G"))
        )
    write_upload_manifest(docs, records, charts)
    pack_root = _pack_dir(tmp_path)
    monkeypatch.setattr(cli, "_make_destination", lambda cdp, loaded: FakeDestination(_known(2)))

    result = _invoke(out_dir, pack_root)
    assert result.exit_code == 0, result.output
    assert _ledger_states(out_dir).get(UploadState.COMPLETED.value) == 2
