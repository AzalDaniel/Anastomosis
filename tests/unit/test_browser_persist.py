# AI-assisted: written with Claude agents under the author's direction and review; see DESIGN.md.
"""Upload-manifest persistence tests: round-trip, determinism, 0700, PHI probe.

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

from anastomosis.core.model import Patient, PatientRecord
from anastomosis.deliver.browser.manifest import build_manifest
from anastomosis.deliver.browser.persist import (
    MANIFEST_NAME,
    MANIFEST_VERSION,
    ManifestError,
    read_upload_manifest,
    write_upload_manifest,
)
from anastomosis.reconstruct.engine import RenderedDoc

PAT_A = "feedface-0000-0000-0000-0000000000a1"
PAT_B = "feedface-0000-0000-0000-0000000000a2"

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

    assert data["version"] == MANIFEST_VERSION
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
    }
    # file_path stored as a basename (relative), never absolute.
    for entry in data["items"]:
        assert "/" not in entry["file_path"]
        assert not Path(entry["file_path"]).is_absolute()


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
