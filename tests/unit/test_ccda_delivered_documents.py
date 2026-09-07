"""The C-CDA an operator hands over carries the patient's documents
(#373): the delivery writes every source document beside the CCD
that names it, under the artifact's own pseudonymous id, and refuses
before reporting success when it cannot -- the reader resolves those
names back to the same artifacts, so the deliverable is a round trip,
not a one-way door.

Synthetic throughout (``feedface-`` ids, 555 exchange, PDFs built in
:func:`_pdf`)."""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

import pytest

from anastomosis.core.model import (
    EXT_INLINE_CONTENT,
    DocumentArtifact,
    Patient,
    PatientRecord,
)
from anastomosis.deliver._shared import DeliveredNameCollision
from anastomosis.deliver.ccda_export import ArtifactNotDelivered, deliver_ccda
from anastomosis.sources.ccda.parser import DeliveredArtifactError, parse_document

PATIENT_ID = "feedface-pati-0000-0000-000000000373"

_DOCUMENT = """<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3">
  <realmCode code="US"/>
  <templateId root="2.16.840.1.113883.10.20.22.1.1" extension="2015-08-01"/>
  <id root="feedface-docu-0000-0000-000000000373"/>
  <code code="34133-9" displayName="Summarization of Episode Note"
        codeSystem="2.16.840.1.113883.6.1"/>
  <title>Scanned Chart</title>
  <effectiveTime value="20230510150000-0500"/>
  <recordTarget>
    <patientRole>
      <id root="{pid}"/>
      <telecom value="tel:+1(206)555-0177" use="HP"/>
      <patient>
        <name><given>Synthia</given><family>Probe</family></name>
        <birthTime value="19800102"/>
      </patient>
    </patientRole>
  </recordTarget>
  {bodies}
</ClinicalDocument>
"""


