"""One patient is one chart, however many documents the source holds them in.

Every per-patient destination is keyed by ``patient.id``: the C-CDA export
writes ``<patient-id>.xml``, the archive and the bundle each write one
directory, and every writer in them is exist_ok/overwrite. The C-CDA adapter
yields one record per DOCUMENT — a document is the unit its conservation ledger
has to account for — so a patient with two documents arrived as two records,
landed in one slot, and the second silently replaced the first while the run
reported two patients (#375).

The fold that fixes it lives at ``pipeline.load_records``, where every adapter
passes, so a source that already yields one record per patient is unaffected.
These tests drive the real pipeline through the CLI and read what it actually
wrote: none of them re-states the merge rule, because a test that re-implements
the thing it checks agrees with the code by construction.

Every byte here is generated — ``feedface-`` ids, the 555 exchange, invented
people — and nothing is copied from any export.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest
from _render_fakes import FakeChromium
from typer.testing import CliRunner

import anastomosis.reconstruct.chromium as chromium
from anastomosis.cli import app
from anastomosis.core.ccda_codes import (
    EXT_PRIOR_LOSS_NARRATIVE,
    LOINC_EXTENSIONS,
    LOSS_NARRATIVE_GENERATION_ROOT,
    LOSS_NARRATIVE_TEMPLATE_ROOT,
    LOSS_NARRATIVE_TITLE,
    OID_SSN,
)

runner = CliRunner()

#: A patient stated by two documents at once. Both name the same ``patientRole``
#: id, which is what the parser derives ``patient.id`` from, so the two records
#: are one patient by the only rule anything downstream has.
_PATIENT = "feedface-0000-0000-0000-000000000375"
_OTHER_PATIENT = "feedface-0000-0000-0000-000000000376"

#: The one contact both fixture documents state, named so a test that adds a
#: second number can rewrite the first without repeating the digits.
_HOME_PHONE = '<telecom value="tel:+1(206)555-0177" use="HP"/>'

_STRUCTURED_CCD = """<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <id root="{document}"/>
  <code code="34133-9" displayName="Summarization of Episode Note"
        codeSystem="2.16.840.1.113883.6.1"/>
  <title>Continuity of Care Document</title>
  <effectiveTime value="20230510150000-0500"/>
  <recordTarget>
    <patientRole>
      <id root="{patient}"/>
      {home_phone}
      <patient>
        <name><given>{given}</given><family>{family}</family></name>
        <administrativeGenderCode code="F" displayName="Female"
                                  codeSystem="2.16.840.1.113883.5.1"/>
        <birthTime value="{birth}"/>
      </patient>
    </patientRole>
  </recordTarget>
  <component><structuredBody>
    <component><section>
      <code code="46240-8" codeSystem="2.16.840.1.113883.6.1"/>
      <title>Encounters</title>
      <text><paragraph>{visit}</paragraph></text>
      <entry><encounter classCode="ENC" moodCode="EVN">
        <id root="{encounter}"/>
        <code code="99213" displayName="{visit}"/>
        <effectiveTime><low value="{date}"/></effectiveTime>
      </encounter></entry>
    </section></component>
    <component><section>
      <code code="11450-4" codeSystem="2.16.840.1.113883.6.1"/>
      <title>Problems</title>
      <text><paragraph>{problem}</paragraph></text>
      <entry><act classCode="ACT" moodCode="EVN">
        <id root="{condition}"/>
        <statusCode code="active"/>
        <entryRelationship typeCode="SUBJ">
          <observation classCode="OBS" moodCode="EVN">
            <code code="55607006" codeSystem="2.16.840.1.113883.6.96"/>
            <effectiveTime><low value="{date}"/></effectiveTime>
            <value xsi:type="CD" code="{snomed}" displayName="{problem}"
                   codeSystem="2.16.840.1.113883.6.96"/>
          </observation>
        </entryRelationship>
      </act></entry>
    </section></component>
    {extra}
  </structuredBody></component>
