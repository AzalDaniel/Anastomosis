"""A scanned chart is still a chart.

A C-CDA Unstructured Document carries its whole clinical content as one
embedded or referenced artifact under ``<nonXMLBody>``, invisible to the
section walk — it must not silently parse into a correct name and an
empty chart. Carried, not refused, except where carrying is impossible —
a missing reference, or an artifact over the declared ceiling.

Every byte here is generated: ``feedface-`` ids, the 555 exchange, and a
PDF built in :func:`_pdf`.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from anastomosis.core.ccda_codes import EXT_PRIOR_LOSS_NARRATIVE
from anastomosis.core.model import EXT_INLINE_CONTENT, PatientRecord
from anastomosis.deliver.ccda_export import build_ccd
from anastomosis.pipeline import ATTACHMENTS_DIRNAME, _carry_attachments
from anastomosis.sources.base import SourceDataError
from anastomosis.sources.ccda import parser as ccda_parser
from anastomosis.sources.ccda.ledger import Disposition, aggregate, document_ledger
from anastomosis.sources.ccda.parser import (
    MAX_ARTIFACT_BYTES,
    UnstructuredBodyMissingError,
    UnstructuredBodyTooLargeError,
    parse_document,
)

_DOCUMENT = """<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3">
  <realmCode code="US"/>
  <templateId root="2.16.840.1.113883.10.20.22.1.1" extension="2015-08-01"/>
  <id root="feedface-docu-0000-0000-000000000313"/>
  <code code="34133-9" displayName="Summarization of Episode Note"
        codeSystem="2.16.840.1.113883.6.1"/>
  <title>Scanned Referral</title>
  <effectiveTime value="20230510150000-0500"/>
  <recordTarget>
    <patientRole>
      <id root="feedface-pati-0000-0000-000000000313"/>
      <telecom value="tel:+1(206)555-0177" use="HP"/>
      <patient>
        <name><given>Synthia</given><family>Probe</family></name>
        <birthTime value="19800102"/>
      </patient>
    </patientRole>
  </recordTarget>
  <component><nonXMLBody>{body}</nonXMLBody></component>