def _pdf(pages: int = 1, tag: bytes = b"synthetic") -> bytes:
    """A small, real PDF (structure only, no glyphs) with a genuine
    catalogue/pages/page tree behind a valid xref -- a document, not a
    string that happens to start with ``%PDF``."""
    kids = b" ".join(f"{index + 3} 0 R".encode() for index in range(pages))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [" + kids + b"] /Count " + str(pages).encode() + b" >>",
        *(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>" for _ in range(pages)),
        b"<< /Tag (" + tag + b") >>",
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


def _embedded_and_referenced_export(directory: Path) -> Path:
    """One C-CDA holding two Unstructured bodies, one inline and one beside
    it, so a delivery that drops either shape (or the reverse) fails
    half this file."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "referenced_1pp.pdf").write_bytes(_pdf(1, b"referenced"))
    inline = base64.b64encode(_pdf(2, b"embedded")).decode()
    bodies = (
        "<component><nonXMLBody>"
        f'<text mediaType="application/pdf" representation="B64">{inline}</text>'
        "</nonXMLBody></component>"
        "<component><nonXMLBody>"
        '<text mediaType="application/pdf"><reference value="referenced_1pp.pdf"/></text>'
        "</nonXMLBody></component>"
    )
    path = directory / "chart.xml"
    path.write_text(_DOCUMENT.format(pid=PATIENT_ID, bodies=bodies), encoding="utf-8")
    return path


def _carried(record: PatientRecord, export: Path, charts: Path) -> Path:
    """Run the pipeline's attachment carry, returning the attachments directory."""
    from anastomosis.pipeline import ATTACHMENTS_DIRNAME, _carry_attachments

    _carry_attachments([record], export, charts)
    return charts / ATTACHMENTS_DIRNAME


def _digests(directory: Path) -> set[str]:
    return {
        hashlib.sha256(path.read_bytes()).hexdigest()
        for path in directory.glob("*.pdf")
        if path.is_file()
    }


# --- conservation ------------------------------------------------------------


def test_an_embedded_artifact_survives_the_ccda_deliverable(tmp_path: Path) -> None:
    """The scan that lives inside the XML reaches the directory the EHR gets.

    It has no file anywhere in the export — that is what "embedded" means — so
    the deliverer writes its bytes, and until #373 nothing did.
    """
    export = tmp_path / "export"
    record = parse_document(_embedded_and_referenced_export(export))
    attachments = _carried(record, export, tmp_path / "charts")
    embedded = next(doc for doc in record.documents if EXT_INLINE_CONTENT in doc.extensions)

    out = tmp_path / "ccda"
    deliver_ccda([record], out, artifacts_dir=attachments)

    assert embedded.sha256 in _digests(out)


def test_a_referenced_artifact_survives_the_ccda_deliverable(tmp_path: Path) -> None:
    """The scan that is a file in the export reaches it too, byte for byte."""
    export = tmp_path / "export"
    record = parse_document(_embedded_and_referenced_export(export))
    attachments = _carried(record, export, tmp_path / "charts")
    referenced = next(doc for doc in record.documents if EXT_INLINE_CONTENT not in doc.extensions)

    out = tmp_path / "ccda"
    deliver_ccda([record], out, artifacts_dir=attachments)

    assert referenced.sha256 in _digests(out)


def test_reparsing_the_delivered_ccda_restores_both_artifacts(tmp_path: Path) -> None:
    """Same identity, same SHA-256, same declared media type -- the three
    facts a receiving system needs to say WHICH document this is and
    that it is intact."""
    export = tmp_path / "export"
    record = parse_document(_embedded_and_referenced_export(export))
    attachments = _carried(record, export, tmp_path / "charts")

    out = tmp_path / "ccda"
    written = deliver_ccda([record], out, artifacts_dir=attachments).paths
    reparsed = parse_document(written[0])

    def identity(rec: PatientRecord) -> set[tuple[str, str | None, str]]:
        return {(doc.id, doc.sha256, doc.mime_type) for doc in rec.documents}

    assert len(reparsed.documents) == 2
    assert identity(reparsed) == identity(record)


def test_the_delivered_document_is_never_named_after_the_source_file(tmp_path: Path) -> None:
    """PHI. A C-CDA export names its attachments after the patient, and this
    directory is the one most likely to travel — emailed to a vendor, dropped on
    a transfer share. So the delivered file carries the artifact's pseudonymous
    id, and the source's own filename stays in the loss ledger with the rest of
    the fields no CDA slot holds."""
    export = tmp_path / "export"
    record = parse_document(_embedded_and_referenced_export(export))
    attachments = _carried(record, export, tmp_path / "charts")

    out = tmp_path / "ccda"
    written = deliver_ccda([record], out, artifacts_dir=attachments).paths

    assert not list(out.glob("referenced_1pp*"))
    assert {p.stem for p in out.glob("*.pdf")} == {doc.id for doc in record.documents}
    # …and the source's name is not lost, only moved: it narrates.
    assert "referenced_1pp.pdf" in written[0].read_text(encoding="utf-8")


def test_an_artifact_the_source_never_resolved_is_not_a_delivery_failure(tmp_path: Path) -> None:
    """A recorded document REFERENCE with no bytes anywhere is not a document
    this delivery lost. Oracle EHI's blob rows are the shape: the source names a
    document it never fetched, so nothing has its bytes to carry, and its fields
    narrate exactly as they did. Refusing here would stop a migration over a
    document that was never in the export."""
    record = PatientRecord(
        patient=Patient(id=PATIENT_ID),
        documents=[DocumentArtifact(patient_id=PATIENT_ID, path=None, sha256=None)],
    )

    result = deliver_ccda([record], tmp_path / "ccda", artifacts_dir=tmp_path / "nothing")

    assert result.artifact_count == 0
    assert result.missing_count == 0
    assert len(result.paths) == 1


def test_a_patient_with_two_documents_is_one_ccd_carrying_both(tmp_path: Path) -> None:
    """One file per patient, carrying every document their records held:
    writing per-encounter records straight to ``<patient-id>.xml`` handed
    the last one and silently dropped the rest (#373's reproduction).
    #375's fold merges a patient's records into one before any deliverer
    sees them, so this one CCD carries every artifact."""
    from anastomosis.pipeline import _fold_records_sharing_a_patient

    def _scanned(tag: bytes, pages: int) -> DocumentArtifact:
        return DocumentArtifact(
            patient_id=PATIENT_ID,
            mime_type="application/pdf",
            extensions={EXT_INLINE_CONTENT: base64.b64encode(_pdf(pages, tag)).decode()},
        )

    records = [
        PatientRecord(
            patient=Patient(id=PATIENT_ID, given_name="Synthia"), documents=[_scanned(b"first", 1)]
        ),
        PatientRecord(
            patient=Patient(id=PATIENT_ID, given_name="Synthia"), documents=[_scanned(b"second", 3)]
        ),
    ]

    out = tmp_path / "ccda"
    result = deliver_ccda(_fold_records_sharing_a_patient(records), out, artifacts_dir=None)

    assert [path.name for path in result.paths] == [f"{PATIENT_ID}.xml"]
    assert result.artifact_count == 2
    assert sorted(
        hashlib.sha256(pdf.read_bytes()).hexdigest() for pdf in out.glob("*.pdf")
    ) == sorted(
        hashlib.sha256(_pdf(pages, tag)).hexdigest()
        for pages, tag in ((1, b"first"), (3, b"second"))
    )


# --- the loud half -----------------------------------------------------------


def test_a_document_the_run_resolved_but_lost_refuses_the_delivery(tmp_path: Path) -> None:
    """A resolved document missing from the run's own directory must refuse
    delivery, not ship a chart naming a file that is not there under a
    green success line (#373)."""
    record = PatientRecord(
        patient=Patient(id=PATIENT_ID),
        documents=[
            DocumentArtifact(patient_id=PATIENT_ID, path="scan.pdf", sha256="0" * 64),
        ],
    )
    empty = tmp_path / "attachments"
    empty.mkdir()

    with pytest.raises(ArtifactNotDelivered) as caught:
        deliver_ccda([record], tmp_path / "ccda", artifacts_dir=empty)

    message = str(caught.value)
    assert "scan.pdf" not in message, "a source names its attachments after the patient"
    assert "not in the directory the run put it in" in message


def test_a_document_whose_bytes_are_not_the_witnessed_ones_refuses(tmp_path: Path) -> None:
    """The digest is taken from the delivered file, not trusted from the
    record: a corrupt or swapped sidecar fails before success, not
    after."""
    attachments = tmp_path / "attachments"
    attachments.mkdir()
    (attachments / "scan.pdf").write_bytes(_pdf(1, b"not-the-witnessed-one"))
    record = PatientRecord(
        patient=Patient(id=PATIENT_ID),
        documents=[
            DocumentArtifact(patient_id=PATIENT_ID, path="scan.pdf", sha256="a" * 64),
        ],
    )

    with pytest.raises(ArtifactNotDelivered, match="different SHA-256"):
        deliver_ccda([record], tmp_path / "ccda", artifacts_dir=attachments)


def test_inline_bytes_that_will_not_decode_refuse_the_delivery(tmp_path: Path) -> None:
    """An artifact arriving as nothing is a refusal, not an empty file beside a
    chart that claims to name it."""
    record = PatientRecord(
        patient=Patient(id=PATIENT_ID),
        documents=[
            DocumentArtifact(
                patient_id=PATIENT_ID,
                path="scan.pdf",
                extensions={EXT_INLINE_CONTENT: "not base64 at all!!"},
            )
        ],
    )

    with pytest.raises(ArtifactNotDelivered, match="did not decode"):
        deliver_ccda([record], tmp_path / "ccda", artifacts_dir=None)


def test_two_documents_under_one_id_refuse_rather_than_merge(tmp_path: Path) -> None:
    """Two scans sharing one artifact id would collide on one delivered
    filename; the witness is the digest of the bytes about to be
    written, so a source that cannot tell two documents apart stops the
    run instead of quietly picking one."""
    shared = "feedface-d0c0-0000-0000-000000000373"
    record = PatientRecord(
        patient=Patient(id=PATIENT_ID),
        documents=[
            DocumentArtifact(
                id=shared,
                patient_id=PATIENT_ID,
                path="a.pdf",
                extensions={EXT_INLINE_CONTENT: base64.b64encode(_pdf(1, b"first")).decode()},
            ),
            DocumentArtifact(
                id=shared,
                patient_id=PATIENT_ID,
                path="b.pdf",
                extensions={EXT_INLINE_CONTENT: base64.b64encode(_pdf(3, b"second")).decode()},
            ),
        ],
    )

    with pytest.raises(DeliveredNameCollision, match="C-CDA document artifact"):
        deliver_ccda([record], tmp_path / "ccda", artifacts_dir=None)


def test_a_delivered_document_that_did_not_travel_refuses_on_re_ingest(tmp_path: Path) -> None:
    """The reader's half of the same rule: split a CCD from its documents
    and re-ingest must refuse rather than carry a patient whose scan
    silently is not there."""
    export = tmp_path / "export"
    record = parse_document(_embedded_and_referenced_export(export))
    attachments = _carried(record, export, tmp_path / "charts")
    out = tmp_path / "ccda"
    written = deliver_ccda([record], out, artifacts_dir=attachments).paths

    for orphan in out.glob("*.pdf"):
        orphan.unlink()

    with pytest.raises(DeliveredArtifactError) as caught:
        parse_document(written[0])
    assert "not beside it" in str(caught.value)


def test_a_delivered_document_edited_after_the_export_refuses_on_re_ingest(
    tmp_path: Path,
) -> None:
    """The digest travels on the ED's own ``@integrityCheck``, so a document
    swapped between the export and the import is caught by the CCD that names
    it rather than by nobody."""
    export = tmp_path / "export"
    record = parse_document(_embedded_and_referenced_export(export))
    attachments = _carried(record, export, tmp_path / "charts")
    out = tmp_path / "ccda"
    written = deliver_ccda([record], out, artifacts_dir=attachments).paths

    swapped = sorted(out.glob("*.pdf"))[0]
    swapped.write_bytes(_pdf(9, b"a different document entirely"))

    with pytest.raises(DeliveredArtifactError, match="do not match the SHA-256"):
        parse_document(written[0])


def test_a_third_partys_multimedia_is_not_read_as_a_delivered_document(tmp_path: Path) -> None:
    """Only this toolkit's own stamped entry is read back as a delivered
    artifact; a third party's ``<observationMedia>`` stays ordinary
    narrative and entries, nothing claimed about it."""
    bodies = (
        "<component><structuredBody><component><section>"
        '<code code="34109-9" displayName="Note" codeSystem="2.16.840.1.113883.6.1"/>'
        "<title>Notes</title><text><paragraph>Imaging</paragraph></text>"
        '<entry><observationMedia classCode="OBS" moodCode="EVN">'
        '<id root="feedface-0the-r000-0000-000000000001"/>'
        '<value xsi:type="ED" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
        ' mediaType="image/jpeg"><reference value="theirs.jpg"/></value>'
        "</observationMedia></entry>"
        "</section></component></structuredBody></component>"
    )
    export = tmp_path / "export"
    export.mkdir()
    path = export / "theirs.xml"
    path.write_text(_DOCUMENT.format(pid=PATIENT_ID, bodies=bodies), encoding="utf-8")

    record = parse_document(path)

    assert record.documents == []
    assert record.patient.extensions["ccda:section:34109-9"]["text"] == "Imaging"
    # And it is preserved the ordinary way: the entry's own bytes, verbatim.
    parked = record.patient.extensions["ccda:entries:34109-9"]
    assert len(parked) == 1
    assert "observationMedia" in parked[0]
    assert "theirs.jpg" in parked[0]


def test_our_own_document_entry_is_read_back_rather_than_parked(tmp_path: Path) -> None:
    """A stamped ``DocumentArtifact`` entry is taken apart, not ALSO parked
    verbatim, or each export restates it beside its own copy and the entry
    doubles every generation. The typed object is the better copy, restated
    with the delivered file's own name and verified digest; an unstamped
    third-party entry (above) stays parked."""
    export = tmp_path / "export"
    _embedded_and_referenced_export(export)
    record = parse_document(next(iter(sorted(export.glob("*.xml")))))
    delivered = tmp_path / "ccda"
    written = deliver_ccda(
        [record], delivered, artifacts_dir=_carried(record, export, tmp_path / "charts")
    ).paths

    reread = parse_document(written[0])

    assert len(reread.documents) == 2
    assert "ccda:entries:34109-9" not in reread.patient.extensions
    assert not any(
        "urn:anastomosis:ccda:artifact" in entry
        for value in reread.patient.extensions.values()
        if isinstance(value, list)
        for entry in value
        if isinstance(entry, str)
    )


# --- generations -------------------------------------------------------------


def test_repeated_export_does_not_grow_the_document_without_bound(tmp_path: Path) -> None:
    """Export -> ingest -> export must settle by generation 3 (not 2, since
    #404's patient-id re-derivation shifts it once), and no generation
    may carry a base64 byte -- documents ride as files with references,
    never inline in the CDA."""
    export = tmp_path / "export"
    _embedded_and_referenced_export(export)
    sizes: list[int] = []
    directory = export
    for generation in range(1, 6):
        record = parse_document(next(iter(sorted(directory.glob("*.xml")))))
        attachments = _carried(record, directory, tmp_path / f"charts{generation}")
        out = tmp_path / f"ccda{generation}"
        written = deliver_ccda([record], out, artifacts_dir=attachments).paths
        text = written[0].read_text(encoding="utf-8")
        assert not re.search(r'representation="B64"', text)
        assert len(list(out.glob("*.pdf"))) == 2
        sizes.append(written[0].stat().st_size)
        directory = out

    assert sizes[2] == sizes[3] == sizes[4], f"the document never settled: {sizes}"


# --- CLI and GUI take one path -----------------------------------------------


class _StopAfterCapture(Exception):
    """Short-circuits the deliverer once it has said what it was handed."""


class _NullSink:
    """The GUI controller needs an event sink; this run has no events to keep."""

    def __call__(self, event: object) -> None:
        return None

    def emit(self, event: object) -> None:
        return None


def _one_record_result() -> object:
    """A ``PipelineResult`` stand-in carrying one record and nothing else.

    ``deliver_outputs`` reads only ``records`` and ``qa_report`` off it, and
    building a real one would need a render this test has no business doing.
    """
    from types import SimpleNamespace

    return SimpleNamespace(records=[PatientRecord(patient=Patient(id=PATIENT_ID))], qa_report=None)


def test_the_cli_and_the_gui_conserve_documents_the_same_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One conservation path, driven from both entry points: CLI and GUI
    both reach ``deliver_outputs``, and this pins that both hand it the
    real attachments directory."""
    import anastomosis.core.commands as commands
    import anastomosis.deliver.ccda_export as ccda_export
    from anastomosis.core.commands import DeliveryCommand, deliver_outputs
    from anastomosis.gui.controller import GuiController
    from anastomosis.pipeline import ATTACHMENTS_DIRNAME

    seen: list[Path | None] = []

    def _capture(records: object, out_dir: object, *, artifacts_dir: Path | None = None) -> object:
        seen.append(artifacts_dir)
        raise _StopAfterCapture

    monkeypatch.setattr(ccda_export, "deliver_ccda", _capture)
    charts = tmp_path / "charts"
    deliveries = (DeliveryCommand("ccda", tmp_path / "ccda"),)

    # The CLI's path: run_pipeline_command hands deliver_outputs the charts dir.
    with pytest.raises(_StopAfterCapture):
        deliver_outputs(_one_record_result(), charts, deliveries)

    # And the GUI's, through the controller a button press reaches. Its own
    # pipeline stage is stubbed out — what is under test is the delivery.
    def _straight_to_delivery(cmd: object, on_event: object = None) -> object:
        from types import SimpleNamespace

        return SimpleNamespace(
            pipeline=None,
            deliveries=deliver_outputs(_one_record_result(), charts, cmd.deliveries),  # type: ignore[attr-defined]
        )

    monkeypatch.setattr(commands, "run_pipeline_command", _straight_to_delivery)
    GuiController(_NullSink()).run_pipeline(str(tmp_path / "export"), str(charts), ccda=True)

    assert seen == [charts / ATTACHMENTS_DIRNAME, charts / ATTACHMENTS_DIRNAME]


# --- end to end --------------------------------------------------------------


def test_the_cli_delivers_the_documents_a_scanned_chart_is_made_of(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#373, end to end through the real CLI: two Unstructured Documents
    must arrive in the ``--ccda`` directory byte-identical at exit 0, named
    by a CCD that references them."""
    pytest.importorskip("pymupdf", reason="pipeline e2e needs PyMuPDF (render extra)")
    from _render_fakes import write_text_pdf
    from typer.testing import CliRunner

    import anastomosis.reconstruct.chromium as chromium
    from anastomosis.cli import app

    class _FakeChromium:
        def __init__(self, **kwargs: object) -> None:
            pass

        def render(self, html: str, pdf_path: Path) -> None:
            write_text_pdf(html, pdf_path)

        def close(self) -> None:
            pass

    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)
    export = tmp_path / "export"
    _embedded_and_referenced_export(export)
    expected = {doc.sha256 for doc in parse_document(export / "chart.xml").documents if doc.sha256}
    ccda_dir = tmp_path / "ccda"

    result = CliRunner().invoke(
        app,
        [
            "pipeline",
            "run",
            str(export),
            "--out",
            str(tmp_path / "charts"),
            "--no-qa",
            "--ccda",
            str(ccda_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    # The count an operator reads. #373's green line said "2 patients" over a
    # directory holding neither of the scans those charts were made of.
    assert "2 documents" in result.output, result.output
    assert expected and _digests(ccda_dir) == expected, result.output
    # And the delivered document names them, so the EHR can find them.
    reparsed = parse_document(next(iter(sorted(ccda_dir.glob("*.xml")))))
    assert {doc.sha256 for doc in reparsed.documents} == expected
