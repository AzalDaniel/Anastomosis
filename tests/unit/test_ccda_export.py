"""C-CDA export tests — the round trip IS the deliverable.

``parse(build_ccd(record)) ≈ record`` through this repo's OWN
``sources/ccda`` parser: a rich synthetic record is exported to CCD XML,
re-ingested, and asserted equivalent section by section on the canonical
fields the parser produces. Where exact equality is impossible (vendor
extension namespaces, the SOAP-section split), the test asserts the
DOCUMENTED loss exactly — nothing undeclared may vanish.

All data is synthetic: feedface- ids, 555-exchange phones, SSN area >= 900,
example.com email.
"""

from __future__ import annotations

import logging
import os
import re
import stat
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from _render_fakes import write_text_pdf
from lxml import etree
from typer.testing import CliRunner

import anastomosis.sources.ccda  # noqa: F401 — registers the adapter
from anastomosis.cli import app
from anastomosis.core.ccda_codes import first_rooted_id, organizer_component_source_id
from anastomosis.core.model import (
    Addendum,
    AdvanceDirective,
    AllergyCategory,
    AllergyIntolerance,
    Condition,
    ContactKind,
    ContactPoint,
    Coverage,
    DocumentArtifact,
    Encounter,
    Facility,
    FamilyMemberHistory,
    Goal,
    Guarantor,
    Identifier,
    IdentifierKind,
    Immunization,
    MedicationStatement,
    NoteSection,
    Observation,
    ObservationCategory,
    PastMedicalHistory,
    Patient,
    PatientContact,
    PatientRecord,
    Practitioner,
    Prescription,
    PrescriptionTransaction,
    Provenance,
    ScreeningEvent,
    SectionKind,
)
from anastomosis.core.model.patient import Address
from anastomosis.deliver.ccda_export import DECLARED_LOSSES, build_ccd, deliver_ccda
from anastomosis.deliver.ccda_export.builder import (
    LOINC_EXTENSIONS,
    LOSS_NARRATIVE_GENERATION_ROOT,
    LOSS_NARRATIVE_TEMPLATE_ROOT,
    LOSS_NARRATIVE_TITLE,
    _carried_forward,
    _entry_key,
    _stated_ids,
    measure_ccd,
)
from anastomosis.sources.ccda.parser import EXT_PRIOR_LOSS_NARRATIVE, _measurements, parse_document

V3 = "urn:hl7-org:v3"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
# The same hardened parser settings the repo's ingest uses.
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)

runner = CliRunner()

PF_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"
# A wall-clock instant with a fixed offset (proves tz survives the TS round trip).
_AT = datetime(2023, 5, 10, 14, 0, 0, tzinfo=timezone(timedelta(hours=-5)))


def _loss_entries(record: PatientRecord) -> list[str]:
    """The 51899-3 loss-ledger entries recovered from a re-ingest.

    The exporter STAMPS its own loss section, so the parser hands its entries
    back discretely under ``ccda:prior_loss_narrative`` — one per emitted
    paragraph, no re-splitting heuristic needed. A third party's 51899-3 section
    is not stamped and lands under ``ccda:section:51899-3`` instead.
    """
    prior = record.patient.extensions.get(EXT_PRIOR_LOSS_NARRATIVE)
    if prior is None:
        return []
    entries: list[str] = list(prior["entries"])
    return entries


def _loss_text(record: PatientRecord) -> str:
    """The recovered loss ledger as one blob, for substring assertions."""
    return " ".join(_loss_entries(record))


def _loss_sections(document: bytes) -> list[etree._Element]:
    """Every 51899-3 section in an exported document — stamped or not. The
    export contract is that there is exactly ONE, however many generations of
    export → ingest → export the chart has been through."""
    root = etree.fromstring(document, _PARSER)
    return [
        section
        for section in root.iter(f"{{{V3}}}section")
        if (code := section.find(f"{{{V3}}}code")) is not None
        and code.get("code") == LOINC_EXTENSIONS
    ]


def _rich_record() -> PatientRecord:
    """A synthetic record exercising every section the parser reads.

    Built so that fields equal what the parser PRODUCES on re-ingest:
    GUID-shaped ids (round-trip exactly), structured encounters with
    note_type == encounter_type (the parser sets both from the encounter code),
    and ``ccda:*`` extensions that survive natively.
    """
    pid = "feedface-pat0-0000-0000-000000000001"
    patient = Patient(
        given_name="Cora",
        middle_name="Lee",
        family_name="Specimen",
        suffix="Jr",
        birth_date=date(1979, 4, 6),
        sex="Female",
        marital_status="Married",
        race=["Asian"],
        ethnicity=["Not Hispanic or Latino"],
        language="en",
        identifiers=[
            Identifier(kind=IdentifierKind.SSN, value="901-65-4329"),
            Identifier(
                kind=IdentifierKind.SOURCE_GUID,
                value=pid,
                system="2.16.840.1.113883.19.5",
            ),
        ],
        telecom=[
            ContactPoint(kind=ContactKind.PHONE_HOME, value="(206) 555-0177"),
            ContactPoint(kind=ContactKind.EMAIL, value="cora.specimen@example.com"),
        ],
        addresses=[
            Address(line1="456 Sample Way", city="Springfield", state="WA", postal_code="98102")
        ],
    )
    conditions = [
        Condition(
            patient_id=pid,
            snomed="38341003",
            icd10="I10",
            display="Hypertensive disorder",
            onset=date(2021, 2, 15),
            active=True,
        ),
        Condition(
            patient_id=pid,
            snomed="37796009",
            display="Migraine",
            onset=date(2018, 3, 1),
            stopped=date(2020, 9, 1),
            active=False,
        ),
    ]
    allergies = [
        AllergyIntolerance(
            patient_id=pid,
            substance="Penicillin G",
            category=AllergyCategory.DRUG,
            reactions=["Hives"],
            severity="Moderate",
            onset=date(2019, 6, 20),
            extensions={"ccda:allergen_code": "7980"},
        ),
        AllergyIntolerance(
            patient_id=pid,
            substance="Peanut",
            category=AllergyCategory.FOOD,
            reactions=["Anaphylaxis"],
            severity="Severe",
            onset=date(2015, 8, 12),
            extensions={"ccda:allergen_code": "256349002"},
        ),
    ]
    medications = [
        MedicationStatement(
            patient_id=pid,
            display_name="Lisinopril 10 MG Oral Tablet",
            rxnorm="314076",
            start=date(2023, 1, 1),
            active=True,
            extensions={"ccda:dose": "1 {tablet}", "ccda:route": "Oral"},
        ),
        MedicationStatement(
            patient_id=pid,
            display_name="Amoxicillin 500 MG Oral Capsule",
            rxnorm="308182",
            start=date(2022, 3, 1),
            stop=date(2022, 3, 14),
            active=False,
            extensions={"ccda:dose": "1 {capsule}", "ccda:route": "Oral"},
        ),
    ]
    immunizations = [
        Immunization(
            patient_id=pid,
            vaccine="Influenza, seasonal, injectable, preservative free",
            administered_on=date(2022, 10, 3),
            lot_number="FLU2022A",
        ),
        Immunization(
            patient_id=pid,
            vaccine="MMR",
            administered_on=date(2022, 10, 3),
            comment="Refused",
            extensions={"ccda:negationInd": "true"},
        ),
    ]
    vitals = [
        Observation(
            patient_id=pid,
            category=ObservationCategory.VITAL_SIGNS,
            code=c,
            display=dn,
            value=v,
            unit=u,
            effective_at=_AT,
        )
        for c, dn, v, u in (
            ("8480-6", "Systolic blood pressure", "122", "mm[Hg]"),
            ("8462-4", "Diastolic blood pressure", "78", "mm[Hg]"),
            ("8867-4", "Heart rate", "70", "/min"),
            ("29463-7", "Body weight", "64", "kg"),
            ("8302-2", "Body height", "170", "cm"),
            ("39156-5", "Body mass index", "22.1", "kg/m2"),  # BMI
        )
    ]
    labs = [
        Observation(
            patient_id=pid,
            category=ObservationCategory.LABORATORY,
            code=c,
            display=dn,
            value=v,
            unit="mg/dL",
            effective_at=_AT,
        )
        for c, dn, v in (("2345-7", "Glucose", "92"), ("2160-0", "Creatinine", "0.9"))
    ]
    social = [
        Observation(
            patient_id=pid,
            category=ObservationCategory.SOCIAL_HISTORY,
            display="Tobacco use",
            value="Never smoker",
            effective_at=_AT,
        )
    ]
    # A structured encounter: the parser sets note_type == encounter_type.
    office = Encounter(
        id="feedface-0000-0000-0000-00000000e001",
        patient_id=pid,
        date_of_service=date(2023, 5, 10),
        encounter_type="Office outpatient visit 15 minutes",
        note_type="Office outpatient visit 15 minutes",
    )
    # A SOAP note encounter (no encounter_type → lives only in the Notes section).
    soap = Encounter(
        id="feedface-0000-0000-0000-00000000d001",
        patient_id=pid,
        date_of_service=date(2023, 5, 10),
        note_type="Progress note",
        sections=[
            NoteSection(
                kind=SectionKind.SUBJECTIVE,
                title="Subjective",
                text="Patient returns for routine blood pressure follow-up.",
            ),
            NoteSection(kind=SectionKind.OBJECTIVE, title="Objective", text="Lungs clear."),
            NoteSection(
                kind=SectionKind.ASSESSMENT, title="Assessment", text="Hypertension, controlled."
            ),
            NoteSection(kind=SectionKind.PLAN, title="Plan", text="Continue lisinopril."),
        ],
    )
    return PatientRecord(
        patient=patient,
        conditions=conditions,
        allergies=allergies,
        medications=medications,
        immunizations=immunizations,
        observations=[*vitals, *labs, *social],
        encounters=[office, soap],
    )


@pytest.fixture(scope="module")
def source() -> PatientRecord:
    return _rich_record()


@pytest.fixture(scope="module")
def reingested(source: PatientRecord, tmp_path_factory: pytest.TempPathFactory) -> PatientRecord:
    out = tmp_path_factory.mktemp("ccda") / "doc.xml"
    out.write_bytes(build_ccd(source))
    return parse_document(out)


# --- the round trip, section by section --------------------------------------


def test_demographics_round_trip(source: PatientRecord, reingested: PatientRecord) -> None:
    p, q = source.patient, reingested.patient
    assert q.display_name == p.display_name  # given + middle + family + suffix
    assert q.birth_date == p.birth_date
    assert q.sex == p.sex
    assert q.race == p.race
    assert q.ethnicity == p.ethnicity
    assert q.language == p.language
    assert q.marital_status == p.marital_status
    assert q.identifier(IdentifierKind.SSN) == "901-65-4329"
    phones = {t.kind.value: t.value for t in q.telecom}
    assert phones == {"phone_home": "(206) 555-0177", "email": "cora.specimen@example.com"}
    assert q.addresses[0].model_dump() == p.addresses[0].model_dump()


def test_conditions_round_trip(reingested: PatientRecord) -> None:
    by_snomed = {c.snomed: c for c in reingested.conditions}
    htn = by_snomed["38341003"]
    assert htn.icd10 == "I10"
    assert htn.active is True
    assert htn.display == "Hypertensive disorder"
    assert htn.onset == date(2021, 2, 15)
    assert htn.stopped is None
    migraine = by_snomed["37796009"]
    assert migraine.active is False
    assert migraine.stopped == date(2020, 9, 1)
    assert migraine.icd10 is None


def test_allergies_round_trip(reingested: PatientRecord) -> None:
    by_substance = {a.substance: a for a in reingested.allergies}
    pen = by_substance["Penicillin G"]
    assert pen.category == AllergyCategory.DRUG
    assert pen.reactions == ["Hives"]
    assert pen.severity == "Moderate"
    assert pen.onset == date(2019, 6, 20)
    assert pen.extensions["ccda:allergen_code"] == "7980"
    peanut = by_substance["Peanut"]
    assert peanut.category == AllergyCategory.FOOD
    assert peanut.reactions == ["Anaphylaxis"]
    assert peanut.severity == "Severe"
    assert peanut.extensions["ccda:allergen_code"] == "256349002"


def test_medications_round_trip(reingested: PatientRecord) -> None:
    by_rxnorm = {m.rxnorm: m for m in reingested.medications}
    lis = by_rxnorm["314076"]
    assert lis.active is True
    assert lis.start == date(2023, 1, 1)
    assert lis.stop is None  # high nullFlavor=UNK → None
    assert lis.extensions["ccda:route"] == "Oral"
    assert lis.extensions["ccda:dose"] == "1 {tablet}"
    assert lis.display_name == "Lisinopril 10 MG Oral Tablet"
    amox = by_rxnorm["308182"]
    assert amox.active is False
    assert amox.stop == date(2022, 3, 14)


def test_immunizations_round_trip(reingested: PatientRecord) -> None:
    flu = next(i for i in reingested.immunizations if "Influenza" in (i.vaccine or ""))
    assert flu.administered_on == date(2022, 10, 3)
    assert flu.lot_number == "FLU2022A"
    assert flu.comment is None
    refused = next(i for i in reingested.immunizations if i.comment == "Refused")
    assert refused.extensions["ccda:negationInd"] == "true"
    assert refused.vaccine == "MMR"


def test_vitals_round_trip_including_bmi(reingested: PatientRecord) -> None:
    vitals = {
        o.code: o for o in reingested.observations if o.category == ObservationCategory.VITAL_SIGNS
    }
    assert vitals["8480-6"].value == "122"
    assert vitals["8480-6"].unit == "mm[Hg]"
    bmi = vitals["39156-5"]
    assert bmi.value == "22.1"
    assert bmi.unit == "kg/m2"
    assert bmi.display == "Body mass index"
    # tz survives the TS round trip (14:00 -0500 → 19:00 UTC).
    eff = vitals["8480-6"].effective_at
    assert eff is not None
    assert eff.utcoffset() == timedelta(hours=-5)


def test_results_round_trip(reingested: PatientRecord) -> None:
    labs = {
        o.code: o for o in reingested.observations if o.category == ObservationCategory.LABORATORY
    }
    assert labs["2345-7"].value == "92"
    assert labs["2345-7"].unit == "mg/dL"
    assert labs["2160-0"].value == "0.9"


def test_nontobacco_social_obs_never_reingest_as_tobacco(tmp_path: Path) -> None:
    # BLOCKER 1 regression: Occupation/Industry social observations (code is None,
    # display != "Tobacco use") must NEVER be stamped with the smoking-status
    # code on export — that would relabel a charted value into a clinically false
    # tobacco statement. They round-trip with ZERO tobacco-labeled observations,
    # and their values survive in the loss narrative instead.
    pid = "feedface-pat0-0000-0000-00000000b001"
    rec = PatientRecord(
        patient=Patient(given_name="Soc", family_name="Hist", id=pid),
        observations=[
            Observation(
                patient_id=pid,
                category=ObservationCategory.SOCIAL_HISTORY,
                display="Occupation",
                value="Carpenter",
                effective_at=_AT,
            ),
            Observation(
                patient_id=pid,
                category=ObservationCategory.SOCIAL_HISTORY,
                display="Industry",
                value="Construction",
                effective_at=_AT,
            ),
        ],
    )
    out = tmp_path / "soc.xml"
    out.write_bytes(build_ccd(rec))
    rt = parse_document(out)
    social = [o for o in rt.observations if o.category == ObservationCategory.SOCIAL_HISTORY]
    # The parser only recovers tobacco (72166-2) social obs; a corrupted export
    # would surface "Carpenter"/"Construction" as Tobacco use. None must appear.
    assert social == [], f"non-tobacco social obs leaked as structured: {social}"
    assert not any(o.display == "Tobacco use" for o in rt.observations)
    # The charted values are preserved — in the loss narrative, not as tobacco.
    text = _loss_text(rt)
    assert "Carpenter" in text
    assert "Construction" in text
    assert "Tobacco" not in text


def test_tobacco_and_nontobacco_social_obs_split_correctly(tmp_path: Path) -> None:
    # A mix: the tobacco observation round-trips structurally; the occupation one
    # rides the narrative. The single structured social entry is the tobacco one.
    pid = "feedface-pat0-0000-0000-00000000b002"
    rec = PatientRecord(
        patient=Patient(given_name="Mix", family_name="Soc", id=pid),
        observations=[
            Observation(
                patient_id=pid,
                category=ObservationCategory.SOCIAL_HISTORY,
                display="Tobacco use",
                value="Former smoker",
                effective_at=_AT,
            ),
            Observation(
                patient_id=pid,
                category=ObservationCategory.SOCIAL_HISTORY,
                display="Occupation",
                value="Welder",
                effective_at=_AT,
            ),
        ],
    )
    out = tmp_path / "mix.xml"
    out.write_bytes(build_ccd(rec))
    rt = parse_document(out)
    social = [o for o in rt.observations if o.category == ObservationCategory.SOCIAL_HISTORY]
    assert len(social) == 1
    assert social[0].display == "Tobacco use"
    assert social[0].value == "Former smoker"
    assert "Welder" in _loss_text(rt)