</ClinicalDocument>
"""

#: A loss ledger as ``deliver/ccda_export`` stamps one: the section code, the
#: exporter's own templateId, and the generation counter. Every marker comes from
#: :mod:`anastomosis.core.ccda_codes`, the one place reader and writer share, so
#: this fixture cannot drift into claiming a stamp the parser does not honour.
_LOSS_LEDGER_SECTION = f"""
    <component><section>
      <templateId root="{LOSS_NARRATIVE_TEMPLATE_ROOT}"/>
      <id root="{LOSS_NARRATIVE_GENERATION_ROOT}" extension="{{generation}}"/>
      <code code="{LOINC_EXTENSIONS}" codeSystem="2.16.840.1.113883.6.1"/>
      <title>{LOSS_NARRATIVE_TITLE}</title>
      <text><paragraph>{{entry}}</paragraph></text>
    </section></component>
"""

#: A C-CDA Unstructured Document: the whole chart is one attached file under
#: ``<nonXMLBody>`` and there are no coded sections at all. ``text/plain`` so the
#: fixture can carry a handful of invented bytes rather than pretending a string
#: is a scan.
_SCANNED_CCD = """<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3">
  <id root="{document}"/>
  <code code="34133-9" displayName="Summarization of Episode Note"
        codeSystem="2.16.840.1.113883.6.1"/>
  <title>Scanned Referral</title>
  <effectiveTime value="20230510150000-0500"/>
  <recordTarget>
    <patientRole>
      <id root="{patient}"/>
      {home_phone}
      <patient>
        <name><given>Ada</given><family>Fixture</family></name>
        <birthTime value="19850314"/>
      </patient>
    </patientRole>
  </recordTarget>
  <component><nonXMLBody>
    <text mediaType="text/plain" representation="B64">{body}</text>
  </nonXMLBody></component>
