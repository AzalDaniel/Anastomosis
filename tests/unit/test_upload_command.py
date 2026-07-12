# AI-assisted: written with Claude agents under the author's direction and review; see DESIGN.md.
"""The shared browser-upload orchestration core (one engine drive, two frontends).

These pin the shared-core guarantees: a SINGLE retry-budget default for both frontends, the
manifest read INSIDE the output lock (lock-then-read — no TOCTOU), and skiplist
support routed through the one ``UploadCommand``. The Playwright touch is the
``attach`` callable, here a :class:`FakeDestination`, so the whole drive runs with
no browser. Synthetic data only (``feedface-`` ids, neutral file names).
"""

from __future__ import annotations

import datetime
import sys
import threading
from pathlib import Path

import pytest

from anastomosis.core.locking import OutputLockedError, output_lock
from anastomosis.core.model import Patient, PatientRecord
from anastomosis.core.upload_command import (
    DEFAULT_MAX_ATTEMPTS,
    LEDGER_NAME,
    UploadCommand,
    UploadCommandResult,
    VerificationUnavailableError,
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
    result = run_upload_command(
        UploadCommand(out_dir=out, verify=False), lambda: FakeDestination(_known())
    )
    assert result.aborted_reason is None
    assert result.counts.get(UploadState.COMPLETED.value) == 3
    assert sum(result.counts.values()) == 3
    assert result.report_path.is_file()
    assert result.report_path.parent == out  # report lands inside the output dir


def test_run_upload_command_honors_skiplist(tmp_path: Path) -> None:
    out = _write_manifest(tmp_path / "out")
    dest = FakeDestination(_known())
    result = run_upload_command(
        UploadCommand(out_dir=out, skiplist=frozenset({"enc-1"}), verify=False),
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
    BEFORE the manifest is read AND before the browser is attached (the
    read-before-lock TOCTOU). Instrumenting BOTH sides proves the ordering — a regression that
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
            run_upload_command(UploadCommand(out_dir=out, verify=False), _attach)
    # NEITHER the manifest read NOR the attach was reached — both live INSIDE the
    # lock, so a locked dir refuses before touching the manifest or the browser.
    assert calls == {"read": 0, "attach": 0}
    # No ledger run was begun either (nothing drove past the lock).
    assert not (out / LEDGER_NAME).exists() or sum(_counts(out).values()) == 0


def test_run_upload_command_locks_the_charts_manifest_dir_too(tmp_path: Path) -> None:
    """Migrate-layout fencing: when the manifest lives under
    <out>/charts (a `migrate` output), the upload must lock <out>/charts — the
    producer's lock dir — not only <out>. Holding the REAL charts lock makes
    run_upload_command(out_dir=<out>) refuse, proving both dirs are locked."""
    out = tmp_path / "out"
    out.mkdir()
    _write_manifest(out / "charts")  # migrate layout: manifest under <out>/charts
    assert resolve_manifest_root(out) == out / "charts"
    # The producer (a concurrent `migrate`/`render`) holds the charts lock.
    with output_lock(out / "charts"), pytest.raises(OutputLockedError):
        run_upload_command(
            UploadCommand(out_dir=out, verify=False), lambda: FakeDestination(_known())
        )


def test_run_upload_command_resumes_after_a_crash(tmp_path: Path) -> None:
    """A process-kill (BaseException) sails out; a re-run resumes with no double-file."""
    out = _write_manifest(tmp_path / "out")
    shared: dict[str, set[str]] = {}

    # First run crashes after one successful upload (FakeCrash is a BaseException
    # — a KeyboardInterrupt subclass — so it sails out exactly like a process kill).
    with pytest.raises(FakeCrash):
        run_upload_command(
            UploadCommand(out_dir=out, verify=False),
            lambda: FakeDestination(_known(), existing=shared, crash_after=1),
        )

    # Second run resumes against the SAME ledger + destination store.
    result = run_upload_command(
        UploadCommand(out_dir=out, verify=False),
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
        UploadCommand(out_dir=out, verify=False), lambda: FakeDestination(_known()), stop=stop
    )
    assert result.aborted_reason is None
    # Everything stays PENDING (no item was driven past the boundary check).
    counts = _counts(out)
    assert counts.get(UploadState.COMPLETED.value, 0) == 0
    assert counts.get(UploadState.PENDING.value, 0) == 3


# --- opt-in L0-L6 verification (the verify lever) ---------------------------
#
# These prove UploadCommand.verify actually drives the LayeredVerifier through
# the shared upload path: a verifiable PDF + manifest + readable FakeDestination
# (the test_verify_composite.py / test_fhir_destination.py fixture pattern) so
# the ladder runs for real, with no browser. The render extra gates them
# per-test (NOT module-level — the fitz-free tests above must still run on a
# machine without the render extra).

# One verifiable patient — synthetic name + DOB the PDF must carry for L2.
_V_PID = "feedface-0000-0000-0000-0000000000aa"
_V_DEST = "dest-aa"
_V_DOB = datetime.date(1990, 1, 2)
_V_NAME = "Synthia Testpatient"  # = the patient's display_name below
_FILLER = [f"Clinical note body line {i} for archival padding." for i in range(20)]
_GOOD_LINES = [_V_NAME, "DOB 01/02/1990", "Date of service: May 10, 2023", *_FILLER]
# A wrong-identity page: a different name AND a different DOB, so L2 fails the
# DOB hard-gate (and the name ratio) — the wrong-chart catch.
_BAD_LINES = ["Wrongname Otherpatient", "DOB 12/31/1965", *_FILLER]


def _verifiable_patient() -> Patient:
    return Patient(id=_V_PID, given_name="Synthia", family_name="Testpatient", birth_date=_V_DOB)


def _write_verifiable_manifest(root: Path, lines: list[str]) -> Path:
    """Write a manifest of ONE real PDF carrying ``lines`` into ``root``.

    The PDF is a genuine (PyMuPDF-rendered) chart so L0/L1 pass and L2 reads its
    page-1 text; the patient demographics ride the manifest so the verifier sees
    the same canonical patient the engine resolves.
    """
    import fitz

    root.mkdir(parents=True, exist_ok=True)
    path = root / "note.pdf"
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_textbox(fitz.Rect(36, 36, 576, 756), "\n".join(lines))
    doc.save(str(path))
    doc.close()
    docs = [RenderedDoc(path=path, encounter_id="enc-aa", patient_id=_V_PID)]
    records = [PatientRecord(id=_V_PID, patient=_verifiable_patient())]
    write_upload_manifest(docs, records, root)
    return root


def _readable_dest() -> FakeDestination:
    """A readable FakeDestination so the post-upload L5/L6 read-back levels run.

    ``page_counts`` keys on the doc id the fake hands back (``doc-<item_key>``);
    every L0-L6 level passes for a good chart against this destination.
    """
    return FakeDestination({_V_PID: _V_DEST}, readable=True, page_counts={})


def test_run_upload_command_verify_true_drives_the_layered_verifier(tmp_path: Path) -> None:
    """verify=True wires the real LayeredVerifier: a good chart passes L0-L6 and
    COMPLETES through the shared upload path (no browser, no engine change)."""
    pytest.importorskip("fitz", reason="verify needs PyMuPDF (the render extra)")
    out = _write_verifiable_manifest(tmp_path / "out", _GOOD_LINES)
    result = run_upload_command(UploadCommand(out_dir=out, verify=True), lambda: _readable_dest())
    assert result.aborted_reason is None
    assert result.counts.get(UploadState.COMPLETED.value) == 1
    assert sum(result.counts.values()) == 1


def test_verify_is_the_safe_default_and_no_verify_is_an_explicit_opt_out(tmp_path: Path) -> None:
    """Verification is ON by default: the SAME wrong-identity chart is caught at
    L2 and routed to PRE_VERIFY_FAILED with the DEFAULT command (no verify arg),
    and only COMPLETES when the operator EXPLICITLY opts out with verify=False.
    Proves the default swaps in the LayeredVerifier (not the pass-through) and
    that --no-verify is a live escape hatch."""
    pytest.importorskip("fitz", reason="verify needs PyMuPDF (the render extra)")
    # (a) DEFAULT (verify on): the wrong-identity chart is caught at L2 and routed
    #     to PRE_VERIFY_FAILED before any bytes are sent — nothing is filed.
    out_on = _write_verifiable_manifest(tmp_path / "on", _BAD_LINES)
    dest_on = _readable_dest()
    on = run_upload_command(UploadCommand(out_dir=out_on), lambda: dest_on)
    assert on.aborted_reason is None
    assert on.counts.get(UploadState.PRE_VERIFY_FAILED.value) == 1
    assert on.counts.get(UploadState.COMPLETED.value, 0) == 0
    assert dest_on.uploads == []

    # (b) Explicit opt-out (--no-verify): the operator accepts an unverified
    #     upload, so the ladder is not consulted and the chart COMPLETES (only the
    #     engine's banner gate runs, which the FakeDestination passes here).
    out_off = _write_verifiable_manifest(tmp_path / "off", _BAD_LINES)
    off = run_upload_command(UploadCommand(out_dir=out_off, verify=False), lambda: _readable_dest())
    assert off.aborted_reason is None
    assert off.counts.get(UploadState.COMPLETED.value) == 1


def test_verify_defaults_on() -> None:
    """The safe default: a command built without a verify arg verifies."""
    assert UploadCommand(out_dir=Path("unused")).verify is True


def test_verify_on_without_render_extra_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """verify on + PyMuPDF unavailable => the run REFUSES (fail closed), never
    files unverified. Nothing touches the destination."""
    out = _write_verifiable_manifest(tmp_path / "out", _GOOD_LINES)  # built while fitz is real
    monkeypatch.setitem(sys.modules, "fitz", None)  # now `import fitz` raises ImportError
    dest = _readable_dest()
    with pytest.raises(VerificationUnavailableError):
        run_upload_command(UploadCommand(out_dir=out), lambda: dest)
    assert dest.uploads == []  # refused before any browser/upload work


def test_run_upload_command_releases_destination_resources(tmp_path: Path) -> None:
    """At run end the command calls the destination's one-shot release() exactly
    once — the owned Playwright driver/CDP teardown. For a destination
    with no owned resources (plain FakeDestination) it is simply skipped."""
    out = _write_manifest(tmp_path / "out")
    released = {"n": 0}

    class _ReleasableDestination(FakeDestination):
        def release(self) -> None:
            released["n"] += 1

    run_upload_command(
        UploadCommand(out_dir=out, verify=False), lambda: _ReleasableDestination(_known())
    )
    assert released["n"] == 1  # called once, at the end of the run

    # A destination without release() (the plain double) is skipped, not an error.
    out2 = _write_manifest(tmp_path / "out2")
    result = run_upload_command(
        UploadCommand(out_dir=out2, verify=False), lambda: FakeDestination(_known())
    )
    assert result.aborted_reason is None


# --- the shared verdict (is_clean / exit_code / nonclean_summary) -----------
#
# The SINGLE classifier both frontends read: the CLI's process exit code and the
# GUI worker's done-vs-error branch. Clean terminal states are completed /
# skipped_skiplist / duplicate_at_destination; any other terminal state, or any
# abort, is non-clean. A non-terminal leftover (pending) is not itself a failure.


@pytest.mark.parametrize(
    ("counts", "aborted", "clean", "code"),
    [
        pytest.param({"completed": 3}, None, True, 0, id="all-completed"),
        pytest.param(
            {"completed": 2, "skipped_skiplist": 1, "duplicate_at_destination": 1},
            None,
            True,
            0,
            id="clean-mix",
        ),
        pytest.param({}, None, True, 0, id="empty-counts"),
        pytest.param({"completed": 2, "failed": 1}, None, False, 1, id="failed"),
        pytest.param({"pre_verify_failed": 2}, None, False, 1, id="pre-verify-failed"),
        pytest.param(
            {"completed": 1, "post_verify_failed": 1}, None, False, 1, id="post-verify-failed"
        ),
        pytest.param({"patient_not_found": 1}, None, False, 1, id="patient-not-found"),
        pytest.param({"completed": 3}, "WrongPatientError", False, 1, id="abort-over-clean-counts"),
        # A cooperatively-stopped run leaves items PENDING (non-terminal) with no
        # abort: not a failure — it resumes next run — so it stays clean.
        pytest.param({"completed": 1, "pending": 2}, None, True, 0, id="pending-leftover-clean"),
    ],
)
def test_result_verdict_truth_table(
    counts: dict[str, int], aborted: str | None, clean: bool, code: int
) -> None:
    result = UploadCommandResult(
        counts=dict(counts), aborted_reason=aborted, report_path=Path("unused")
    )
    assert result.is_clean is clean
    assert result.exit_code == code


def test_result_nonclean_summary_names_states_and_counts_only() -> None:
    """The GUI error message: non-clean TERMINAL state names + counts, nothing
    else — no clean states, no non-terminal leftovers, no patient values."""
    result = UploadCommandResult(
        counts={"completed": 5, "pending": 3, "failed": 1, "pre_verify_failed": 2},
        aborted_reason=None,
        report_path=Path("unused"),
    )
    summary = result.nonclean_summary()
    # Alphabetical, PHI-safe: "<total> item(s) in non-clean terminal states: ...".
    assert summary == "3 item(s) in non-clean terminal states: failed=1, pre_verify_failed=2"
    # Clean + non-terminal states are NOT named (only the offenders are).
    assert "completed" not in summary
    assert "pending" not in summary


# --- resource ownership: no leak on a post-attach construction failure ------
#
# attach() hands us the destination's owned Playwright driver + CDP BEFORE the
# TrackingDB / LayeredVerifier are built. Each resource is registered with the
# ExitStack the instant it is owned, so a construction failure in a LATER
# resource still releases the earlier ones — no leaked driver/CDP.


def test_release_on_tracking_construction_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A TrackingDB constructor failure AFTER attach still releases the
    destination — the release is ExitStack-owned the instant attach returns."""
    import anastomosis.deliver.browser.tracking as tracking_mod

    out = _write_manifest(tmp_path / "out")
    released = {"n": 0}

    class _ReleasableDestination(FakeDestination):
        def release(self) -> None:
            released["n"] += 1

    class _BoomTrackingDB:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("ledger open failed")

    monkeypatch.setattr(tracking_mod, "TrackingDB", _BoomTrackingDB)

    with pytest.raises(RuntimeError):
        run_upload_command(
            UploadCommand(out_dir=out, verify=False),
            lambda: _ReleasableDestination(_known()),
        )
    assert released["n"] == 1  # released despite the ledger construction failure


def test_release_and_close_on_verifier_construction_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A LayeredVerifier constructor failure (after attach AND TrackingDB) closes
    the ledger AND releases the destination — both are ExitStack-owned by then,
    so neither leaks, and both fire exactly once."""
    pytest.importorskip("fitz", reason="verify needs PyMuPDF (the render extra)")
    import anastomosis.deliver.browser.tracking as tracking_mod
    import anastomosis.deliver.verify as verify_mod

    out = _write_manifest(tmp_path / "out")
    released = {"n": 0}
    closed = {"n": 0}

    class _ReleasableDestination(FakeDestination):
        def release(self) -> None:
            released["n"] += 1

    class _StubTrackingDB:
        # Construction succeeds; the verifier fails before the ledger is used, so
        # only close() need be observable here.
        def __init__(self, path: Path) -> None:
            self._path = path

        def close(self) -> None:
            closed["n"] += 1

    class _BoomVerifier:
        def __init__(self, **kwargs: object) -> None:
            raise RuntimeError("verifier construction failed")

    monkeypatch.setattr(tracking_mod, "TrackingDB", _StubTrackingDB)
    monkeypatch.setattr(verify_mod, "LayeredVerifier", _BoomVerifier)

    with pytest.raises(RuntimeError):
        run_upload_command(
            UploadCommand(out_dir=out),  # verify defaults ON -> builds the verifier
            lambda: _ReleasableDestination(_known()),
        )
    assert closed["n"] == 1  # the ledger handle was closed on the failure unwind
    assert released["n"] == 1  # and the destination released — neither leaked