def test_social_history_round_trip(reingested: PatientRecord) -> None:
    social = [
        o for o in reingested.observations if o.category == ObservationCategory.SOCIAL_HISTORY
    ]
    smoking = next(o for o in social if o.display == "Tobacco use")
    assert smoking.value == "Never smoker"


def test_encounters_round_trip(reingested: PatientRecord) -> None:
    office = next(
        e for e in reingested.encounters if (e.encounter_type or "").startswith("Office outpatient")
    )
    assert office.date_of_service == date(2023, 5, 10)
    # GUID-shaped ids round-trip exactly (the parser's _GUID_RE accepts them).
    assert office.id == "feedface-0000-0000-0000-00000000e001"


def test_soap_note_round_trips_as_one_narrative(reingested: PatientRecord) -> None:
    # DECLARED LOSS: the SOAP kind split does not survive — it comes back as a
    # single narrative section with the section labels inline.
    note = next(e for e in reingested.encounters if any(s.kind for s in e.sections))
    assert note.date_of_service == date(2023, 5, 10)
    assert [s.kind for s in note.sections] == [SectionKind.NARRATIVE]
    text = note.sections[0].text or ""
    assert "routine blood pressure follow-up" in text
    # All four SOAP bodies preserved (labelled), even though the kinds collapsed.
    for label in ("SUBJECTIVE", "OBJECTIVE", "ASSESSMENT", "PLAN"):
        assert label in text


# --- declared losses ---------------------------------------------------------


def test_nonnative_extensions_land_in_declared_loss_section(tmp_path: Path) -> None:
    # A vendor extension namespace with no structured CDA slot must NOT vanish:
    # it lands in the 51899-3 narrative (the only DECLARED home), recovered as
    # a section extension — never silently dropped, never back on its model.
    rec = PatientRecord(
        patient=Patient(
            given_name="Ven", family_name="Dor", extensions={"pf_tebra:PatientStatusCode": "A"}
        ),
        medications=[
            MedicationStatement(
                patient_id="x",
                display_name="Metformin",
                rxnorm="6809",
                extensions={"ccda:route": "Oral", "pf_tebra:RxControlled": "no"},
            )
        ],
    )
    out = tmp_path / "v.xml"
    out.write_bytes(build_ccd(rec))
    rt = parse_document(out)
    text = _loss_text(rt)
    assert "pf_tebra:PatientStatusCode = A" in text
    assert "pf_tebra:RxControlled = no" in text
    # ccda:route is a NATIVE round-trip; it stays on the model, not the loss section.
    assert rt.medications[0].extensions["ccda:route"] == "Oral"
    assert "ccda:route" not in text


def test_populated_native_fields_land_in_loss_narrative(tmp_path: Path) -> None:
    # BLOCKER 2: populated NATIVE canonical fields with no CDA slot must not
    # vanish silently. A representative sweep across models — patient demographics
    # extras, an Encounter addendum (signed-note amendment = clinical narrative),
    # an Immunization expiry, a Condition acuity, a Medication strength — plus a
    # record-level list the parser cannot produce (a Coverage) must all appear in
    # the recovered 51899-3 loss narrative.
    pid = "feedface-pat0-0000-0000-00000000c001"
    rec = PatientRecord(
        patient=Patient(
            id=pid,
            given_name="Lossy",
            family_name="Native",
            gender_identity="Genderqueer",
            mothers_maiden_name="Riverstone",
            notes="Prefers afternoon appointments.",
        ),
        conditions=[Condition(patient_id=pid, display="Asthma", acuity="chronic", active=True)],
        medications=[
            MedicationStatement(
                patient_id=pid,
                display_name="Albuterol",
                rxnorm="435",
                strength="90 mcg",
                sig="2 puffs as needed",
            )
        ],
        immunizations=[
            Immunization(
                patient_id=pid,
                vaccine="Tdap",
                administered_on=date(2021, 5, 1),
                expires=date(2031, 5, 1),
                source="State registry",
            )
        ],
        encounters=[
            Encounter(
                id="feedface-0000-0000-0000-00000000c0e1",
                patient_id=pid,
                date_of_service=date(2023, 1, 2),
                encounter_type="Office visit",
                note_type="Office visit",
                chief_complaint="Wheezing",
                addenda=[Addendum(text="corrected dosage to 20mg", status="Accepted")],
            )
        ],
        coverages=[Coverage(patient_id=pid, payer="Acme Health", member_id="MEM12345")],
    )
    out = tmp_path / "loss.xml"
    out.write_bytes(build_ccd(rec))
    text = _loss_text(parse_document(out))
    for expected in (
        "Genderqueer",
        "Riverstone",
        "Prefers afternoon appointments.",
        "chronic",  # Condition.acuity
        "90 mcg",  # Medication.strength
        "2 puffs as needed",  # Medication.sig
        "2031-05-01",  # Immunization.expires (ISO date)
        "State registry",  # Immunization.source
        "Wheezing",  # Encounter.chief_complaint
        "corrected dosage to 20mg",  # Encounter.addenda[0].text — signed-note amendment
        "Accepted",  # Encounter.addenda[0].status
        "Acme Health",  # Coverage.payer (record-level list the parser cannot produce)
        "MEM12345",  # Coverage.member_id
    ):
        assert expected in text, f"populated native field vanished: {expected!r}"


def test_addenda_path_line_shape(tmp_path: Path) -> None:
    # The loss narrative uses deterministic path = value lines, e.g.
    # encounters[0].addenda[0].text = ... — proves the path format is emitted.
    #
    # Positional at BOTH levels. The outer subscript used to be the object's own
    # id, which for a model the adapter mints is a fresh uuid4 per load — so the
    # document was not reproducible between two ingests of one export. The inner
    # subscript was already positional; these now agree.
    pid = "feedface-pat0-0000-0000-00000000c002"
    enc_id = "feedface-0000-0000-0000-00000000c0e2"
    rec = PatientRecord(
        patient=Patient(id=pid, given_name="Path", family_name="Shape"),
        encounters=[
            Encounter(
                id=enc_id,
                patient_id=pid,
                encounter_type="Visit",
                note_type="Visit",
                addenda=[Addendum(text="amended note body")],
            )
        ],
    )
    out = tmp_path / "path.xml"
    out.write_bytes(build_ccd(rec))
    entries = _loss_entries(parse_document(out))
    assert "encounters[0].addenda[0].text = amended note body" in entries
    assert enc_id not in " ".join(entries), "a canonical id must not subscript a ledger path"


# A source CCD whose Problems entry is NOT the shape `_conditions` parses, so
# the section's <text> is the only surviving copy of the clinical statement —
# and a header whose id/title differ from the ones this exporter writes.
_UNPARSABLE_ENTRY_CCD = """<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3">
  <id root="feedface-0000-0000-0000-00000000cda2"/>
  <title>Unsupported-entry CCD</title>
  <recordTarget><patientRole>
    <id root="feedface-0000-0000-0000-000000000001"/>
    <patient><name><given>Ada</given><family>Fixture</family></name></patient>
  </patientRole></recordTarget>
  <component><structuredBody>
    <component><section>
      <code code="11450-4" codeSystem="2.16.840.1.113883.6.1"/>
      <title>Problems</title>
      <text>SENTINEL-RESCUED-NARRATIVE (active, onset 2021-02-15)</text>
      <entry><observation classCode="OBS" moodCode="EVN">
        <value code="38341003" codeSystem="2.16.840.1.113883.6.96"/>
      </observation></entry>
    </section></component>
  </structuredBody></component>
</ClinicalDocument>
"""


def test_ingested_section_narrative_survives_the_export_round_trip(tmp_path: Path) -> None:
    """A narrative the C-CDA parser rescued from an entry it could not take
    apart is the ONLY copy of that clinical statement, and this exporter emits
    no section narratives of its own — so ``ccda:section:*`` must ride the loss
    narrative like any other extension key, and come back on re-ingest."""
    source_doc = tmp_path / "in.xml"
    source_doc.write_text(_UNPARSABLE_ENTRY_CCD, encoding="utf-8")
    ingested = parse_document(source_doc)
    assert ingested.conditions == []  # nothing structural survived the entry shape
    problems = ingested.patient.extensions["ccda:section:11450-4"]
    assert "SENTINEL-RESCUED-NARRATIVE" in problems["text"]

    exported = tmp_path / "out.xml"
    exported.write_bytes(build_ccd(ingested))
    assert b"SENTINEL-RESCUED-NARRATIVE" in exported.read_bytes()
    text = _loss_text(parse_document(exported))
    assert "patient.extensions.ccda:section:11450-4.text = SENTINEL-RESCUED-NARRATIVE" in text


def test_source_document_metadata_rides_the_loss_narrative(tmp_path: Path) -> None:
    """The header this exporter writes carries its own title and its own
    deterministic id/effectiveTime, so the SOURCE document's metadata is not
    re-derived on re-ingest — it narrates instead of vanishing."""
    source_doc = tmp_path / "meta_in.xml"
    source_doc.write_text(_UNPARSABLE_ENTRY_CCD, encoding="utf-8")
    ingested = parse_document(source_doc)
    exported = tmp_path / "meta_out.xml"
    exported.write_bytes(build_ccd(ingested))
    reingested = parse_document(exported)
    text = _loss_text(reingested)
    assert "patient.extensions.ccda:documentId = feedface-0000-0000-0000-00000000cda2" in text
    assert "patient.extensions.ccda:title = Unsupported-entry CCD" in text
    # The re-derived header keys are the EXPORTER's, not the source's — which is
    # exactly why the source values had to be narrated.
    assert reingested.patient.extensions["ccda:title"] == "Continuity of Care Document"


# --- preserved entries: delivered as entries, never narrated -----------------
#
# A C-CDA ingest parks every section's entries verbatim under
# `ccda:entries:<code>`, because prose about a section is not a copy of the
# entries beneath it. This exporter re-emits those bytes as the entries of the
# section carrying that code. Narrating them instead would serialise XML into
# `path = value` lines no emitter consumes, so the next generation would park
# and re-narrate them — measured at ~15 KB per round trip, without bound.


def _entries_under(document: bytes, code: str | None) -> list[etree._Element]:
    """Every ``<entry>`` of every section carrying ``code`` (``None`` for a
    section that states no code at all)."""
    root = etree.fromstring(document, _PARSER)
    out: list[etree._Element] = []
    for section in root.iter(f"{{{V3}}}section"):
        node = section.find(f"{{{V3}}}code")
        spelled = None if node is None else node.get("code")
        if spelled == code:
            out += section.findall(f"{{{V3}}}entry")
    return out


def _parked(record: PatientRecord) -> dict[str, list[str]]:
    """The verbatim entries a record carries, by section code."""
    prefix = "ccda:entries:"
    out: dict[str, list[str]] = {}
    for key, value in record.patient.extensions.items():
        if key.startswith(prefix):
            out.setdefault(key[len(prefix) :].partition("#")[0], []).extend(value)
    return out


def _shape(entry: str) -> object:
    """One entry as the element it is: tags, attributes and non-blank text.

    Re-emitting an entry into a document that declares more namespaces than its
    source did, and through a pretty-printer, can change the STRING without
    changing anything the element says: it gains an unused ``xmlns:sdtc``, and a
    closing tag written flush against its child gains the indentation the
    printer would have given it. Whether the BYTES survive is asked separately,
    of documents that already carry both — which every C-CDA this repository
    ships does.
    """

    def shape(node: etree._Element) -> object:
        return (
            node.tag,
            sorted(node.attrib.items()),
            (node.text or "").strip(),
            [shape(child) for child in node],
        )

    return shape(etree.fromstring(entry.encode(), _PARSER))


def _shapes(record: PatientRecord) -> dict[str, list[object]]:
    return {code: [_shape(e) for e in entries] for code, entries in _parked(record).items()}


_PROSE_AND_ENTRY_CCD = """<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3">
  <id root="feedface-0000-0000-0000-00000000cda5"/>
  <title>Prose-and-entry CCD</title>
  <recordTarget><patientRole>
    <id root="feedface-0000-0000-0000-000000000005"/>
    <patient><name><given>Pia</given><family>Prose</family></name></patient>
  </patientRole></recordTarget>
  <component><structuredBody>
    <component><section>
      <code code="47519-4" codeSystem="2.16.840.1.113883.6.1"/>
      <title>Procedures</title>
      <text>PROSE-THAT-STATES-NOTHING-OF-THE-ENTRY</text>
      <entry><procedure classCode="PROC" moodCode="EVN">
        <id root="feedface-proc-0000-0000-000000000905"/>
        <code code="430193006" displayName="SENTINEL-PROCEDURE"/>
      </procedure></entry>
    </section></component>
  </structuredBody></component>
</ClinicalDocument>
"""


def test_an_entry_under_prose_leaves_as_an_entry_and_is_not_narrated(tmp_path: Path) -> None:
    """The defect this closes, from the export side.

    A coded procedure under a section this exporter has no emitter for used to
    reach the document only as narrative — and only if its section rendered no
    text. It leaves as the entry it arrived as now, and the loss ledger does not
    restate it: a ledger line holding a whole XML entry is a line the next
    generation parks and narrates again.
    """
    source_doc = tmp_path / "prose_in.xml"
    source_doc.write_text(_PROSE_AND_ENTRY_CCD, encoding="utf-8")
    ingested = parse_document(source_doc)
    assert _parked(ingested)["47519-4"], "the parser parked nothing to deliver"

    document = build_ccd(ingested)
    entries = _entries_under(document, "47519-4")
    assert len(entries) == 1
    assert entries[0].find(f"{{{V3}}}procedure/{{{V3}}}code").get("displayName") == (
        "SENTINEL-PROCEDURE"
    )

    exported = tmp_path / "prose_out.xml"
    exported.write_bytes(document)
    text = _loss_text(parse_document(exported))
    assert "ccda:entries" not in text, "the entries were narrated as well as delivered"
    assert "SENTINEL-PROCEDURE" not in text
    # The section's own prose is a different key and still narrates.
    assert "PROSE-THAT-STATES-NOTHING-OF-THE-ENTRY" in text


@pytest.mark.parametrize(
    "fixture",
    [
        Path("ccda") / "feedface_ccd.xml",
        Path("synthea") / "synthea_ccda_sample.xml",
        Path("ccda_edge_cases") / "feedface_ccd_duplicate_encounter_id.xml",
    ],
    ids=lambda path: path.name,
)
def test_a_preserved_entry_comes_back_as_the_same_bytes(fixture: Path, tmp_path: Path) -> None:
    """What went in comes back out: the same strings, the same count.

    Asked of every C-CDA this repository ships, because these are the documents
    with a real header — an entry re-emitted into one carries exactly the
    namespace declarations its source did, so the byte question is answerable at
    all. An entry delivered twice would show here as two, and one reformatted on
    the way through as a different string.
    """
    source_doc = Path(__file__).resolve().parents[1] / "fixtures" / fixture
    ingested = parse_document(source_doc)
    assert _parked(ingested), "the parser parked nothing to deliver"

    exported = tmp_path / "bytes_out.xml"
    exported.write_bytes(build_ccd(ingested))
    assert _parked(parse_document(exported)) == _parked(ingested)


def test_a_re_emitted_entry_says_the_same_thing_and_then_stops_moving(tmp_path: Path) -> None:
    """A source that declares fewer namespaces than this exporter writes.

    Its entry says exactly what it said — same element, same attributes — but
    the string gains the declarations the export root carries and the indent the
    printer gives a flush closing tag. That is a one-generation settling, not a
    drift: generation 2 and generation 3 are the same bytes, which is what keeps
    the round trip a fixed point rather than a slow leak.
    """
    source_doc = tmp_path / "settle_in.xml"
    source_doc.write_text(_PROSE_AND_ENTRY_CCD, encoding="utf-8")
    ingested = parse_document(source_doc)

    out = tmp_path / "settle_out.xml"
    generations = []
    for _ in range(3):
        out.write_bytes(build_ccd(ingested))
        ingested = parse_document(out)
        generations.append(_parked(ingested))

    assert [_shape(e) for e in generations[0]["47519-4"]] == [
        _shape(e) for e in _parked(parse_document(source_doc))["47519-4"]
    ]
    assert generations[1] == generations[2], "the preserved bytes never settle"


