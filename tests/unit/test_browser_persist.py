"""Upload-manifest persistence tests: round-trip, determinism, 0700, PHI probe.

Covers both schema versions the reader accepts: the v2 file a render run writes
today (pack + expected page count + date of service, the fields the L0-L6 ladder
verifies against) and the v1 file older trees still hold, which must load with
degraded verification and say so out loud rather than be refused.

Synthetic data only — ``feedface-`` ids, neutral file names in ``tmp_path``.
A name-shaped basename is deliberately used to prove the writer logs only an
item COUNT and never the path/name.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import stat
from pathlib import Path

import pytest

from anastomosis.core.model import Encounter, Patient, PatientRecord
from anastomosis.deliver.browser.manifest import build_manifest
from anastomosis.deliver.browser.persist import (
    LADDER_VERSION,
    MANIFEST_NAME,
    MANIFEST_VERSION,
    ManifestError,
    load_upload_manifest,
    read_upload_manifest,
    write_upload_manifest,
)
from anastomosis.reconstruct.engine import RenderedDoc

PAT_A = "feedface-0000-0000-0000-0000000000a1"
PAT_B = "feedface-0000-0000-0000-0000000000a2"
ENC_A = "feedface-e000-0000-0000-0000000000a1"
DOS = datetime.date(2023, 5, 10)

# A name-shaped basename: it must never reach a log line (PHI probe).
NAME_SHAPED = "Featherstonehaugh_Aloysius_03-14-1980.pdf"


def _patient(pid: str, *, family: str, given: str, dob: datetime.date | None) -> Patient:
    return Patient(id=pid, family_name=family, given_name=given, birth_date=dob)


def _record(patient: Patient) -> PatientRecord:
    return PatientRecord(id=patient.id, patient=patient)


def _fixture(tmp_path: Path) -> tuple[list[RenderedDoc], list[PatientRecord]]:
    """Two charts for two patients (one with a name-shaped filename + a DOB)."""
    doc_a = tmp_path / NAME_SHAPED
    doc_a.write_bytes(b"chart-a-bytes")
    doc_b = tmp_path / "note-b.pdf"
    doc_b.write_bytes(b"chart-b-bytes-longer")
    docs = [
        RenderedDoc(path=doc_a, encounter_id="enc-a", patient_id=PAT_A),
        RenderedDoc(path=doc_b, encounter_id="enc-b", patient_id=PAT_B),
    ]
    records = [
        _record(_patient(PAT_A, family="Family", given="Given", dob=datetime.date(1980, 3, 14))),
        _record(_patient(PAT_B, family="Other", given="Person", dob=None)),
    ]
    return docs, records


# --- round-trip -------------------------------------------------------------


def test_round_trip_items_match_build_manifest(tmp_path: Path) -> None:
    docs, records = _fixture(tmp_path)
    out_dir = tmp_path / "out"

    path = write_upload_manifest(docs, records, out_dir)
    assert path == out_dir / MANIFEST_NAME

    items, _patients = read_upload_manifest(out_dir)

    # The items match build_manifest(docs) on identity/integrity fields, sorted
    # by item_key (the writer's stable order).
    expected = sorted(build_manifest(docs), key=lambda it: it.item_key)
    assert [i.item_key for i in items] == [e.item_key for e in expected]
    for got, exp in zip(items, expected, strict=True):
        assert got.encounter_id == exp.encounter_id
        assert got.patient_id == exp.patient_id
        assert got.sha256 == exp.sha256
        assert got.size_bytes == exp.size_bytes
        assert got.fingerprint == exp.fingerprint
        # file_path is re-absolutized against out_dir from the stored basename.
        assert got.file_path == out_dir / exp.file_path.name


def test_round_trip_patients_include_birth_date(tmp_path: Path) -> None:
    docs, records = _fixture(tmp_path)
    out_dir = tmp_path / "out"
    write_upload_manifest(docs, records, out_dir)

    _items, patients = read_upload_manifest(out_dir)

    assert set(patients) == {PAT_A, PAT_B}
    assert patients[PAT_A].family_name == "Family"
    assert patients[PAT_A].given_name == "Given"
    # birth_date round-trips through model_dump(mode="json") / model_validate.
    assert patients[PAT_A].birth_date == datetime.date(1980, 3, 14)
    assert patients[PAT_B].birth_date is None


def test_only_referenced_patients_written(tmp_path: Path) -> None:
    """A record with no rendered chart contributes no patient entry."""
    docs, records = _fixture(tmp_path)
    unused = "feedface-0000-0000-0000-0000000000a9"
    extra = _record(_patient(unused, family="No", given="Docs", dob=None))
    out_dir = tmp_path / "out"
    write_upload_manifest(docs, [*records, extra], out_dir)

    data = json.loads((out_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert set(data["patients"]) == {PAT_A, PAT_B}


# --- determinism ------------------------------------------------------------


def test_two_writes_are_byte_identical(tmp_path: Path) -> None:
    docs, records = _fixture(tmp_path)
    out_dir = tmp_path / "out"

    first = write_upload_manifest(docs, records, out_dir).read_bytes()
    second = write_upload_manifest(docs, records, out_dir).read_bytes()
    assert first == second


def test_manifest_shape_and_version(tmp_path: Path) -> None:
    docs, records = _fixture(tmp_path)
    out_dir = tmp_path / "out"
    write_upload_manifest(docs, records, out_dir)
    data = json.loads((out_dir / MANIFEST_NAME).read_text(encoding="utf-8"))

    # The version describes the FILE: this caller recorded no gates, so what
    # landed carries no v3 field and says version 2 rather than claiming a
    # record it does not hold.
    assert data["version"] == LADDER_VERSION == 2
    # The run-level pack name: absent here (no pack was passed), never omitted.
    assert data["pack"] is None
    # The reviewed context, likewise present-and-null rather than missing: this
    # caller recorded no route and no gates, and the file says so.
    assert data["route"] is None
    assert data["gates"] is None
    # items sorted by item_key; each carries the documented keys.
    keys = data["items"][0].keys()
    assert set(keys) == {
        "item_key",
        "encounter_id",
        "patient_id",
        "file_path",
        "sha256",
        "size_bytes",
        "fingerprint",
        "expected_pages",
        "date_of_service",
    }
    # file_path stored as a basename (relative), never absolute.
    for entry in data["items"]:
        assert "/" not in entry["file_path"]
        assert not Path(entry["file_path"]).is_absolute()


# --- v2: what the L0-L6 ladder verifies against -----------------------------


def _pdf(path: Path, pages: int) -> Path:
    """A real ``pages``-page PDF, so the writer MEASURES a count it cannot guess."""
    pymupdf = pytest.importorskip("pymupdf", reason="page counts need PyMuPDF (render extra)")
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page(width=612, height=792)
    doc.save(str(path))
    doc.close()
    return path


def _pdf_fixture(
    tmp_path: Path, *, pages: int = 3, with_encounter: bool = True
) -> tuple[list[RenderedDoc], list[PatientRecord]]:
    """One real multi-page chart for one patient, with or without its encounter."""
    doc = _pdf(tmp_path / "note-a.pdf", pages)
    patient = _patient(PAT_A, family="Family", given="Given", dob=datetime.date(1980, 3, 14))
    encounters = (
        [Encounter(id=ENC_A, patient_id=PAT_A, date_of_service=DOS)] if with_encounter else []
    )
    return (
        [RenderedDoc(path=doc, encounter_id=ENC_A, patient_id=PAT_A)],
        [PatientRecord(id=PAT_A, patient=patient, encounters=encounters)],
    )


def test_v2_round_trip_carries_pack_expected_pages_and_dos(tmp_path: Path) -> None:
    docs, records = _pdf_fixture(tmp_path, pages=3)
    out_dir = tmp_path / "out"
    write_upload_manifest(docs, records, out_dir, pack="generic_soap")

    manifest = load_upload_manifest(out_dir)

    assert manifest.version == LADDER_VERSION
    assert manifest.degraded is False
    assert manifest.pack == "generic_soap"
    [item] = manifest.items
    # Measured off the rendered PDF — 3, not "at least 1".
    assert manifest.expected_pages == {item.item_key: 3}
    # Exactly the encounter field L3 reads, keyed the way it looks it up.
    encounter = manifest.encounters[ENC_A]
    assert encounter.date_of_service == DOS
    # ...and nothing more: the manifest stores no clinical content.
    assert encounter.sections == []
    assert encounter.addenda == []
    assert encounter.chief_complaint is None


def test_the_item_carries_its_own_date_of_service(tmp_path: Path) -> None:
    """The date reaches the upload DRIVER, not only the verifier.

    A destination whose filing dialog asks for a document date has to be handed
    the right one; before this the date reached the encounter map L3 checks
    against and stopped there, so the driver had nothing to type. Read once,
    handed out twice — no new field is written, so the file stays v2.
    """
    docs, records = _pdf_fixture(tmp_path)
    out_dir = tmp_path / "out"
    write_upload_manifest(docs, records, out_dir, pack="generic_soap")

    manifest = load_upload_manifest(out_dir)

    [item] = manifest.items
    assert item.date_of_service == DOS
    assert manifest.encounters[ENC_A].date_of_service == DOS
    # Still a v2 file: the value was already in it, nothing new was written.
    written = json.loads((out_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert written["version"] == LADDER_VERSION


def test_a_v1_item_has_no_date_of_service_to_carry(tmp_path: Path) -> None:
    """A v1 tree never recorded one, so the item says None and a pack that
    needs a document date refuses it — never a date invented on the way past."""
    docs, records = _pdf_fixture(tmp_path)
    out_dir = tmp_path / "out"
    write_upload_manifest(docs, records, out_dir, pack="generic_soap")
    _downgrade_to_v1(out_dir)

    [item] = load_upload_manifest(out_dir).items

    assert item.date_of_service is None


def test_v2_item_with_no_encounter_records_a_null_dos(tmp_path: Path) -> None:
    """The whole-patient ccda-standard view has no encounter — null, never a guess.

    A null DOS fails L3's ``dos`` field loudly on upload; it never lets the level
    pass on an assumed date.
    """
    docs, records = _pdf_fixture(tmp_path, with_encounter=False)
    out_dir = tmp_path / "out"
    write_upload_manifest(docs, records, out_dir, pack=None)

    data = json.loads((out_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert data["items"][0]["date_of_service"] is None
    assert load_upload_manifest(out_dir).encounters[ENC_A].date_of_service is None


def test_unmeasurable_page_count_is_null_and_logged_as_a_count(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A render that will not parse costs its page count, loudly — not the manifest.

    The miss is reported as a count plus the exception TYPE; the item is written
    with a null count, so upload L1 falls back to its page floor for it.
    """
    docs, records = _fixture(tmp_path)  # plain bytes, not PDFs
    out_dir = tmp_path / "out"
    with caplog.at_level(logging.WARNING, logger="anastomosis.deliver.browser.persist"):
        write_upload_manifest(docs, records, out_dir, pack="generic_soap")

    assert load_upload_manifest(out_dir).expected_pages == {}
    blob = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "2 of 2 item(s)" in blob
    # PHI probe: counts and an exception type name, never a path or a name.
    assert NAME_SHAPED not in blob
    assert "Featherstonehaugh" not in blob
    assert ".pdf" not in blob


