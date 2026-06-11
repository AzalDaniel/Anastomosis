"""Tests for the C-CDA / CCD adapter against the synthetic fixture.

Each test asserts one section's mapping (or one trap) documented in
tests/fixtures/ccda/README.md.
"""

from datetime import UTC, date, timedelta
from pathlib import Path

import pytest

import anastomosis.sources.ccda
import anastomosis.sources.pf_tebra  # noqa: F401 — for the cross-adapter detect test
from anastomosis.core.fhir import from_bundle, to_bundle
from anastomosis.core.model import (
    AllergyCategory,
    IdentifierKind,
    ObservationCategory,
    PatientRecord,
)
from anastomosis.sources import get_source

CCDA_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "ccda"
PF_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"


@pytest.fixture(scope="module")
def record() -> PatientRecord:
    adapter = get_source("ccda")
    assert adapter.detect(CCDA_FIXTURE)
    loaded = list(adapter.load(CCDA_FIXTURE))
    assert len(loaded) == 1
    return loaded[0]


# --- detection ---------------------------------------------------------------


def test_detect_true_on_ccda_dir() -> None:
    assert get_source("ccda").detect(CCDA_FIXTURE)


def test_detect_false_on_empty_and_pf_dirs(tmp_path: Path) -> None:
    assert not get_source("ccda").detect(tmp_path)
    assert not get_source("ccda").detect(PF_FIXTURE)


def test_pf_adapter_does_not_claim_ccda_dir() -> None:
    assert not get_source("pf-tebra").detect(CCDA_FIXTURE)


# --- demographics ------------------------------------------------------------


def test_demographics(record: PatientRecord) -> None:
    p = record.patient
    assert p.display_name == "Cora Specimen"
    assert p.birth_date == date(1979, 4, 6)
    assert p.sex == "Female"
    assert p.race == ["Asian"]
    assert p.ethnicity == ["Not Hispanic or Latino"]
    assert p.language == "en"
    assert p.identifier(IdentifierKind.SSN) == "901-65-4329"
    phones = {t.kind.value: t.value for t in p.telecom}
    assert phones["phone_home"] == "(206) 555-0177"
    assert phones["email"] == "cora.specimen@example.com"
    assert p.addresses[0].line1 == "456 Sample Way"
    assert p.addresses[0].postal_code == "98102"


# --- problems ----------------------------------------------------------------


def test_conditions(record: PatientRecord) -> None:
    htn = next(c for c in record.conditions if c.snomed == "38341003")
    assert htn.icd10 == "I10"
    assert htn.active is True
    assert htn.display == "Hypertensive disorder"
    migraine = next(c for c in record.conditions if c.snomed == "37796009")
    assert migraine.active is False  # has effectiveTime/high
    assert migraine.stopped == date(2020, 9, 1)
    assert migraine.icd10 is None


# --- allergies ---------------------------------------------------------------


def test_allergies(record: PatientRecord) -> None:
    by_substance = {a.substance: a for a in record.allergies}
    penicillin = by_substance["Penicillin G"]
    assert penicillin.category == AllergyCategory.DRUG
    assert penicillin.reactions == ["Hives"]
    assert penicillin.severity == "Moderate"
    assert penicillin.extensions["ccda:allergen_code"] == "7980"
    peanut = by_substance["Peanut"]
    assert peanut.category == AllergyCategory.FOOD
    assert peanut.reactions == ["Anaphylaxis"]
    assert peanut.severity == "Severe"
    assert peanut.extensions["ccda:allergen_code"] == "256349002"


# --- medications -------------------------------------------------------------


def test_medications(record: PatientRecord) -> None:
    by_rxnorm = {m.rxnorm: m for m in record.medications}
    lisinopril = by_rxnorm["314076"]
    assert lisinopril.active is True
    assert lisinopril.start == date(2023, 1, 1)
    assert lisinopril.stop is None  # high nullFlavor="UNK"
    assert lisinopril.extensions["ccda:route"] == "Oral"
    amoxicillin = by_rxnorm["308182"]
    assert amoxicillin.active is False  # statusCode completed
    assert amoxicillin.stop == date(2022, 3, 14)