def test_a_code_the_builder_emits_no_section_for_still_delivers_its_entries(
    tmp_path: Path,
) -> None:
    """47519-4 has no structured emitter here, and the entries arrive anyway.

    The decision this pins: a carrier section, not a refusal. A code with no
    emitter is the ordinary case, and an export that refused the chart would
    refuse the common path. The carrier states the code and nothing the record
    does not — no codeSystem, because a parked key preserves the section's code
    and not the system it was drawn from.
    """
    source_doc = tmp_path / "carrier_in.xml"
    source_doc.write_text(_PROSE_AND_ENTRY_CCD, encoding="utf-8")
    document = build_ccd(parse_document(source_doc))

    root = etree.fromstring(document, _PARSER)
    carriers = [
        section
        for section in root.iter(f"{{{V3}}}section")
        if (node := section.find(f"{{{V3}}}code")) is not None and node.get("code") == "47519-4"
    ]
    assert len(carriers) == 1, "the entries reached no section of their own"
    code = carriers[0].find(f"{{{V3}}}code")
    assert code.get("codeSystem") is None, "a code system the record never stated"
    assert carriers[0].findall(f"{{{V3}}}entry")


def test_a_foreign_loss_sections_entries_are_not_swallowed_by_our_ledger(
    tmp_path: Path,
) -> None:
    """51899-3 is a public LOINC, and a re-ingest reads OUR 51899-3 section as
    paragraphs and never looks at its entries.

    So a third party's 51899-3 entries may not be appended to the stamped ledger
    this tool writes: they would leave the document and never come back. They get
    a carrier of their own, which is why the "exactly one" rule is about the
    STAMPED section rather than about the code.
    """
    foreign = _FOREIGN_LOSS_CODE_CCD.replace(
        "</section></component>",
        """<entry><observation classCode="OBS" moodCode="EVN">
             <id root="feedface-vend-0000-0000-000000000906"/>
             <code code="75326-9" displayName="SENTINEL-VENDOR-ENTRY"/>
           </observation></entry></section></component>""",
    )
    source_doc = tmp_path / "foreign_entries_in.xml"
    source_doc.write_text(foreign, encoding="utf-8")
    ingested = parse_document(source_doc)
    assert _parked(ingested)[LOINC_EXTENSIONS]

    exported = tmp_path / "foreign_entries_out.xml"
    exported.write_bytes(build_ccd(ingested))
    reingested = parse_document(exported)
    assert _shapes(reingested) == _shapes(ingested), "a third party's entries were lost"
    # Ours is still exactly one, and still the stamped one.
    stamped = [
        section
        for section in _loss_sections(exported.read_bytes())
        if section.find(f"{{{V3}}}templateId") is not None
    ]
    assert len(stamped) == 1


def test_a_code_less_sections_entries_are_delivered_too(tmp_path: Path) -> None:
    """A section with no ``<code>`` parks under the one bucket both halves name,
    and its carrier states no code either — the record preserved none."""
    document = _PROSE_AND_ENTRY_CCD.replace(
        '<code code="47519-4" codeSystem="2.16.840.1.113883.6.1"/>', ""
    )
    source_doc = tmp_path / "codeless_in.xml"
    source_doc.write_text(document, encoding="utf-8")
    ingested = parse_document(source_doc)
    assert "unknown" in _parked(ingested)

    exported = tmp_path / "codeless_out.xml"
    exported.write_bytes(build_ccd(ingested))
    assert len(_entries_under(exported.read_bytes(), None)) == 1
    assert _shapes(parse_document(exported)) == _shapes(ingested)


def test_the_object_and_the_entry_it_came_from_are_stated_once(tmp_path: Path) -> None:
    """The compounding this avoids, measured on the repository's own fixture.

    Every condition, medication and measurement in it was read out of an entry
    the parser also parked. Emitting the derived entry beside the preserved one
    would state each fact twice, a re-ingest would read two objects where the
    chart has one, and the generation after that would read four.
    """
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "ccda" / "feedface_ccd.xml"
    ingested = parse_document(fixture)
    out = tmp_path / "once.xml"

    counts = []
    for _ in range(3):
        out.write_bytes(build_ccd(ingested))
        ingested = parse_document(out)
        counts.append(
            (
                len(ingested.conditions),
                len(ingested.medications),
                len(ingested.allergies),
                len(ingested.observations),
                len(ingested.encounters),
            )
        )
    assert counts == [(2, 2, 2, 8, 2)] * 3, f"a chart doubled around the loop: {counts}"


def test_an_object_no_preserved_entry_states_keeps_its_structured_entry(tmp_path: Path) -> None:
    """Only the object the preserved entries actually state is left to them.

    A record can carry both — a C-CDA ingest's parked entries and an object from
    somewhere else — and the one nothing preserved has to be emitted, or the
    export drops it silently.
    """
    source_doc = tmp_path / "mixed_in.xml"
    source_doc.write_text(_PROSE_AND_ENTRY_CCD, encoding="utf-8")
    ingested = parse_document(source_doc)
    ingested.patient.extensions["ccda:entries:11450-4"] = ingested.patient.extensions[
        "ccda:entries:47519-4"
    ]
    ingested.conditions = [
        Condition(
            patient_id=ingested.patient.id,
            snomed="38341003",
            display="ELSEWHERE-CONDITION",
            active=True,
            provenance=Provenance(source_system="pf-tebra", source_id="pf-row-0001"),
        )
    ]

    exported = tmp_path / "mixed_out.xml"
    exported.write_bytes(build_ccd(ingested))
    reingested = parse_document(exported)
    assert [c.display for c in reingested.conditions] == ["ELSEWHERE-CONDITION"]
    assert _shape(_parked(ingested)["11450-4"][0]) in _shapes(reingested)["11450-4"], (
        "the preserved entry was dropped in favour of the structured one"
    )
    assert len(_parked(reingested)["11450-4"]) == 2, "one fact, two entries"


def test_an_entry_with_no_id_of_its_own_is_not_stated_twice(tmp_path: Path) -> None:
    """The object an id-less entry produced is matched by the absence of an id.

    An entry carrying no ``<id>`` gives its object no source id, so identity
    cannot match them by one. It is the only kind of entry such an object can
    have come from, and pairing them on that is what keeps a malformed document
    — C-CDA requires the id — from doubling its entries on every export.
    """
    document = _PROSE_AND_ENTRY_CCD.replace(
        '<id root="feedface-proc-0000-0000-000000000905"/>', ""
    ).replace("47519-4", "11450-4")
    source_doc = tmp_path / "noid_in.xml"
    source_doc.write_text(document, encoding="utf-8")
    ingested = parse_document(source_doc)
    ingested.conditions = [
        Condition(patient_id=ingested.patient.id, display="FROM-THE-ID-LESS-ENTRY", active=True)
    ]

    out = tmp_path / "noid_out.xml"
    counts = []
    for _ in range(3):
        out.write_bytes(build_ccd(ingested))
        ingested = parse_document(out)
        counts.append(len(_entries_under(out.read_bytes(), "11450-4")))
    assert counts == [1, 1, 1], f"an id-less entry multiplied around the loop: {counts}"


def test_two_sections_sharing_a_code_deliver_into_the_one_section(tmp_path: Path) -> None:
    """Problems (Active) and Problems (Resolved) are both 11450-4, and the parser
    parks the second under ``…#2``. This exporter writes one section per code, so
    both lists are delivered into it and a re-ingest parks them as one."""
    section = """
    <component><section>
      <code code="47519-4" codeSystem="2.16.840.1.113883.6.1"/>
      <title>Procedures ({half})</title>
      <entry><procedure classCode="PROC" moodCode="EVN">
        <id root="feedface-proc-0000-0000-00000000090{half}"/>
        <code code="430193006" displayName="SENTINEL-{half}"/>
      </procedure></entry>
    </section></component>"""
    document = _PROSE_AND_ENTRY_CCD.replace(
        "<component><structuredBody>",
        "<component><structuredBody>" + section.format(half=1) + section.format(half=2),
    )
    source_doc = tmp_path / "repeat_in.xml"
    source_doc.write_text(document, encoding="utf-8")
    ingested = parse_document(source_doc)
    assert "ccda:entries:47519-4#2" in ingested.patient.extensions

    exported = tmp_path / "repeat_out.xml"
    exported.write_bytes(build_ccd(ingested))
    assert len(_entries_under(exported.read_bytes(), "47519-4")) == 3
    assert _shapes(parse_document(exported))["47519-4"] == _shapes(ingested)["47519-4"]


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(["<entry>SENTINEL-UNDELIVERABLE"], id="not-well-formed"),
        pytest.param(
            ['<observation xmlns="urn:hl7-org:v3">SENTINEL-UNDELIVERABLE</observation>'],
            id="not-an-entry",
        ),
        pytest.param({"entry": "SENTINEL-UNDELIVERABLE"}, id="not-a-list"),
        pytest.param([{"note": "SENTINEL-UNDELIVERABLE"}], id="not-a-string"),
        # A mapping iterates its KEYS, so a shape read by iterating alone would
        # deliver this one's key and drop its value without a word.
        pytest.param(
            {'<entry xmlns="urn:hl7-org:v3"><observation/></entry>': "SENTINEL-UNDELIVERABLE"},
            id="a-mapping-whose-key-looks-like-an-entry",
        ),
    ],
)
def test_a_preserved_value_the_exporter_cannot_re_emit_is_narrated_instead(
    value: object, tmp_path: Path
) -> None:
    """A key this exporter cannot deliver must not be BOTH skipped here and
    exempted there — that is the silent drop.

    Four shapes it cannot: bytes that will not parse, an element that is not an
    ``<entry>`` (appending one to a section puts it somewhere the parser's own
    entry walk never looks, which is a drop wearing a delivery's clothes), a
    value that is not a list of them, and a member that is not a string. Each
    narrates instead. The value is patient content, so an unparseable one is
    answered with the narrative tier rather than with an exception quoting the
    bytes.
    """
    record = PatientRecord(
        patient=Patient(
            id="feedface-0000-0000-0000-000000000007",
            extensions={"ccda:entries:11450-4": value},
        )
    )
    exported = tmp_path / "undeliverable_out.xml"
    exported.write_bytes(build_ccd(record))
    assert "SENTINEL-UNDELIVERABLE" in _loss_text(parse_document(exported))


def test_the_loss_ledger_stops_growing_for_a_chart_ingested_from_ccda(tmp_path: Path) -> None:
    """The constraint that made this change hard, pinned.

    Narrating the parked entries instead of delivering them grew the 51899-3
    section by ~15 KB per generation for as long as the loop ran. Delivered, the
    chart reaches its fixed point at generation 2 — measured on this repository's
    own C-CDA fixture — and the converged section is within ~100 bytes of what it
    was before any entry under prose was preserved at all.
    """
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "ccda" / "feedface_ccd.xml"
    record = parse_document(fixture)
    # One stable file name: the parser derives fallback ids from it, so a
    # churning name would inject churn the real path does not have.
    out = tmp_path / "generations.xml"
    sizes = []
    for _ in range(3):
        document = build_ccd(record)
        out.write_bytes(document)
        sizes.append(measure_ccd(document).preserved_bytes)
        record = parse_document(out)
    assert sizes[1] == sizes[2], f"the loss ledger is still growing: {sizes}"
    assert sizes[2] < 11_000, f"the converged ledger is far larger than it was: {sizes}"


def test_an_organizer_component_with_no_id_of_its_own_is_stated_once(tmp_path: Path) -> None:
    """A results organizer with an id, and a component with none, is one fact.

    ``_stated_ids`` used to pair a preserved entry with its structured twin
    only by a shared ``<id root>`` — so an id-less component observation
    paired with nothing, and ``_Preserved.own`` kept re-emitting it beside
    its own preserved bytes: 1 observation -> 2 -> 2, a stable duplicate
    rather than a stable count. Red on the unpatched head (goes to 2 and
    stays there); the fix pairs the component to its organizer instead of to
    absence, so the count never leaves 1. See #378.
    """
    source_doc = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "ccda_edge_cases"
        / "feedface_ccd_idless_result_component.xml"
    )
    record = parse_document(source_doc)
    out = tmp_path / "idless_gen.xml"
    counts = [len(record.observations)]
    for _ in range(3):
        out.write_bytes(build_ccd(record))
        record = parse_document(out)
        counts.append(len(record.observations))
    assert counts == [1, 1, 1, 1], f"the lab observation duplicated across generations: {counts}"
    assert [o.code for o in record.observations] == ["2345-7"]


def test_a_rebuilt_document_invents_no_start_for_a_sentinel_medication(tmp_path: Path) -> None:
    """#385: parse -> build_ccd -> parse, three generations, on a fixture whose
    only medication states its start as an all-zero TS. Red on main — the
    FIRST parse already raised (`ValueError: unrecognized date/time format:
    '0'`), so `build_ccd` never ran at all.

    Fixed, the medication survives every generation with `start is None` —
    never an invented date. The exporter is untouched by #385 (out of scope
    per the issue's file list) and its pre-existing behavior for a record
    whose provenance names a source id a preserved `<entry>` already states
    is to deliver that entry's OWN bytes verbatim rather than rebuild one —
    `_Preserved.own`, driven here rather than assumed: every generation's
    `<low>` still reads the source's original "0" `@value`, unmodified, never
    `_nullable`'s fresh-build `nullFlavor="NI"` (that path is for a start with
    no preserved entry to match at all). Either way nothing is invented: the
    "0" that comes back out is the exact "0" that went in.
    """
    fixture = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "ccda_edge_cases"
        / "feedface_ccd_zero_date_sentinel.xml"
    )
    record = parse_document(fixture)
    out = tmp_path / "zero_sentinel_gen.xml"
    counts: list[int] = []
    starts: list[date | None] = []
    sizes: list[int] = []
    document = b""
    for _ in range(3):
        document = build_ccd(record)
        out.write_bytes(document)
        sizes.append(len(document))
        record = parse_document(out)
        counts.append(len(record.medications))
        starts.append(record.medications[0].start if record.medications else None)
        assert record.patient.extensions["ccda:timestamp_named_no_instant"] == {
            "effectiveTime/low": 1
        }
    assert counts == [1, 1, 1], f"the medication did not survive every generation: {counts}"
    assert starts == [None, None, None], f"a generation invented a start date: {starts}"
    assert sizes[1] == sizes[2], f"the document is still growing: {sizes}"

    root = etree.fromstring(document, _PARSER)
    [low] = root.findall(f".//{{{V3}}}substanceAdministration/{{{V3}}}effectiveTime/{{{V3}}}low")
    assert low.get("nullFlavor") is None
    assert low.get("value") == "0"  # the source's own byte, carried forward — never invented


# The two readings of an organizer/component id had to be genuinely one: before
# `core.ccda_codes.first_rooted_id` existed, the parser read an `<id>` through
# `_attr` (stripped, nullFlavor-aware) while the builder read one by raw
# truthiness (unstripped), and the parser looked only at a component's FIRST
# `<id>` child while the builder scanned all of them — so a padded root, a
# padded extension, or a component whose first `<id>` is nullFlavor and whose
# second is rooted derived one id on ingest and stated a different one (or
# none) on export: exactly the growth #378 had just closed, reopened for four
# shapes. See #378.
_ORGANIZER_SECTION = """<section xmlns="urn:hl7-org:v3">
  <code code="30954-2" codeSystem="2.16.840.1.113883.6.1"/>
  <entry>
    <organizer classCode="BATTERY" moodCode="EVN">
      {orgids}
      <code code="30954-2" codeSystem="2.16.840.1.113883.6.1"/>
      <statusCode code="completed"/>
      <component>
        <observation classCode="OBS" moodCode="EVN">
          {compids}
          <code code="26436-6" codeSystem="2.16.840.1.113883.6.1"/>
          <statusCode code="completed"/>
          <effectiveTime value="20240101"/>
          <value xsi:type="PQ" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                 value="5" unit="mg/dL"/>
        </observation>
      </component>
    </organizer>
  </entry>
</section>"""


@pytest.mark.parametrize(
    "name,orgids,compids",
    [
        pytest.param(
            "padded organizer extension",
            '<id root="1.2.3.4" extension="  LAB1  "/>',
            '<id nullFlavor="NI"/>',
            id="padded organizer extension",
        ),
        pytest.param(
            "padded organizer root",
            '<id root="  1.2.3.4  " extension="LAB1"/>',
            '<id nullFlavor="NI"/>',
            id="padded organizer root",
        ),
        pytest.param(
            "component nullFlavor then rooted",
            '<id root="1.2.3.4" extension="LAB1"/>',
            '<id nullFlavor="NI"/><id root="COMPROOT"/>',
            id="component nullFlavor then rooted",
        ),
        pytest.param(
            "component whitespace-only root",
            '<id root="1.2.3.4" extension="LAB1"/>',
            '<id root="   "/>',
            id="component whitespace-only root",
        ),
    ],
)
def test_the_parsers_derived_id_is_always_one_the_builder_states(
    name: str, orgids: str, compids: str
) -> None:
    """The parser's ``source_id`` is a member of the built entry's ``_stated_ids``.

    Feeding the SAME organizer XML through both halves (mirrors the reviewer's
    ``qaprobe/p1_divergence.py``): whatever id the parser derives or reads for
    the component, the builder's ``_stated_ids`` walk over that same XML must
    already contain it, or a re-export pairs the component with nothing and
    the fact duplicates without bound. Red on the unpatched head for all four
    of these (the two id-reading mismatches above); green once both sides
    read through ``first_rooted_id``.
    """
    section = etree.fromstring(
        _ORGANIZER_SECTION.format(orgids=orgids, compids=compids).encode(), _PARSER
    )
    [observation] = _measurements(
        section, "patient-1", ObservationCategory.LABORATORY, "v3:organizer", "f.xml"
    )
    entry = section.find(f"{{{V3}}}entry")
    stated = _stated_ids(entry)
    assert observation.provenance is not None
    assert observation.provenance.source_id in stated, (
        f"{name}: parser derived {observation.provenance.source_id!r}, "
        f"builder states {sorted(str(s) for s in stated)!r}"
    )


