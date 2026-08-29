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
)
from anastomosis.sources.ccda.parser import EXT_PRIOR_LOSS_NARRATIVE, parse_document

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
        import pymupdf

        from anastomosis.core.textutil import html_to_text

        doc = pymupdf.open()
        page = doc.new_page(width=612, height=792)
        page.insert_textbox(
            pymupdf.Rect(18, 18, 594, 774), html_to_text(html) or "(empty)", fontsize=7
        )
        doc.save(str(pdf_path))
        doc.close()

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
    assert written == []
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
