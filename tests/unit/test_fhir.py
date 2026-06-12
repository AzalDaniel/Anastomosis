"""FHIR export/ingest tests: the round-trip IS the lossless guarantee.

Every fixture record must survive canonical → Bundle → canonical with
nothing changed (provenance excluded: it's local lineage, not exported).
"""

import base64
import copy
import json
from pathlib import Path

import pytest

import anastomosis.sources.pf_tebra  # noqa: F401 — registers the adapter
from anastomosis.core.fhir import from_bundle, to_bundle
from anastomosis.core.model import PatientRecord, SectionKind
from anastomosis.sources import get_source

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"

_LIST_FIELDS = (
    "encounters",
    "observations",
    "conditions",
    "allergies",
    "medications",
    "prescriptions",
    "immunizations",
    "family_history",
    "past_medical_history",
    "advance_directives",
    "health_concerns",
    "goals",
    "devices",
    "lab_orders",
    "coverages",
    "documents",
    "practitioners",
    "facilities",
)


@pytest.fixture(scope="module")
def records() -> list[PatientRecord]:
    return list(get_source("pf-tebra").load(FIXTURE))


def _dumps(models: list) -> list[dict]:
    return [m.model_dump(mode="json", exclude={"provenance"}) for m in models]


def test_round_trip_is_lossless(records: list[PatientRecord]) -> None:
    for record in records:
        rebuilt = from_bundle(to_bundle(record))
        assert rebuilt.patient.model_dump(mode="json", exclude={"provenance"}) == (
            record.patient.model_dump(mode="json", exclude={"provenance"})
        ), f"patient mismatch for {record.patient.id}"
        for field in _LIST_FIELDS:
            assert _dumps(getattr(rebuilt, field)) == _dumps(getattr(record, field)), (
                f"{field} mismatch for {record.patient.id}"
            )


def test_bundle_is_standard_shaped(records: list[PatientRecord]) -> None:
    bundle = to_bundle(records[0])  # Ada Fixture
    assert bundle["resourceType"] == "Bundle" and bundle["type"] == "collection"
    by_type: dict[str, list[dict]] = {}
    for entry in bundle["entry"]:
        by_type.setdefault(entry["resource"]["resourceType"], []).append(entry["resource"])

    patient = by_type["Patient"][0]
    assert patient["birthDate"] == "1985-03-14"
    assert patient["gender"] == "female"
    assert {"system": "http://hl7.org/fhir/sid/us-ssn", "value": "900-12-3456"} in patient[
        "identifier"
    ]

    systolic = next(
        o
        for o in by_type["Observation"]
        if o["code"].get("coding", [{}])[0].get("code") == "8480-6"
    )
    assert systolic["valueQuantity"]["value"] == 118.0
    assert systolic["category"][0]["coding"][0]["code"] == "vital-signs"

    htn = next(c for c in by_type["Condition"] if c["code"]["text"] == "Essential hypertension")
    systems = {c["system"]: c["code"] for c in htn["code"]["coding"]}
    assert systems["http://hl7.org/fhir/sid/icd-10-cm"] == "I10"
    assert systems["http://www.snomed.info/sct"] == "59621000"

    penicillin = by_type["AllergyIntolerance"][0]
    assert penicillin["category"] == ["medication"]  # drug → FHIR's 'medication'
    assert penicillin["reaction"][0]["severity"] == "severe"

    # References resolve within the bundle.
    full_urls = {entry["fullUrl"] for entry in bundle["entry"]}
    encounter = by_type["Encounter"][0]
    assert encounter["subject"]["reference"] in full_urls


def test_note_documentreference_carries_readable_html(records: list[PatientRecord]) -> None:
    bundle = to_bundle(records[0])
    docrefs = [
        e["resource"]
        for e in bundle["entry"]
        if e["resource"]["resourceType"] == "DocumentReference"
    ]
    html = base64.b64decode(docrefs[0]["content"][0]["attachment"]["data"]).decode()
    assert 'data-kind="subjective"' in html
    assert "Reports good medication adherence" in html
    assert docrefs[0]["docStatus"] == "final"  # signed note