@pytest.mark.parametrize(
    "fixture_name",
    [
        "feedface_ccd_organizer_extension_whitespace.xml",
        "feedface_ccd_organizer_root_whitespace.xml",
        "feedface_ccd_component_id_nullflavor_then_rooted.xml",
        "feedface_ccd_component_root_whitespace.xml",
    ],
)
def test_a_padded_or_partially_id_less_organizer_never_grows(
    fixture_name: str, tmp_path: Path
) -> None:
    """Generation counts stay flat at 1 for each of the four mismatch shapes.

    Copies of the reviewer's ``qaprobe/fx/{E_ws_extension,F_ws_root,
    H_nullflavor_then_rooted,J_ws_root_component}.xml``. Red on the unpatched
    head: each grows 1 -> 2 -> 3 -> 4 -> 5, because the parser's derived id
    for the lab observation was never a member of what the builder's
    ``_stated_ids`` states for that entry, so ``_Preserved.own`` paired it
    with nothing and it duplicated once per generation, unbounded — a faster
    growth than #378's own id-less-component defect, which stabilised at 2.
    """
    source_doc = Path(__file__).resolve().parents[1] / "fixtures" / "ccda_edge_cases" / fixture_name
    record = parse_document(source_doc)
    out = tmp_path / "gen.xml"
    counts = [len(record.observations)]
    for _ in range(3):
        out.write_bytes(build_ccd(record))
        record = parse_document(out)
        counts.append(len(record.observations))
    assert counts == [1, 1, 1, 1], f"{fixture_name}: {counts}"


def test_two_id_less_components_under_one_organizer_derive_distinct_ids(
    tmp_path: Path,
) -> None:
    """Two id-less analytes under one panel get two different derived ids.

    ``index`` is what tells them apart once their own ids are gone — without
    it both would hash to the same ``organizer_component_source_id`` and
    collapse to one observation. Pinned against the LITERAL uuid5 strings (a
    recipe change is a decision that rewrites every already-migrated chart's
    provenance, and a test that recomputes the value under test proves
    nothing about the value itself — see #378's own round two). Generations
    stay flat at 2, not 4: each analyte pairs with its own preserved twin.
    """
    root = "feedface-idls-0000-0000-000000000001"
    extension = "feedface-idls-panel-0001"
    expected_first = organizer_component_source_id(root, extension, 0)
    expected_second = organizer_component_source_id(root, extension, 1)
    assert expected_first == "7029466a-7630-5f95-9072-85ca63f186dc"
    assert expected_second == "f244f1d6-d7d6-5bc0-ac55-fc41390a3c9b"
    assert expected_first != expected_second

    source_doc = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "ccda_edge_cases"
        / "feedface_ccd_two_idless_components.xml"
    )
    record = parse_document(source_doc)
    ids = sorted(o.provenance.source_id for o in record.observations if o.provenance is not None)
    assert ids == sorted([expected_first, expected_second])

    out = tmp_path / "two_idless_gen.xml"
    counts = [len(record.observations)]
    for _ in range(3):
        out.write_bytes(build_ccd(record))
        record = parse_document(out)
        counts.append(len(record.observations))
    assert counts == [2, 2, 2, 2], f"the two lab facts collapsed or duplicated: {counts}"


@pytest.mark.parametrize(
    "xml,expected",
    [
        pytest.param(
            '<organizer xmlns="urn:hl7-org:v3"><id root="1.2.3.4" extension="LAB1"/></organizer>',
            ("1.2.3.4", "LAB1"),
            id="plain root and extension",
        ),
        pytest.param(
            '<organizer xmlns="urn:hl7-org:v3">'
            '<id root="  1.2.3.4  " extension="LAB1"/></organizer>',
            ("1.2.3.4", "LAB1"),
            id="padded root is stripped",
        ),
        pytest.param(
            '<organizer xmlns="urn:hl7-org:v3">'
            '<id root="1.2.3.4" extension="  LAB1  "/></organizer>',
            ("1.2.3.4", "LAB1"),
            id="padded extension is stripped",
        ),
        pytest.param(
            '<organizer xmlns="urn:hl7-org:v3"><id root="1.2.3.4" extension="   "/></organizer>',
            ("1.2.3.4", None),
            id="whitespace-only extension normalizes to None",
        ),
        pytest.param(
            '<organizer xmlns="urn:hl7-org:v3">'
            '<id nullFlavor="NI"/><id root="1.2.3.4"/></organizer>',
            ("1.2.3.4", None),
            id="nullFlavor id skipped, next rooted id wins",
        ),
        pytest.param(
            '<organizer xmlns="urn:hl7-org:v3"><id root=""/><id root="ZZZ"/></organizer>',
            ("ZZZ", None),
            id="blank root skipped, next rooted id wins",
        ),
        pytest.param(
            '<organizer xmlns="urn:hl7-org:v3"><id root="   "/><id root="ZZZ"/></organizer>',
            ("ZZZ", None),
            id="whitespace-only root skipped, next rooted id wins",
        ),
        pytest.param(
            '<organizer xmlns="urn:hl7-org:v3"><id nullFlavor="NI" root="X"/></organizer>',
            None,
            id="nullFlavor wins even with a root present",
        ),
        pytest.param(
            '<organizer xmlns="urn:hl7-org:v3"><id nullFlavor="NI"/></organizer>',
            None,
            id="only a nullFlavor id: no id at all",
        ),
        pytest.param(
            '<organizer xmlns="urn:hl7-org:v3"></organizer>',
            None,
            id="no id children at all",
        ),
    ],
)
def test_first_rooted_id_reads_every_id_child_stripped_and_nullflavor_aware(
    xml: str, expected: tuple[str, str | None] | None
) -> None:
    """Pins ``first_rooted_id`` against an INDEPENDENT expectation, never
    against the other half merely agreeing with it.

    The cross-check above (``test_the_parsers_derived_id_is_...``) proves the
    parser and the builder read an id the SAME way, which a bug inside
    ``first_rooted_id`` itself can never fail: both callers share the one
    function, so a mistake shared by both sides is invisible to a
    mutual-agreement check (driven: mutating away either ``.strip()`` call,
    or narrowing the scan to only the first ``<id>`` child, survives the
    whole suite without this test). This one pins the VALUE independently —
    a padded root or extension strips to the unpadded string; a blank
    extension normalizes to ``None``; a nullFlavor id is skipped even when it
    also carries a root; and the scan continues past a blank or nullFlavor
    id to the NEXT ``<id>`` rather than stopping at the first child, which is
    what left #378 open for the nullFlavor-then-rooted shape the first time.
    """
    element = etree.fromstring(xml.encode())
    assert first_rooted_id(element) == expected


def test_a_nullflavor_id_is_skipped_even_when_it_also_carries_a_root() -> None:
    """``<id nullFlavor="NI" root="X"/>`` states no id: nullFlavor wins.

    A vendor id that carries both attributes at once is still an absent id —
    reading the root anyway (mut.py's M4: drop the nullFlavor check) would
    treat a null id as a real, if odd, stated one on one side without the
    other agreeing, reopening exactly the mismatch this file's other new
    tests close.
    """
    organizer = etree.fromstring(
        '<organizer xmlns="urn:hl7-org:v3"><id nullFlavor="NI" root="X"/></organizer>',
        _PARSER,
    )
    assert first_rooted_id(organizer) is None


def test_a_component_that_is_not_an_observation_is_never_derived_for() -> None:
    """A ``<procedure>`` component does not count toward the derived index.

    ``_measurements``/``_derived_component_ids`` both walk
    ``component/observation`` only, so a non-observation component sibling
    (a procedure, in template order before the observation) is invisible to
    the derivation on both sides — an id-less component's index is its
    position among the organizer's OBSERVATION components, never its
    position among all of them.
    """
    section = etree.fromstring(
        b"""<section xmlns="urn:hl7-org:v3">
  <code code="30954-2" codeSystem="2.16.840.1.113883.6.1"/>
  <entry>
    <organizer classCode="BATTERY" moodCode="EVN">
      <id root="1.2.3.4" extension="LAB1"/>
      <code code="30954-2" codeSystem="2.16.840.1.113883.6.1"/>
      <statusCode code="completed"/>
      <component>
        <procedure classCode="PROC" moodCode="EVN">
          <id nullFlavor="NI"/>
          <code code="X" codeSystem="2.16.840.1.113883.6.1"/>
        </procedure>
      </component>
      <component>
        <observation classCode="OBS" moodCode="EVN">
          <id nullFlavor="NI"/>
          <code code="26436-6" codeSystem="2.16.840.1.113883.6.1"/>
          <statusCode code="completed"/>
          <effectiveTime value="20240101"/>
          <value xsi:type="PQ" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                 value="5" unit="mg/dL"/>
        </observation>
      </component>
    </organizer>
  </entry>
</section>""",
        _PARSER,
    )
    [observation] = _measurements(
        section, "patient-1", ObservationCategory.LABORATORY, "v3:organizer", "f.xml"
    )
    assert observation.provenance is not None
    assert observation.provenance.source_id == organizer_component_source_id("1.2.3.4", "LAB1", 0)
    entry = section.find(f"{{{V3}}}entry")
    assert observation.provenance.source_id in _stated_ids(entry)


def test_a_nested_organizers_own_idless_component_is_still_derived_for() -> None:
    """An organizer inside an organizer: the INNER one's id-less component too.

    ``_derived_component_ids`` finds an organizer at ANY depth
    (``entry.iter``, not ``entry.findall`` — mut.py's M6), because a
    preserved entry's structure is not this exporter's to assume flat. The
    OUTER organizer's own id-less component is a direct child of ``entry``
    either way, so a round-trip observation COUNT cannot tell the two walks
    apart on this shape — ``_measurements`` reads only ONE organizer per
    entry (its direct child), so the inner organizer's fact is never
    structurally extracted at all, only preserved verbatim (see the flat
    ``[1, 1, 1, 1]`` in the regression test below). What has to hold
    independently of that is that ``_stated_ids``, read over the preserved
    entry, still credits the INNER organizer's own analyte — or a future
    structural reader that does walk nested organizers would duplicate it
    the same way #378 duplicated an id-less component in the first place.
    """
    section = etree.fromstring(
        b"""<section xmlns="urn:hl7-org:v3">
  <code code="30954-2" codeSystem="2.16.840.1.113883.6.1"/>
  <entry>
    <organizer classCode="BATTERY" moodCode="EVN">
      <id root="1.2.3.4" extension="OUTER"/>
      <code code="30954-2" codeSystem="2.16.840.1.113883.6.1"/>
      <statusCode code="completed"/>
      <component>
        <observation classCode="OBS" moodCode="EVN">
          <id nullFlavor="NI"/>
          <code code="26436-6" codeSystem="2.16.840.1.113883.6.1"/>
        </observation>
      </component>
      <component>
        <organizer classCode="BATTERY" moodCode="EVN">
          <id root="5.6.7.8" extension="INNER"/>
          <code code="30954-2" codeSystem="2.16.840.1.113883.6.1"/>
          <statusCode code="completed"/>
          <component>
            <observation classCode="OBS" moodCode="EVN">
              <id nullFlavor="NI"/>
              <code code="2951-2" codeSystem="2.16.840.1.113883.6.1"/>
            </observation>
          </component>
        </organizer>
      </component>
    </organizer>
  </entry>
</section>""",
        _PARSER,
    )
    entry = section.find(f"{{{V3}}}entry")
    stated = _stated_ids(entry)
    outer_derived = organizer_component_source_id("1.2.3.4", "OUTER", 0)
    inner_derived = organizer_component_source_id("5.6.7.8", "INNER", 0)
    assert outer_derived in stated, "the direct-child organizer's own id-less analyte is lost"
    assert inner_derived in stated, "the nested organizer's own id-less analyte is lost"


def test_a_nested_organizer_never_duplicates_or_crashes(tmp_path: Path) -> None:
    """Regression pin: a real (if unusual) organizer-inside-organizer document
    round-trips without growth. The parser only structurally reads the
    entry's direct-child organizer, so only the outer id-less analyte ever
    becomes an ``Observation`` — the inner one rides the preserved entry's
    bytes — and the count this fixture actually produces is 1, held flat.
    """
    source_doc = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "ccda_edge_cases"
        / "feedface_ccd_nested_organizer.xml"
    )
    record = parse_document(source_doc)
    out = tmp_path / "nested_gen.xml"
    counts = [len(record.observations)]
    for _ in range(3):
        out.write_bytes(build_ccd(record))
        record = parse_document(out)
        counts.append(len(record.observations))
    assert counts == [1, 1, 1, 1], f"a nested organizer's components misbehaved: {counts}"


def test_a_component_that_carries_its_own_id_keeps_it(tmp_path: Path) -> None:
    """Regression guard, not an acceptance test for #378 — passes unpatched too.

    The organizer-derived fallback in ``_stated_ids``/``_measurements`` must
    never shadow a component's own stated id: the five vitals of
    ``feedface_ccd.xml`` each carry one, and a build->parse must hand every
    one of those five GUIDs back unchanged.
    """
    source_doc = Path(__file__).resolve().parents[1] / "fixtures" / "ccda" / "feedface_ccd.xml"
    record = parse_document(source_doc)
    before = {
        o.code: o.provenance.source_id if o.provenance else None
        for o in record.observations
        if o.category == ObservationCategory.VITAL_SIGNS
    }
    assert len(before) == 5
    assert all(sid is not None and sid.startswith("feedface-vitl-") for sid in before.values())

    exported = tmp_path / "vitals_out.xml"
    exported.write_bytes(build_ccd(record))
    reingested = parse_document(exported)
    after = {
        o.code: o.provenance.source_id if o.provenance else None
        for o in reingested.observations
        if o.category == ObservationCategory.VITAL_SIGNS
    }
    assert after == before


@pytest.mark.parametrize(
    "fixture,expected_added",
    [
        pytest.param(
            Path("ccda") / "feedface_ccd.xml",
            frozenset(),
            id="feedface_ccd.xml",
        ),
        pytest.param(
            Path("ccda_edge_cases") / "feedface_ccd_duplicate_encounter_id.xml",
            frozenset(),
            id="feedface_ccd_duplicate_encounter_id.xml",
        ),
        pytest.param(
            Path("ccda_edge_cases") / "feedface_ccd_idless_result_component.xml",
            frozenset(
                {
                    organizer_component_source_id(
                        "feedface-idls-0000-0000-000000000001",
                        "feedface-idls-panel-0001",
                        0,
                    )
                }
            ),
            id="feedface_ccd_idless_result_component.xml",
        ),
    ],
)
def test_a_preserved_entry_states_more_than_it_did_and_never_less(
    fixture: Path, expected_added: frozenset[str]
) -> None:
    """The new ``_stated_ids`` set is a strict superset of the old one — proven positively.

    This test used to re-implement ``_stated_ids``'s OWN any-depth walk inline
    and compare it against the real thing, so ``new >= old`` and ``(None in
    new) == (None in old)`` held trivially even with the derived-id branch
    deleted entirely (``new == old`` then, by construction of the comparison
    itself) — a superset check that could not fail. It now asserts the actual
    diff every entry contributes across a fixture: nothing for the two
    fixtures with no id-less organizer component, and exactly the one
    derived id — computed independently via
    :func:`organizer_component_source_id`, not read back off ``_stated_ids``
    — for the fixture that has one. Red on the unpatched head for the third
    fixture (``total_added`` comes back empty where one id was expected).
    """
    source_doc = Path(__file__).resolve().parents[1] / "fixtures" / fixture
    root = etree.parse(str(source_doc), _PARSER).getroot()
    entries = list(root.iter(f"{{{V3}}}entry"))
    assert entries, "fixture carries no entries to compare"
    total_added: set[str | None] = set()
    for entry in entries:
        old = {r for node in entry.iter(f"{{{V3}}}id") if (r := node.get("root")) is not None} or {
            None
        }
        new = _stated_ids(entry)
        assert new >= old, f"lost a previously-stated id: {old - new}"
        assert (None in new) == (None in old), "None's presence changed where a walk alone decides"
        total_added |= new - old
    assert total_added == set(expected_added), f"{fixture.name}: added {total_added}"