# --- immunizations -----------------------------------------------------------


def test_immunizations(record: PatientRecord) -> None:
    flu = next(i for i in record.immunizations if "Influenza" in (i.vaccine or ""))
    assert flu.administered_on == date(2022, 10, 3)
    assert flu.lot_number == "FLU2022A"
    assert flu.comment is None
    refused = next(i for i in record.immunizations if i.comment == "Refused")
    assert refused.extensions["ccda:negationInd"] == "true"


# --- vitals + results --------------------------------------------------------


def test_vitals(record: PatientRecord) -> None:
    vitals = [o for o in record.observations if o.category == ObservationCategory.VITAL_SIGNS]
    assert len(vitals) >= 4
    by_code = {o.code: o for o in vitals}
    assert by_code["8480-6"].value == "122"  # systolic
    assert by_code["8462-4"].value == "78"  # diastolic
    assert by_code["8480-6"].unit == "mm[Hg]"
    effective = by_code["8480-6"].effective_at
    assert effective is not None
    assert effective.utcoffset() == timedelta(hours=-5)
    assert effective.astimezone(UTC).hour == 19  # 14:00 -0500 → 19:00 UTC


def test_results(record: PatientRecord) -> None:
    labs = [o for o in record.observations if o.category == ObservationCategory.LABORATORY]
    glucose = next(o for o in labs if o.code == "2345-7")
    assert glucose.value == "92"
    assert glucose.unit == "mg/dL"
    creatinine = next(o for o in labs if o.code == "2160-0")
    assert creatinine.value == "0.9"


# --- social history ----------------------------------------------------------


def test_smoking_observation(record: PatientRecord) -> None:
    social = [o for o in record.observations if o.category == ObservationCategory.SOCIAL_HISTORY]
    smoking = next(o for o in social if o.display == "Tobacco use")
    assert smoking.value == "Never smoker"


# --- encounters + notes ------------------------------------------------------


def test_encounters_include_office_visit_and_note(record: PatientRecord) -> None:
    assert len(record.encounters) >= 2
    office = next(
        e for e in record.encounters if (e.encounter_type or "").startswith("Office outpatient")
    )
    assert office.date_of_service == date(2023, 5, 10)
    note_encounter = next(
        e for e in record.encounters if any(s.kind.value == "narrative" for s in e.sections)
    )
    assert note_encounter.date_of_service == date(2023, 5, 10)
    narrative = note_encounter.sections[0]
    assert narrative.html is None
    assert narrative.text is not None
    assert "routine blood pressure follow-up" in narrative.text


# --- losslessness ------------------------------------------------------------


def test_unparsed_section_and_document_metadata_survive(record: PatientRecord) -> None:
    ext = record.patient.extensions
    plan = ext["ccda:section:18776-5"]
    assert plan["title"] == "Plan of Treatment"
    assert "recheck blood pressure in three months" in plan["text"]
    assert ext["ccda:documentId"] == "feedface-0000-0000-0000-00000000cda1"
    assert "ccda:title" in ext


# --- cross-adapter FHIR round trip -------------------------------------------


def _dumps(models: list) -> list[dict]:
    return [m.model_dump(mode="json", exclude={"provenance"}) for m in models]


def test_fhir_round_trip_is_lossless(record: PatientRecord) -> None:
    rebuilt = from_bundle(to_bundle(record))
    assert rebuilt.patient.model_dump(mode="json", exclude={"provenance"}) == (
        record.patient.model_dump(mode="json", exclude={"provenance"})
    )
    assert _dumps(rebuilt.conditions) == _dumps(record.conditions)
    assert _dumps(rebuilt.allergies) == _dumps(record.allergies)
