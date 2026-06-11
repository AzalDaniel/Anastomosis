"""Canonical-model tests: construction, serialization, the lossless guarantee."""

from __future__ import annotations

from datetime import UTC, date, datetime

from anastomosis.core.model import (
    Addendum,
    AllergyCategory,
    AllergyIntolerance,
    Condition,
    Coverage,
    Encounter,
    Facility,
    Identifier,
    IdentifierKind,
    MedicationStatement,
    NoteSection,
    Observation,
    ObservationCategory,
    Patient,
    PatientRecord,
    Practitioner,
    Prescription,
    PrescriptionTransaction,
    Provenance,
    SectionKind,
)


def make_patient() -> Patient:
    return Patient(
        id="feedface-0000-0000-0000-000000000001",
        given_name="Testa",
        family_name="Fixture",
        birth_date=date(1980, 3, 14),
        sex="Female",
        identifiers=[Identifier(kind=IdentifierKind.PRN, value="PRN-0001")],
        provenance=Provenance(source_system="pf_tebra", source_file="patient-demographics.tsv"),
    )


def test_patient_display_name_and_identifier() -> None:
    p = make_patient()
    assert p.display_name == "Testa Fixture"
    assert p.identifier(IdentifierKind.PRN) == "PRN-0001"
    assert p.identifier(IdentifierKind.MRN) is None


def test_extensions_preserve_unmapped_fields_through_serialization() -> None:
    p = make_patient()
    p.extensions["pf_tebra:SomeVendorColumn"] = r"\N was cleaned upstream"
    p.extensions["pf_tebra:Nested"] = {"a": 1, "b": [1, 2]}
    rehydrated = Patient.model_validate_json(p.model_dump_json())
    assert rehydrated.extensions == p.extensions
    assert rehydrated.provenance is not None
    assert rehydrated.provenance.source_system == "pf_tebra"


def test_encounter_sections_and_emptiness() -> None:
    enc = Encounter(
        patient_id="feedface-0000-0000-0000-000000000001",
        date_of_service=datetime(2025, 6, 1, 14, 30, tzinfo=UTC),
        chief_complaint="Annual physical",
        sections=[
            NoteSection(kind=SectionKind.SUBJECTIVE, html="<p>Feels well.</p>", text="Feels well."),
            NoteSection(kind=SectionKind.OBJECTIVE, text="   "),
        ],
        addenda=[Addendum(text="PHQ-9: 2", status="Accepted", author_name="Synthetic Provider")],
    )
    subj = enc.section(SectionKind.SUBJECTIVE)
    assert subj is not None and not subj.is_empty
    obj = enc.section(SectionKind.OBJECTIVE)
    assert obj is not None and obj.is_empty
    assert enc.section(SectionKind.PLAN) is None
    assert enc.has_note_content


def test_patient_record_lookups_and_roundtrip() -> None:
    patient = make_patient()
    prov = Practitioner(
        id="feedface-0000-0000-0000-00000000000a",
        given_name="Synthetic",
        family_name="Provider",
        credential="Nurse Practitioner",
    )
    fac = Facility(id="feedface-0000-0000-0000-00000000000b", name="Test Family Health")
    enc = Encounter(patient_id=patient.id, provider_id=prov.id, facility_id=fac.id)
    obs = Observation(
        patient_id=patient.id,
        encounter_id=enc.id,
        category=ObservationCategory.VITAL_SIGNS,
        code="39156-5",
        display="BMI",
        value="24.13",
    )
    record = PatientRecord(
        patient=patient,
        encounters=[enc],
        observations=[obs],
        conditions=[Condition(patient_id=patient.id, icd10="E11.9", display="Type 2 diabetes")],
        allergies=[
            AllergyIntolerance(
                patient_id=patient.id,
                substance="Penicillin",
                category=AllergyCategory.DRUG,
                reactions=["Hives"],
            )
        ],
        medications=[
            MedicationStatement(
                patient_id=patient.id,
                generic_name="metformin",
                display_name="metFORMIN (Glucophage) 500 mg Oral Tablet",
            )
        ],
        prescriptions=[
            Prescription(
                patient_id=patient.id,
                prefix="ESCRIPT",
                status_label="DISPENSED",
                transactions=[
                    PrescriptionTransaction(
                        kind="Order sent", at=datetime(2025, 6, 1, 18, 6, tzinfo=UTC)
                    )
                ],
            )
        ],
        coverages=[Coverage(patient_id=patient.id, payer="Synthetic Health Plan", active=True)],
        practitioners=[prov],
        facilities=[fac],
    )

    assert record.practitioner(prov.id) is prov
    assert record.practitioner("nope") is None
    assert record.facility(fac.id) is fac
    assert record.observations_for(enc.id) == [obs]

    rehydrated = PatientRecord.model_validate_json(record.model_dump_json())
    assert rehydrated.patient.display_name == "Testa Fixture"
    assert rehydrated.medications[0].display_name is not None
    assert rehydrated.prescriptions[0].transactions[0].kind == "Order sent"


def test_extra_fields_are_rejected() -> None:
    """Adapters must use `extensions`, never invent ad-hoc attributes."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Patient(given_name="X", not_a_field="boom")  # type: ignore[call-arg]
