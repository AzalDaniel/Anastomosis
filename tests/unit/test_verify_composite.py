"""Tests for LayeredVerifier — directly and through a real UploadEngine run.

The composite stacks L0-L6 behind the engine's Verifier seam. The engine-level
tests drive a real :class:`UploadEngine` against :class:`FakeDestination`
(readable=True) so the whole pre/post path is exercised end to end, with no
browser and no I/O beyond tiny PyMuPDF-generated PDFs in tmp_path.

Synthetic data only — ``feedface-`` ids, "Synthia Testpatient", DOB 1990-01-02.
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz", reason="verify tests need PyMuPDF (render extra)")

from anastomosis.core.model import Encounter, Patient  # noqa: E402
from anastomosis.deliver.browser.engine import UploadEngine  # noqa: E402
from anastomosis.deliver.browser.errors import PermanentDeliveryError  # noqa: E402
from anastomosis.deliver.browser.fake import FakeDestination  # noqa: E402
from anastomosis.deliver.browser.manifest import build_manifest  # noqa: E402
from anastomosis.deliver.browser.states import UploadState  # noqa: E402
from anastomosis.deliver.browser.tracking import TrackingDB  # noqa: E402
from anastomosis.deliver.verify import LayeredVerifier, LevelStatus  # noqa: E402
from anastomosis.destinations.base import UploadItem, UploadReceipt  # noqa: E402
from anastomosis.reconstruct.engine import RenderedDoc  # noqa: E402

PAT = "feedface-0000-0000-0000-0000000000aa"
ENC = "feedface-e000-0000-0000-0000000000aa"
DEST = "dest-aa"
DOB = date(1990, 1, 2)
DOS = date(2023, 5, 10)
NAME = "Synthia Testpatient"  # = display_name of the patient below

_FILLER = [f"Clinical note body line {i} for archival padding." for i in range(20)]
GOOD_LINES = [NAME, "DOB 01/02/1990", "Date of service: May 10, 2023", *_FILLER]
BAD_DOB_LINES = [NAME, "DOB 12/31/1965", "Date of service: May 10, 2023", *_FILLER]


def _patient() -> Patient:
    return Patient(id=PAT, given_name="Synthia", family_name="Testpatient", birth_date=DOB)


def _encounter() -> Encounter:
    return Encounter(id=ENC, patient_id=PAT, date_of_service=DOS)


def _make_pdf(path: Path, lines: list[str]) -> Path:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_textbox(fitz.Rect(36, 36, 576, 756), "\n".join(lines))
    doc.save(str(path))
    doc.close()
    return path


def _item(path: Path) -> UploadItem:
    data = path.read_bytes()
    return UploadItem(
        item_key=f"{ENC}:{hashlib.sha256(data).hexdigest()[:12]}",
        encounter_id=ENC,
        patient_id=PAT,
        file_path=path,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
    )


# --- LayeredVerifier directly ---


def test_verify_pre_passes_and_collects_results(tmp_path: Path) -> None:
    item = _item(_make_pdf(tmp_path / "g.pdf", GOOD_LINES))
    v = LayeredVerifier()  # standalone: no destination, no pack
    v.verify_pre(item, _patient())  # does not raise
    table = v.results_for(item.item_key)
    levels = [r.level for r in table]
    assert levels == ["L0", "L1", "L2", "L3", "L4"]
    # L3 (no pack) and L4 (no banner) skip; L0-L2 pass.
    by_level = {r.level: r.status for r in table}
    assert by_level["L0"] is LevelStatus.PASS
    assert by_level["L2"] is LevelStatus.PASS
    assert by_level["L3"] is LevelStatus.SKIP
    assert by_level["L4"] is LevelStatus.SKIP


def test_verify_pre_bad_dob_raises_permanent_and_records_all(tmp_path: Path) -> None:
    item = _item(_make_pdf(tmp_path / "b.pdf", BAD_DOB_LINES))
    v = LayeredVerifier()
    with pytest.raises(PermanentDeliveryError) as exc:
        v.verify_pre(item, _patient())
    # PHI-safe message: level + field name, no patient value/date.
    msg = str(exc.value)
    assert "L2" in msg and "birth_date" in msg
    assert "Synthia" not in msg and "Testpatient" not in msg and "1965" not in msg
    # All five pre-levels still recorded (the table is complete after a fail).
    assert [r.level for r in v.results_for(item.item_key)] == ["L0", "L1", "L2", "L3", "L4"]


def test_verify_pre_wrong_banner_raises_wrong_patient(tmp_path: Path) -> None:
    from anastomosis.deliver.browser.errors import WrongPatientError

    item = _item(_make_pdf(tmp_path / "g.pdf", GOOD_LINES))
    dest = FakeDestination({PAT: DEST}, wrong_patient_ids={PAT})
    v = LayeredVerifier(destination=dest)
    with pytest.raises(WrongPatientError):
        v.verify_pre(item, _patient())
    # L0-L3 ran and were recorded before L4's banner raised; L4 produced no
    # LevelResult (it aborted via the exception, not a fail status).
    assert [r.level for r in v.results_for(item.item_key)] == ["L0", "L1", "L2", "L3"]


def test_verify_post_skips_without_readers(tmp_path: Path) -> None:
    item = _item(_make_pdf(tmp_path / "g.pdf", GOOD_LINES))
    dest = FakeDestination({PAT: DEST})  # not readable
    v = LayeredVerifier(destination=dest)
    v.verify_pre(item, _patient())
    v.verify_post(item, UploadReceipt(destination_doc_id="doc-x"))
    post = [r for r in v.results_for(item.item_key) if r.level in {"L5", "L6"}]
    assert all(r.status is LevelStatus.SKIP for r in post)


def test_levels_filter_runs_subset(tmp_path: Path) -> None:
    item = _item(_make_pdf(tmp_path / "g.pdf", GOOD_LINES))
    v = LayeredVerifier(levels=frozenset({"L0", "L2"}))
    v.verify_pre(item, _patient())
    assert [r.level for r in v.results_for(item.item_key)] == ["L0", "L2"]


def test_last_results_tracks_full_table_across_pre_and_post(tmp_path: Path) -> None:
    # Through a real engine run so the upload populates the readable fake's
    # store before verify_post reads it back.
    item = _item(_make_pdf(tmp_path / "g.pdf", GOOD_LINES))
    dest = FakeDestination({PAT: DEST}, readable=True, page_counts={f"doc-{item.item_key}": 1})
    v = LayeredVerifier(records={ENC: _encounter()}, destination=dest)
    _run(dest, item, v, tmp_path)
    # last_results and results_for agree, and post-levels were appended to pre.
    assert [r.level for r in v.last_results] == ["L0", "L1", "L2", "L3", "L4", "L5", "L6"]
    assert v.last_results == v.results_for(item.item_key)


# --- through a real UploadEngine run ---


def _run(
    dest: FakeDestination, item: UploadItem, verifier: LayeredVerifier, tmp_path: Path
) -> tuple[dict[str, int], TrackingDB]:
    docs = [RenderedDoc(path=item.file_path, encounter_id=ENC, patient_id=PAT)]
    items = build_manifest(docs)
    tracking = TrackingDB(tmp_path / "ledger.sqlite")
    run_id = tracking.begin_run(dest.name)
    engine = UploadEngine(dest, tracking, verifier=verifier)
    result = engine.run(items, {PAT: _patient()}, run_id)
    return dict(result.counts), tracking


def test_engine_good_item_completes_all_levels_pass(tmp_path: Path) -> None:
    item = _item(_make_pdf(tmp_path / "note.pdf", GOOD_LINES))
    dest = FakeDestination({PAT: DEST}, readable=True, page_counts={f"doc-{item.item_key}": 1})
    verifier = LayeredVerifier(records={ENC: _encounter()}, destination=dest)
    counts, _tracking = _run(dest, item, verifier, tmp_path)

    assert counts == {UploadState.COMPLETED.value: 1}
    table = {r.level: r.status for r in verifier.results_for(item.item_key)}
    # Every level ran; L0-L2 pass, L3 skips (no pack), L4 banner passes,
    # L5/L6 pass via the readable destination.
    assert table["L4"] is LevelStatus.PASS
    assert table["L5"] is LevelStatus.PASS
    assert table["L6"] is LevelStatus.PASS


def test_engine_bad_dob_item_routes_to_pre_verify_failed(tmp_path: Path) -> None:
    item = _item(_make_pdf(tmp_path / "bad.pdf", BAD_DOB_LINES))
    dest = FakeDestination({PAT: DEST}, readable=True)
    verifier = LayeredVerifier(destination=dest)
    counts, _tracking = _run(dest, item, verifier, tmp_path)

    assert counts == {UploadState.PRE_VERIFY_FAILED.value: 1}
    assert dest.uploads == []  # never uploaded — caught before any bytes sent


def test_engine_reprocessed_readback_completes_via_identity(tmp_path: Path) -> None:
    """A destination that re-processes uploads (different bytes, same chart)
    must COMPLETE through L6's identity re-assertion. Dies if the composite
    stops caching the canonical patient for verify_post (the fail-safe would
    silently turn every legitimate reprocessing destination into
    POST_VERIFY_FAILED)."""
    item = _item(_make_pdf(tmp_path / "note.pdf", GOOD_LINES))
    reprocessed = _make_pdf(tmp_path / "reprocessed.pdf", GOOD_LINES).read_bytes()
    assert hashlib.sha256(reprocessed).hexdigest() != item.sha256

    class _ReprocessingDest(FakeDestination):
        """Read-back returns re-saved (byte-different) but equivalent bytes;
        metadata reports nothing, so L5 has nothing to contradict."""

        def _read_back(self, patient: object, destination_doc_id: str) -> bytes:
            return reprocessed

        def _read_metadata(self, patient: object, destination_doc_id: str) -> dict[str, int]:
            return {}

    dest = _ReprocessingDest({PAT: DEST}, readable=True)
    verifier = LayeredVerifier(records={ENC: _encounter()}, destination=dest)
    counts, _tracking = _run(dest, item, verifier, tmp_path)

    assert counts == {UploadState.COMPLETED.value: 1}
    table = {r.level: r for r in verifier.results_for(item.item_key)}
    assert table["L6"].status is LevelStatus.PASS
    assert table["L6"].detail == "reprocessed"


def test_engine_corrupt_readback_routes_to_post_verify_failed(tmp_path: Path) -> None:
    item = _item(_make_pdf(tmp_path / "note.pdf", GOOD_LINES))
    dest = FakeDestination(
        {PAT: DEST},
        readable=True,
        page_counts={f"doc-{item.item_key}": 1},
        corrupt_readback={item.item_key},
    )
    verifier = LayeredVerifier(records={ENC: _encounter()}, destination=dest)
    counts, _tracking = _run(dest, item, verifier, tmp_path)

    assert counts == {UploadState.POST_VERIFY_FAILED.value: 1}
    # The upload itself landed; L6's round-trip read-back caught the corruption.
    assert dest.uploads == [(item.item_key, DEST)]
    table = {r.level: r.status for r in verifier.results_for(item.item_key)}
    assert table["L6"] is LevelStatus.FAIL


# --- PHI discipline ---


def test_no_phi_in_level_details_or_messages(tmp_path: Path) -> None:
    """LevelResult.detail values and raised messages carry no patient value."""
    bad = _item(_make_pdf(tmp_path / "bad.pdf", BAD_DOB_LINES))
    dest = FakeDestination({PAT: DEST}, readable=True)
    verifier = LayeredVerifier(records={ENC: _encounter()}, destination=dest)
    with pytest.raises(PermanentDeliveryError) as exc:
        verifier.verify_pre(bad, _patient())

    probe = [str(exc.value)] + [r.detail for r in verifier.results_for(bad.item_key)]
    blob = "\n".join(probe)
    # The synthetic name, its parts, and the DOB digits never appear.
    for forbidden in ("Synthia", "Testpatient", "1990", "1965", "01/02", "12/31"):
        assert forbidden not in blob, f"PHI leak: {forbidden!r} in {blob!r}"
    # Paths never leak either.
    assert str(bad.file_path) not in blob
    assert "bad.pdf" not in blob
