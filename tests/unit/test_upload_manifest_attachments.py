"""Upload-route parity with the bundle: a patient whose whole chart is a
C-CDA Unstructured Document still has files to carry, and the upload
manifest must record exactly what the bundle records — one item per
carried file, resolvable back off disk, attributed to one patient, and
verified only to the level its bytes actually support.

Synthetic: ``feedface-`` ids, the 555 exchange, and structure-only PDFs
with no glyphs — no byte here is anybody's.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from _render_fakes import write_text_pdf

from anastomosis.core.model import (
    DocumentArtifact,
    Encounter,
    Patient,
    PatientRecord,
)
from anastomosis.deliver.browser.manifest import AttachmentNotDeliverable
from anastomosis.deliver.browser.persist import (
    GATE_VERSION,
    MANIFEST_NAME,
    POLICY_VERSION,
    ManifestError,
    load_upload_manifest,
    write_upload_manifest,
)
from anastomosis.deliver.verify.types import VerifyPolicy
from anastomosis.pipeline import ATTACHMENTS_DIRNAME
from anastomosis.reconstruct.engine import RenderedDoc

PAT = "feedface-0000-0000-0000-0000000003a1"
OTHER = "feedface-0000-0000-0000-0000000003a2"
ENC = "feedface-e000-0000-0000-0000000003a1"
DOS = datetime.date(2023, 5, 10)


def _pdf(pages: int = 1) -> bytes:
    """A real ``pages``-page PDF: structure only, no glyphs, but a genuine
    catalogue/pages/page tree behind a valid xref — a document, not a
    string that merely starts with ``%PDF``."""
    kids = b" ".join(f"{index + 3} 0 R".encode() for index in range(pages))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [" + kids + b"] /Count " + str(pages).encode() + b" >>",
        *(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>" for _ in range(pages)),
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    table = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode()
    out += f"startxref\n{table}\n%%EOF\n".encode()
    return bytes(out)


def _patient(pid: str = PAT) -> Patient:
    return Patient(
        id=pid, family_name="Probe", given_name="Synthia", birth_date=datetime.date(1980, 1, 2)
    )


def _carry(out_dir: Path, name: str, content: bytes) -> Path:
    """Put one file where ``pipeline._carry_attachments`` puts it."""
    landing = out_dir / ATTACHMENTS_DIRNAME
    landing.mkdir(parents=True, exist_ok=True)
    path = landing / name
    path.write_bytes(content)
    return path


def _artifact(name: str, content: bytes, *, patient_id: str = PAT, **rest: Any) -> DocumentArtifact:
    """The record's side of a carried file: it names the path and its digest."""
    return DocumentArtifact(
        patient_id=patient_id, path=name, sha256=hashlib.sha256(content).hexdigest(), **rest
    )


def _scanned_record(out_dir: Path) -> tuple[PatientRecord, bytes, bytes]:
    """The #374 shape: one patient, no encounters, two carried documents —
    one embedded body under the artifact's own uuid5 name, one referenced
    body keeping the export's name (C-CDA ``<nonXMLBody>``'s two forms)."""
    referenced, embedded = _pdf(pages=1), _pdf(pages=2)
    _carry(out_dir, "referred_report.pdf", referenced)
    _carry(out_dir, "feedface-a000-0000-0000-0000000003a1.pdf", embedded)
    record = PatientRecord(
        id=PAT,
        patient=_patient(),
        documents=[
            _artifact("referred_report.pdf", referenced),
            _artifact("feedface-a000-0000-0000-0000000003a1.pdf", embedded),
        ],
    )
    return record, referenced, embedded


# --- the defect, stated as the fix ------------------------------------------


def test_an_attachment_only_chart_reaches_the_upload_manifest(tmp_path: Path) -> None:
    """Zero encounters, zero rendered charts — and two items, not none."""
    out_dir = tmp_path / "charts"
    record, referenced, embedded = _scanned_record(out_dir)

    written = write_upload_manifest([], [record], out_dir)

    assert (written.items, written.charts, written.documents) == (2, 0, 2)
    manifest = load_upload_manifest(out_dir)
    assert {item.sha256 for item in manifest.items} == {
        hashlib.sha256(referenced).hexdigest(),
        hashlib.sha256(embedded).hexdigest(),
    }
    assert {item.size_bytes for item in manifest.items} == {len(referenced), len(embedded)}