</ClinicalDocument>
"""


def _pdf(pages: int = 1) -> bytes:
    """A small, real PDF, generated here rather than copied from anywhere:
    structure only, no glyphs, but a genuine catalogue/pages/page tree
    behind a valid xref — a document, not a string that merely starts
    with ``%PDF``."""
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


def _write(directory: Path, body: str, name: str = "scan.xml") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(_DOCUMENT.format(body=body), encoding="utf-8")
    return path


def _embedded(content: bytes, media_type: str | None = "application/pdf") -> str:
    declared = f' mediaType="{media_type}"' if media_type is not None else ""
    return f'<text{declared} representation="B64">{base64.b64encode(content).decode()}</text>'


def _referencing(target: str, media_type: str = "application/pdf") -> str:
    return f'<text mediaType="{media_type}"><reference value="{target}"/></text>'


def _sole_document(record: PatientRecord) -> object:
    assert len(record.documents) == 1, f"{len(record.documents)} artifacts, expected one"
    return record.documents[0]


# --- embedded ----------------------------------------------------------------


def test_the_embedded_artifact_becomes_the_chart(tmp_path: Path) -> None:
    """The defect, stated as the fix: the record leaves with the scan on it."""
    pdf = _pdf(pages=2)
    record = parse_document(_write(tmp_path / "export", _embedded(pdf)))

    artifact = _sole_document(record)
    assert artifact.patient_id == record.patient.id  # type: ignore[attr-defined]
    assert base64.b64decode(artifact.extensions[EXT_INLINE_CONTENT]) == pdf  # type: ignore[attr-defined]
    # Everything else the document offered still arrives: this is a carry, not a
    # special case that trades demographics for an attachment.
    assert record.patient.family_name == "Probe"


def test_the_media_type_is_the_one_the_document_declared(tmp_path: Path) -> None:
    """Read, never guessed: a claim about what a clinical artifact is can
    only be the document's own, so an unusual declared type must survive
    verbatim, and the delivered filename follows the declaration rather
    than leading it."""
    record = parse_document(
        _write(tmp_path / "export", _embedded(b"II*\x00 synthetic scan", "image/tiff"))
    )

    artifact = _sole_document(record)
    assert artifact.mime_type == "image/tiff"  # type: ignore[attr-defined]
    assert str(artifact.path).endswith(".tiff")  # type: ignore[attr-defined]


def test_a_body_that_declares_no_media_type_says_so(tmp_path: Path) -> None:
    """ "The document said octet-stream" and "the document said nothing" are
    different facts about a chart, and both have to survive."""
    record = parse_document(_write(tmp_path / "export", _embedded(_pdf(), media_type=None)))

    artifact = _sole_document(record)
    assert artifact.mime_type == "application/octet-stream"  # type: ignore[attr-defined]
    assert artifact.extensions["ccda:nonXMLBody"]["mediaType_declared"] is False  # type: ignore[attr-defined]


def test_a_plain_text_body_is_carried_as_its_own_characters(tmp_path: Path) -> None:
    """Not every non-XML body is base64. CDA's default for ED is the element's
    own text, and a typed referral letter arrives exactly that way."""
    body = '<text mediaType="text/plain">Referral: see attached.\nLine two.</text>'
    record = parse_document(_write(tmp_path / "export", body))

    artifact = _sole_document(record)
    carried = base64.b64decode(artifact.extensions[EXT_INLINE_CONTENT])  # type: ignore[attr-defined]
    assert carried == b"Referral: see attached.\nLine two."


def test_the_nonxmlbody_declarations_are_not_dropped(tmp_path: Path) -> None:
    """Losslessness: an attribute with no ``DocumentArtifact`` field rides
    ``extensions`` under a namespaced key rather than being read and thrown."""
    body = (
        '<languageCode code="en-US"/>'
        '<text mediaType="application/pdf" representation="B64">'
        f"{base64.b64encode(_pdf()).decode()}</text>"
    )
    record = parse_document(_write(tmp_path / "export", body))

    declared = _sole_document(record).extensions["ccda:nonXMLBody"]  # type: ignore[attr-defined]
    assert declared["representation"] == "B64"
    assert declared["languageCode"] == "en-US"


def test_the_note_says_what_a_physician_needs_to_know(tmp_path: Path) -> None:
    """The record has to say, in words, that the chart is the attachment and
    that no coded data was there to migrate — a count of zero problems reads the
    same whether the source had none or the adapter lost them all."""
    record = parse_document(_write(tmp_path / "export", _embedded(_pdf())))

    note = _sole_document(record).extensions["ccda:nonXMLBody"]["note"]  # type: ignore[attr-defined]
    assert "attachment" in note
    assert "no coded clinical data" in note.lower()


# --- referenced --------------------------------------------------------------


def test_a_referenced_artifact_beside_the_document_is_carried(tmp_path: Path) -> None:
    """Resolved relative to the document's own location, which is the only
    place a C-CDA reference without a scheme can mean."""
    export = tmp_path / "export"
    pdf = _pdf()
    export.mkdir()
    (export / "referral.pdf").write_bytes(pdf)

    record = parse_document(_write(export, _referencing("referral.pdf")))

    artifact = _sole_document(record)
    assert artifact.path == "referral.pdf"  # type: ignore[attr-defined]
    # The file is already in the export, so nothing rides the record: the
    # pipeline copies it exactly as it copies every other source attachment.
    assert EXT_INLINE_CONTENT not in artifact.extensions  # type: ignore[attr-defined]
    assert artifact.sha256 is not None  # type: ignore[attr-defined]


def test_a_dangling_reference_refuses_the_run(tmp_path: Path) -> None:
    """The loud half of the contract: nothing else is on this chart, so a
    reference that does not resolve is a total loss for that patient —
    the adapter must fail closed rather than deliver correct demographics
    over nothing at all."""
    path = _write(tmp_path / "export", _referencing("referral.pdf"))

    with pytest.raises(UnstructuredBodyMissingError) as caught:
        parse_document(path)

    message = str(caught.value)
    assert "referral.pdf" not in message, "a C-CDA names its files after the patient"
    assert "not beside it in the export" in message
    # The pipeline shows an adapter refusal's MESSAGE, not just its type, and it
    # may only do that for refusals written to be PHI-safe.
    assert isinstance(caught.value, SourceDataError)


def test_a_reference_pointing_outside_the_export_is_refused(tmp_path: Path) -> None:
    """The reference is a third party's word about the filesystem. Following a
    ``../`` out of the directory the operator pointed at would read a file they
    never offered and deliver it onward."""
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(_pdf())

    with pytest.raises(UnstructuredBodyMissingError):
        parse_document(_write(tmp_path / "export", _referencing("../secret.pdf")))


def test_the_adapter_passes_the_refusal_through(tmp_path: Path) -> None:
    """``CCDAAdapter.load`` re-raises a ``SourceDataError`` unchanged, so the
    diagnosis reaches the operator instead of being flattened into a type name."""
    from anastomosis.sources.ccda import CCDAAdapter

    export = tmp_path / "export"
    _write(export, _referencing("referral.pdf"))

    with pytest.raises(UnstructuredBodyMissingError):
        list(CCDAAdapter().load(export))


def test_a_reference_that_names_no_file_at_all_is_refused(tmp_path: Path) -> None:
    """A ``#`` fragment, a URL, an absolute path: none of them is a file beside
    the document, and this parser reads the export the operator pointed at and
    fetches nothing. All three end where a dangling reference ends."""
    with pytest.raises(UnstructuredBodyMissingError):
        parse_document(_write(tmp_path / "export", _referencing("#scan-1")))


def test_a_body_stating_no_text_offers_nothing_and_loses_nothing(tmp_path: Path) -> None:
    """CDA requires the wrapper; a wrapper with nothing in it is not a
    loss. The ledger must read this back as ``source_empty`` — a different
    fact from a body that was there and did not survive."""
    path = _write(tmp_path / "export", "")

    assert parse_document(path).documents == []
    assert _body_disposition(document_ledger(path)) == {Disposition.SOURCE_EMPTY.value: 1}


def test_two_bodies_in_one_document_are_two_artifacts(tmp_path: Path) -> None:
    """CDA allows a document one body; an export is not obliged to obey.
    Carrying only the first would deliver a document that looks whole and
    is half there, and one derived name for both would file the second
    over the first."""
    first, second = _pdf(pages=1), _pdf(pages=4)
    body = "</nonXMLBody><nonXMLBody>".join((_embedded(first), _embedded(second)))
    export = tmp_path / "export"
    record = parse_document(_write(export, body))

    assert len(record.documents) == 2
    carried = [base64.b64decode(doc.extensions[EXT_INLINE_CONTENT]) for doc in record.documents]
    assert carried == [first, second]
    assert _carry_attachments([record], export, tmp_path / "charts") == 2


# --- the ceiling -------------------------------------------------------------


def test_the_ceiling_is_declared_not_discovered() -> None:
    """A limit found by running out of memory is one nobody can read, reproduce
    or raise. This one is a module constant with a number in it."""
    assert MAX_ARTIFACT_BYTES == 32 * 1024 * 1024


def test_an_oversize_embedded_artifact_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refused whole, never truncated: half a scanned discharge summary is
    not a smaller version of it, but one whose second half silently does
    not exist. The ceiling is lowered here rather than a 32 MiB fixture
    written; ``test_the_ceiling_is_declared_not_discovered`` holds the
    shipped number."""
    monkeypatch.setattr(ccda_parser, "MAX_ARTIFACT_BYTES", 128)
    path = _write(tmp_path / "export", _embedded(_pdf(pages=40)))

    with pytest.raises(UnstructuredBodyTooLargeError) as caught:
        parse_document(path)

    message = str(caught.value)
    assert "128-byte ceiling" in message
    assert "Probe" not in message and "Synthia" not in message