</ClinicalDocument>
"""


def _write_structured(export: Path, name: str, *, patient: str = _PATIENT, **fields: str) -> None:
    """One structured CCD in ``export``, with defaults for what a test ignores."""
    export.mkdir(parents=True, exist_ok=True)
    values = {
        "given": "Ada",
        "family": "Fixture",
        "birth": "19850314",
        "visit": "Office visit",
        "problem": "Asthma",
        "snomed": "195967001",
        "date": "20230510",
        "extra": "",
        "home_phone": _HOME_PHONE,
        **fields,
    }
    (export / name).write_text(_STRUCTURED_CCD.format(patient=patient, **values), encoding="utf-8")


def _write_scanned(export: Path, name: str, *, document: str, body: bytes) -> None:
    """One Unstructured Document in ``export``, carrying ``body`` as its chart."""
    export.mkdir(parents=True, exist_ok=True)
    (export / name).write_text(
        _SCANNED_CCD.format(
            document=document,
            patient=_PATIENT,
            home_phone=_HOME_PHONE,
            body=base64.b64encode(body).decode(),
        ),
        encoding="utf-8",
    )


def _run(export: Path, out: Path, *deliveries: str) -> str:
    """Drive ``anast pipeline run`` over ``export``; the run's output.

    Chromium is not available in the unit lane, so the shared fake renderer
    stands in and writes a real PDF (see ``_render_fakes``). QA is off: what is
    under test is how many charts get written and where, not how they grade.
    """
    result = runner.invoke(
        app,
        [
            "pipeline",
            "run",
            str(export),
            "--source",
            "ccda",
            "--out",
            str(out),
            "--no-qa",
            *deliveries,
        ],
    )
    assert result.exit_code == 0, result.output
    return result.output


def _reported(output: str, label: str) -> int:
    """The patient count the run printed on one delivery line."""
    normalized = " ".join(output.split())
    match = re.search(rf"{label}: (\d+) patients", normalized)
    assert match is not None, f"the run printed no {label} line: {normalized}"
    return int(match.group(1))


def _bundle_attachment_names(bundle_json: Path) -> list[str]:
    """The delivered file name each DocumentReference in a bundle points at."""
    bundle = json.loads(bundle_json.read_text(encoding="utf-8"))
    return [
        json.loads(extension["valueString"])
        for entry in bundle["entry"]
        if entry["resource"]["resourceType"] == "DocumentReference"
        for extension in entry["resource"]["extension"]
        if extension["url"] == "urn:anastomosis:field:path"
    ]


@pytest.fixture(autouse=True)
def _fake_renderer(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("pymupdf", reason="the pipeline lane needs PyMuPDF (render extra)")
    monkeypatch.setattr(chromium, "ChromiumRenderer", FakeChromium)


# --- the defect, both ways it shows ------------------------------------------


def test_two_documents_for_one_patient_deliver_one_ccda(tmp_path: Path) -> None:
    """Two ordinary CCDs for one patient are one C-CDA document on disk, and the
    count the run REPORTS is the count that is there.

    Before the fold the run said two patients and wrote one file: the second
    record's CCD landed on the first through ``write_bytes``, so a physician
    opening the delivered document read one visit and could not tell that the
    other had ever existed.
    """
    export, out, ccda = tmp_path / "export", tmp_path / "charts", tmp_path / "cda"
    _write_structured(
        export,
        "one.xml",
        document="feedface-doc0-0000-0000-000000000001",
        encounter="feedface-enc0-0000-0000-000000000001",
        condition="feedface-cnd0-0000-0000-000000000001",
    )
    _write_structured(
        export,
        "two.xml",
        document="feedface-doc0-0000-0000-000000000002",
        encounter="feedface-enc0-0000-0000-000000000002",
        condition="feedface-cnd0-0000-0000-000000000002",
        visit="Follow up",
        problem="Hypertension",
        snomed="38341003",
        date="20230712",
    )

    output = _run(export, out, "--ccda", str(ccda))

    delivered = sorted(ccda.glob("*.xml"))
    assert len(delivered) == 1, "one patient, one C-CDA document"
    assert _reported(output, "C-CDA") == len(delivered)


def test_both_documents_of_one_patient_keep_their_visits(tmp_path: Path) -> None:
    """The merged chart is BOTH documents, not the first one twice.

    A fold that delivered one file by dropping the second document would pass
    the count test above while losing exactly what the count was protecting.
    """
    export, out, bundles = tmp_path / "export", tmp_path / "charts", tmp_path / "bun"
    _write_structured(
        export,
        "one.xml",
        document="feedface-doc0-0000-0000-000000000001",
        encounter="feedface-enc0-0000-0000-000000000001",
        condition="feedface-cnd0-0000-0000-000000000001",
    )
    _write_structured(
        export,
        "two.xml",
        document="feedface-doc0-0000-0000-000000000002",
        encounter="feedface-enc0-0000-0000-000000000002",
        condition="feedface-cnd0-0000-0000-000000000002",
        visit="Follow up",
        problem="Hypertension",
        snomed="38341003",
        date="20230712",
    )

    _run(export, out, "--bundle", str(bundles))

    (patient_dir,) = [path for path in bundles.iterdir() if path.is_dir()]
    bundle = json.loads((patient_dir / "bundle.json").read_text(encoding="utf-8"))
    resources = [entry["resource"] for entry in bundle["entry"]]
    encounters = [r for r in resources if r["resourceType"] == "Encounter"]
    conditions = [r for r in resources if r["resourceType"] == "Condition"]
    assert len(encounters) == 2, "each document's visit survives the merge"
    assert len(conditions) == 2, "each document's problem survives the merge"


def test_two_scanned_documents_for_one_patient_both_reach_the_chart(tmp_path: Path) -> None:
    """Two Unstructured Documents for one patient: the bundle references both
    scans exactly once, and the archive's attachments directory holds exactly
    those files.

    This is the same defect wearing its other face. The attachments are carried
    on a different path — named per DOCUMENT, so both always landed in the
    charts directory — while the record that names them is what the archive
    keys by patient. One record therefore reached ``bundle.json`` and the other
    did not, and a delivered archive claimed to be complete while holding a
    scan nothing in it referred to.
    """
    export, out, archive = tmp_path / "export", tmp_path / "charts", tmp_path / "arc"
    _write_scanned(
        export, "scan_1.xml", document="feedface-doc0-0000-0000-00000000000a", body=b"referral one"
    )
    _write_scanned(
        export, "scan_2.xml", document="feedface-doc0-0000-0000-00000000000b", body=b"referral two"
    )

    _run(export, out, "--archive", str(archive))

    (patient_dir,) = (archive / "patients").iterdir()
    referenced = _bundle_attachment_names(patient_dir / "bundle.json")
    assert len(referenced) == 2, "both source documents are referenced"
    assert len(set(referenced)) == 2, "and each of them exactly once"
    on_disk = sorted(path.name for path in (patient_dir / "attachments").iterdir())
    assert on_disk == sorted(referenced), "the bundle and the attachments agree"


# --- the counterweight: the fold must not over-merge --------------------------


def test_two_different_patients_still_deliver_two_of_everything(tmp_path: Path) -> None:
    """Two records that are two PATIENTS stay two charts everywhere.

    A fold keyed on anything coarser than the patient id — or one that folded
    unconditionally — would deliver a single merged chart here, which is the
    wrong-patient failure the whole toolkit exists to prevent. The counterweight
    is worth as much as the fix.
    """
    export, out = tmp_path / "export", tmp_path / "charts"
    archive, bundles, ccda = tmp_path / "arc", tmp_path / "bun", tmp_path / "cda"
    _write_structured(
        export,
        "one.xml",
        document="feedface-doc0-0000-0000-000000000001",
        encounter="feedface-enc0-0000-0000-000000000001",
        condition="feedface-cnd0-0000-0000-000000000001",
    )
    _write_structured(
        export,
        "two.xml",
        patient=_OTHER_PATIENT,
        given="Boris",
        family="Specimen",
        birth="19911122",
        document="feedface-doc0-0000-0000-000000000002",
        encounter="feedface-enc0-0000-0000-000000000002",
        condition="feedface-cnd0-0000-0000-000000000002",
    )

    output = _run(
        export,
        out,
        "--archive",
        str(archive),
        "--bundle",
        str(bundles),
        "--ccda",
        str(ccda),
    )

    assert len(list(ccda.glob("*.xml"))) == 2
    assert len(list((archive / "patients").iterdir())) == 2
    assert len([path for path in bundles.iterdir() if path.is_dir()]) == 2
    assert _reported(output, "C-CDA") == 2
    assert _reported(output, "Archive") == 2
    assert _reported(output, "Bundles") == 2


# --- a source that contradicts itself is refused, never reconciled ------------


def test_disagreeing_demographics_under_one_id_refuse_the_run(tmp_path: Path) -> None:
    """Two records for one patient id stating two birth dates are two people as
    far as anything here can tell, and merging them would put a date of birth on
    a chart that half the source contradicts. The run stops at exit 2.

    The message names the FIELD and the counts and nothing else: the values are
    on the operator's screen already, and a refusal that quoted them would put a
    patient's date of birth into a log line and a terminal scrollback.
    """
    export, out = tmp_path / "export", tmp_path / "charts"
    _write_structured(
        export,
        "one.xml",
        document="feedface-doc0-0000-0000-000000000001",
        encounter="feedface-enc0-0000-0000-000000000001",
        condition="feedface-cnd0-0000-0000-000000000001",
        birth="19850314",
    )
    _write_structured(
        export,
        "two.xml",
        document="feedface-doc0-0000-0000-000000000002",
        encounter="feedface-enc0-0000-0000-000000000002",
        condition="feedface-cnd0-0000-0000-000000000002",
        birth="19911122",
    )

    result = runner.invoke(
        app,
        ["pipeline", "run", str(export), "--source", "ccda", "--out", str(out), "--no-qa"],
    )

    assert result.exit_code == 2, result.output
    message = " ".join(result.output.split())
    assert "birth_date" in message
    assert "2 records share one patient id" in message
    for value in ("1985", "1991", "19850314", "19911122", "Ada", "Fixture", "one.xml", "two.xml"):
        assert value not in message, f"the refusal leaked {value!r}"


def test_a_gap_in_one_record_is_filled_from_the_other(tmp_path: Path) -> None:
    """A field one document states and the other leaves empty is not a
    disagreement — it is the half of the chart the other document holds, and the
    merged record keeps it."""
    from anastomosis.pipeline import load_records
    from anastomosis.sources import get_source

    export = tmp_path / "export"
    _write_structured(
        export,
        "one.xml",
        document="feedface-doc0-0000-0000-000000000001",
        encounter="feedface-enc0-0000-0000-000000000001",
        condition="feedface-cnd0-0000-0000-000000000001",
    )
    _write_structured(
        export,
        "two.xml",
        document="feedface-doc0-0000-0000-000000000002",
        encounter="feedface-enc0-0000-0000-000000000002",
        condition="feedface-cnd0-0000-0000-000000000002",
    )
    # Strip the FIRST document's family name, so the only place the merged
    # record can get one is the second — the gap running against document order.
    (export / "one.xml").write_text(
        (export / "one.xml").read_text(encoding="utf-8").replace("<family>Fixture</family>", ""),
        encoding="utf-8",
    )

    (record,) = load_records(get_source("ccda"), export)

    assert record.patient.given_name == "Ada"
    assert record.patient.family_name == "Fixture", "taken from the record that states it"


def test_a_second_phone_number_is_not_a_second_patient(tmp_path: Path) -> None:
    """A demographic that holds SEVERAL values at once cannot contradict itself.

    One document lists the home phone; the other lists the home phone and a
    mobile. That is a patient with two numbers — which is the whole reason the
    field is a list — and the merged record keeps both, each once. Reading it as
    a disagreement would refuse the ordinary export rather than the ambiguous
    one.
    """
    from anastomosis.pipeline import load_records
    from anastomosis.sources import get_source

    export = tmp_path / "export"
    _write_structured(
        export,
        "one.xml",
        document="feedface-doc0-0000-0000-000000000001",
        encounter="feedface-enc0-0000-0000-000000000001",
        condition="feedface-cnd0-0000-0000-000000000001",
    )
    _write_structured(
        export,
        "two.xml",
        document="feedface-doc0-0000-0000-000000000002",
        encounter="feedface-enc0-0000-0000-000000000002",
        condition="feedface-cnd0-0000-0000-000000000002",
    )
    (export / "two.xml").write_text(
        (export / "two.xml")
        .read_text(encoding="utf-8")
        .replace(_HOME_PHONE, _HOME_PHONE + '<telecom value="tel:+1(206)555-0199" use="MC"/>'),
        encoding="utf-8",
    )

    (record,) = load_records(get_source("ccda"), export)

    assert [contact.value for contact in record.patient.telecom] == [
        "(206) 555-0177",
        "(206) 555-0199",
    ], "both numbers, and the one both documents state is not listed twice"


def test_a_document_that_omits_an_identifier_does_not_stop_the_run(tmp_path: Path) -> None:
    """The same rule, on the field that makes it urgent.

    One export repeats the patient's SSN and the next one does not — the
    commonest asymmetry a real pair of documents has. Requiring the identifier
    lists to match would call that two people and write no chart at all; and
    because the parser also derives an identifier from the ``patientRole`` id,
    any pair of documents filed under different assigning authorities would go
    the same way. The merged record keeps every identifier either document
    stated.
    """
    from anastomosis.core.model import IdentifierKind
    from anastomosis.pipeline import load_records
    from anastomosis.sources import get_source

    export = tmp_path / "export"
    _write_structured(
        export,
        "one.xml",
        document="feedface-doc0-0000-0000-000000000001",
        encounter="feedface-enc0-0000-0000-000000000001",
        condition="feedface-cnd0-0000-0000-000000000001",
    )
    _write_structured(
        export,
        "two.xml",
        document="feedface-doc0-0000-0000-000000000002",
        encounter="feedface-enc0-0000-0000-000000000002",
        condition="feedface-cnd0-0000-0000-000000000002",
    )
    # Only the FIRST document carries the social security number.
    (export / "one.xml").write_text(
        (export / "one.xml")
        .read_text(encoding="utf-8")
        .replace(
            f'<id root="{_PATIENT}"/>',
            f'<id root="{OID_SSN}" extension="901-65-4329"/><id root="{_PATIENT}"/>',
        ),
        encoding="utf-8",
    )

    (record,) = load_records(get_source("ccda"), export)

    assert [identifier.kind for identifier in record.patient.identifiers] == [
        IdentifierKind.SSN,
        IdentifierKind.SOURCE_GUID,
    ], "the omitted identifier is a gap in one document, not a contradiction"


# --- nothing the source said is dropped by the merge --------------------------


def test_what_two_documents_say_differently_is_both_kept(tmp_path: Path) -> None:
    """``extensions`` is where every source field with no structured slot is
    parked, so a merge that let one document's key overwrite another's would
    defeat the losslessness guarantee at the one point it exists to hold.

    Both documents' Problems narratives and both document ids survive: the
    second lands at the ``#2`` variant the C-CDA parser already parks a repeated
    section under, so nothing has to learn a second scheme to find it.
    """
    from anastomosis.pipeline import load_records
    from anastomosis.sources import get_source

    export = tmp_path / "export"
    _write_structured(
        export,
        "one.xml",
        document="feedface-doc0-0000-0000-000000000001",
        encounter="feedface-enc0-0000-0000-000000000001",
        condition="feedface-cnd0-0000-0000-000000000001",
    )
    _write_structured(
        export,
        "two.xml",
        document="feedface-doc0-0000-0000-000000000002",
        encounter="feedface-enc0-0000-0000-000000000002",
        condition="feedface-cnd0-0000-0000-000000000002",
        problem="Hypertension",
        snomed="38341003",
    )

    (record,) = load_records(get_source("ccda"), export)

    extensions = record.patient.extensions
    assert extensions["ccda:section:11450-4"]["text"] == "Asthma"
    assert extensions["ccda:section:11450-4#2"]["text"] == "Hypertension"
    assert {extensions["ccda:documentId"], extensions["ccda:documentId#2"]} == {
        "feedface-doc0-0000-0000-000000000001",
        "feedface-doc0-0000-0000-000000000002",
    }


def test_a_single_record_passes_through_the_fold_untouched() -> None:
    """One patient, one record: the fold hands back the object the adapter
    built, not an equal one rebuilt from it.

    Asserted at the seam because nothing downstream can see it: every source
    that already meets the one-record-per-patient contract has to take exactly
    the path it took before this fold existed, and "equal to what it used to
    produce" is a weaker claim than "the same object".
    """
    from anastomosis.core.model import Patient, PatientRecord
    from anastomosis.pipeline import _fold_records_sharing_a_patient

    records = [PatientRecord(patient=Patient(id=_PATIENT, family_name="Fixture"))]

    assert _fold_records_sharing_a_patient(records)[0] is records[0]


def test_the_merged_chart_says_how_many_source_records_it_is(tmp_path: Path) -> None:
    """A merged chart can answer "how many source records am I?".

    It is the only thing left that can: an operator reconciling one delivered
    chart against two documents in the export has nowhere else to look, and the
    count travels inside ``bundle.json`` with the rest of the record's
    extensions. A COUNT and not the file names — a C-CDA export names its files
    after the patient, which is why the adapter names an unreadable document by
    position rather than by name.

    A patient with one document says nothing at all, because nothing was folded:
    a single-document export is the record the adapter built, untouched.
    """
    from anastomosis.pipeline import EXT_FOLDED_RECORDS, load_records
    from anastomosis.sources import get_source

    merged, alone = tmp_path / "merged", tmp_path / "alone"
    for export in (merged, alone):
        _write_structured(
            export,
            "one.xml",
            document="feedface-doc0-0000-0000-000000000001",
            encounter="feedface-enc0-0000-0000-000000000001",
            condition="feedface-cnd0-0000-0000-000000000001",
        )
    _write_structured(
        merged,
        "two.xml",
        document="feedface-doc0-0000-0000-000000000002",
        encounter="feedface-enc0-0000-0000-000000000002",
        condition="feedface-cnd0-0000-0000-000000000002",
    )

    (folded,) = load_records(get_source("ccda"), merged)
    (untouched,) = load_records(get_source("ccda"), alone)

    assert folded.extensions[EXT_FOLDED_RECORDS] == 2
    bookkeeping = json.dumps(folded.extensions, default=str)
    assert "one.xml" not in bookkeeping and "two.xml" not in bookkeeping
    assert EXT_FOLDED_RECORDS not in untouched.extensions


def test_two_carried_loss_ledgers_merge_into_one_ledger(tmp_path: Path) -> None:
    """A carried-forward loss ledger is not an ordinary clashing key.

    It is a previous export generation's account of what could not be carried
    structurally, and the exporter dedupes exactly one of them on the way back
    out. Parking the second at a ``#2`` variant would hide it from that
    carry-forward and grow the document by a whole ledger every round trip — the
    unbounded growth the stamped section was introduced to stop. So the entries
    concatenate into the one key and the higher generation wins.
    """
    from anastomosis.pipeline import load_records
    from anastomosis.sources import get_source

    export = tmp_path / "export"
    _write_structured(
        export,
        "one.xml",
        document="feedface-doc0-0000-0000-000000000001",
        encounter="feedface-enc0-0000-0000-000000000001",
        condition="feedface-cnd0-0000-0000-000000000001",
        extra=_LOSS_LEDGER_SECTION.format(generation="2", entry="patient.notes = first"),
    )
    _write_structured(
        export,
        "two.xml",
        document="feedface-doc0-0000-0000-000000000002",
        encounter="feedface-enc0-0000-0000-000000000002",
        condition="feedface-cnd0-0000-0000-000000000002",
        extra=_LOSS_LEDGER_SECTION.format(generation="5", entry="patient.notes = second"),
    )

    (record,) = load_records(get_source("ccda"), export)

    carried = record.patient.extensions[EXT_PRIOR_LOSS_NARRATIVE]
    assert carried["entries"] == ["patient.notes = first", "patient.notes = second"]
    assert carried["generation"] == 5
    assert f"{EXT_PRIOR_LOSS_NARRATIVE}#2" not in record.patient.extensions


# --- the backstop, once the fold is past --------------------------------------


def test_two_records_under_one_id_that_reach_a_deliverer_collide(tmp_path: Path) -> None:
    """The guard behind the fold, proved by handing each deliverer what the fold
    is there to prevent.

    Every one of these writers is exist_ok/overwrite, so without a witness on
    the claim the second record's chart lands on the first and the run reports
    two. The witness is the record itself, so a re-claim under one id by a
    DIFFERENT record is a refusal — which is what makes a future regression in
    the fold loud instead of silent.
    """
    from anastomosis.core.model import Patient, PatientRecord
    from anastomosis.deliver._shared import DeliveredNameCollision
    from anastomosis.deliver.archive import ArchiveDeliverer
    from anastomosis.deliver.bundle import BundleDeliverer
    from anastomosis.deliver.ccda_export import deliver_ccda

    records = [
        PatientRecord(patient=Patient(id=_PATIENT, given_name="Ada", family_name="Fixture")),
        PatientRecord(
            patient=Patient(id=_PATIENT, given_name="Ada", family_name="Fixture", sex="Female")
        ),
    ]

    with pytest.raises(DeliveredNameCollision, match="C-CDA document"):
        deliver_ccda(records, tmp_path / "cda")
    with pytest.raises(DeliveredNameCollision, match="patient directory"):
        ArchiveDeliverer().deliver(records, None, tmp_path / "arc")
    with pytest.raises(DeliveredNameCollision, match="patient directory"):
        BundleDeliverer().deliver_records(records, None, tmp_path / "bun")


def test_the_collision_names_the_shared_id_as_a_surrogate_only(tmp_path: Path) -> None:
    """The refusal says which KIND collided and correlates the two claims by a
    run-scoped surrogate. A patient id is a source identifier, so the raw one
    never appears — the same rule every log line in this toolkit keeps."""
    from anastomosis.core.model import Patient, PatientRecord
    from anastomosis.deliver._shared import DeliveredNameCollision
    from anastomosis.deliver.ccda_export import deliver_ccda

    records = [
        PatientRecord(patient=Patient(id=_PATIENT, family_name="Fixture")),
        PatientRecord(patient=Patient(id=_PATIENT, family_name="Fixture", sex="Female")),
    ]

    with pytest.raises(DeliveredNameCollision) as caught:
        deliver_ccda(records, tmp_path / "cda")

    message = str(caught.value)
    assert "carry the same source id" in message
    assert _PATIENT not in message
    assert "Fixture" not in message


def test_one_record_delivered_into_its_own_slot_is_not_a_collision(tmp_path: Path) -> None:
    """The witness must not turn an ordinary delivery into a failure: one record
    claims its slot once, and claiming it again with the same record is the
    no-op the ledger has always allowed."""
    from anastomosis.core.model import Patient, PatientRecord
    from anastomosis.deliver.ccda_export import deliver_ccda

    record = PatientRecord(patient=Patient(id=_PATIENT, given_name="Ada", family_name="Fixture"))

    result = deliver_ccda([record, record], tmp_path / "cda")

    assert result.missing_count == 0
    assert len(list((tmp_path / "cda").glob("*.xml"))) == 1