def test_the_patient_is_written_even_with_no_encounter_renders(tmp_path: Path) -> None:
    """``patients`` was empty because only items put a patient there.

    That rule is right and is left alone: what was wrong is that a scanned chart
    produced no item to invoke it.
    """
    out_dir = tmp_path / "charts"
    record, _referenced, _embedded = _scanned_record(out_dir)

    write_upload_manifest([], [record], out_dir)

    manifest = load_upload_manifest(out_dir)
    assert set(manifest.patients) == {PAT}
    assert manifest.patients[PAT].birth_date == datetime.date(1980, 1, 2)
    assert {item.patient_id for item in manifest.items} == {PAT}


def test_the_stored_path_is_relative_to_the_bundle_not_a_bare_basename(tmp_path: Path) -> None:
    """A basename would re-absolutize to a file that is not there: charts
    keep the bundle-root basename, but a source document sits one
    directory down and must say so, or the item resolves to nothing."""
    out_dir = tmp_path / "charts"
    record, _referenced, _embedded = _scanned_record(out_dir)

    write_upload_manifest([], [record], out_dir)

    data = json.loads((out_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    stored = sorted(entry["file_path"] for entry in data["items"])
    assert stored == [
        f"{ATTACHMENTS_DIRNAME}/feedface-a000-0000-0000-0000000003a1.pdf",
        f"{ATTACHMENTS_DIRNAME}/referred_report.pdf",
    ]
    for entry in data["items"]:
        assert not Path(entry["file_path"]).is_absolute()


def test_a_fresh_load_resolves_both_files_and_reopens_them_at_their_page_counts(
    tmp_path: Path,
) -> None:
    """Nothing carries over from the write: the manifest is read back off
    disk, each item's path re-absolutized, and the file at the end of it
    opened and counted."""
    pymupdf = pytest.importorskip("pymupdf", reason="page counts need PyMuPDF (render extra)")
    out_dir = tmp_path / "charts"
    record, _referenced, _embedded = _scanned_record(out_dir)
    write_upload_manifest([], [record], out_dir)

    manifest = load_upload_manifest(out_dir)

    opened = {}
    for item in manifest.items:
        assert item.file_path.is_file()
        with pymupdf.open(item.file_path) as doc:
            opened[item.file_path.name] = doc.page_count
        assert manifest.expected_pages[item.item_key] == opened[item.file_path.name]
    assert opened == {
        "referred_report.pdf": 1,
        "feedface-a000-0000-0000-0000000003a1.pdf": 2,
    }


def test_a_mixed_record_lists_its_chart_and_its_documents_each_once(tmp_path: Path) -> None:
    """One record with both kinds: three items, no double-counting either way."""
    out_dir = tmp_path / "charts"
    out_dir.mkdir()
    chart = out_dir / "Probe_Synthia_2023-05-10_note.pdf"
    chart.write_bytes(_pdf(pages=1))
    referenced, embedded = _pdf(pages=1), _pdf(pages=2)
    _carry(out_dir, "referred_report.pdf", referenced)
    _carry(out_dir, "lab.pdf", embedded)
    record = PatientRecord(
        id=PAT,
        patient=_patient(),
        encounters=[Encounter(id=ENC, patient_id=PAT, date_of_service=DOS)],
        documents=[
            _artifact("referred_report.pdf", referenced),
            _artifact("lab.pdf", embedded, encounter_id=ENC),
        ],
    )
    docs = [RenderedDoc(path=chart, encounter_id=ENC, patient_id=PAT)]

    written = write_upload_manifest(docs, [record], out_dir)

    assert (written.charts, written.documents) == (1, 2)
    manifest = load_upload_manifest(out_dir)
    assert len({item.item_key for item in manifest.items}) == 3
    assert sorted(item.file_path.name for item in manifest.items) == [
        "Probe_Synthia_2023-05-10_note.pdf",
        "lab.pdf",
        "referred_report.pdf",
    ]
    # The document that names an encounter is filed under it — and so inherits
    # the date of service a destination's filing dialog asks for.
    filed = next(item for item in manifest.items if item.file_path.name == "lab.pdf")
    assert filed.encounter_id == ENC
    assert filed.date_of_service == DOS


def test_two_documents_for_one_patient_get_distinct_item_keys(tmp_path: Path) -> None:
    """The item key is the ledger's identity: two files may never share one."""
    out_dir = tmp_path / "charts"
    record, _referenced, _embedded = _scanned_record(out_dir)

    write_upload_manifest([], [record], out_dir)

    manifest = load_upload_manifest(out_dir)
    assert len({item.item_key for item in manifest.items}) == len(manifest.items) == 2


def test_one_file_two_records_name_is_listed_once(tmp_path: Path) -> None:
    """A document referenced twice is one file on disk and one thing to upload."""
    out_dir = tmp_path / "charts"
    content = _pdf(pages=1)
    _carry(out_dir, "referred_report.pdf", content)
    record = PatientRecord(
        id=PAT,
        patient=_patient(),
        documents=[
            _artifact("referred_report.pdf", content),
            _artifact("referred_report.pdf", content),
        ],
    )

    written = write_upload_manifest([], [record], out_dir)

    assert written.documents == 1


# --- refusals ---------------------------------------------------------------


def test_a_document_two_patients_both_claim_is_refused(tmp_path: Path) -> None:
    """Two records sharing one artifact id and bytes re-claim the same
    delivered slot and pass ``_carry_attachments``'s digest check —
    attributing it to whichever sorted first would file one patient's
    document into another patient's chart."""
    out_dir = tmp_path / "charts"
    content = _pdf(pages=1)
    _carry(out_dir, "shared.pdf", content)
    shared_id = "feedface-a000-0000-0000-0000000003ff"
    records = [
        PatientRecord(
            id=PAT,
            patient=_patient(),
            documents=[_artifact("shared.pdf", content, id=shared_id)],
        ),
        PatientRecord(
            id=OTHER,
            patient=_patient(OTHER),
            documents=[_artifact("shared.pdf", content, id=shared_id, patient_id=OTHER)],
        ),
    ]

    with pytest.raises(AttachmentNotDeliverable, match="another patient's chart"):
        write_upload_manifest([], records, out_dir)


def test_two_files_that_would_share_an_item_key_are_refused(tmp_path: Path) -> None:
    """The ledger keys on ``item_key``: two files that collide (no
    encounter to tell them apart, same patient id, identical digest)
    enqueue as one ledger row and one upload — the other never goes."""
    out_dir = tmp_path / "charts"
    content = _pdf(pages=1)
    _carry(out_dir, "referral.pdf", content)
    _carry(out_dir, "referral-copy.pdf", content)
    record = PatientRecord(
        id=PAT,
        patient=_patient(),
        documents=[
            _artifact("referral.pdf", content),
            _artifact("referral-copy.pdf", content),
        ],
    )

    with pytest.raises(ManifestError, match="share an item_key"):
        write_upload_manifest([], [record], out_dir)


def test_a_document_that_changed_under_the_bundle_is_refused(tmp_path: Path) -> None:
    """What would be filed is not what was read, so nothing is filed."""
    out_dir = tmp_path / "charts"
    record, _referenced, _embedded = _scanned_record(out_dir)
    (out_dir / ATTACHMENTS_DIRNAME / "referred_report.pdf").write_bytes(_pdf(pages=3))

    with pytest.raises(AttachmentNotDeliverable, match="no longer hashes"):
        write_upload_manifest([], [record], out_dir)


def test_a_document_the_bundle_never_carried_is_reported_not_dropped_in_silence(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Absent is not a refusal: ``migrate --render ccda-standard`` writes
    its manifest with no attachment carried at all, so refusing would
    strand that mode. What it may not do is pass without saying so."""
    out_dir = tmp_path / "charts"
    content = _pdf(pages=1)
    record = PatientRecord(
        id=PAT, patient=_patient(), documents=[_artifact("never_carried.pdf", content)]
    )

    with caplog.at_level(logging.WARNING, logger="anastomosis.deliver.browser.persist"):
        written = write_upload_manifest([], [record], out_dir)

    assert (written.items, written.not_carried) == (0, 1)
    blob = "\n".join(entry.getMessage() for entry in caplog.records)
    assert "1 source document(s)" in blob and "NOT in its upload manifest" in blob
    # PHI: the warning counts, it never names the file.
    assert "never_carried" not in blob


def test_an_artifact_with_no_file_is_not_an_item(tmp_path: Path) -> None:
    """An Oracle EHI remote blob names no path: nothing carried it, so nothing
    can deliver it, and it is not counted as a document the bundle is missing
    either — the record never claimed a file."""
    out_dir = tmp_path / "charts"
    record = PatientRecord(
        id=PAT,
        patient=_patient(),
        documents=[DocumentArtifact(patient_id=PAT, mime_type="application/octet-stream")],
    )

    written = write_upload_manifest([], [record], out_dir)

    assert (written.items, written.not_carried) == (0, 0)


def test_the_reader_refuses_a_stored_path_that_climbs_out_of_the_bundle(tmp_path: Path) -> None:
    """A manifest travels between machines; what it names must stay inside it."""
    out_dir = tmp_path / "charts"
    record, _referenced, _embedded = _scanned_record(out_dir)
    write_upload_manifest([], [record], out_dir)
    path = out_dir / MANIFEST_NAME
    data = json.loads(path.read_text(encoding="utf-8"))
    data["items"][0]["file_path"] = "../../etc/passwd"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ManifestError, match="outside its own directory"):
        load_upload_manifest(out_dir)


# --- the verification policy -------------------------------------------------


def test_a_pdf_the_source_declared_is_paged_and_carries_its_page_count(tmp_path: Path) -> None:
    out_dir = tmp_path / "charts"
    record, _referenced, _embedded = _scanned_record(out_dir)

    write_upload_manifest([], [record], out_dir)

    manifest = load_upload_manifest(out_dir)
    assert set(manifest.verify_policies.values()) == {VerifyPolicy.SOURCE_PAGED}
    assert all(manifest.expected_pages[item.item_key] >= 1 for item in manifest.items)


def test_a_media_type_nothing_pages_is_carried_with_no_page_expectation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A TIFF scan is deliverable and unpageable; the manifest says both.
    The media type is the one the SOURCE declared — sniffing the bytes to
    page it anyway would make a clinical claim only the document itself
    may make, and would report a spurious "unreadable count" warning."""
    out_dir = tmp_path / "charts"
    content = b"II*\x00 synthetic scan, not a real TIFF"
    _carry(out_dir, "scan.tiff", content)
    record = PatientRecord(
        id=PAT,
        patient=_patient(),
        documents=[_artifact("scan.tiff", content, mime_type="image/tiff")],
    )

    with caplog.at_level(logging.WARNING, logger="anastomosis.deliver.browser.persist"):
        written = write_upload_manifest([], [record], out_dir)

    assert written.documents == 1
    manifest = load_upload_manifest(out_dir)
    [item] = manifest.items
    assert manifest.verify_policies[item.item_key] is VerifyPolicy.SOURCE_OPAQUE
    assert item.item_key not in manifest.expected_pages  # null: never a guessed count
    assert item.size_bytes == len(content)
    assert "page count unreadable" not in "\n".join(e.getMessage() for e in caplog.records)


def test_the_ladder_reads_bytes_off_a_source_document_and_nothing_else(tmp_path: Path) -> None:
    """L0 and L1 still mean something over a scan; L2 and L3 cannot — a
    scanned referral has no rendered text layer and no pack ever touched
    it, so those levels would fail every source document for lacking
    something that was never going to be there."""
    pytest.importorskip("pymupdf", reason="the ladder reads PDFs with PyMuPDF")
    from anastomosis.deliver.verify import LayeredVerifier, LevelStatus

    out_dir = tmp_path / "charts"
    record, _referenced, _embedded = _scanned_record(out_dir)
    write_upload_manifest([], [record], out_dir)
    manifest = load_upload_manifest(out_dir)
    verifier = LayeredVerifier(
        expected_pages=manifest.expected_pages,
        verify_policies=manifest.verify_policies,
        levels=frozenset({"L0", "L1", "L2", "L3"}),
    )

    verifier.verify_pre(manifest.items[0], manifest.patients[PAT])

    table = {result.level: result for result in verifier.last_results}
    assert table["L0"].status is LevelStatus.PASS
    assert table["L1"].status is LevelStatus.PASS  # the source declared it a PDF
    assert table["L2"].status is LevelStatus.SKIP
    assert table["L3"].status is LevelStatus.SKIP
    assert "source document" in table["L2"].detail


def test_a_small_source_pdf_is_not_condemned_by_the_chart_size_floor(tmp_path: Path) -> None:
    """The sub-KiB size floor is a fact about a Chromium print, not a
    PDF: a scanner's output is whatever the scanner wrote. L0 already
    re-hashes those bytes against the source's own digest, which says
    more about a source file than any size heuristic."""
    pytest.importorskip("pymupdf", reason="the ladder reads PDFs with PyMuPDF")
    from anastomosis.deliver.verify import LayeredVerifier, LevelStatus

    out_dir = tmp_path / "charts"
    record, referenced, _embedded = _scanned_record(out_dir)
    assert len(referenced) < 1024  # the floor a rendered chart would fail on
    write_upload_manifest([], [record], out_dir)
    manifest = load_upload_manifest(out_dir)
    item = next(item for item in manifest.items if item.file_path.name == "referred_report.pdf")
    verifier = LayeredVerifier(
        expected_pages=manifest.expected_pages,
        verify_policies=manifest.verify_policies,
        levels=frozenset({"L1"}),
    )

    verifier.verify_pre(item, manifest.patients[PAT])

    [result] = verifier.last_results
    assert result.status is LevelStatus.PASS
    assert "page_count=1" in result.detail


# --- schema version ----------------------------------------------------------


def test_a_bundle_carrying_source_documents_is_a_version_4_file(tmp_path: Path) -> None:
    """The version describes the CONTENT: a v3 reader would take an
    ``attachments/`` item for a rendered chart and run the chart ladder over a
    scan."""
    out_dir = tmp_path / "charts"
    record, _referenced, _embedded = _scanned_record(out_dir)

    write_upload_manifest([], [record], out_dir)

    data = json.loads((out_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert data["version"] == POLICY_VERSION == 4
    assert {entry["verify_policy"] for entry in data["items"]} == {"source_paged"}


def test_a_bundle_of_charts_alone_is_the_file_it_always_was(tmp_path: Path) -> None:
    """Nothing moves for a run with no source documents: same version, same
    keys, so an existing tree and its reader are untouched."""
    from anastomosis.deliver.browser.gates import RunGates

    out_dir = tmp_path / "charts"
    out_dir.mkdir()
    chart = out_dir / "note.pdf"
    chart.write_bytes(_pdf(pages=1))
    record = PatientRecord(
        id=PAT,
        patient=_patient(),
        encounters=[Encounter(id=ENC, patient_id=PAT, date_of_service=DOS)],
    )
    docs = [RenderedDoc(path=chart, encounter_id=ENC, patient_id=PAT)]

    write_upload_manifest(
        docs,
        [record],
        out_dir,
        pack="generic_soap",
        gates=RunGates.from_run(qa_ok=True, layout_hash=None),
    )

    data = json.loads((out_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert data["version"] == GATE_VERSION == 3
    assert "verify_policy" not in data["items"][0]
    assert data["items"][0]["file_path"] == "note.pdf"


def test_a_pre_v4_manifest_still_loads_and_its_items_are_charts(tmp_path: Path) -> None:
    """Operators have rendered trees on disk; every item in one is a chart."""
    from anastomosis.deliver.browser.gates import RunGates

    out_dir = tmp_path / "charts"
    out_dir.mkdir()
    chart = out_dir / "note.pdf"
    chart.write_bytes(_pdf(pages=1))
    docs = [RenderedDoc(path=chart, encounter_id=ENC, patient_id=PAT)]
    record = PatientRecord(id=PAT, patient=_patient())
    write_upload_manifest(
        docs, [record], out_dir, gates=RunGates.from_run(qa_ok=True, layout_hash=None)
    )

    manifest = load_upload_manifest(out_dir)

    assert manifest.version == GATE_VERSION
    assert set(manifest.verify_policies.values()) == {VerifyPolicy.RENDERED_CHART}


def test_a_v4_item_missing_its_policy_is_a_defect(tmp_path: Path) -> None:
    """At the version that has the field, an absent key means the file does not
    match the version it declares — the reader's rule for every other field
    group, applied to this one."""
    out_dir = tmp_path / "charts"
    record, _referenced, _embedded = _scanned_record(out_dir)
    write_upload_manifest([], [record], out_dir)
    path = out_dir / MANIFEST_NAME
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["items"][0]["verify_policy"]
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ManifestError, match="verify_policy"):
        load_upload_manifest(out_dir)


def test_an_unknown_policy_is_refused_rather_than_read_as_a_chart(tmp_path: Path) -> None:
    """Falling back to the chart ladder is the wrong direction to fail in."""
    out_dir = tmp_path / "charts"
    record, _referenced, _embedded = _scanned_record(out_dir)
    write_upload_manifest([], [record], out_dir)
    path = out_dir / MANIFEST_NAME
    data = json.loads(path.read_text(encoding="utf-8"))
    data["items"][0]["verify_policy"] = "something_a_later_build_writes"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ManifestError, match="unknown verify_policy"):
        load_upload_manifest(out_dir)


# --- PHI ---------------------------------------------------------------------


def test_the_writer_never_logs_a_document_name(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A source document's filename is a source identifier and can be a patient
    value (an export naming a scan after the patient). Counts only."""
    out_dir = tmp_path / "charts"
    name_shaped = "Featherstonehaugh_Aloysius_03-14-1980.pdf"
    content = _pdf(pages=1)
    _carry(out_dir, name_shaped, content)
    record = PatientRecord(id=PAT, patient=_patient(), documents=[_artifact(name_shaped, content)])

    with caplog.at_level(logging.DEBUG, logger="anastomosis.deliver.browser.persist"):
        write_upload_manifest([], [record], out_dir)

    blob = "\n".join(entry.getMessage() for entry in caplog.records)
    assert name_shaped not in blob
    assert "Featherstonehaugh" not in blob
    assert "1 item(s)" in blob


def test_a_base64_body_is_never_what_the_manifest_measures(tmp_path: Path) -> None:
    """The item's digest is of the FILE on disk, not the record's copy: a
    C-CDA Unstructured Document carries its bytes inline and delivery
    writes them out, so hashing the base64 instead would record a digest
    no upload could ever re-measure."""
    from anastomosis.core.model import EXT_INLINE_CONTENT

    out_dir = tmp_path / "charts"
    content = _pdf(pages=2)
    _carry(out_dir, "scan.pdf", content)
    record = PatientRecord(
        id=PAT,
        patient=_patient(),
        documents=[
            _artifact(
                "scan.pdf",
                content,
                extensions={EXT_INLINE_CONTENT: base64.b64encode(content).decode("ascii")},
            )
        ],
    )

    write_upload_manifest([], [record], out_dir)

    [item] = load_upload_manifest(out_dir).items
    assert item.size_bytes == len(content)
    assert item.sha256 == hashlib.sha256(content).hexdigest()


# --- both frontends, one path -------------------------------------------------

#: One C-CDA Unstructured Document with two ``<nonXMLBody>`` components — an
#: embedded body and one referencing a file beside it — for one patient with no
#: encounters. The shape #374 was reported against, as an export on disk.
_UNSTRUCTURED = """<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3">
  <realmCode code="US"/>
  <templateId root="2.16.840.1.113883.10.20.22.1.1" extension="2015-08-01"/>
  <id root="feedface-docu-0000-0000-000000000374"/>
  <code code="34133-9" displayName="Summarization of Episode Note"
        codeSystem="2.16.840.1.113883.6.1"/>
  <title>Scanned Referral</title>
  <effectiveTime value="20230510150000-0500"/>
  <recordTarget>
    <patientRole>
      <id root="feedface-pati-0000-0000-000000000374"/>
      <telecom value="tel:+1(206)555-0177" use="HP"/>
      <patient>
        <name><given>Synthia</given><family>Probe</family></name>
        <birthTime value="19800102"/>
      </patient>
    </patientRole>
  </recordTarget>
  <component><nonXMLBody>
    <text mediaType="application/pdf" representation="B64">{embedded}</text>
  </nonXMLBody></component>
  <component><nonXMLBody>
    <text mediaType="application/pdf"><reference value="referred_report.pdf"/></text>
  </nonXMLBody></component>
</ClinicalDocument>
"""


def _unstructured_export(tmp_path: Path) -> Path:
    export = tmp_path / "export"
    export.mkdir()
    (export / "referred_report.pdf").write_bytes(_pdf(pages=1))
    (export / "scan.xml").write_text(
        _UNSTRUCTURED.format(embedded=base64.b64encode(_pdf(pages=2)).decode("ascii")),
        encoding="utf-8",
    )
    return export


def _items_on_disk(charts: Path) -> list[tuple[str, str, str, int]]:
    """The manifest's items as comparable tuples (no clock, no host paths)."""
    manifest = load_upload_manifest(charts)
    return sorted(
        (item.patient_id, item.file_path.name, item.sha256, item.size_bytes)
        for item in manifest.items
    )


def test_the_cli_and_the_gui_write_the_same_manifest_for_a_scanned_chart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``anast pipeline run --upload-manifest`` and the GUI's run console
    reach ``write_upload_manifest`` through the same ``PipelineCommand``,
    so a fix in one is a fix in both — driven here, not merely claimed."""
    pytest.importorskip("pymupdf", reason="the record summary is rendered with PyMuPDF")
    from typer.testing import CliRunner

    import anastomosis.reconstruct.ccda_standard.renderer as ccda_renderer
    import anastomosis.reconstruct.chromium as chromium
    from anastomosis.cli import app
    from anastomosis.gui.controller import GuiController

    class _FakeChromium:
        def __init__(self, **kwargs: object) -> None:
            pass

        def render(self, html: str, pdf_path: Path) -> None:
            write_text_pdf(html, pdf_path)

        def close(self) -> None:
            pass

    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)
    monkeypatch.setattr(ccda_renderer, "_default_renderer", lambda: _FakeChromium())
    export = _unstructured_export(tmp_path)

    cli_out = tmp_path / "cli"
    result = CliRunner().invoke(
        app,
        [
            "pipeline",
            "run",
            str(export),
            "--out",
            str(cli_out),
            "--source",
            "ccda",
            "--no-qa",
            "--upload-manifest",
        ],
    )

    assert result.exit_code == 0, result.output
    # The line the issue quoted as "manifest: 0 item(s)".
    assert "manifest: 2 item(s)" in " ".join(result.output.split())

    gui_out = tmp_path / "gui"
    outcome = GuiController(lambda _event: None).run_pipeline(
        str(export), str(gui_out), source="ccda", qa=False, write_manifest=True
    )

    assert outcome["ok"] is True, outcome
    assert _items_on_disk(cli_out) == _items_on_disk(gui_out)
    assert len(_items_on_disk(cli_out)) == 2