# --- export → ingest generations ---------------------------------------------
#
# A migration legitimately runs export → ingest → export more than once. Before
# the stamp existed, the parser read the exporter's OWN 51899-3 section back as
# an ordinary `ccda:section:51899-3` narrative, so generation N re-narrated
# generation N-1's ENTIRE ledger as one line inside its own — measured growth of
# ~1.2 KB per generation, forever, with the real entries buried inside a blob
# nobody can read. The exporter now stamps the section and the parser recovers
# its entries discretely, so each generation carries its own entries plus the
# prior ledger DEDUPLICATED: identical entries collapse, distinct ones all
# survive, and the ledger stops growing.


def _generation_record() -> PatientRecord:
    """A chart whose canonical ids survive re-ingest verbatim (GUID-shaped, per
    the parser's ``_GUID_RE``), carrying one loss of every class the ledger has
    to hold across generations: a native patient field with no CDA slot, a vendor
    extension namespace, a native encounter field, and TWO medications sharing
    one strength value (the multiplicity the dedupe must not collapse)."""
    pid = "feedface-0000-0000-0000-0000000000aa"
    return PatientRecord(
        patient=Patient(
            id=pid,
            given_name="Gene",
            family_name="Ration",
            gender_identity="GENSENTINEL",
            identifiers=[
                Identifier(
                    kind=IdentifierKind.SOURCE_GUID, value=pid, system="2.16.840.1.113883.19.5"
                )
            ],
            telecom=[ContactPoint(kind=ContactKind.PHONE_HOME, value="(206) 555-0188")],
            extensions={"pf_tebra:PatientStatusCode": "A"},
        ),
        medications=[
            MedicationStatement(
                patient_id=pid, display_name="Metformin", rxnorm="6809", strength="500 mg"
            ),
            MedicationStatement(
                patient_id=pid, display_name="Aspirin", rxnorm="1191", strength="500 mg"
            ),
        ],
        encounters=[
            Encounter(
                id="feedface-0000-0000-0000-0000000000e9",
                patient_id=pid,
                date_of_service=date(2023, 1, 2),
                encounter_type="Office visit",
                note_type="Office visit",
                chief_complaint="CHIEFSENTINEL",
            )
        ],
    )


def _generations(record: PatientRecord, count: int, tmp_path: Path) -> list[bytes]:
    """``count`` rounds of export → ingest, returning each generation's document.

    One stable file name per round: the deliverer names a document after the
    patient id, and the parser's fallback ids are derived from the file name, so
    re-exporting under a churning name would inject churn the real path lacks.
    """
    out = tmp_path / f"{record.patient.id}.xml"
    documents: list[bytes] = []
    for _ in range(count):
        document = build_ccd(record)
        out.write_bytes(document)
        documents.append(document)
        record = parse_document(out)
    return documents


def test_three_generations_keep_exactly_one_loss_narrative_section(tmp_path: Path) -> None:
    for generation, document in enumerate(_generations(_generation_record(), 3, tmp_path), start=1):
        sections = _loss_sections(document)
        assert len(sections) == 1, (
            f"generation {generation} emitted {len(sections)} 51899-3 sections; "
            "the loss ledger must be exactly one section per document"
        )
        section = sections[0]
        # Stamped as ours, with the generation counter a later ingest reads back.
        template = section.find(f"{{{V3}}}templateId")
        assert template is not None
        assert template.get("root") == LOSS_NARRATIVE_TEMPLATE_ROOT
        stamp = section.find(f"{{{V3}}}id")
        assert stamp is not None
        assert stamp.get("root") == LOSS_NARRATIVE_GENERATION_ROOT
        assert stamp.get("extension") == str(generation)


def test_three_generations_keep_every_distinct_loss_entry(tmp_path: Path) -> None:
    """Distinct entries from generation 1 must still be readable at generation 3
    — the whole point of a carry-forward ledger."""
    documents = _generations(_generation_record(), 3, tmp_path)
    entries = _entries_of(documents[-1])
    for expected in (
        "encounters[0].chief_complaint = CHIEFSENTINEL",
        "patient.gender_identity = GENSENTINEL",
        "patient.extensions.pf_tebra:PatientStatusCode = A",
    ):
        assert expected in entries, f"generation-1 loss entry dropped by generation 3: {expected!r}"
    # Two medications share one strength; BOTH entries survive — dedupe collapses
    # identical entries, never the cardinality of two genuinely distinct objects.
    assert sum(entry.endswith(".strength = 500 mg") for entry in entries) == 2


def test_loss_narrative_reaches_a_fixed_point_by_generation_three(tmp_path: Path) -> None:
    """Identical entries must not multiply: once the chart has been through one
    full round trip the ledger stops growing. An unbounded ledger is the
    regression this guards.

    This record's ids survive re-ingest verbatim, which is the fastest case —
    it settles at generation 2. ``test_ledger_is_bounded_for_a_record_whose_id_
    is_rederived`` covers the slower one.
    """
    documents = _generations(_generation_record(), 3, tmp_path)
    counts = [len(_entries_of(document)) for document in documents]
    assert counts[1] == counts[2], f"loss ledger still growing across generations: {counts}"
    # Entry for entry, not just in count. The only text that moves between the
    # two is the regenerated canonical id inside a path — a DECLARED loss, and
    # precisely what the dedupe key erases.
    assert sorted(_entry_key(entry) for entry in _entries_of(documents[1])) == sorted(
        _entry_key(entry) for entry in _entries_of(documents[2])
    )
    # And nothing swallowed a whole prior ledger as a single line.
    assert not any("ccda:section:51899-3" in entry for entry in _entries_of(documents[2]))
    assert not any(EXT_PRIOR_LOSS_NARRATIVE in entry for entry in _entries_of(documents[2]))


def test_ledger_is_bounded_for_a_record_whose_id_is_rederived(tmp_path: Path) -> None:
    """The bound holds for a chart that needs an extra generation to reach it.

    ``document_id`` defaults to a uuid5 over the patient id. A chart whose id
    the parser re-derives on first ingest therefore gets a different derived
    document id on its second export, which narrates one entry the first pass
    could not — so the ledger settles a generation later than one whose ids
    survive verbatim. Both settle; only the generation differs. The docs used to
    promise generation 2 flatly, which is true only of the fast case, so the
    growth this guards could have run a full generation unnoticed.
    """
    documents = _generations(_rich_record(), 5, tmp_path)
    counts = [len(_entries_of(document)) for document in documents]
    assert counts[2] == counts[3] == counts[4], f"ledger still growing: {counts}"
    # Bounded, not merely equal in count: entry for entry, modulo the
    # regenerated canonical ids inside paths that the dedupe key erases.
    keys = [sorted(_entry_key(entry) for entry in _entries_of(d)) for d in documents[2:]]
    assert keys[0] == keys[1] == keys[2], "the ledger's contents keep churning"
    # And the extra generation really is needed — asserting it at 2 would be a
    # promise this record breaks.
    assert counts[1] < counts[2], (
        f"this record no longer needs the extra generation ({counts}); if that is "
        "deliberate, the fast case is now the only case and the docs should say so"
    )


def _entries_of(document: bytes) -> list[str]:
    """The loss-ledger entries of an exported document, read straight off the
    emitted paragraphs (no re-ingest — this is what the document SAYS)."""
    sections = _loss_sections(document)
    return [
        text
        for section in sections
        for node in section.findall(f"{{{V3}}}text/{{{V3}}}paragraph")
        if (text := node.text)
    ]


# --- a re-rendered ledger: mixed narrative content ---------------------------
#
# A document this tool wrote, passed through a system that re-rendered the
# ledger as a table or a list, or hand-edited. The stamp survives; the shape of
# the narrative does not. Only `<paragraph>` children used to be collected, with
# the whole `<text>` kept as a single entry when there were NONE — so a section
# holding one paragraph AND a table lost the table outright. It reached neither
# the carried-forward ledger nor the ordinary foreign-narrative key, and did not
# survive re-export: a silent content drop inside the mechanism whose entire job
# is to prevent silent content drops.


def _stamped_mixed_ledger(generation: str = "1") -> str:
    """A stamped ledger whose text holds one of everything a renderer might
    leave behind: a paragraph, loose text, a table and a list."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3">
  <id root="feedface-0000-0000-0000-00000000cda4"/>
  <title>Re-rendered CCD</title>
  <recordTarget><patientRole>
    <id root="2.16.840.1.113883.19.5" extension="feedface-0000-0000-0000-0000000000f1"/>
    <patient><name><given>Mix</given><family>Content</family></name></patient>
  </patientRole></recordTarget>
  <component><structuredBody>
    <component><section>
      <templateId root="{LOSS_NARRATIVE_TEMPLATE_ROOT}" extension="1"/>
      <id root="{LOSS_NARRATIVE_GENERATION_ROOT}" extension="{generation}"/>
      <code code="{LOINC_EXTENSIONS}" codeSystem="2.16.840.1.113883.6.1"/>
      <title>{LOSS_NARRATIVE_TITLE}</title>
      <text>LOOSE-LEAD
        <paragraph>synthetic:AlphaField = one</paragraph>
        LOOSE-TAIL
        <table><tbody><tr><td>synthetic:BetaField</td><td>two</td></tr></tbody></table>
        <list><item>synthetic:GammaField = three</item></list>
      </text>
    </section></component>
  </structuredBody></component>
</ClinicalDocument>
"""


def test_mixed_content_ledger_keeps_every_narrative_node(tmp_path: Path) -> None:
    """Paragraph, table, list and loose text all come back, and all re-export.

    Before the fix the table and the list were silently dropped on ingest: the
    paragraph was found, so the whole-text fallback never fired, and nothing
    else was ever looked at.
    """
    source_doc = tmp_path / "mixed.xml"
    source_doc.write_text(_stamped_mixed_ledger(), encoding="utf-8")
    ingested = parse_document(source_doc)

    recovered = " | ".join(_loss_entries(ingested))
    for field in ("AlphaField", "BetaField", "GammaField", "LOOSE-LEAD", "LOOSE-TAIL"):
        assert field in recovered, f"{field} lost on ingest"

    # And it survives the trip back out — the entries are re-emitted as this
    # generation's carry-forward appendix, not stranded in the parsed record.
    exported = tmp_path / "mixed_out.xml"
    exported.write_bytes(build_ccd(ingested))
    reexported = " | ".join(_entries_of(exported.read_bytes()))
    for field in ("AlphaField", "BetaField", "GammaField", "LOOSE-LEAD", "LOOSE-TAIL"):
        assert field in reexported, f"{field} lost on re-export"


def test_a_comment_inside_the_ledger_does_not_abort_the_parse(tmp_path: Path) -> None:
    """lxml gives a comment a CALLABLE tag and raises on ``itertext()``, so
    walking the children without checking would turn one stray comment into a
    failed ingest of the whole document."""
    source_doc = tmp_path / "commented.xml"
    source_doc.write_text(
        _stamped_mixed_ledger().replace(
            "<paragraph>synthetic:AlphaField = one</paragraph>",
            "<!-- rendered by a downstream system -->\n"
            "<paragraph>synthetic:AlphaField = one</paragraph>",
        ),
        encoding="utf-8",
    )
    entries = " | ".join(_loss_entries(parse_document(source_doc)))
    assert "AlphaField" in entries
    assert "BetaField" in entries


def test_a_crafted_generation_stamp_does_not_abort_the_ingest(tmp_path: Path) -> None:
    """``str.isdigit()`` is true for characters ``int()`` refuses — a superscript
    among them — so guarding with one and converting with the other left a gap a
    crafted document could fall through, raising out of ``parse_document`` and
    taking the whole ingest with it. An unreadable counter is a missing counter,
    not a failed document: the entries are what matter.
    """
    source_doc = tmp_path / "crafted.xml"
    source_doc.write_text(_stamped_mixed_ledger(generation="\u00b2"), encoding="utf-8")

    ingested = parse_document(source_doc)  # must not raise
    prior = ingested.patient.extensions[EXT_PRIOR_LOSS_NARRATIVE]
    assert isinstance(prior, dict)
    assert prior["generation"] is None, "an unreadable stamp reads as absent, not as a number"
    assert "AlphaField" in " | ".join(_loss_entries(ingested)), "content lost with the counter"


# A third party's 51899-3 section: same LOINC, no anastomosis stamp, its own
# title. It is ordinary foreign content and must keep round-tripping as such.
_FOREIGN_LOSS_CODE_CCD = """<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3">
  <id root="feedface-0000-0000-0000-00000000cda3"/>
  <title>Third-party CCD</title>
  <recordTarget><patientRole>
    <id root="2.16.840.1.113883.19.5" extension="feedface-0000-0000-0000-0000000000f0"/>
    <patient><name><given>Bea</given><family>Foreign</family></name></patient>
  </patientRole></recordTarget>
  <component><structuredBody>
    <component><section>
      <code code="51899-3" codeSystem="2.16.840.1.113883.6.1"/>
      <title>Vendor Supplemental Data</title>
      <text>FOREIGN-51899-NARRATIVE kept verbatim</text>
    </section></component>
  </structuredBody></component>
</ClinicalDocument>
"""


def test_foreign_51899_section_is_not_claimed_as_ours(tmp_path: Path) -> None:
    """51899-3 is a public LOINC. An UNSTAMPED section under it belongs to
    whoever wrote the document, so it lands under ``ccda:section:51899-3`` like
    any other foreign narrative and rides the loss narrative on re-export —
    exactly as a foreign Problems narrative does. Claiming it would let a third
    party's text be re-emitted as this tool's own loss ledger."""
    source_doc = tmp_path / "foreign.xml"
    source_doc.write_text(_FOREIGN_LOSS_CODE_CCD, encoding="utf-8")
    ingested = parse_document(source_doc)
    assert ingested.patient.extensions[f"ccda:section:{LOINC_EXTENSIONS}"] == {
        "title": "Vendor Supplemental Data",
        "text": "FOREIGN-51899-NARRATIVE kept verbatim",
    }
    assert EXT_PRIOR_LOSS_NARRATIVE not in ingested.patient.extensions

    exported = tmp_path / "foreign_out.xml"
    exported.write_bytes(build_ccd(ingested))
    entries = _loss_entries(parse_document(exported))
    assert (
        "patient.extensions.ccda:section:51899-3.text = FOREIGN-51899-NARRATIVE kept verbatim"
        in entries
    )
    assert "patient.extensions.ccda:section:51899-3.title = Vendor Supplemental Data" in entries
    # Still one ledger section in the document this tool wrote.
    assert len(_loss_sections(exported.read_bytes())) == 1


def test_legacy_titled_loss_section_is_recognized_without_the_stamp(tmp_path: Path) -> None:
    """Documents exported before the templateId stamp existed carry only the
    generator's own section title. That title names this tool outright, so it
    stays a valid marker — otherwise every already-delivered document would take
    one more generation of blob growth before the fix could bite."""
    legacy = f"""<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3">
  <id root="feedface-0000-0000-0000-00000000cda4"/>
  <recordTarget><patientRole>
    <id root="2.16.840.1.113883.19.5" extension="feedface-0000-0000-0000-0000000000f1"/>
    <patient><name><given>Leg</given><family>Acy</family></name></patient>
  </patientRole></recordTarget>
  <component><structuredBody>
    <component><section>
      <code code="51899-3" codeSystem="2.16.840.1.113883.6.1"/>
      <title>{LOSS_NARRATIVE_TITLE}</title>
      <text><paragraph>patient.gender_identity = LEGACYSENTINEL</paragraph></text>
    </section></component>
  </structuredBody></component>
</ClinicalDocument>
"""
    source_doc = tmp_path / "legacy.xml"
    source_doc.write_text(legacy, encoding="utf-8")
    ingested = parse_document(source_doc)
    assert _loss_entries(ingested) == ["patient.gender_identity = LEGACYSENTINEL"]
    # No stamped generation to read: the counter restarts, the entries do not.
    assert ingested.patient.extensions[EXT_PRIOR_LOSS_NARRATIVE]["generation"] is None
    assert f"ccda:section:{LOINC_EXTENSIONS}" not in ingested.patient.extensions

    exported = tmp_path / "legacy_out.xml"
    exported.write_bytes(build_ccd(ingested))
    assert "patient.gender_identity = LEGACYSENTINEL" in _entries_of(exported.read_bytes())


