"""Tests for the L0-L6 verification levels (PLAN item 11).

Real tiny PDFs are generated WITH PyMuPDF itself (no Chromium): open an empty
doc, insert page-1 text carrying synthetic identity strings, save to tmp_path.
Synthetic data only — ``feedface-`` ids, "Testpatient Synthia", DOB 1990-01-02.
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz", reason="verify tests need PyMuPDF (render extra)")

from anastomosis.core.model import Encounter, Patient  # noqa: E402
from anastomosis.deliver.browser.errors import WrongPatientError  # noqa: E402
from anastomosis.deliver.verify.levels import (  # noqa: E402
    L0FileIntegrity,
    L1PageAndSize,
    L2IdentityText,
    L3HeaderFields,
    L4Banner,
    L5Metadata,
    L6RoundTrip,
    LevelStatus,
    date_renderings,
    fuzzy_contains,
)
from anastomosis.destinations.base import DestinationPatient, UploadItem  # noqa: E402
from anastomosis.reconstruct.packs import LoadedPack, PackManifest  # noqa: E402

# --- synthetic constants (never real) ---

PAT_ID = "feedface-0000-0000-0000-0000000000aa"
ENC_ID = "feedface-e000-0000-0000-0000000000aa"
DOB = date(1990, 1, 2)
DOS = date(2023, 5, 10)
DEST_PATIENT = DestinationPatient(destination_patient_id="dest-aa", matched_on=("id",))
DOC_ID = "doc-x"

# display_name is given + family => "Synthia Testpatient"; the page renders
# that exact string so the fuzzy matcher scores it 1.0. The filler lines push
# the real PyMuPDF output comfortably past L1's 1 KiB floor (a one-line PDF is
# sub-KiB), modelling a real chart body.
_FILLER = [f"Clinical note body line {i} for archival padding." for i in range(20)]
GOOD_PAGE_LINES = [
    "Synthia Testpatient",
    "DOB 01/02/1990",
    "Date of service: May 10, 2023",
    "Heart rate 72 bpm",
    *_FILLER,
]


def _patient(**overrides: object) -> Patient:
    fields: dict[str, object] = {
        "id": PAT_ID,
        "given_name": "Synthia",
        "family_name": "Testpatient",
        "birth_date": DOB,
    }
    fields.update(overrides)
    return Patient(**fields)  # type: ignore[arg-type]


def _make_pdf(path: Path, lines: list[str], *, pages: int = 1) -> Path:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_textbox(fitz.Rect(36, 36, 576, 756), "\n".join(lines))
    for _ in range(pages - 1):
        doc.new_page(width=612, height=792)
    doc.save(str(path))
    doc.close()
    return path


def _item(path: Path) -> UploadItem:
    data = path.read_bytes()
    return UploadItem(
        item_key=f"{ENC_ID}:{hashlib.sha256(data).hexdigest()[:12]}",
        encounter_id=ENC_ID,
        patient_id=PAT_ID,
        file_path=path,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
    )


def _good_item(tmp_path: Path, name: str = "note.pdf") -> UploadItem:
    return _item(_make_pdf(tmp_path / name, GOOD_PAGE_LINES))


def _pack(fields: list[str]) -> LoadedPack:
    manifest = PackManifest(name="test", version="1.0", verify_header_fields=fields)
    root = Path("/nonexistent")  # L3 never touches disk via the pack
    return LoadedPack(
        manifest=manifest, root=root, template_path=root / "t.html", build_context=lambda **_: {}
    )


# --- helper matchers ---


def test_date_renderings_built_without_glibc_codes() -> None:
    renders = date_renderings(DOB)
    assert "01/02/1990" in renders  # padded
    assert "1/2/1990" in renders  # unpadded, built by hand (no %-d)
    assert "January 2, 1990" in renders


def test_fuzzy_contains_exact_and_absent() -> None:
    assert fuzzy_contains("Synthia Testpatient", "header Synthia Testpatient DOB") == 1.0
    assert fuzzy_contains("Wrongname Different", "Synthia Testpatient only") < 0.88


# --- L0 file integrity ---


def test_l0_passes_on_intact_file(tmp_path: Path) -> None:
    result = L0FileIntegrity().run(_good_item(tmp_path))
    assert result.level == "L0"
    assert result.status is LevelStatus.PASS


def test_l0_fails_missing_file(tmp_path: Path) -> None:
    item = _good_item(tmp_path)
    item.file_path.unlink()
    result = L0FileIntegrity().run(item)
    assert result.status is LevelStatus.FAIL
    assert result.detail == "file missing"


def test_l0_fails_on_tampered_bytes(tmp_path: Path) -> None:
    item = _good_item(tmp_path)
    item.file_path.write_bytes(b"tampered-different-bytes-entirely")
    result = L0FileIntegrity().run(item)
    assert result.status is LevelStatus.FAIL
    # Size differs first here, so the size detail wins; either is a FAIL.
    assert "mismatch" in result.detail


def test_l0_works_without_pymupdf(tmp_path: Path) -> None:
    # L0 must not import fitz: it passes on a plain (non-PDF) intact file.
    p = tmp_path / "plain.bin"
    p.write_bytes(b"not a pdf but intact")
    item = _item(p)
    assert L0FileIntegrity().run(item).status is LevelStatus.PASS


# --- L1 page count + size ---


def test_l1_passes(tmp_path: Path) -> None:
    item = _good_item(tmp_path)
    result = L1PageAndSize().run(item)
    assert result.status is LevelStatus.PASS


def test_l1_fails_sub_kib(tmp_path: Path) -> None:
    # A real one-page PyMuPDF doc is > 1 KiB; force a tiny size_bytes to trip
    # the floor without depending on PyMuPDF's exact output size.
    item = _good_item(tmp_path)
    tiny = UploadItem(
        item_key=item.item_key,
        encounter_id=item.encounter_id,
        patient_id=item.patient_id,
        file_path=item.file_path,
        sha256=item.sha256,
        size_bytes=512,
    )
    result = L1PageAndSize().run(tiny)
    assert result.status is LevelStatus.FAIL
    assert "floor" in result.detail


def test_l1_expected_pages_match_and_mismatch(tmp_path: Path) -> None:
    item = _item(_make_pdf(tmp_path / "two.pdf", GOOD_PAGE_LINES, pages=2))
    assert L1PageAndSize().run(item, expected_pages=2).status is LevelStatus.PASS
    bad = L1PageAndSize().run(item, expected_pages=3)
    assert bad.status is LevelStatus.FAIL
    assert "expected 3" in bad.detail


# --- L2 identity text + DOB hard-fail ---


def test_l2_passes_with_name_and_dob(tmp_path: Path) -> None:
    result = L2IdentityText().run(_good_item(tmp_path), _patient())
    assert result.status is LevelStatus.PASS


def test_l2_fails_different_patient_name(tmp_path: Path) -> None:
    # A different patient's page: name ratio below threshold.
    lines = ["Someone Else Entirely", "DOB 03/04/1975"]
    item = _item(_make_pdf(tmp_path / "other.pdf", lines))
    # Our patient has no DOB so the DOB gate does not pre-empt the name check.
    result = L2IdentityText().run(item, _patient(birth_date=None))
    assert result.status is LevelStatus.FAIL
    assert "ratio" in result.detail


def test_l2_fails_similar_but_wrong_name_in_threshold_band(tmp_path: Path) -> None:
    """Pins the 0.88 threshold itself: a sound-alike wrong name (the classic
    wrong-patient hazard) lands in the (0.5, 0.88) band and must FAIL — a
    regression loosening the threshold to ~0.5 dies here."""
    lines = ["Cynthia Testpatient", "DOB 01/02/1990"]
    item = _item(_make_pdf(tmp_path / "soundalike.pdf", lines))
    patient = _patient(birth_date=None)  # isolate the name check from the DOB gate
    # Band guard mirrors the page content (the matcher's window extends past
    # the name, so trailing text is part of the measured condition).
    band_ratio = fuzzy_contains("Synthia Testpatient", "\n".join(lines))
    assert 0.5 < band_ratio < 0.88, "fixture drifted out of the threshold band"
    result = L2IdentityText().run(item, patient)
    assert result.status is LevelStatus.FAIL
    assert "ratio" in result.detail


def test_l2_dob_hard_fail_beats_passing_name(tmp_path: Path) -> None:
    # Right name, WRONG DOB on the page: must FAIL even though name ratio = 1.0.
    lines = ["Synthia Testpatient", "DOB 12/31/1965"]
    item = _item(_make_pdf(tmp_path / "wrongdob.pdf", lines))
    result = L2IdentityText().run(item, _patient())  # patient DOB is 1990-01-02
    assert result.status is LevelStatus.FAIL
    assert "birth_date" in result.detail


def test_l2_skips_when_patient_has_no_name(tmp_path: Path) -> None:
    result = L2IdentityText().run(
        _good_item(tmp_path), _patient(given_name=None, family_name=None, birth_date=None)
    )
    assert result.status is LevelStatus.SKIP


# --- L3 pack header fields ---


def test_l3_passes_for_supported_fields(tmp_path: Path) -> None:
    enc = Encounter(id=ENC_ID, patient_id=PAT_ID, date_of_service=DOS)
    result = L3HeaderFields().run(
        _good_item(tmp_path), _patient(), pack=_pack(["patient_name", "dob", "dos"]), encounter=enc
    )
    assert result.status is LevelStatus.PASS


def test_l3_empty_list_skips(tmp_path: Path) -> None:
    result = L3HeaderFields().run(_good_item(tmp_path), _patient(), pack=_pack([]), encounter=None)
    assert result.status is LevelStatus.SKIP
    assert result.detail == "no header fields declared"


def test_l3_unsupported_field_fails_loud(tmp_path: Path) -> None:
    result = L3HeaderFields().run(
        _good_item(tmp_path), _patient(), pack=_pack(["mrn"]), encounter=None
    )
    assert result.status is LevelStatus.FAIL
    assert "mrn" in result.detail


def test_l3_missing_dos_field_fails(tmp_path: Path) -> None:
    # dos declared but the page carries no DOS the encounter could match.
    lines = ["Synthia Testpatient", "DOB 01/02/1990"]
    item = _item(_make_pdf(tmp_path / "nodos.pdf", lines))
    enc = Encounter(id=ENC_ID, patient_id=PAT_ID, date_of_service=DOS)
    result = L3HeaderFields().run(item, _patient(), pack=_pack(["dos"]), encounter=enc)
    assert result.status is LevelStatus.FAIL
    assert "dos" in result.detail


# --- L4 banner ---


class _Banner:
    def __init__(self, *, matches: bool) -> None:
        self._matches = matches

    def current_patient_matches(self, expected: Patient) -> bool:
        return self._matches


def test_l4_skips_without_banner() -> None:
    result = L4Banner().run(_patient(), banner=None)
    assert result.status is LevelStatus.SKIP


def test_l4_passes_on_match() -> None:
    result = L4Banner().run(_patient(), banner=_Banner(matches=True))
    assert result.status is LevelStatus.PASS


def test_l4_raises_wrong_patient_on_mismatch() -> None:
    with pytest.raises(WrongPatientError):
        L4Banner().run(_patient(), banner=_Banner(matches=False))


# --- L5 metadata / L6 read-back doubles ---


class _Reader:
    """Minimal MetadataReader + DocumentReader double for the post levels."""

    def __init__(self, data: bytes, *, page_count: int | None, size: int | None) -> None:
        self._data = data
        self._page_count = page_count
        self._size = size

    def read_metadata(
        self, patient: DestinationPatient, destination_doc_id: str
    ) -> dict[str, str | int]:
        meta: dict[str, str | int] = {}
        if self._size is not None:
            meta["size_bytes"] = self._size
        if self._page_count is not None:
            meta["page_count"] = self._page_count
        return meta

    def read_back(self, patient: DestinationPatient, destination_doc_id: str) -> bytes:
        return self._data


# --- L5 metadata ---


def test_l5_skips_without_reader(tmp_path: Path) -> None:
    result = L5Metadata().run(_good_item(tmp_path), DEST_PATIENT, DOC_ID, reader=None)
    assert result.status is LevelStatus.SKIP


def test_l5_passes_on_matching_metadata(tmp_path: Path) -> None:
    item = _good_item(tmp_path)
    reader = _Reader(item.file_path.read_bytes(), page_count=1, size=item.size_bytes)
    result = L5Metadata().run(item, DEST_PATIENT, DOC_ID, reader=reader)
    assert result.status is LevelStatus.PASS


def test_l5_fails_on_size_mismatch(tmp_path: Path) -> None:
    item = _good_item(tmp_path)
    reader = _Reader(item.file_path.read_bytes(), page_count=1, size=item.size_bytes + 99)
    result = L5Metadata().run(item, DEST_PATIENT, DOC_ID, reader=reader)
    assert result.status is LevelStatus.FAIL


def test_l5_fails_on_page_count_mismatch(tmp_path: Path) -> None:
    item = _good_item(tmp_path)
    reader = _Reader(item.file_path.read_bytes(), page_count=9, size=item.size_bytes)
    result = L5Metadata().run(item, DEST_PATIENT, DOC_ID, reader=reader)
    assert result.status is LevelStatus.FAIL


# --- L6 round-trip ---


def test_l6_skips_without_reader(tmp_path: Path) -> None:
    result = L6RoundTrip().run(_good_item(tmp_path), DEST_PATIENT, DOC_ID, reader=None)
    assert result.status is LevelStatus.SKIP


def test_l6_passes_byte_identical(tmp_path: Path) -> None:
    item = _good_item(tmp_path)
    reader = _Reader(item.file_path.read_bytes(), page_count=1, size=item.size_bytes)
    result = L6RoundTrip().run(item, DEST_PATIENT, DOC_ID, reader=reader)
    assert result.status is LevelStatus.PASS
    assert result.detail == "byte-identical read-back"


def test_l6_passes_reprocessed_tier(tmp_path: Path) -> None:
    # An equivalent PDF re-saved with the same page-1 text but different bytes:
    # not byte-identical, but same page count + the patient's identity still
    # provable on the read-back => pass.
    item = _good_item(tmp_path, "orig.pdf")
    reprocessed = _make_pdf(tmp_path / "reprocessed.pdf", GOOD_PAGE_LINES)
    reprocessed_bytes = reprocessed.read_bytes()
    assert hashlib.sha256(reprocessed_bytes).hexdigest() != item.sha256
    reader = _Reader(reprocessed_bytes, page_count=1, size=len(reprocessed_bytes))
    result = L6RoundTrip().run(item, DEST_PATIENT, DOC_ID, reader=reader, patient=_patient())
    assert result.status is LevelStatus.PASS
    assert result.detail == "reprocessed"


def test_l6_fails_swapped_chart_with_shared_boilerplate(tmp_path: Path) -> None:
    """The adversarial-probe regression: a DIFFERENT patient's chart sharing
    all body boilerplate scores ~0.99 on whole-page similarity — L6 must fail
    it on identity (name/DOB), never pass it as 'reprocessed'."""
    item = _good_item(tmp_path, "orig.pdf")
    swapped_lines = [
        "Wrong Otherpatient",
        "DOB 03/04/1975",
        "Date of service: May 10, 2023",
        "Heart rate 72 bpm",
        *_FILLER,
    ]
    swapped = _make_pdf(tmp_path / "swapped.pdf", swapped_lines)
    reader = _Reader(swapped.read_bytes(), page_count=1, size=swapped.stat().st_size)
    result = L6RoundTrip().run(item, DEST_PATIENT, DOC_ID, reader=reader, patient=_patient())
    assert result.status is LevelStatus.FAIL


def test_l6_fails_reprocessed_without_patient_context(tmp_path: Path) -> None:
    # Fail-safe: differing bytes whose identity cannot be re-asserted (no
    # canonical patient in hand) must FAIL, not pass on similarity.
    item = _good_item(tmp_path, "orig.pdf")
    reprocessed = _make_pdf(tmp_path / "reprocessed.pdf", GOOD_PAGE_LINES)
    reader = _Reader(reprocessed.read_bytes(), page_count=1, size=reprocessed.stat().st_size)
    result = L6RoundTrip().run(item, DEST_PATIENT, DOC_ID, reader=reader, patient=None)
    assert result.status is LevelStatus.FAIL
    assert "identity" in result.detail


def test_l6_fails_corrupt_readback(tmp_path: Path) -> None:
    item = _good_item(tmp_path, "orig.pdf")
    # A different document: different page-1 text and a corrupted tail.
    bad = _make_pdf(tmp_path / "bad.pdf", ["Totally Different Document", "no identity here"])
    reader = _Reader(bad.read_bytes(), page_count=1, size=bad.stat().st_size)
    result = L6RoundTrip().run(item, DEST_PATIENT, DOC_ID, reader=reader)
    assert result.status is LevelStatus.FAIL


def test_l6_fails_page_count_differs(tmp_path: Path) -> None:
    item = _good_item(tmp_path, "orig.pdf")
    two = _make_pdf(tmp_path / "two.pdf", GOOD_PAGE_LINES, pages=2)
    reader = _Reader(two.read_bytes(), page_count=2, size=two.stat().st_size)
    result = L6RoundTrip().run(item, DEST_PATIENT, DOC_ID, reader=reader)
    assert result.status is LevelStatus.FAIL
    assert "page_count" in result.detail