def test_v2_item_missing_a_ladder_key_raises(tmp_path: Path) -> None:
    """A v2 file whose items lack the v2 fields does not match the version it
    declares — a defect, so it raises rather than reading as "unknown"."""
    docs, records = _fixture(tmp_path)
    out_dir = tmp_path / "out"
    write_upload_manifest(docs, records, out_dir, pack="generic_soap")
    path = out_dir / MANIFEST_NAME
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["items"][0]["expected_pages"]
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ManifestError, match="malformed"):
        load_upload_manifest(out_dir)


def test_v2_missing_pack_key_raises(tmp_path: Path) -> None:
    docs, records = _fixture(tmp_path)
    out_dir = tmp_path / "out"
    write_upload_manifest(docs, records, out_dir, pack="generic_soap")
    path = out_dir / MANIFEST_NAME
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["pack"]
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ManifestError, match="pack"):
        load_upload_manifest(out_dir)


# --- v1: still loads, verification degraded, said out loud ------------------


def _downgrade_to_v1(out_dir: Path) -> None:
    """Rewrite the manifest the way version 1 wrote it: no ladder fields at all.

    Downgraded from a real v2 file rather than hand-typed, so the fixture is the
    v1 shape this repo actually shipped instead of a guess at it.
    """
    path = out_dir / MANIFEST_NAME
    data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = 1
    del data["pack"]
    for entry in data["items"]:
        del entry["expected_pages"]
        del entry["date_of_service"]
    path.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def test_v1_manifest_loads_with_degraded_verification_and_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An already-rendered v1 tree must still upload — never refused — but the
    operator is told, in the run's own log, what is no longer checked."""
    docs, records = _fixture(tmp_path)
    out_dir = tmp_path / "out"
    write_upload_manifest(docs, records, out_dir, pack="generic_soap")
    _downgrade_to_v1(out_dir)

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="anastomosis.deliver.browser.persist"):
        manifest = load_upload_manifest(out_dir)

    # Loads in full: nothing about the items or demographics is lost.
    assert manifest.version == 1
    assert manifest.degraded is True
    assert len(manifest.items) == 2
    assert set(manifest.patients) == {PAT_A, PAT_B}
    # ...and carries nothing for L1's exact page check or L3 to check against.
    assert manifest.pack is None
    assert manifest.expected_pages == {}
    assert manifest.encounters == {}

    [line] = [rec.getMessage() for rec in caplog.records if "DEGRADED" in rec.getMessage()]
    # Versions, a count, and the level names — no patient value, no path.
    assert "version 1" in line
    assert "2 item(s)" in line
    assert "L3" in line
    assert "L1" in line
    assert NAME_SHAPED not in line
    assert "Featherstonehaugh" not in line