def test_two_stamped_ledgers_in_one_document_merge_rather_than_overwrite(tmp_path: Path) -> None:
    """A document assembled from two exports can carry two stamped ledgers.
    Neither may overwrite the other: the entries concatenate into the one
    carry-forward key so the next export dedupes a single ledger."""
    section = f"""    <component><section>
      <templateId root="{LOSS_NARRATIVE_TEMPLATE_ROOT}" extension="1"/>
      <id root="{LOSS_NARRATIVE_GENERATION_ROOT}" extension="{{gen}}"/>
      <code code="51899-3" codeSystem="2.16.840.1.113883.6.1"/>
      <title>{LOSS_NARRATIVE_TITLE}</title>
      <text><paragraph>patient.notes = {{sentinel}}</paragraph></text>
    </section></component>"""
    merged = f"""<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3">
  <id root="feedface-0000-0000-0000-00000000cda5"/>
  <recordTarget><patientRole>
    <id root="2.16.840.1.113883.19.5" extension="feedface-0000-0000-0000-0000000000f2"/>
    <patient><name><given>Mer</given><family>Ged</family></name></patient>
  </patientRole></recordTarget>
  <component><structuredBody>
{section.format(gen=2, sentinel="MERGE-A")}
{section.format(gen=5, sentinel="MERGE-B")}
  </structuredBody></component>
</ClinicalDocument>
"""
    source_doc = tmp_path / "merged.xml"
    source_doc.write_text(merged, encoding="utf-8")
    ingested = parse_document(source_doc)
    assert _loss_entries(ingested) == ["patient.notes = MERGE-A", "patient.notes = MERGE-B"]
    # Highest generation wins — the counter must not walk backwards.
    assert ingested.patient.extensions[EXT_PRIOR_LOSS_NARRATIVE]["generation"] == 5
    exported = _loss_sections(build_ccd(ingested))
    assert len(exported) == 1
    assert exported[0].find(f"{{{V3}}}id").get("extension") == "6"


def test_carried_forward_collapses_repeats_and_keeps_distinct_entries() -> None:
    """The dedupe rule, stated directly: a prior entry the current narrative
    already restates is a repeat and drops; every distinct entry survives, and
    multiplicity survives with it (two objects sharing one value keep two
    entries, minus however many the current narrative restates)."""
    prior = [
        "medications[feedface-0000-0000-0000-00000000ab01].strength = 500 mg",
        "medications[feedface-0000-0000-0000-00000000ab02].strength = 500 mg",
        "medications[feedface-0000-0000-0000-00000000ab03].strength = 250 mg",
        "patient.gender_identity = Genderqueer",
    ]
    current = ["medications[feedface-0000-0000-0000-00000000ab99].strength = 500 mg"]
    assert _carried_forward(prior, current) == [
        # Only ONE of the two 500 mg entries is a repeat of the current one.
        "medications[feedface-0000-0000-0000-00000000ab02].strength = 500 mg",
        "medications[feedface-0000-0000-0000-00000000ab03].strength = 250 mg",
        "patient.gender_identity = Genderqueer",
    ]
    assert _carried_forward(prior, []) == prior  # nothing restated → nothing dropped


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # Per-object indices are erased from the PATH: a canonical id is a
        # declared loss, regenerated on every re-ingest.
        (
            "medications[feedface-0000-0000-0000-00000000ab01].strength = 500 mg",
            "medications[].strength = 500 mg",
        ),
        ("encounters[e1].addenda[0].text = amended", "encounters[].addenda[].text = amended"),
        # ...and NEVER from the value, which may carry brackets or its own " = ".
        ("patient.notes = dose [2] = 5 mg", "patient.notes = dose [2] = 5 mg"),
        # A line with no separator at all is its own key (never silently reshaped).
        ("no separator here", "no separator here"),
    ],
)
def test_entry_key_erases_indices_from_the_path_only(line: str, expected: str) -> None:
    assert _entry_key(line) == expected


def test_unrecognized_prior_narrative_value_narrates_instead_of_vanishing(tmp_path: Path) -> None:
    """The carry-forward exemption is scoped to a value this exporter can
    actually re-emit. A hand-set value of any other shape must narrate like any
    vendor extension — skipping it in both places is the silent drop."""
    rec = PatientRecord(
        patient=Patient(
            given_name="Odd",
            family_name="Shape",
            extensions={EXT_PRIOR_LOSS_NARRATIVE: "MALFORMEDSENTINEL"},
        ),
        extensions={EXT_PRIOR_LOSS_NARRATIVE: {"entries": ["record.notes = RECORDLEVELSENTINEL"]}},
    )
    out = tmp_path / "odd.xml"
    out.write_bytes(build_ccd(rec))
    entries = _entries_of(out.read_bytes())
    assert f"patient.extensions.{EXT_PRIOR_LOSS_NARRATIVE} = MALFORMEDSENTINEL" in entries
    # The key is only ever written onto the PATIENT by a re-ingest, so a
    # record-level one is an ordinary vendor extension and narrates too.
    assert any("RECORDLEVELSENTINEL" in entry for entry in entries)


def test_declared_losses_is_structured_and_minimal() -> None:
    # NIT 4: DECLARED_LOSSES is now a {field-path pattern: reason} mapping that
    # covers ONLY what cannot ride the loss narrative — the SOAP kind split, the
    # narrative-only-recovery caveat, and the structural id/provenance plumbing.
    assert isinstance(DECLARED_LOSSES, dict)
    assert all(isinstance(k, str) and isinstance(v, str) and v for k, v in DECLARED_LOSSES.items())
    assert "*.NoteSection.kind" in DECLARED_LOSSES
    assert any("narrative-only recovery" in pattern for pattern in DECLARED_LOSSES)
    assert "*.id" in DECLARED_LOSSES
    assert "*.provenance" in DECLARED_LOSSES


def test_every_extensions_key_declared_losses_names_is_one_that_exists(tmp_path: Path) -> None:
    """The ledger is the contract, so the keys it cites have to be real.

    The narrative tier's whole promise is "not on its typed field, but here" —
    it is only worth anything if "here" is somewhere you can look. The ledger
    named ``patient.extensions['ccda:section:51899-3']``, which is where a
    stamped ledger landed BEFORE the generation fix stopped re-ingesting it as
    one undifferentiated blob. That key has not existed since; a reader who
    followed the contract to recover a narrated field found nothing and had no
    reason to doubt the document.

    Rather than pin today's spelling, this reads whatever keys the ledger cites
    and requires each to appear on a real round trip — so the two cannot drift
    apart again in either direction.
    """
    cited = {
        key
        for reason in DECLARED_LOSSES.values()
        for key in re.findall(r"extensions\[['\"]([^'\"]+)['\"]\]", reason)
    }
    assert cited, "the ledger cites at least one extensions key"

    # A record whose only interesting field has no structured slot, so the
    # narrative tier is exercised rather than described.
    record = PatientRecord(
        patient=Patient(id="feedface-0000-0000-0000-000000000001"),
        encounters=[
            Encounter(
                id="feedface-0000-0000-0000-0000000000e1",
                patient_id="feedface-0000-0000-0000-000000000001",
                date_of_service=date(2023, 5, 10),
                encounter_type="Office visit",
                chief_complaint="Persistent cough for three weeks",
            )
        ],
    )
    blob = build_ccd(record)
    path = tmp_path / "ccd.xml"
    path.write_bytes(blob if isinstance(blob, bytes) else blob.encode())
    extensions = parse_document(path).patient.extensions or {}

    missing = sorted(key for key in cited if key not in extensions)
    assert not missing, f"DECLARED_LOSSES names extensions keys that never appear: {missing}"

    # And the promise behind the key holds: the narrated field is recoverable.
    entries = extensions[EXT_PRIOR_LOSS_NARRATIVE]["entries"]
    assert any("chief_complaint = Persistent cough for three weeks" in e for e in entries)


# --- the pinning property test (SHOULD-FIX 3) --------------------------------
#
# Build a MAXIMALLY-populated record (every field of every canonical model set to
# a distinctive synthetic value), export → re-ingest, then walk every populated
# leaf of the ORIGINAL. Each leaf must be either (a)/(b) preserved ON ITS OWN
# FIELD PATH — same field of the same collection on the re-ingested record, or a
# 51899-3 narrative line naming that path — or (c) matched by a DECLARED_LOSSES
# pattern. If a model field is added tomorrow and the exporter is not updated,
# that field's value appears in neither place and this test fails. THAT is the
# test's purpose.
#
# The oracle is path-aware rather than "the value is somewhere in the document":
# a value that comes back on the WRONG field is misattribution, not preservation,
# and a whole-document substring search cannot tell the two apart (it passed
# Identifier.kind == "mrn" purely because "mrn" occurs inside the id ROOT of a
# different field). Values are compared whitespace-collapsed, and containment
# within the same field is accepted for the declared SOAP-kind labelling.


def _maximal_record() -> PatientRecord:
    """Every field of every canonical model, populated with distinctive
    synthetic values (so substring checks cannot collide). PHI-safe: feedface-
    ids, 555 phones, SSN area >= 900, example.com."""
    pid = "feedface-pat0-0000-0000-0000000000ff"
    addr = Address(
        line1="11 Maximal Way",
        line2="Suite MAXLINE2",
        city="Faxborough",
        state="WA",
        postal_code="98199",
    )
    patient = Patient(
        id=pid,
        given_name="Maxgiven",
        middle_name="Maxmiddle",
        family_name="Maxfamily",
        suffix="MaxSuffixIII",
        birth_date=date(1971, 7, 13),
        sex="Female",  # round-trips via displayName
        gender_identity="MaxGenderIdentity",
        sexual_orientation="MaxSexualOrientation",
        race=["MaxRaceAsian"],
        ethnicity=["MaxEthnNotHispanic"],
        language="en",
        marital_status="MaxMarriedStatus",
        mothers_maiden_name="MaxMaidenName",
        contact_preference="MaxContactPref",
        status="MaxActiveStatus",
        notes="MaxPatientNotes block.",
        identifiers=[
            Identifier(kind=IdentifierKind.SSN, value="901-55-0199"),
            Identifier(kind=IdentifierKind.SOURCE_GUID, value=pid, system="2.16.840.1.113883.19.5"),
            Identifier(kind=IdentifierKind.MRN, value="MaxMRN0001"),
        ],
        telecom=[
            ContactPoint(kind=ContactKind.PHONE_HOME, value="(206) 555-0143"),
            ContactPoint(kind=ContactKind.EMAIL, value="maxpatient@example.com"),
        ],
        addresses=[addr],
        contacts=[
            PatientContact(
                name="MaxContactName",
                relationship="MaxContactRel",
                phone="(206) 555-0144",
                address=Address(line1="22 Contact Rd", city="Faxborough"),
            )
        ],
        guarantor=Guarantor(
            name="MaxGuarantorName",
            relationship_to_patient="MaxGuarantorRel",
            birth_date=date(1950, 2, 2),
            sex="Male",
            ssn="901-55-0200",
            address=Address(line1="33 Guarantor Blvd", city="Faxborough"),
            phones=[ContactPoint(kind=ContactKind.PHONE_WORK, value="(206) 555-0145")],
            payment_preference="MaxPaymentPref",
        ),
    )
    observations = [
        Observation(
            patient_id=pid,
            encounter_id="feedface-0000-0000-0000-0000000000a1",
            category=ObservationCategory.VITAL_SIGNS,
            code="8480-6",
            display="Systolic blood pressure",
            value="121",
            unit="mm[Hg]",
            effective_at=_AT,
            recorded_at=_AT,
        ),
        Observation(
            patient_id=pid,
            category=ObservationCategory.SOCIAL_HISTORY,
            display="Tobacco use",
            value="MaxNeverSmoker",
            effective_at=_AT,
        ),
        Observation(
            patient_id=pid,
            category=ObservationCategory.SCREENING,
            display="MaxScreeningDisplay",
            value="MaxScreeningValue",
        ),
    ]
    conditions = [
        Condition(
            patient_id=pid,
            icd10="I10",
            snomed="38341003",
            display="MaxHypertension",
            acuity="MaxAcuityChronic",
            onset=date(2011, 1, 1),
            stopped=date(2012, 2, 2),
            recorded_at=_AT,
            active=True,
        )
    ]
    allergies = [
        AllergyIntolerance(
            patient_id=pid,
            substance="MaxPenicillin",
            category=AllergyCategory.DRUG,
            reactions=["MaxHives"],
            severity="MaxModerate",
            onset=date(2013, 3, 3),
            active=True,
            extensions={"ccda:allergen_code": "7980"},
        )
    ]
    medications = [
        MedicationStatement(
            patient_id=pid,
            generic_name="MaxGenericLisinopril",
            brand_name="MaxBrandPrinivil",
            strength="MaxStrength10mg",
            route="MaxRouteOral",
            dose_form="MaxDoseFormTablet",
            display_name="MaxDisplayLisinopril 10 MG",
            sig="MaxSigOnceDaily",
            associated_dx="MaxAssocDxHTN",
            rxnorm="314076",
            start=date(2014, 4, 4),
            stop=date(2015, 5, 5),
            last_modified_at=_AT,
            active=True,
            prescription_ids=["feedface-0000-0000-0000-0000000000b1"],
            extensions={"ccda:route": "Oral", "ccda:dose": "1 {tablet}"},
        )
    ]
    prescriptions = [
        Prescription(
            patient_id=pid,
            medication_id="feedface-0000-0000-0000-0000000000c1",
            prescriber_id="feedface-0000-0000-0000-0000000000c2",
            prefix="ESCRIPT",
            status_label="MaxStatusDispensed",
            display_date=_AT,
            sig="MaxRxSig",
            refills="MaxRefills3",
            quantity="MaxQuantity30",
            transactions=[
                PrescriptionTransaction(
                    kind="MaxTxnSent",
                    description="MaxTxnDesc",
                    note="MaxTxnNote",
                    at=_AT,
                    destination_type="MaxTxnDest",
                )
            ],
        )
    ]
    immunizations = [
        Immunization(
            patient_id=pid,
            vaccine="MaxInfluenzaVaccine",
            administered_on=date(2016, 6, 6),
            source="MaxImmSource",
            lot_number="MaxLot0001",
            expires=date(2031, 7, 7),
            comment="MaxImmComment",
        )
    ]
    family_history = [
        FamilyMemberHistory(
            patient_id=pid,
            diagnosis="MaxFamDiagnosis",
            relation="MaxFamRelation",
            onset_date=date(1990, 9, 9),
        )
    ]
    past_medical_history = [
        PastMedicalHistory(patient_id=pid, kind="MaxPmhKind", text="MaxPmhText block.")
    ]
    advance_directives = [
        AdvanceDirective(patient_id=pid, directive="MaxDirectiveDNR", recorded_at=_AT)
    ]
    goals = [Goal(patient_id=pid, description="MaxGoal", effective=date(2018, 1, 1), active=True)]
    # Health concerns reuse Goal and, like goals, have no structured C-CDA
    # emitter — so the oracle below is what proves they reach the loss
    # narrative instead of falling out of the export unremarked.
    health_concerns = [
        Goal(
            patient_id=pid,
            description="MaxHealthConcern",
            effective=date(2017, 2, 3),
            active=False,
        )
    ]
    # Screening events have no structured C-CDA emitter either, so the oracle
    # is what proves they narrate instead of vanishing on export.
    screening_events = [
        ScreeningEvent(
            patient_id=pid,
            encounter_id="feedface-0000-0000-0000-0000000000a1",
            name="MaxScreeningName",
            result="MaxScreeningResult",
            comments="MaxScreeningComments",
            negated=True,
        )
    ]
    coverages = [
        Coverage(
            patient_id=pid,
            payer="MaxPayer",
            plan_name="MaxPlanName",
            plan_type="MaxPlanTypePPO",
            coverage_type="MaxCoverageMedical",
            member_id="MaxMember0001",
            group_number="MaxGroup0001",
            order_of_benefits=3,
            priority_label="MaxPrimaryPayer",
            employer="MaxEmployer",
            relationship_to_insured="MaxRelInsured",
            payment_type="MaxPaymentType",
            copay="MaxCopay25",
            start=date(2019, 1, 1),
            end=date(2020, 12, 31),
            active=True,
            status_label="MaxCoverageStatus",
        )
    ]
    documents = [
        DocumentArtifact(
            patient_id=pid,
            encounter_id="feedface-0000-0000-0000-0000000000e1",
            path="MaxDocPath/chart.pdf",
            sha256="MaxSha256Digest",
            mime_type="application/pdf",
            title="MaxDocTitle",
            page_count=13,
            pack_name="MaxPackName",
            generated_at=_AT,
        )
    ]
    practitioners = [
        Practitioner(
            given_name="MaxProvGiven",
            family_name="MaxProvFamily",
            display_name="MaxProvDisplay",
            credential="MaxProvCredential",
            npi="MaxNPI0001",
        )
    ]
    facilities = [
        Facility(
            name="MaxFacilityName",
            address_line1="44 Facility St",
            address_line2="MaxFacLine2",
            city="Faxborough",
            state="WA",
            postal_code="98198",
            phone="(206) 555-0146",
            fax="(206) 555-0147",
        )
    ]
    encounters = [
        Encounter(
            id="feedface-0000-0000-0000-0000000000f1",
            patient_id=pid,
            date_of_service=date(2021, 8, 8),
            chief_complaint="MaxChiefComplaint",
            encounter_type="MaxEncounterType",
            note_type="MaxEncounterType",
            provider_id="feedface-0000-0000-0000-0000000000f2",
            facility_id="feedface-0000-0000-0000-0000000000f3",
            signed_by_id="feedface-0000-0000-0000-0000000000f4",
            signed_at=_AT,
            last_modified_at=_AT,
            sections=[
                NoteSection(
                    kind=SectionKind.SUBJECTIVE,
                    title="MaxSubjTitle",
                    html="<p>MaxSubjHtml</p>",
                    text="MaxSubjectiveText body.",
                )
            ],
            addenda=[
                Addendum(
                    text="MaxAddendumText amendment.",
                    status="MaxAddendumStatus",
                    author_name="MaxAddendumAuthor",
                    author_credential="MaxAddendumCred",
                    source="MaxAddendumSource",
                    at=_AT,
                )
            ],
            diagnosis_ids=["feedface-0000-0000-0000-0000000000f5"],
        )
    ]
    # Record-LEVEL extensions: the vendor namespaces a source hangs off the
    # record itself (pf_tebra's unmapped tables, fhir_r4's note metadata). They
    # have no typed home at all, so the 51899-3 narrative is their only route
    # out — the loss class the collector used to skip entirely.
    extensions = {
        "pf_tebra:unmapped:patient-procedures": [
            {"ProcedureName": "MaxRecordExtProcedure", "ProcedureCode": "MaxRecordExtCode"}
        ],
        "fhir_r4:note_meta": {"n1": {"fhir_r4:status": "MaxRecordExtStatus"}},
    }
    return PatientRecord(
        patient=patient,
        encounters=encounters,
        observations=observations,
        conditions=conditions,
        allergies=allergies,
        medications=medications,
        prescriptions=prescriptions,
        immunizations=immunizations,
        family_history=family_history,
        past_medical_history=past_medical_history,
        advance_directives=advance_directives,
        goals=goals,
        health_concerns=health_concerns,
        screening_events=screening_events,
        coverages=coverages,
        documents=documents,
        practitioners=practitioners,
        facilities=facilities,
        extensions=extensions,
    )