def test_an_oversize_referenced_artifact_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file on disk is measured before it is carried, for the same reason."""
    monkeypatch.setattr(ccda_parser, "MAX_ARTIFACT_BYTES", 128)
    export = tmp_path / "export"
    export.mkdir()
    (export / "referral.pdf").write_bytes(_pdf(pages=40))

    with pytest.raises(UnstructuredBodyTooLargeError):
        parse_document(_write(export, _referencing("referral.pdf")))


# --- delivery ----------------------------------------------------------------


def test_the_embedded_artifact_reaches_the_output_directory(tmp_path: Path) -> None:
    """An artifact carried into the record and never written is the same loss,
    one stage later. It lands beside the charts, byte for byte."""
    export = tmp_path / "export"
    pdf = _pdf(pages=3)
    record = parse_document(_write(export, _embedded(pdf)))
    out = tmp_path / "charts"

    assert _carry_attachments([record], export, out) == 1

    landed = out / ATTACHMENTS_DIRNAME / str(record.documents[0].path)
    assert landed.read_bytes() == pdf
    # Inside the hardened directory the charts go in, not beside it.
    assert landed.parent.parent == out


def test_a_referenced_artifact_reaches_the_output_directory(tmp_path: Path) -> None:
    """The other half of the same seam, proved end to end rather than assumed
    from the pf_tebra case it shares."""
    export = tmp_path / "export"
    pdf = _pdf()
    export.mkdir()
    (export / "referral.pdf").write_bytes(pdf)
    record = parse_document(_write(export, _referencing("referral.pdf")))
    out = tmp_path / "charts"

    assert _carry_attachments([record], export, out) == 1
    assert (out / ATTACHMENTS_DIRNAME / "referral.pdf").read_bytes() == pdf


def test_two_scanned_documents_do_not_land_on_one_name(tmp_path: Path) -> None:
    """Two patients' scans filed under one name is the wrong-patient failure the
    delivery ledger exists to refuse. Names are derived per document, so two
    documents are two files."""
    export = tmp_path / "export"
    first = parse_document(_write(export, _embedded(_pdf(pages=1)), name="a.xml"))
    second = parse_document(_write(export, _embedded(_pdf(pages=5)), name="b.xml"))
    out = tmp_path / "charts"

    assert _carry_attachments([first, second], export, out) == 2
    # The hardened directory writes its own PHI-warning README; the
    # attachments are what does not start with an underscore.
    landed = sorted(
        p.name for p in (out / ATTACHMENTS_DIRNAME).iterdir() if not p.name.startswith("_")
    )
    assert len(landed) == 2


# --- what the ledger can then prove ------------------------------------------


def _body_disposition(ledger: object) -> dict[str, int]:
    corpus = aggregate([ledger])  # type: ignore[list-item]
    row = next(row for row in corpus.rows if row.construct == "body:nonXMLBody")
    return {disposition.value: count for disposition, count in row.instances.items() if count}


def test_the_ledger_credits_the_carry(tmp_path: Path) -> None:
    path = _write(tmp_path / "export", _embedded(_pdf()))

    assert _body_disposition(document_ledger(path)) == {Disposition.STRUCTURALLY_PARSED.value: 1}


def test_the_ledger_reads_a_record_and_not_a_table(tmp_path: Path) -> None:
    """The reading is evidence from the record, not a dispatch table: with
    the artifact taken back off the same document's record, the row
    reverts to ``unsupported``."""
    path = _write(tmp_path / "export", _embedded(_pdf()))
    record = parse_document(path)
    record.documents = []

    assert _body_disposition(document_ledger(path, record)) == {Disposition.UNSUPPORTED.value: 1}


# --- what the export says ----------------------------------------------------


def test_the_export_narrates_what_happened_and_not_the_bytes(tmp_path: Path) -> None:
    """Exported onward, the record still says what a physician has to
    know. The artifact's own BYTES are the one thing never narrated —
    they live beside the chart — or a re-ingest/re-export cycle would
    inline the scan and grow the document generation over generation."""
    record = parse_document(_write(tmp_path / "export", _embedded(_pdf(pages=6))))
    exported = build_ccd(record)
    path = tmp_path / "ccd.xml"
    path.write_bytes(exported if isinstance(exported, bytes) else exported.encode())

    entries = parse_document(path).patient.extensions[EXT_PRIOR_LOSS_NARRATIVE]["entries"]
    assert any("no coded clinical data" in entry.lower() for entry in entries)
    assert any("mime_type = application/pdf" in entry for entry in entries)
    assert not any(EXT_INLINE_CONTENT in entry for entry in entries)
