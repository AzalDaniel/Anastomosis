"""`anast upload` CLI driver tests — exercisable with NO browser/Chromium.

Both delivery routes are covered with their live seam monkeypatched, so nothing
here launches a browser or reaches a FHIR server:

* the BROWSER route's Playwright touch is the single ``cli._make_destination``
  seam, monkeypatched to a :class:`FakeDestination`. The CDP loopback validation
  runs for real on a loopback URL, and a fixture pack dir ships a ready
  ``selectors.yaml`` so the ``.ready`` gate passes without the discovery wizard;
* the API route's touch is ``cli._make_fhir_destination``, monkeypatched the same
  way. The FhirEndpoint https-or-loopback gate runs for real, and the bearer
  token is asserted to come from the ENVIRONMENT (never argv).

Synthetic data only (``feedface-`` ids, neutral file names).
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
from anastomosis.deliver.fhir_api.attach import DEFAULT_TOKEN_ENV
from anastomosis.destinations.browserpack import SelectorMap
from anastomosis.reconstruct.engine import RenderedDoc

runner = CliRunner()

LOOPBACK = "http://127.0.0.1:9222"
DEST = "testdest"

# The API route's fixtures. A loopback http base URL is the one cleartext form
# FhirEndpoint allows (the local-HAPI case); the token is synthetic and only
# ever reaches the CLI through the environment.
FHIR_URL = "http://127.0.0.1:8080/fhir"
FHIR_TOKEN = "feedface-bearer-token"
ALT_TOKEN_ENV = "ANAST_FHIR_TOKEN_ALT"

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

    result = _invoke(out_dir, pack_root, "--no-verify")  # drive test; stub PDFs

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
    first = _invoke(out_dir, pack_root, "--no-verify")  # drive/resume test; stub PDFs
    # The crash propagated out of the run (no clean exit).
    assert first.exit_code != 0

    # Second run: resume against the SAME ledger + shared destination store.
    monkeypatch.setattr(
        cli,
        "_make_destination",
        lambda cdp, loaded: FakeDestination(_known(), existing=shared),
    )
    second = _invoke(out_dir, pack_root, "--no-verify")

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

    result = _invoke(out_dir, pack_root, "--skiplist", str(skip), "--no-verify")

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


# --- (5b) finished-but-failed (no abort) still exits 1 ----------------------


def test_failed_items_no_abort_exit_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A run that FINISHES with no abort but leaves items in a non-clean terminal
    state (every upload fails permanently -> FAILED) exits 1 — so a script can
    branch on a failed file. Delegates to the shared result.exit_code verdict."""
    from anastomosis.core.upload_command import resolve_manifest_root
    from anastomosis.deliver.browser.persist import read_upload_manifest

    out_dir = _write_manifest(tmp_path)
    pack_root = _pack_dir(tmp_path)
    # Read the manifest to learn the item_keys, then fail every upload.
    items, _patients = read_upload_manifest(resolve_manifest_root(out_dir))
    fail_keys = {item.item_key for item in items}
    monkeypatch.setattr(
        cli,
        "_make_destination",
        lambda cdp, loaded: FakeDestination(_known(), permanent_failures=fail_keys),
    )

    result = _invoke(out_dir, pack_root, "--no-verify")

    assert result.exit_code == 1, result.output
    counts = _ledger_states(out_dir)
    assert counts.get(UploadState.FAILED.value) == 3
    # It was NOT an abort — the run finished; the items just failed.
    import json

    report = json.loads(next(out_dir.glob("run-report-*.json")).read_text(encoding="utf-8"))
    assert report["aborted_reason"] is None


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


# --- (11) the --verify lever threads into the UploadCommand -----------------