def _is_declared_loss(path: tuple[str | int, ...]) -> bool:
    """Whether ``path`` (a tuple of dict keys / list indices into the json dump)
    is covered by a DECLARED_LOSSES pattern — the structural plumbing and the
    SOAP kind split that cannot ride the loss narrative."""
    str_segments = [seg for seg in path if isinstance(seg, str)]
    # *.id and *.provenance — identity / ingest metadata, regenerated on parse.
    if "id" in str_segments or "provenance" in str_segments:
        return True
    # *.NoteSection.kind — the per-kind split collapses to one narrative section.
    if "sections" in str_segments and str_segments[-1] == "kind":
        return True
    return False


def _walk_leaves(value: object, path: tuple[str | int, ...] = ()) -> list[tuple[tuple, str]]:
    """Every populated scalar leaf of a json-native value as (path, str-value),
    pruning None and empty containers."""
    out: list[tuple[tuple, str]] = []
    if value is None:
        return out
    if isinstance(value, dict):
        for key in value:
            out += _walk_leaves(value[key], (*path, key))
    elif isinstance(value, list):
        for index, element in enumerate(value):
            out += _walk_leaves(element, (*path, index))
    else:
        text = str(value)
        if text != "":
            out.append((path, text))
    return out


def _field_path(path: tuple[str | int, ...]) -> str:
    """``path`` as a dotted FIELD path with list indices erased —
    ``("coverages", 0, "payer")`` → ``coverages[].payer``.

    Erasing the index (not the field) is what makes the oracle path-AWARE
    without being order-brittle: a value must come back on the same field of the
    same collection, but the collection may be re-ordered or re-keyed."""
    out = ""
    for segment in path:
        if isinstance(segment, int):
            out += "[]"
        else:
            out += segment if out == "" else f".{segment}"
    return out


def _structured_values(record: PatientRecord) -> dict[str, set[str]]:
    """``field path -> values`` for every populated leaf of a record — the
    locations a value may legitimately come back on."""
    out: dict[str, set[str]] = {}
    for path, value in _walk_leaves(dumped(record)):
        out.setdefault(_field_path(path), set()).add(_collapse(value))
    return out


def dumped(record: PatientRecord) -> dict:
    return record.model_dump(mode="json")


def _collapse(text: str) -> str:
    """Whitespace collapsed the way the C-CDA parser collapses narrative text,
    so a value compares equal whether it came back structured or narrated."""
    return " ".join(text.split())


def _narrated_values(record: PatientRecord) -> dict[str, set[str]]:
    """``field path -> values`` for every line of the recovered 51899-3 loss
    narrative — the declared, path-carrying home for what CDA cannot structure.

    The entries come back already split (one per emitted paragraph, recovered
    under ``ccda:prior_loss_narrative``), so no re-splitting heuristic is needed.
    The narrative roots the record's own dict attrs at a synthetic ``record.``
    (there is no model to name); the dump paths root them at the attribute, so
    that prefix is normalized away here."""
    out: dict[str, set[str]] = {}
    for line in _loss_entries(record):
        path, sep, value = line.partition(" = ")
        if not sep:
            continue
        field = re.sub(r"\[[^\]]*\]", "[]", path).removeprefix("record.")
        out.setdefault(field, set()).add(_collapse(value))
    return out


def _survives(recovered: dict[str, set[str]], path: str, value: str) -> bool:
    """Whether ``value`` comes back ON ``path``: equal to a recovered value
    there, or contained in one.

    Containment covers the declared ``*.NoteSection.kind`` loss — a note body
    re-ingests LABELLED with its SOAP kind, so the body is a substring of the
    recovered text on that same field. It stays scoped to the one field path, so
    a cross-field collision or a misattribution to another model can never
    satisfy it."""
    candidates = recovered.get(path, set())
    return value in candidates or any(value in candidate for candidate in candidates)


def _undeclared_losses(original: PatientRecord, out: Path) -> list[str]:
    """Populated leaves of ``original`` that survive re-ingest in no form.

    A leaf passes if it comes back (a) on its own field path structurally,
    (b) narrated under that path in the 51899-3 section, or (c) matched by a
    DECLARED_LOSSES pattern. Anything else is a silent loss.
    """
    out.write_bytes(build_ccd(original))
    reingested = parse_document(out)
    recovered = _structured_values(reingested)
    for field, values in _narrated_values(reingested).items():
        recovered.setdefault(field, set()).update(values)

    return [
        f"{'.'.join(str(p) for p in path)} = {value!r}"
        for path, value in _walk_leaves(dumped(original))
        if not _is_declared_loss(path)
        and not _survives(recovered, _field_path(path), _collapse(value))
    ]


def test_no_undeclared_native_loss(tmp_path: Path) -> None:
    """Every populated leaf of the source record must come back from re-ingest
    ON ITS OWN FIELD PATH — structurally, or as a narrative line naming that
    path — or be covered by a DECLARED_LOSSES pattern.

    Path-aware on purpose: "the value appears somewhere in the document" would
    pass a value that came back on the WRONG field (a cross-field collision) or
    on another patient's model (a misattribution), which is corruption, not
    preservation."""
    undeclared = _undeclared_losses(_maximal_record(), tmp_path / "max.xml")
    assert not undeclared, (
        "fields silently lost (not round-tripped onto their own field path, not "
        "narrated under it in the 51899-3 section, and not a declared loss): "
        + "; ".join(undeclared)
    )


@pytest.mark.parametrize(
    ("shape", "encounter"),
    [
        ("typed — the only shape the maximal record has", {"encounter_type": "Office visit"}),
        # The two that were losing a field. Neither is exotic: a thin visit row
        # with a date and nothing else, and a note header with no body yet.
        ("no type, no note content", {}),
        (
            "no type, but a note",
            {
                "note_type": "SOAP",
                "sections": [NoteSection(kind=SectionKind.NARRATIVE, text="Reports a cough.")],
            },
        ),
        (
            "typed, note_type set, no note content",
            {"encounter_type": "Office visit", "note_type": "SOAP"},
        ),
    ],
)
def test_no_undeclared_loss_whichever_section_takes_the_encounter(
    tmp_path: Path, shape: str, encounter: dict[str, object]
) -> None:
    """The oracle above, run over every gate combination rather than one.

    Two gates decide where an encounter goes — ``encounter_type`` for the
    Encounters section, note content for Notes — and an encounter can clear
    neither. The consumed-field allowlist claimed all five fields regardless,
    so an encounter no emitter wrote still had its fields suppressed from the
    narrative. Two shapes lost a field outright: a typeless, noteless visit
    lost ``date_of_service``, and a note header with no body lost ``note_type``.

    Every encounter in ``_maximal_record`` carries a type, which is the only
    reason the oracle never saw it.
    """
    record = PatientRecord(
        patient=Patient(id="feedface-0000-0000-0000-000000000001"),
        encounters=[
            Encounter(
                id="feedface-0000-0000-0000-0000000000e1",
                patient_id="feedface-0000-0000-0000-000000000001",
                date_of_service=date(2023, 5, 10),
                chief_complaint="Cough for three weeks",
                **encounter,  # type: ignore[arg-type]
            )
        ],
    )
    undeclared = _undeclared_losses(record, tmp_path / "shape.xml")
    assert not undeclared, f"silently lost for a {shape} encounter: {'; '.join(undeclared)}"


# A field the emitter round-trips AND the narrative also carries. The one that
# earns it: Identifier.kind is not fully recoverable, because the parser rebuilds
# the kind from the id's OID and only SSN has one — an MRN comes back as
# SOURCE_GUID. So the whole field is narrated, and the two kinds that happen to
# survive structurally are carried twice as a consequence of the one that cannot.
_PARTIALLY_RECOVERABLE = frozenset({"patient.identifiers[].kind"})


def test_a_field_the_export_round_trips_is_not_also_narrated(tmp_path: Path) -> None:
    """The bound the losslessness invariant does not have.

    That invariant is one-directional: it forbids losing a field, and says
    nothing about carrying one twice. So nothing anywhere checked that
    ``_EXPORTED_FIELDS`` is load-bearing — emptying it entirely passes all 1815
    tests, while every field that already round-trips structurally also gets
    written into the ledger. Not a loss, so no oracle objects; just a document
    that grows, and a ledger whose entries stop meaning "this did not survive".
    That is the same growth the generation fix bounded from the other end.

    Stated as a property rather than a count, so it survives new fields: a value
    recovered on its own field path structurally must not ALSO come back as a
    narrative line for that path, except where the field is only partly
    recoverable and the narrative is what makes it whole.
    """
    original = _maximal_record()
    out = tmp_path / "max.xml"
    out.write_bytes(build_ccd(original))
    reingested = parse_document(out)
    structured = _structured_values(reingested)
    narrated = _narrated_values(reingested)

    redundant = sorted(
        {
            _field_path(path)
            for path, value in _walk_leaves(dumped(original))
            if not _is_declared_loss(path)
            and _survives(structured, _field_path(path), _collapse(value))
            and _survives(narrated, _field_path(path), _collapse(value))
        }
        - _PARTIALLY_RECOVERABLE
    )
    assert not redundant, (
        "carried both structurally and in the loss narrative, so the emitter's "
        "consumed-field allowlist is not doing its job: " + "; ".join(redundant)
    )


# --- determinism + well-formedness -------------------------------------------


def test_two_builds_are_byte_identical(source: PatientRecord) -> None:
    """One record object, built twice.

    Necessary and not sufficient: this holds even when the export is NOT
    reproducible, because the ids were minted once when the object was made and
    both builds see the same ones. The determinism the docs promise is about the
    same RECORD, not the same object — see the two tests below.
    """
    assert build_ccd(source) == build_ccd(source)


def test_two_ingests_of_one_export_build_identical_documents(tmp_path: Path) -> None:
    """The contract as `docs/CCDA_EXPORT.md` states it: same record in,
    byte-identical bytes out.

    It did not hold. Two `anast migrate` runs over the same fixture produced
    documents differing in 168, 80 and 48 lines — 248 of them a runtime uuid,
    48 a wall clock. Clinical content was unaffected, so nothing failed; what
    broke was checksum dedup and every "re-run and diff to see what changed"
    workflow, which is how a migration is audited.

    Two independent ingests, because that is where fresh ids come from.
    """
    import anastomosis.sources.pf_tebra  # noqa: F401 — registers the adapter
    from anastomosis.sources import get_source

    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"
    first = {r.patient.id: build_ccd(r) for r in get_source("pf-tebra").load(fixture)}
    second = {r.patient.id: build_ccd(r) for r in get_source("pf-tebra").load(fixture)}

    assert set(first) == set(second)
    differing = sorted(pid for pid in first if first[pid] != second[pid])
    assert not differing, f"{len(differing)} document(s) differ between two ingests"


def test_a_runtime_minted_id_does_not_reach_the_narrative(tmp_path: Path) -> None:
    """A collection entry was subscripted by the object's own id.

    `_model_index` returned it and called itself "a stable per-item index"; for
    every model the adapter mints rather than reads, that id is a fresh uuid4
    per load. The narrative is sorted by path, so both the ids and the line
    ORDER moved every run. The subscript is positional now, as it already was
    for every nested list in the same serializer.
    """
    pid = "feedface-0000-0000-0000-0000000000b1"
    lines = _loss_entries_of(
        PatientRecord(
            patient=Patient(id=pid, given_name="Sub", family_name="Script"),
            advance_directives=[
                AdvanceDirective(patient_id=pid, directive="Do not resuscitate."),
            ],
        )
    )
    directive = next(line for line in lines if "directive" in line)
    assert "advance_directives[0]." in directive, directive
    assert not _UUID_RE.search(directive), f"a runtime id reached the ledger: {directive}"


def test_provenance_nested_in_an_extension_is_not_narrated(tmp_path: Path) -> None:
    """`DECLARED_LOSSES["*.provenance"]` says provenance is never narrated. The
    `*` means any depth; the code implemented it only at the top level of a
    model, and extension payloads — which carry whole model dumps, ingest
    timestamp and all — were walked by a serializer with no notion of it."""
    pid = "feedface-0000-0000-0000-0000000000b2"
    lines = _loss_entries_of(
        PatientRecord(
            patient=Patient(id=pid, given_name="Nest", family_name="Prov"),
            extensions={
                "vendor:parked": [
                    {
                        "reason": "empty_note",
                        "encounter": {
                            "chief_complaint": "KEEPSENTINEL",
                            "provenance": {
                                "source_system": "vendor",
                                "ingested_at": "2026-08-28T05:31:25.864176Z",
                            },
                        },
                    }
                ]
            },
        )
    )
    blob = " | ".join(lines)
    assert "KEEPSENTINEL" in blob, "the payload itself must still narrate"
    assert "ingested_at" not in blob
    assert "source_system" not in blob


_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _loss_entries_of(record: PatientRecord) -> list[str]:
    """The ledger lines a record's own export writes (no re-ingest)."""
    return _entries_of(build_ccd(record))


def test_default_document_id_is_deterministic_uuid5(source: PatientRecord) -> None:
    # No explicit id → derived from patient id, so stable across builds.
    first = parse_document_bytes(build_ccd(source))
    second = parse_document_bytes(build_ccd(source))
    assert (
        first.patient.extensions["ccda:documentId"]
        == (second.patient.extensions["ccda:documentId"])
    )


def test_well_formed_under_hardened_parser(source: PatientRecord) -> None:
    root = etree.fromstring(build_ccd(source), _PARSER)
    assert etree.QName(root).localname == "ClinicalDocument"
    assert root.tag == f"{{{V3}}}ClinicalDocument"
    # xsi:type usage parses (the namespace is declared and used on values).
    typed = root.findall(f".//{{{V3}}}value[@{{{XSI}}}type]")
    assert typed, "expected xsi:type-qualified <value> elements"
    assert any(v.get(f"{{{XSI}}}type") == "CD" for v in typed)
    assert any(v.get(f"{{{XSI}}}type") == "PQ" for v in typed)


# --- empty record + nullFlavor -----------------------------------------------


def test_empty_record_exports_and_reingests_cleanly(tmp_path: Path) -> None:
    rec = PatientRecord(patient=Patient(given_name="Pat", family_name="Empty"))
    out = tmp_path / "e.xml"
    out.write_bytes(build_ccd(rec))
    rt = parse_document(out)
    assert rt.patient.display_name == "Pat Empty"
    # Sentinel discipline: absent fields come back None / empty, never placeholders.
    assert rt.patient.sex is None
    assert rt.patient.birth_date is None
    assert rt.patient.telecom == []
    assert rt.patient.addresses == []
    assert rt.conditions == []
    assert rt.medications == []


