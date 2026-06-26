"""The shared browser-upload orchestration core (one engine drive, two frontends).

These pin codex P1-3/P1-4: a SINGLE retry-budget default for both frontends, the
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
            run_upload_command(UploadCommand(out_dir=out, verify=False), _attach)
    # NEITHER the manifest read NOR the attach was reached — both live INSIDE the
    # lock, so a locked dir refuses before touching the manifest or the browser.
    assert calls == {"read": 0, "attach": 0}
    # No ledger run was begun either (nothing drove past the lock).
    assert not (out / LEDGER_NAME).exists() or sum(_counts(out).values()) == 0


def test_run_upload_command_locks_the_charts_manifest_dir_too(tmp_path: Path) -> None:
    """Migrate-layout fencing (codex P1-3): when the manifest lives under
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
    once — the owned Playwright driver/CDP teardown (codex #5). For a destination
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