def test_v1_manifest_reads_back_the_same_items_a_v1_reader_saw(tmp_path: Path) -> None:
    """The degraded path is a degraded LADDER, not degraded data: the items and
    demographics a v1 tree yields are byte-for-byte what it always yielded."""
    docs, records = _fixture(tmp_path)
    out_dir = tmp_path / "out"
    write_upload_manifest(docs, records, out_dir, pack="generic_soap")
    v2_items, v2_patients = read_upload_manifest(out_dir)
    _downgrade_to_v1(out_dir)

    v1_items, v1_patients = read_upload_manifest(out_dir)

    assert v1_items == v2_items
    assert v1_patients == v2_patients


# --- 0700 placement ---------------------------------------------------------


def test_written_under_hardened_dir(tmp_path: Path) -> None:
    docs, records = _fixture(tmp_path)
    out_dir = tmp_path / "out"
    path = write_upload_manifest(docs, records, out_dir)

    assert path.parent == out_dir
    if os.name == "posix":
        assert stat.S_IMODE(out_dir.stat().st_mode) == 0o700


# --- PHI probe --------------------------------------------------------------


def test_writer_logs_count_only_never_the_name(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    docs, records = _fixture(tmp_path)
    out_dir = tmp_path / "out"
    with caplog.at_level(logging.INFO, logger="anastomosis.deliver.browser.persist"):
        write_upload_manifest(docs, records, out_dir)

    blob = "\n".join(rec.getMessage() for rec in caplog.records)
    # The name-shaped basename (and its parts) must never appear in a log line.
    assert NAME_SHAPED not in blob
    assert "Featherstonehaugh" not in blob
    assert ".pdf" not in blob
    # The count is logged.
    assert "2 item(s)" in blob


# --- loud-on-malformed ------------------------------------------------------


def test_read_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ManifestError):
        read_upload_manifest(tmp_path / "no-such-dir")


def test_read_version_mismatch_raises(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / MANIFEST_NAME).write_text(
        json.dumps({"version": 999, "items": [], "patients": {}}), encoding="utf-8"
    )
    with pytest.raises(ManifestError, match="version"):
        read_upload_manifest(out_dir)


def test_read_missing_key_raises(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    # No "patients" key.
    (out_dir / MANIFEST_NAME).write_text(
        json.dumps({"version": MANIFEST_VERSION, "items": []}), encoding="utf-8"
    )
    with pytest.raises(ManifestError, match="patients"):
        read_upload_manifest(out_dir)


def test_read_not_json_raises(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / MANIFEST_NAME).write_text("{not json", encoding="utf-8")
    with pytest.raises(ManifestError):
        read_upload_manifest(out_dir)


def test_write_references_missing_record_raises(tmp_path: Path) -> None:
    """An item whose patient_id has no record is a defect — raise, never half-write."""
    docs, _records = _fixture(tmp_path)
    out_dir = tmp_path / "out"
    with pytest.raises(ManifestError, match="no matching record"):
        write_upload_manifest(docs, [], out_dir)
