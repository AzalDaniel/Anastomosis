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
import stat
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from lxml import etree
from typer.testing import CliRunner

import anastomosis.sources.ccda  # noqa: F401 — registers the adapter
from anastomosis.cli import app
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
    HealthConcern,
    Identifier,
    IdentifierKind,
    Immunization,
    ImplantableDevice,
    LabOrder,
    LabOrderItem,
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
    SectionKind,
)
from anastomosis.core.model.patient import Address
from anastomosis.deliver.ccda_export import DECLARED_LOSSES, build_ccd, deliver_ccda
from anastomosis.deliver.ccda_export.builder import LOINC_EXTENSIONS
from anastomosis.sources.ccda.parser import parse_document

V3 = "urn:hl7-org:v3"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
# The same hardened parser settings the repo's ingest uses.
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)

runner = CliRunner()

PF_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"
# A wall-clock instant with a fixed offset (proves tz survives the TS round trip).
_AT = datetime(2023, 5, 10, 14, 0, 0, tzinfo=timezone(timedelta(hours=-5)))


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
    section = rt.patient.extensions[f"ccda:section:{LOINC_EXTENSIONS}"]
    assert "Carpenter" in section["text"]
    assert "Construction" in section["text"]
    assert "Tobacco" not in section["text"]


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
    section = rt.patient.extensions[f"ccda:section:{LOINC_EXTENSIONS}"]
    assert "Welder" in section["text"]


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
    section = rt.patient.extensions[f"ccda:section:{LOINC_EXTENSIONS}"]
    assert "pf_tebra:PatientStatusCode = A" in section["text"]
    assert "pf_tebra:RxControlled = no" in section["text"]
    # ccda:route is a NATIVE round-trip; it stays on the model, not the loss section.
    assert rt.medications[0].extensions["ccda:route"] == "Oral"
    assert "ccda:route" not in section["text"]


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
    text = parse_document(out).patient.extensions[f"ccda:section:{LOINC_EXTENSIONS}"]["text"]
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
    # encounter[<id>].addenda[0].text = ... — proves the path format is emitted.
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
    text = parse_document(out).patient.extensions[f"ccda:section:{LOINC_EXTENSIONS}"]["text"]
    assert f"encounters[{enc_id}].addenda[0].text = amended note body" in text


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


# --- the pinning property test (SHOULD-FIX 3) --------------------------------
#
# Build a MAXIMALLY-populated record (every field of every canonical model set to
# a distinctive synthetic value), export → re-ingest, then walk every populated
# leaf of the ORIGINAL. Each leaf must be either (a)/(b) preserved — its value
# present somewhere on the re-ingested record (structured fields OR the recovered
# 51899-3 loss narrative, which itself lives in patient.extensions) — or (c)
# matched by a DECLARED_LOSSES pattern. If a model field is added tomorrow and
# the exporter is not updated, that field's distinctive value appears in neither
# place and this test fails. THAT is the test's purpose.


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
    health_concerns = [
        HealthConcern(
            patient_id=pid, description="MaxHealthConcern", effective=date(2017, 1, 1), active=True
        )
    ]
    goals = [Goal(patient_id=pid, description="MaxGoal", effective=date(2018, 1, 1), active=True)]
    devices = [ImplantableDevice(patient_id=pid, description="MaxDevicePacemaker", recorded_at=_AT)]
    lab_orders = [
        LabOrder(
            patient_id=pid,
            encounter_id="feedface-0000-0000-0000-0000000000d1",
            lab_name="MaxLabName",
            ordered_at=_AT,
            items=[LabOrderItem(test_name="MaxLabTest", note="MaxLabNote")],
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
        health_concerns=health_concerns,
        goals=goals,
        devices=devices,
        lab_orders=lab_orders,
        coverages=coverages,
        documents=documents,
        practitioners=practitioners,
        facilities=facilities,
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


def _reingested_haystack(record: PatientRecord) -> str:
    """All scalar leaf values of a re-ingested record (structured fields AND the
    recovered 51899-3 loss narrative, which lives inside patient.extensions),
    concatenated — the search space for 'preserved by value'."""
    leaves = _walk_leaves(record.model_dump(mode="json"))
    return "\n".join(text for _, text in leaves)


def test_no_undeclared_native_loss(tmp_path: Path) -> None:
    original = _maximal_record()
    out = tmp_path / "max.xml"
    out.write_bytes(build_ccd(original))
    reingested = parse_document(out)
    haystack = _reingested_haystack(reingested)

    undeclared: list[str] = []
    for path, value in _walk_leaves(original.model_dump(mode="json")):
        if _is_declared_loss(path):
            continue  # (c) covered by a DECLARED_LOSSES pattern
        if value in haystack:
            continue  # (a) equal on re-ingest OR (b) present in the loss narrative
        undeclared.append(f"{'.'.join(str(p) for p in path)} = {value!r}")
    assert not undeclared, (
        "fields silently lost (not round-tripped, not in the 51899-3 narrative, "
        "and not a declared loss): " + "; ".join(undeclared)
    )


# --- determinism + well-formedness -------------------------------------------


def test_two_builds_are_byte_identical(source: PatientRecord) -> None:
    assert build_ccd(source) == build_ccd(source)


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
    written = deliver_ccda(records, out)
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
        import fitz

        from anastomosis.core.textutil import html_to_text

        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        page.insert_textbox(
            fitz.Rect(18, 18, 594, 774), html_to_text(html) or "(empty)", fontsize=7
        )
        doc.save(str(pdf_path))
        doc.close()

    def close(self) -> None:
        pass


def test_cli_pipeline_run_ccda_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("fitz", reason="pipeline e2e needs PyMuPDF (render extra)")
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
    # Counts and the opaque patient id may appear; patient-derived values may not.
    assert "Cora" not in blob
    assert "Specimen" not in blob
    assert "901-65-4329" not in blob
    assert "cora.specimen@example.com" not in blob


def test_no_patient_values_logged_on_deliverer_failure(
    caplog: pytest.LogCaptureFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force a build failure; the deliverer must log the exception TYPE only.
    import anastomosis.deliver.ccda_export.deliverer as deliverer_mod

    def _boom(_record: PatientRecord, **_kw: object) -> bytes:
        raise RuntimeError("Cora Specimen 901-65-4329")  # message embeds PHI

    monkeypatch.setattr(deliverer_mod, "build_ccd", _boom)
    with caplog.at_level(logging.DEBUG, logger="anastomosis.deliver.ccda_export.deliverer"):
        written = deliver_ccda([_rich_record()], tmp_path / "out")
    assert written == []
    blob = " ".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in blob  # the exception type IS logged
    assert "Cora" not in blob and "901-65-4329" not in blob


# --- helpers -----------------------------------------------------------------


def parse_document_bytes(data: bytes) -> PatientRecord:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "doc.xml"
        path.write_bytes(data)
        return parse_document(path)