def test_html_fallback_when_json_rendition_absent(records: list[PatientRecord]) -> None:
    # A bundle from a foreign system may carry only the HTML rendition;
    # section structure must still come back via the data-kind wrappers.
    bundle = copy.deepcopy(to_bundle(records[0]))
    for entry in bundle["entry"]:
        resource = entry["resource"]
        if resource["resourceType"] == "DocumentReference" and "context" in resource:
            resource["content"] = [
                c for c in resource["content"] if c["attachment"]["contentType"] == "text/html"
            ]
    rebuilt = from_bundle(bundle)
    original = records[0].encounters[0]
    parsed = next(e for e in rebuilt.encounters if e.id == original.id)
    assert [s.kind for s in parsed.sections] == [s.kind for s in original.sections]
    subjective = parsed.section(SectionKind.SUBJECTIVE)
    assert subjective is not None and subjective.text == (
        original.section(SectionKind.SUBJECTIVE).text  # type: ignore[union-attr]
    )
    assert parsed.addenda == original.addenda if original.addenda else True


def test_bundle_validates_against_fhir_r4_schema(records: list[PatientRecord]) -> None:
    pytest.importorskip("fhir.resources", reason="schema validation needs the fhir extra")
    from fhir.resources.R4B.bundle import Bundle

    for record in records:
        Bundle.model_validate(to_bundle(record))


def test_ingest_requires_a_patient() -> None:
    with pytest.raises(ValueError, match="no Patient"):
        from_bundle({"resourceType": "Bundle", "type": "collection", "entry": []})


def test_round_trip_edge_cases_from_qa_review() -> None:
    """Every shape the adversarial QA review proved lossy must round-trip.

    Notably: real values that collide with FHIR required-field placeholders
    ("Unknown" reactions/diagnoses/payers), None values that must NOT come
    back as placeholders, sparse name/address slots, empty strings, and
    record-level extensions.
    """
    from datetime import date

    from anastomosis.core.model import (
        Address,
        AllergyIntolerance,
        Coverage,
        Encounter,
        FamilyMemberHistory,
        Immunization,
        MedicationStatement,
        Observation,
        Patient,
    )

    pid = "feedface-0000-0000-0000-0000000000ee"
    record = PatientRecord(
        extensions={"pf_tebra:RecordLevel": "survives"},
        patient=Patient(
            id=pid,
            middle_name="Q",  # no given_name: sparse slot
            addresses=[Address(line2="Apt 4")],  # no line1: sparse slot
        ),
        encounters=[
            Encounter(
                id="feedface-e000-0000-0000-0000000000ee",
                patient_id=pid,
                date_of_service=date(2023, 1, 1),
            )
        ],
        observations=[
            Observation(patient_id=pid, display="Empty-string value", value=""),
            Observation(patient_id=pid, code="8480-6", value="120/80", unit="mmHg"),
            Observation(patient_id=pid, code="8867-4", value="NaN"),
        ],
        allergies=[
            AllergyIntolerance(
                patient_id=pid,
                substance="Probe",
                reactions=["Unknown"],  # a REAL charted value, not a placeholder
                severity="Life-threatening",  # not a FHIR severity code
            )
        ],
        medications=[MedicationStatement(patient_id=pid, generic_name="metformin")],
        immunizations=[Immunization(patient_id=pid)],  # vaccine=None
        family_history=[FamilyMemberHistory(patient_id=pid, diagnosis="Unknown", relation=None)],
        coverages=[
            Coverage(patient_id=pid, payer="Unknown", order_of_benefits=0),
            Coverage(patient_id=pid),  # payer=None must NOT come back "Unknown"
        ],
    )
    bundle = to_bundle(record)
    json.dumps(bundle)  # NaN guard: bundle must stay JSON-serializable
    rebuilt = from_bundle(bundle)
    assert rebuilt.id == record.id
    assert rebuilt.extensions == record.extensions
    assert rebuilt.patient.model_dump(mode="json", exclude={"provenance"}) == (
        record.patient.model_dump(mode="json", exclude={"provenance"})
    )
    for field in _LIST_FIELDS:
        assert _dumps(getattr(rebuilt, field)) == _dumps(getattr(record, field)), field

    pytest.importorskip("fhir.resources", reason="schema validation needs the fhir extra")
    from fhir.resources.R4B.bundle import Bundle

    Bundle.model_validate(bundle)