def _capture_cmd(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Patch the shared run_upload_command to capture the UploadCommand it gets.

    The CLI imports run_upload_command lazily FROM anastomosis.core.upload_command,
    so the patch lives on that module (the source), not on cli's namespace — the
    same lock-then-read patch discipline test_upload_command.py uses for persist.
    """
    import anastomosis.core.upload_command as upload_command
    from anastomosis.core.upload_command import UploadCommand, UploadCommandResult

    captured: dict[str, object] = {}

    def _fake(cmd: UploadCommand, attach: object, **kwargs: object) -> UploadCommandResult:
        captured["cmd"] = cmd
        # A clean, all-completed result so the CLI exits 0 without a browser.
        return UploadCommandResult(
            counts={UploadState.COMPLETED.value: 1},
            aborted_reason=None,
            report_path=cmd.out_dir / "run-report-x.json",
        )

    monkeypatch.setattr(upload_command, "run_upload_command", _fake)
    return captured


def test_no_verify_flag_threads_into_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_dir = _write_manifest(tmp_path)
    pack_root = _pack_dir(tmp_path)
    monkeypatch.setattr(cli, "_make_destination", lambda cdp, loaded: FakeDestination(_known()))
    captured = _capture_cmd(monkeypatch)

    result = _invoke(out_dir, pack_root, "--no-verify")

    assert result.exit_code == 0, result.output
    cmd = captured["cmd"]
    assert cmd.verify is False  # type: ignore[union-attr]  — explicit opt-out


def test_verify_defaults_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out_dir = _write_manifest(tmp_path)
    pack_root = _pack_dir(tmp_path)
    monkeypatch.setattr(cli, "_make_destination", lambda cdp, loaded: FakeDestination(_known()))
    captured = _capture_cmd(monkeypatch)

    result = _invoke(out_dir, pack_root)  # no flag — the SAFE default is on

    assert result.exit_code == 0, result.output
    cmd = captured["cmd"]
    assert cmd.verify is True  # type: ignore[union-attr]


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

    result = _invoke(out_dir, pack_root, "--no-verify")  # drive test; stub PDFs
    assert result.exit_code == 0, result.output
    assert _ledger_states(out_dir).get(UploadState.COMPLETED.value) == 2


# --- (12) the FHIR API route ------------------------------------------------
#
# Same command, same engine, same ledger — only the pre-flight and the attach
# seam differ. Every test here patches ``cli._make_fhir_destination``, so no
# request is ever made; the https-or-loopback gate and the env-var token
# resolution run for real.


def _invoke_fhir(out_dir: Path, *extra: str) -> object:
    return runner.invoke(app, ["upload", str(out_dir), "--fhir", FHIR_URL, *extra])


def _fhir_spy(
    monkeypatch: pytest.MonkeyPatch, dest: FakeDestination | None = None
) -> list[dict[str, object]]:
    """Patch the API attach seam, recording the endpoint config it receives.

    Mirrors the browser route's ``cli._make_destination`` monkeypatch: the seam
    is resolved LATE through the ``cli`` module, so patching the attribute here
    is what the command actually calls. Returns the (mutable) call log.
    """
    calls: list[dict[str, object]] = []
    made = dest if dest is not None else FakeDestination(_known())

    def _spy(base_url: str, *, bearer_token: str | None, create_missing_patients: bool) -> object:
        calls.append(
            {
                "base_url": base_url,
                "bearer_token": bearer_token,
                "create_missing_patients": create_missing_patients,
            }
        )
        return made

    monkeypatch.setattr(cli, "_make_fhir_destination", _spy)
    return calls


def test_fhir_route_drives_the_engine_with_no_browser_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--fhir alone is a complete route: it drives the same engine to the same
    ledger, and it must NOT show the shared-machine warning or ask for the
    attach confirmation — there is no browser to attach to."""
    monkeypatch.delenv(DEFAULT_TOKEN_ENV, raising=False)
    out_dir = _write_manifest(tmp_path)
    calls = _fhir_spy(monkeypatch)

    # No --yes: a prompt on this route would abort on the empty stdin.
    result = _invoke_fhir(out_dir, "--no-verify")  # drive test; stub PDFs

    assert result.exit_code == 0, result.output
    counts = _ledger_states(out_dir)
    assert counts.get(UploadState.COMPLETED.value) == 3
    assert sum(counts.values()) == 3
    assert "completed=3" in result.output
    assert "run report" in result.output
    # The browser route's confirmation flow is absent, not merely auto-accepted.
    assert "multi-user" not in result.output.lower()
    assert "attach to this browser" not in result.output.lower()
    # The seam got the base URL verbatim, exactly once.
    assert [call["base_url"] for call in calls] == [FHIR_URL]


def test_both_routes_exit_2_and_no_seam_is_touched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_dir = _write_manifest(tmp_path)
    pack_root = _pack_dir(tmp_path)
    browser_calls = {"n": 0}

    def _browser_spy(cdp: str, loaded: object) -> object:
        browser_calls["n"] += 1
        return FakeDestination(_known())

    monkeypatch.setattr(cli, "_make_destination", _browser_spy)
    fhir_calls = _fhir_spy(monkeypatch)

    result = runner.invoke(
        app,
        [
            "upload",
            str(out_dir),
            "--to",
            DEST,
            "--cdp",
            LOOPBACK,
            "--fhir",
            FHIR_URL,
            "--pack-dir",
            str(pack_root),
            "--yes",
        ],
    )

    assert result.exit_code == 2, result.output
    assert "one upload route" in result.output.lower()
    assert browser_calls["n"] == 0
    assert fhir_calls == []


def test_no_route_exit_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out_dir = _write_manifest(tmp_path)
    fhir_calls = _fhir_spy(monkeypatch)

    result = runner.invoke(app, ["upload", str(out_dir)])

    assert result.exit_code == 2, result.output
    assert "choose an upload route" in result.output.lower()
    assert fhir_calls == []


@pytest.mark.parametrize("partial", [["--to", DEST], ["--cdp", LOOPBACK]])
def test_half_specified_browser_route_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, partial: list[str]
) -> None:
    """The browser route needs BOTH --to and --cdp; half of it is a usage error,
    never a run that guesses the missing half."""
    out_dir = _write_manifest(tmp_path)
    monkeypatch.setattr(cli, "_make_destination", lambda cdp, loaded: FakeDestination(_known()))

    result = runner.invoke(app, ["upload", str(out_dir), *partial])

    assert result.exit_code == 2, result.output
    assert "choose an upload route" in result.output.lower()