def test_absent_phone_and_address_become_nullflavor_then_none(tmp_path: Path) -> None:
    rec = PatientRecord(
        patient=Patient(
            given_name="No",
            family_name="Contact",
            birth_date=date(1990, 1, 1),
            sex="Male",
            identifiers=[Identifier(kind=IdentifierKind.SSN, value="901-00-0001")],
        )
    )
    out = tmp_path / "n.xml"
    out.write_bytes(build_ccd(rec))
    # The serialized doc uses nullFlavor (not empty elements) for absent contact.
    root = etree.fromstring(out.read_bytes(), _PARSER)
    role = root.find(f".//{{{V3}}}recordTarget/{{{V3}}}patientRole")
    assert role is not None
    assert role.find(f"{{{V3}}}telecom").get("nullFlavor") == "NI"
    assert role.find(f"{{{V3}}}addr").get("nullFlavor") == "NI"
    # And the re-ingest yields None/empty, never "" or a placeholder.
    rt = parse_document(out)
    assert rt.patient.telecom == []
    assert rt.patient.addresses == []
    assert rt.patient.sex == "Male"  # present field still round-trips


# --- deliverer ---------------------------------------------------------------


def test_deliverer_writes_one_xml_per_patient_in_secure_dir(tmp_path: Path) -> None:
    records = [
        _rich_record(),
        PatientRecord(
            patient=Patient(
                id="feedface-pat0-0000-0000-000000000002", given_name="Sam", family_name="Two"
            )
        ),
    ]
    out = tmp_path / "ccda_out"
    written = deliver_ccda(records, out).paths
    assert len(written) == 2
    # Filenames are patient ids only (no name-derived component).
    names = sorted(p.name for p in written)
    assert names == [f"{r.patient.id}.xml" for r in sorted(records, key=lambda r: r.patient.id)]
    for r in records:
        assert "Two" not in (out / f"{r.patient.id}.xml").name
    # Secure output dir: PHI README present; 0700 on POSIX.
    assert (out / "_PHI_WARNING_README.txt").exists()
    if os.name == "posix":
        assert stat.S_IMODE(out.stat().st_mode) == 0o700
    # Each written file is a valid CCD that re-ingests.
    for path in written:
        assert parse_document(path).patient.display_name


# --- CLI end to end ----------------------------------------------------------


class _FakeChromium:
    """Writes a real one-page PDF so the pipeline's render/QA stages run."""

    def __init__(self, **kwargs: object) -> None:
        pass

    def render(self, html: str, pdf_path: Path) -> None:
        write_text_pdf(html, pdf_path)

    def close(self) -> None:
        pass


def test_cli_pipeline_run_ccda_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("pymupdf", reason="pipeline e2e needs PyMuPDF (render extra)")
    import anastomosis.reconstruct.chromium as chromium

    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)
    charts = tmp_path / "charts"
    ccda_dir = tmp_path / "ccda"
    result = runner.invoke(
        app,
        ["pipeline", "run", str(PF_FIXTURE), "--out", str(charts), "--ccda", str(ccda_dir)],
    )
    assert result.exit_code == 0, result.output
    assert "C-CDA:" in result.output
    xmls = sorted(ccda_dir.glob("*.xml"))
    assert xmls, "expected one CCD per patient"
    assert (ccda_dir / "_PHI_WARNING_README.txt").exists()
    # Every emitted document is a real CCD this repo's parser accepts.
    for path in xmls:
        assert parse_document(path).patient.display_name


# --- PHI probe ---------------------------------------------------------------


def test_no_patient_values_logged_on_export(caplog: pytest.LogCaptureFixture) -> None:
    rec = _rich_record()
    with caplog.at_level(logging.DEBUG, logger="anastomosis.deliver.ccda_export.builder"):
        build_ccd(rec)
    blob = " ".join(r.getMessage() for r in caplog.records)
    # Counts and the run-scoped surrogate may appear; patient-derived values and
    # the raw source patient GUID may not.
    assert "Cora" not in blob
    assert "Specimen" not in blob
    assert "901-65-4329" not in blob
    assert "cora.specimen@example.com" not in blob
    assert rec.patient.id not in blob


def test_no_patient_values_logged_on_deliverer_failure(
    caplog: pytest.LogCaptureFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force a build failure; the deliverer must log the exception TYPE only.
    import anastomosis.deliver.ccda_export.deliverer as deliverer_mod

    def _boom(_record: PatientRecord, **_kw: object) -> bytes:
        raise RuntimeError("Cora Specimen 901-65-4329")  # message embeds PHI

    monkeypatch.setattr(deliverer_mod, "build_ccd", _boom)
    rec = _rich_record()
    with caplog.at_level(logging.DEBUG, logger="anastomosis.deliver.ccda_export.deliverer"):
        written = deliver_ccda([rec], tmp_path / "out")
    assert written.paths == []
    # The patient that failed to build is COUNTED, not just dropped — the caller
    # needs a number to report, or a whole chart vanishes behind a green line.
    assert written.missing_count == 1
    blob = " ".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in blob  # the exception type IS logged
    assert "Cora" not in blob and "901-65-4329" not in blob
    # The pid logged is the run-scoped surrogate, never the raw source GUID.
    assert rec.patient.id not in blob


def test_deliverer_never_logs_output_path(caplog: pytest.LogCaptureFixture, tmp_path: Path) -> None:
    """The ccda deliverer's completion log carries counts only — never the output
    PATH. An operator dir named after a patient would otherwise enter the logs
    (SECURITY.md: never a path)."""
    rec = _rich_record()
    # A directory whose NAME is a stand-in for a patient-derived operator dir.
    out = tmp_path / "Specimen_Cora_export"
    with caplog.at_level(logging.DEBUG, logger="anastomosis.deliver.ccda_export.deliverer"):
        deliver_ccda([rec], out)
    blob = " ".join(r.getMessage() for r in caplog.records)
    # No os.sep-joined output-dir string (and no bare dir name) reaches the log.
    assert str(out) not in blob
    assert out.name not in blob
    # The PHI-safe completion count IS logged.
    assert "1 of 1 patient" in blob


def test_deliverer_refuses_two_patient_ids_that_sanitize_alike(tmp_path: Path) -> None:
    """``MRN 1234`` and ``MRN/1234`` both sanitize to ``MRN_1234``.

    ``write_bytes`` overwrites, so a silent collision would leave ONE
    ``MRN_1234.xml`` carrying the second patient's chart over the first — a
    C-CDA is the artifact most likely to travel, so a merged one is the worst
    kind of wrong. The batch-continues handler deliberately does NOT swallow
    this: a collision is not a survivable per-record failure.
    """
    from anastomosis.deliver._shared import DeliveredNameCollision

    first = _rich_record()
    second = _rich_record()
    first.patient.id = "MRN 1234"
    second.patient.id = "MRN/1234"

    with pytest.raises(DeliveredNameCollision, match="C-CDA document"):
        deliver_ccda([first, second], tmp_path / "export")


# --- helpers -----------------------------------------------------------------


def parse_document_bytes(data: bytes) -> PatientRecord:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "doc.xml"
        path.write_bytes(data)
        return parse_document(path)


@pytest.mark.parametrize(
    ("source_sex", "expect_code"),
    [("F", "F"), ("M", "M"), ("Female", "F"), ("male", "M"), ("f", "F")],
)
def test_the_real_gender_spellings_get_a_real_gender_code(
    source_sex: str, expect_code: str
) -> None:
    """The lookup used to be exact-match over "Female"/"Male" (#242).

    The real Practice Fusion Gender column holds "M"/"F" — the reference
    generator translates {'M': 'Male', 'F': 'Female'} off it — so every one of
    2,167 patients fell through to "UN", Undifferentiated. A receiving EHR reads
    the normative @code; @displayName is decorative in CDA. Sex-keyed decision
    support, screening reminders and reference ranges all mis-key on that.
    """
    blob = build_ccd(
        PatientRecord(patient=Patient(id="feedface-0000-0000-0000-000000000001", sex=source_sex))
    )
    text = blob.decode() if isinstance(blob, bytes) else str(blob)
    (emitted,) = re.findall(r"<administrativeGenderCode[^>]*/>", text)
    assert f'code="{expect_code}"' in emitted
    assert 'code="UN"' not in emitted
    # displayName stays verbatim — the parser reads it first, so it is the round trip.
    assert f'displayName="{source_sex}"' in emitted


def test_an_unmapped_gender_is_nullFlavoured_not_given_a_fabricated_code(tmp_path: Path) -> None:
    """ "UN" is the real code for Undifferentiated — a clinical claim about the
    patient, not a shrug at a string we did not recognise, and the receiver
    cannot tell the two apart. Say nothing, and carry the source spelling.
    """
    blob = build_ccd(
        PatientRecord(patient=Patient(id="feedface-0000-0000-0000-000000000001", sex="Nonbinary"))
    )
    text = blob.decode() if isinstance(blob, bytes) else str(blob)
    assert 'nullFlavor="OTH"' in text
    assert 'code="UN"' not in text
    assert "<originalText>Nonbinary</originalText>" in text

    # And it survives the round trip rather than being lost to the nullFlavor.
    path = tmp_path / "ccd.xml"
    path.write_bytes(blob if isinstance(blob, bytes) else blob.encode())
    assert parse_document(path).patient.sex == "Nonbinary"


def _encounter_ccd(tmp_path: Path, encounter_type: str | None) -> tuple[str, PatientRecord]:
    """One encounter through build → parse, returning the XML and what came back."""
    record = PatientRecord(
        patient=Patient(id="feedface-0000-0000-0000-000000000001"),
        encounters=[
            Encounter(
                id="feedface-0000-0000-0000-0000000000e1",
                patient_id="feedface-0000-0000-0000-000000000001",
                date_of_service=date(2023, 5, 10),
                encounter_type=encounter_type,
            )
        ],
    )
    blob = build_ccd(record)
    path = tmp_path / "ccd.xml"
    path.write_bytes(blob if isinstance(blob, bytes) else blob.encode())
    return (blob.decode() if isinstance(blob, bytes) else str(blob)), parse_document(path)


def test_an_encounter_is_not_given_a_fabricated_cpt_code(tmp_path: Path) -> None:
    """No source field carries a CPT code, so the export must not assert one.

    Every encounter went out as ``code="99999" codeSystem="…6.12"`` — the CPT
    OID — with the real type demoted to @displayName. 99999 is not an assigned
    CPT code, but nothing downstream can tell that from a placeholder: a coded
    <code> with a codeSystem is a claim, and a receiver reconciling code against
    displayName resolves in the code's favour. Every migrated visit in the
    practice lands under one invented procedure.
    """
    text, _ = _encounter_ccd(tmp_path, "Office outpatient visit 15 minutes")
    assert "99999" not in text
    assert "2.16.840.1.113883.6.12" not in text, "the CPT OID is not claimed at all"


def test_an_encounter_type_travels_as_text_and_survives_the_round_trip(
    tmp_path: Path,
) -> None:
    """Saying nothing must not mean losing the type. OTH — the value is real and
    outside CPT — with the words in originalText, which is what comes back."""
    text, reingested = _encounter_ccd(tmp_path, "Office outpatient visit 15 minutes")
    assert 'nullFlavor="OTH"' in text
    assert "<originalText>Office outpatient visit 15 minutes</originalText>" in text
    (enc,) = reingested.encounters
    assert enc.encounter_type == "Office outpatient visit 15 minutes"


def test_an_encounter_with_no_type_says_no_information() -> None:
    """The helper directly, because no document reaches this branch.

    ``_structured_encounters`` keeps only typed encounters, so a typeless one is
    filtered out before the section is built and never gets a <code> at all.
    The branch is still worth pinning: OTH is the wrong fallback if that filter
    ever widens, since it asserts a real value outside CPT while showing none of
    it. NI says what is true instead.
    """
    from anastomosis.deliver.ccda_export.builder import _encounter_code

    node = etree.Element("encounter")
    _encounter_code(node, None)
    (code,) = list(node)
    assert code.get("nullFlavor") == "NI"
    assert code.get("code") is None and code.get("codeSystem") is None
    assert list(code) == [], "nothing to show, so nothing shown"


# --- how much of what a destination receives is preservation (#118) -----------
#
# The C-CDA is the one artifact handed to somebody else's EHR. On a real
# Practice Fusion export the preserved-source-fields section was 97% of it —
# 1.6 MB of narrative accompanying 49 KB of clinical content — and nothing
# measured it, so the operator found out when the destination refused the file.


def test_a_document_reports_its_size_and_its_preservation_share() -> None:
    from anastomosis.deliver.ccda_export.builder import measure_ccd

    record = _generation_record()
    measured = measure_ccd(build_ccd(record))

    assert measured.total_bytes == len(build_ccd(record))
    assert 0 < measured.preserved_bytes < measured.total_bytes
    assert 0.0 < measured.preserved_share < 1.0


def test_a_record_with_nothing_unmapped_measures_no_preservation() -> None:
    """Zero is the honest answer, not a division error or a missing key."""
    from anastomosis.core.model import Patient, PatientRecord
    from anastomosis.deliver.ccda_export.builder import measure_ccd

    bare = PatientRecord(
        patient=Patient(
            id="feedface-0000-0000-0000-0000000000ff",
            given_name="Synthia",
            family_name="Probe",
        )
    )
    measured = measure_ccd(build_ccd(bare))
    assert measured.preserved_bytes == 0
    assert measured.preserved_share == 0.0
    assert measured.total_bytes > 0


def test_an_empty_document_has_a_share_rather_than_a_zero_division() -> None:
    from anastomosis.deliver.ccda_export.builder import CcdMeasurement

    assert CcdMeasurement(total_bytes=0, preserved_bytes=0).preserved_share == 0.0


def test_the_export_reports_the_shape_of_what_it_wrote(tmp_path: Path) -> None:
    from anastomosis.deliver.ccda_export import deliver_ccda

    records = [_generation_record(), _generation_record()]
    records[1].patient.id = "feedface-0000-0000-0000-0000000000ab"
    # A second, bigger chart, so "largest" is a distinct number from "total".
    records[1].patient.notes = "N" * 4096
    result = deliver_ccda(records, tmp_path / "ccda")

    assert len(result.paths) == 2
    # Totals across the batch, and the LARGEST single document — a
    # destination's size limit applies per document, so the total is the wrong
    # number to compare against one.
    assert result.total_bytes == sum(p.stat().st_size for p in result.paths)
    assert result.largest_bytes == max(p.stat().st_size for p in result.paths)
    assert result.largest_bytes < result.total_bytes
    assert 0 < result.preserved_bytes < result.total_bytes


def _preservation_heavy_record() -> PatientRecord:
    """A chart the way a real vendor export produces one: a little clinical
    content and a lot of source fields C-CDA has no structured slot for.

    The shipped synthetic fixtures are too small to show the ratio #118
    measured, which is exactly why it went unseen until a real Practice Fusion
    export was run. Sixty invented vendor keys reproduce the SHAPE without any
    real data — the point is the proportion, not the values.
    """
    record = _generation_record()
    record.patient.extensions.update(
        {f"pf_tebra:VendorField{n:02d}": f"synthetic value {n}" * 8 for n in range(60)}
    )
    return record


def test_an_ordinary_export_does_not_cry(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The other half of the pair. A check that fires on the normal case is one
    operators learn to skip, so a chart whose preservation is a minority of the
    document says nothing."""
    from anastomosis.deliver.ccda_export import deliver_ccda

    with caplog.at_level(logging.WARNING, logger="anastomosis.deliver.ccda_export.deliverer"):
        result = deliver_ccda([_generation_record()], tmp_path / "ccda")

    assert result.preserved_share < 0.5
    assert not [
        r.getMessage() for r in caplog.records if "preserved source fields" in r.getMessage()
    ]


def test_a_mostly_preservation_export_says_so_before_the_destination_does(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The 'found out the hard way' failure this closes.

    The warning is about THIS tool's own output — what a given EHR will accept
    is not something it can know — so it reports the share and points at the
    destination's own limit rather than inventing one.
    """
    from anastomosis.deliver.ccda_export import deliver_ccda

    with caplog.at_level(logging.WARNING, logger="anastomosis.deliver.ccda_export.deliverer"):
        result = deliver_ccda([_preservation_heavy_record()], tmp_path / "ccda")

    assert result.preserved_share >= 0.5, "the fixture no longer reproduces the shape"
    warnings = [r.getMessage() for r in caplog.records]
    assert any("preserved source fields" in m for m in warnings), warnings
    assert any("per-document size limit" in m for m in warnings), warnings