def test_fhir_token_read_from_env_never_from_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bearer token reaches the seam through the ENVIRONMENT (argv is
    ps-visible), and never surfaces in the command's own output."""
    monkeypatch.setenv(DEFAULT_TOKEN_ENV, FHIR_TOKEN)
    out_dir = _write_manifest(tmp_path)
    calls = _fhir_spy(monkeypatch)

    result = _invoke_fhir(out_dir, "--no-verify")

    assert result.exit_code == 0, result.output
    assert calls[0]["bearer_token"] == FHIR_TOKEN
    assert FHIR_TOKEN not in result.output


def test_fhir_token_env_var_is_selectable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--fhir-token-env picks WHICH variable holds the token; the default one is
    then ignored (an operator with several destinations keeps them apart)."""
    monkeypatch.setenv(DEFAULT_TOKEN_ENV, "feedface-wrong-token")
    monkeypatch.setenv(ALT_TOKEN_ENV, FHIR_TOKEN)
    out_dir = _write_manifest(tmp_path)
    calls = _fhir_spy(monkeypatch)

    result = _invoke_fhir(out_dir, "--fhir-token-env", ALT_TOKEN_ENV, "--no-verify")

    assert result.exit_code == 0, result.output
    assert calls[0]["bearer_token"] == FHIR_TOKEN


@pytest.mark.parametrize("value", [None, "", "  \n"])
def test_fhir_absent_or_blank_env_is_unauthenticated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    """An unset (or blank) variable means unauthenticated — fine for a local
    HAPI. Blank is normalized to None so no empty Authorization header is sent."""
    if value is None:
        monkeypatch.delenv(DEFAULT_TOKEN_ENV, raising=False)
    else:
        monkeypatch.setenv(DEFAULT_TOKEN_ENV, value)
    out_dir = _write_manifest(tmp_path)
    calls = _fhir_spy(monkeypatch)

    result = _invoke_fhir(out_dir, "--no-verify")

    assert result.exit_code == 0, result.output
    assert calls[0]["bearer_token"] is None


@pytest.mark.parametrize(
    ("flag", "expected"),
    [(None, True), ("--create-patients", True), ("--no-create-patients", False)],
)
def test_create_patients_defaults_on_and_is_overridable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str | None, expected: bool
) -> None:
    """ON by default: a migration target may not hold the patients yet."""
    monkeypatch.delenv(DEFAULT_TOKEN_ENV, raising=False)
    out_dir = _write_manifest(tmp_path)
    calls = _fhir_spy(monkeypatch)

    extra = ["--no-verify"] if flag is None else [flag, "--no-verify"]
    result = _invoke_fhir(out_dir, *extra)

    assert result.exit_code == 0, result.output
    assert calls[0]["create_missing_patients"] is expected


def test_fhir_cleartext_off_loopback_exit_2_seam_never_called(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FhirEndpoint's https-or-loopback rule runs as a PRE-FLIGHT gate: its
    ValueError becomes a clean exit 2 before the seam is ever reached."""
    out_dir = _write_manifest(tmp_path)
    calls = _fhir_spy(monkeypatch)

    result = runner.invoke(app, ["upload", str(out_dir), "--fhir", "http://10.0.0.5/fhir"])

    assert result.exit_code == 2, result.output
    assert "loopback" in result.output.lower()
    assert calls == []


def test_fhir_route_threads_skiplist_and_verify_into_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shared levers are route-agnostic: --verify (ON by default),
    --skiplist and --max-attempts reach the SAME UploadCommand on the API
    route as on the browser one."""
    monkeypatch.delenv(DEFAULT_TOKEN_ENV, raising=False)
    out_dir = _write_manifest(tmp_path)
    skip = tmp_path / "skip.txt"
    skip.write_text("enc-1\n", encoding="utf-8")
    _fhir_spy(monkeypatch)
    captured = _capture_cmd(monkeypatch)

    result = _invoke_fhir(out_dir, "--skiplist", str(skip), "--max-attempts", "5")

    assert result.exit_code == 0, result.output
    cmd = captured["cmd"]
    assert cmd.verify is True  # type: ignore[union-attr]  — the SAFE default
    assert cmd.skiplist == frozenset({"enc-1"})  # type: ignore[union-attr]
    assert cmd.max_attempts == 5  # type: ignore[union-attr]
