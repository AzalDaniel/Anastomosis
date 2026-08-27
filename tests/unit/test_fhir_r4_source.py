"""Tests for the FHIR R4 / US Core source adapter (``sources/fhir_r4``).

Drives the adapter against the in-repo synthetic US Core fixture
(``tests/fixtures/fhir_r4/uscore_bundle.json``) — hand-authored standard US
Core, NOT this project's own export shape — so these pin the real-world
mapping: which coding system lands in which canonical field, how observation
categories and BP panels resolve, what is preserved losslessly, multi-patient
grouping, determinism, the Bulk-Data NDJSON path, and an end-to-end render.
"""

from __future__ import annotations

import base64
import json
from collections import defaultdict
from pathlib import Path

import pytest

import anastomosis.reconstruct.chromium as chromium

# Importing the package registers the adapter; also exercised via get_source.
import anastomosis.sources.fhir_r4  # noqa: F401
from anastomosis.core.model import AllergyCategory, ObservationCategory
from anastomosis.sources import detect_source, get_source
from anastomosis.sources.fhir_r4.mapper import AmbiguousUnanchoredError, records_from_resources

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "fhir_r4"
BUNDLE = FIXTURE_DIR / "uscore_bundle.json"

# A synthetic patient id used by the crafted-resource mapping tests below.
PID = "feedface-0001-0000-0000-000000000001"
_SUBJECT = {"reference": f"Patient/{PID}"}


def _patient_resource(family: str = "Specimen", pid: str = PID) -> dict:
    return {"resourceType": "Patient", "id": pid, "name": [{"family": family, "given": ["Dexter"]}]}


def _record_with(*extra: dict):
    """One PatientRecord from a single synthetic patient plus crafted resources."""
    return next(iter(records_from_resources([_patient_resource(), *extra])))


def _adapter():
    return get_source("fhir-r4")


def _records():
    return list(_adapter().load(FIXTURE_DIR))


def _by_name():
    return {r.patient.display_name: r for r in _records()}


class _FakeChromium:
    """Writes a REAL pdf carrying the chart text (the test_cli.py pattern)."""

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


# --- detect ----------------------------------------------------------------


def test_detect_identifies_bundle_dir() -> None:
    assert _adapter().detect(FIXTURE_DIR) is True
    detected = detect_source(FIXTURE_DIR)
    assert detected is not None and detected.name == "fhir-r4"


def test_detect_unknown_dir_is_false(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("not fhir", encoding="utf-8")
    assert _adapter().detect(tmp_path) is False


def test_detect_ndjson_export_dir(tmp_path: Path) -> None:
    (tmp_path / "Patient.ndjson").write_text(
        json.dumps({"resourceType": "Patient", "id": "feedface-0001-0000-0000-000000000001"})
        + "\n",
        encoding="utf-8",
    )
    assert _adapter().detect(tmp_path) is True


def test_utf8_bom_bundle_loads(tmp_path: Path) -> None:
    """A UTF-8 BOM (common from Windows export tools) is stripped on read, so a
    BOM-prefixed Bundle JSON loads instead of crashing json.loads on line 1
    (which previously surfaced as an opaque bad_input with no encoding hint)."""
    text = BUNDLE.read_text(encoding="utf-8")
    (tmp_path / "bundle.json").write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    records = list(_adapter().load(tmp_path))
    assert records  # non-empty: the BOM did not silently break parsing


def test_utf8_bom_ndjson_streams_and_loads(tmp_path: Path) -> None:
    """A BOM-prefixed NDJSON $export file streams (utf-8-sig, line-by-line) and
    loads — the BOM is stripped and the resource parses."""
    patient = json.dumps(_patient_resource())
    (tmp_path / "Patient.ndjson").write_bytes(b"\xef\xbb\xbf" + (patient + "\n").encode("utf-8"))
    adapter = _adapter()
    assert adapter.detect(tmp_path) is True
    records = list(adapter.load(tmp_path))
    assert len(records) == 1


# --- patient demographics --------------------------------------------------


def test_patient_demographics_mapped() -> None:
    rec = _by_name()["Dexter Quill Specimen Jr."]
    p = rec.patient
    assert (p.given_name, p.middle_name, p.family_name, p.suffix) == (
        "Dexter",
        "Quill",
        "Specimen",
        "Jr.",
    )
    assert p.birth_date.isoformat() == "1978-02-15"
    assert p.sex == "male"
    assert p.race == ["White"]
    assert p.ethnicity == ["Not Hispanic or Latino"]
    assert p.language == "English"
    assert p.marital_status == "Married"
    kinds = {i.kind.value: i.value for i in p.identifiers}
    assert kinds["mrn"] == "MRN-000123"
    assert kinds["ssn"] == "900-00-1234"
    telecom = {t.kind.value: t.value for t in p.telecom}
    assert telecom["phone_home"] == "555-555-0123"
    assert telecom["email"] == "dexter.specimen@example.com"
    assert rec.patient.provenance is not None
    assert rec.patient.provenance.source_system == "fhir-r4"


def test_two_patients_grouped_in_bundle_order() -> None:
    records = _records()
    assert [r.patient.display_name for r in records] == [
        "Dexter Quill Specimen Jr.",
        "Wendell Placeholder",
    ]


# --- encounters + clinical-note narratives ---------------------------------


def test_encounters_and_note_narratives() -> None:
    rec = _by_name()["Dexter Quill Specimen Jr."]
    assert len(rec.encounters) == 2
    enc1, enc2 = rec.encounters
    assert enc1.date_of_service.isoformat() == "2023-05-10"
    assert enc1.note_type == "Office Visit"
    assert enc1.chief_complaint == "Cough"
    # The clinical-note DocumentReference narrative attached to its encounter.
    assert len(enc1.sections) == 1
    assert "cough for three days" in (enc1.sections[0].text or "")
    assert enc2.date_of_service.isoformat() == "2023-09-22"
    assert "Cough resolved" in (enc2.sections[0].text or "")
    # The encounter's diagnosis reference resolved to a condition id.
    assert enc1.diagnosis_ids and enc1.diagnosis_ids[0].startswith("feedface-0004")
    # Provider/facility references resolved into denormalized objects.
    assert rec.practitioner(enc1.provider_id) is not None
    assert rec.practitioner(enc1.provider_id).npi == "1234567893"
    assert rec.facility(enc1.facility_id).name == "Example Family Clinic"


# --- observations ----------------------------------------------------------


def test_observations_bp_split_and_categories() -> None:
    rec = _by_name()["Dexter Quill Specimen Jr."]
    by_code = {o.code: o for o in rec.observations}
    # The BP panel split into its systolic/diastolic LOINC components.
    assert by_code["8480-6"].value == "128"
    assert by_code["8480-6"].category is ObservationCategory.VITAL_SIGNS
    assert by_code["8462-4"].value == "82"
    assert "85354-9" not in by_code  # the value-less panel itself is not emitted
    # Height/weight vitals carry value + unit.
    assert (by_code["8302-2"].value, by_code["8302-2"].unit) == ("70", "in")
    assert (by_code["29463-7"].value, by_code["29463-7"].unit) == ("180", "lb")
    # The lab lands in the laboratory category; smoking status in social-history.
    assert by_code["2339-0"].category is ObservationCategory.LABORATORY
    smoking = by_code["72166-2"]
    assert smoking.category is ObservationCategory.SOCIAL_HISTORY
    assert smoking.value == "Former smoker"  # valueCodeableConcept text
    # Vitals are attached to their encounter (so they render on that chart).
    assert by_code["8302-2"].encounter_id == rec.encounters[0].id


# --- problems / meds / allergies / immunizations / coverage ----------------


def test_conditions_code_systems() -> None:
    rec = _by_name()["Dexter Quill Specimen Jr."]
    htn = next(c for c in rec.conditions if c.display == "Essential hypertension")
    assert (htn.icd10, htn.snomed, htn.active) == ("I10", None, True)
    dm = next(c for c in rec.conditions if "diabetes" in (c.display or ""))
    assert dm.snomed == "44054006" and dm.icd10 is None


def test_medication_request_becomes_med_list_entry() -> None:
    rec = _by_name()["Dexter Quill Specimen Jr."]
    assert len(rec.medications) == 1
    med = rec.medications[0]
    assert med.display_name == "Metformin 500 MG Oral Tablet"
    assert med.rxnorm == "860975"
    assert med.sig == "Take 1 tablet by mouth twice daily with meals"
    assert med.active is True
    # The FHIR resource kind/intent are preserved (the request/statement distinction).
    assert med.extensions["fhir_r4:resource_type"] == "MedicationRequest"
    assert med.extensions["fhir_r4:intent"] == "order"


def test_allergy_immunization_coverage() -> None:
    rec = _by_name()["Dexter Quill Specimen Jr."]
    allergy = rec.allergies[0]
    assert allergy.substance == "Penicillin"
    assert allergy.category is AllergyCategory.DRUG
    assert allergy.reactions == ["Hives"]
    assert allergy.severity == "moderate"
    imm = rec.immunizations[0]
    assert imm.vaccine == "Influenza, seasonal"
    assert imm.extensions["fhir_r4:cvx"] == "140"
    assert imm.administered_on.isoformat() == "2023-10-01"
    cov = rec.coverages[0]
    assert cov.payer == "Acme Health Plan"
    assert cov.member_id == "MEMBER-000999"
    assert cov.group_number == "GRP-0001"
    assert cov.order_of_benefits == 0  # FHIR order 1 → canonical 0-based primary


def test_goal_and_family_history() -> None:
    rec = _by_name()["Dexter Quill Specimen Jr."]
    assert rec.goals[0].description == "Lower blood pressure to below 130/80"
    assert rec.goals[0].active is True
    fam = rec.family_history[0]
    assert fam.relation == "Father"
    assert "diabetes" in (fam.diagnosis or "")
    assert fam.extensions["fhir_r4:onset_string"] == "age 55"


# --- lossless preservation -------------------------------------------------


def test_unmapped_procedure_preserved_in_extensions() -> None:
    """Procedure has no canonical home, so it must round-trip into extensions
    verbatim (the lossless guarantee) rather than vanish."""
    rec = _by_name()["Dexter Quill Specimen Jr."]
    procs = rec.extensions["fhir_r4:Procedure"]
    assert isinstance(procs, list) and len(procs) == 1
    assert procs[0]["resourceType"] == "Procedure"
    assert procs[0]["code"]["text"] == "Appendectomy"


def test_condition_verification_status_preserved() -> None:
    """A refuted/entered-in-error verificationStatus must NOT be dropped — losing
    it would migrate a ruled-out diagnosis as active (meaning reversal)."""
    cond = {
        "resourceType": "Condition",
        "id": "c-refuted",
        "subject": _SUBJECT,
        "clinicalStatus": {"coding": [{"code": "active"}]},
        "verificationStatus": {"coding": [{"code": "refuted"}]},
        "category": [{"coding": [{"code": "encounter-diagnosis"}]}],
        "code": {"text": "Influenza (ruled out)"},
    }
    cond_obj = _record_with(cond).conditions[0]
    assert "fhir_r4:verificationStatus" in cond_obj.extensions
    assert cond_obj.extensions["fhir_r4:verificationStatus"]["coding"][0]["code"] == "refuted"
    assert "fhir_r4:category" in cond_obj.extensions  # also not lost


def test_observation_entered_in_error_status_preserved() -> None:
    obs = {
        "resourceType": "Observation",
        "id": "o-void",
        "status": "entered-in-error",
        "subject": _SUBJECT,
        "code": {"coding": [{"system": "http://loinc.org", "code": "2339-0"}]},
        "valueQuantity": {"value": 400, "unit": "mg/dL"},
    }
    obs_obj = _record_with(obs).observations[0]
    assert obs_obj.extensions["fhir_r4:status"] == "entered-in-error"


def test_unknown_resource_type_preserved_in_record_extensions() -> None:
    device = {
        "resourceType": "Device",
        "id": "dev-1",
        "patient": _SUBJECT,
        "type": {"text": "Cardiac pacemaker"},
    }
    rec = _record_with(device)
    assert rec.extensions["fhir_r4:Device"][0]["type"]["text"] == "Cardiac pacemaker"


def test_orphan_resource_preserved_for_single_patient() -> None:
    """A resource referencing a patient not in the data is preserved (single
    patient → unambiguous), never silently dropped."""
    orphan = {
        "resourceType": "Condition",
        "id": "stray",
        "subject": {"reference": "Patient/does-not-exist"},
        "clinicalStatus": {"coding": [{"code": "active"}]},
        "code": {"text": "Stray problem"},
    }
    rec = _record_with(orphan)
    unanchored = rec.extensions["fhir_r4:unanchored"]
    assert any(r["id"] == "stray" for r in unanchored)
    # It did not leak into the typed conditions list (it isn't this patient's).
    assert rec.conditions == []


def test_orphan_resource_across_patients_refuses_the_load() -> None:
    """With several patients an unanchored resource can be attributed to none of
    them: attaching it would misattribute one patient's data to another and
    omitting it would drop clinical data silently, so the load fails loudly."""
    resources = [
        _patient_resource(family="Specimen", pid=PID),
        _patient_resource(family="Placeholder", pid="feedface-0001-0000-0000-000000000002"),
        {
            "resourceType": "Condition",
            "id": "stray",
            "subject": {"reference": "Patient/does-not-exist"},
            "clinicalStatus": {"coding": [{"code": "active"}]},
            "code": {"text": "Stray"},
        },
    ]
    with pytest.raises(AmbiguousUnanchoredError) as exc:
        list(records_from_resources(resources))
    assert exc.value.counts == {"Condition": 1}
    assert "Condition (1)" in str(exc.value)
    # Resource TYPES and counts are schema; no id or patient-derived value leaks.
    assert "stray" not in str(exc.value)
    assert "Patient/does-not-exist" not in str(exc.value)


PID2 = "feedface-0001-0000-0000-000000000002"


def test_patient_less_resources_do_not_refuse_a_multi_patient_bundle() -> None:
    """A PractitionerRole, a Provenance and a Medication reference no patient at
    all — they are bundle-level, not dangling — so a two-patient bundle loads,
    and every one of them is accounted for under ``fhir_r4:shared`` on each
    record (preserved, with no attribution claimed)."""
    shared_resources = [
        {"resourceType": "PractitionerRole", "id": "pr1", "practitioner": {"reference": "P/x"}},
        {"resourceType": "Provenance", "id": "pv1", "target": [{"reference": f"Patient/{PID}"}]},
        {"resourceType": "Medication", "id": "m1", "code": {"text": "Amoxicillin"}},
    ]
    records = list(
        records_from_resources(
            [
                _patient_resource(),
                _patient_resource(family="Placeholder", pid=PID2),
                *shared_resources,
            ]
        )
    )
    assert len(records) == 2
    for record in records:
        assert record.extensions["fhir_r4:shared"] == shared_resources
    # They never leak into a typed collection of either patient.
    assert all(not record.medications for record in records)


def test_dangling_patient_reference_still_refuses_a_multi_patient_bundle() -> None:
    """The refusal narrows to the real ambiguity: a resource that NAMES a
    patient the data does not contain."""
    resources = [
        _patient_resource(),
        _patient_resource(family="Placeholder", pid=PID2),
        {
            "resourceType": "Condition",
            "id": "stray",
            "subject": {"reference": "Patient/does-not-exist"},
            "code": {"text": "Stray"},
        },
        {"resourceType": "Medication", "id": "m1", "code": {"text": "Amoxicillin"}},
    ]
    with pytest.raises(AmbiguousUnanchoredError) as exc:
        list(records_from_resources(resources))
    # Only the dangling Condition is counted; the patient-less Medication is not.
    assert exc.value.counts == {"Condition": 1}


def test_untrusted_resource_type_is_not_echoed_into_the_message() -> None:
    """resourceType arrives from a file this adapter does not author. Anything
    that is not a plain type name reads as "unknown", so a crafted export cannot
    smuggle patient text into an operator-facing message."""
    resources = [
        _patient_resource(),
        _patient_resource(family="Placeholder", pid=PID2),
        {
            "resourceType": "Observation MRN 88231 DOE JANE",
            "id": "x",
            "subject": {"reference": "Patient/ghost"},
        },
    ]
    with pytest.raises(AmbiguousUnanchoredError) as exc:
        list(records_from_resources(resources))
    assert exc.value.counts == {"unknown": 1}
    assert "88231" not in str(exc.value)
    assert "DOE" not in str(exc.value)


def test_versioned_patient_reference_resolves() -> None:
    """``Patient/<id>/_history/2`` is the same logical patient — reading the
    version as the id would make an ordinary versioned reference dangle."""
    condition = {
        "resourceType": "Condition",
        "id": "c-versioned",
        "subject": {"reference": f"http://ex.org/fhir/Patient/{PID}/_history/2"},
        "code": {"text": "Versioned reference"},
    }
    records = list(
        records_from_resources(
            [_patient_resource(), _patient_resource(family="Placeholder", pid=PID2), condition]
        )
    )
    anchored = {r.patient.id: [c.display for c in r.conditions] for r in records}
    assert anchored[PID] == ["Versioned reference"]
    assert anchored[PID2] == []


def test_identifier_only_reference_resolves_against_patient_identifier() -> None:
    """A logical reference carries no id at all; US Core resolves it against
    Patient.identifier (system + value)."""
    patient = _patient_resource()
    patient["identifier"] = [{"system": "urn:oid:1.2.3", "value": "MRN-909"}]
    condition = {
        "resourceType": "Condition",
        "id": "c-logical",
        "subject": {"identifier": {"system": "urn:oid:1.2.3", "value": "MRN-909"}},
        "code": {"text": "Logical reference"},
    }
    records = list(
        records_from_resources(
            [patient, _patient_resource(family="Placeholder", pid=PID2), condition]
        )
    )
    anchored = {r.patient.id: [c.display for c in r.conditions] for r in records}
    assert anchored[PID] == ["Logical reference"]
    assert anchored[PID2] == []
    # The anchor also reaches the typed model, not just the grouping.
    assert next(r for r in records if r.patient.id == PID).conditions[0].patient_id == PID


def test_unmatched_identifier_reference_is_shared_not_a_refusal() -> None:
    """An identifier reference that matches no Patient names nobody the data can
    identify — unlike ``Patient/<id>``, it makes no claim about a patient being
    present. Refusing the whole load over one would block ordinary valid input,
    so it is preserved bundle-level instead (nothing dropped, nothing guessed)."""
    condition = {
        "resourceType": "Condition",
        "id": "c-unmatched",
        "subject": {"identifier": {"system": "urn:oid:1.2.3", "value": "MRN-NO-MATCH"}},
        "code": {"text": "Unmatched logical reference"},
    }
    records = list(
        records_from_resources(
            [_patient_resource(), _patient_resource(family="Placeholder", pid=PID2), condition]
        )
    )
    assert len(records) == 2
    for record in records:
        assert record.extensions["fhir_r4:shared"] == [condition]
        assert record.conditions == []


def test_unread_race_sub_extensions_survive_the_lift() -> None:
    """The race lift reads ONE sub-extension (``text``) and one field of it. The
    ombCategory codings it skipped — and a ``detailed`` sub-extension it never
    reads at all — must therefore stay in the residue, not be marked consumed on
    the coat-tails of the entry that supplied the typed value."""
    resource = {
        "resourceType": "Patient",
        "id": PID,
        "name": [{"family": "Specimen", "given": ["Dexter"]}],
        "extension": [
            {
                "url": "http://hl7.org/fhir/us/core/StructureDefinition/us-core-race",
                "extension": [
                    {"url": "ombCategory", "valueCoding": {"code": "2106-3", "display": "White"}},
                    {"url": "detailed", "valueCoding": {"code": "1735-0", "display": "Inupiat"}},
                    {"url": "text", "valueString": "White"},
                ],
            }
        ],
    }
    patient = next(iter(records_from_resources([resource]))).patient
    assert patient.race == ["White"]  # the typed lift is unchanged
    assert patient.extensions["fhir_r4:extension"] == [
        {
            "extension": [
                {"url": "ombCategory", "valueCoding": {"code": "2106-3", "display": "White"}},
                {"url": "detailed", "valueCoding": {"code": "1735-0", "display": "Inupiat"}},
            ]
        }
    ]


def test_omb_category_race_keeps_its_codes() -> None:
    """With no ``text``, the lift takes the ombCategory DISPLAYS — so the codes
    behind them (which nothing reads) still ride the residue."""
    resource = {
        "resourceType": "Patient",
        "id": PID,
        "name": [{"family": "Specimen"}],
        "extension": [
            {
                "url": "http://hl7.org/fhir/us/core/StructureDefinition/us-core-ethnicity",
                "extension": [
                    {
                        "url": "ombCategory",
                        "valueCoding": {"code": "2135-2", "display": "Hispanic or Latino"},
                    }
                ],
            }
        ],
    }
    patient = next(iter(records_from_resources([resource]))).patient
    assert patient.ethnicity == ["Hispanic or Latino"]
    assert patient.extensions["fhir_r4:extension"] == [
        {"extension": [{"valueCoding": {"code": "2135-2"}}]}
    ]


def test_vendor_shaped_race_extension_the_lift_cannot_read_survives_whole() -> None:
    """An entry carrying a lifted url but a shape the lift reads NOTHING from
    must not be marked consumed: nothing was lifted, so everything is residue."""
    resource = {
        "resourceType": "Patient",
        "id": PID,
        "name": [{"family": "Specimen"}],
        "extension": [
            {
                "url": "http://hl7.org/fhir/us/core/StructureDefinition/us-core-race",
                "valueString": "SENTINEL-VENDOR-RACE",
            }
        ],
    }
    patient = next(iter(records_from_resources([resource]))).patient
    assert patient.race == []
    assert patient.extensions["fhir_r4:extension"] == [
        {
            "url": "http://hl7.org/fhir/us/core/StructureDefinition/us-core-race",
            "valueString": "SENTINEL-VENDOR-RACE",
        }
    ]


def test_leading_placeholder_name_entry_does_not_blank_the_patient() -> None:
    """``name: [{}, {...}]`` must migrate the REAL name into the typed slots —
    selecting the placeholder loses nothing (it rides fhir_r4:name) but leaves
    every typed name slot empty, which the wrong-patient defenses fail closed on.
    The consumed-path bookkeeping follows the selected entry, so the entry's
    unread sub-keys (prefix) still narrate and the lifted ones do not duplicate.
    """
    resource = {
        "resourceType": "Patient",
        "id": PID,
        "name": [
            {},
            {"family": "SENTINEL-Real", "given": ["SENTINEL-Given"], "prefix": ["Dr"]},
        ],
    }
    patient = next(iter(records_from_resources([resource]))).patient
    assert (patient.given_name, patient.family_name) == ("SENTINEL-Given", "SENTINEL-Real")
    assert patient.extensions["fhir_r4:name"] == [{"prefix": ["Dr"]}]


def test_patient_name_subkeys_and_custom_extension_are_preserved() -> None:
    """Reading part of `name`/`extension` must not consume the whole element:
    HumanName.prefix/use and a non-US-Core extension survive at a namespaced
    path, while the sub-keys the mapper DID lift do not duplicate into it."""
    custom = "http://example.com/fhir/StructureDefinition/sentinel-flag"
    resource = {
        "resourceType": "Patient",
        "id": PID,
        "name": [{"use": "official", "prefix": ["Dr"], "given": ["Dexter"], "family": "Specimen"}],
        "extension": [
            {"url": custom, "valueString": "SENTINEL-CUSTOM-EXT"},
            {
                "url": "http://hl7.org/fhir/us/core/StructureDefinition/us-core-race",
                "extension": [{"url": "text", "valueString": "White"}],
            },
        ],
    }
    patient = next(iter(records_from_resources([resource]))).patient
    assert patient.extensions["fhir_r4:name"] == [{"use": "official", "prefix": ["Dr"]}]
    assert patient.extensions["fhir_r4:extension"] == [
        {"url": custom, "valueString": "SENTINEL-CUSTOM-EXT"}
    ]
    # The lifted parts still land in their typed slots, and only there.
    assert (patient.given_name, patient.family_name, patient.race) == (
        "Dexter",
        "Specimen",
        ["White"],
    )


def test_observation_panel_with_value_and_components_emits_all() -> None:
    obs = {
        "resourceType": "Observation",
        "id": "panel",
        "subject": _SUBJECT,
        "code": {"coding": [{"system": "http://loinc.org", "code": "85354-9"}]},
        "valueString": "see components",
        "component": [
            {
                "code": {"coding": [{"system": "http://loinc.org", "code": "8480-6"}]},
                "valueQuantity": {"value": 120, "unit": "mmHg"},
            },
            {
                "code": {"coding": [{"system": "http://loinc.org", "code": "8462-4"}]},
                "valueQuantity": {"value": 80, "unit": "mmHg"},
            },
        ],
    }
    observations = _record_with(obs).observations
    assert sorted(o.code for o in observations) == ["8462-4", "8480-6", "85354-9"]
    assert len({o.id for o in observations}) == 3  # unique ids, no collision


def test_components_sharing_a_loinc_get_distinct_ids() -> None:
    obs = {
        "resourceType": "Observation",
        "id": "dup",
        "subject": _SUBJECT,
        "code": {"coding": [{"system": "http://loinc.org", "code": "55284-4"}]},
        "component": [{"valueQuantity": {"value": 1}}, {"valueQuantity": {"value": 2}}],
    }
    ids = [o.id for o in _record_with(obs).observations]
    assert len(ids) == len(set(ids)) == 2


def test_html_note_is_downconverted_to_text_only() -> None:
    """An external text/html note is carried as TEXT (never NoteSection.html),
    so untrusted markup is not placed in the pack's `| safe` render slot."""
    body = "<p>Patient <b>improving</b>.</p><script>alert(1)</script>"
    enc = {
        "resourceType": "Encounter",
        "id": "e1",
        "subject": _SUBJECT,
        "period": {"start": "2023-01-01"},
    }
    docref = {
        "resourceType": "DocumentReference",
        "id": "d1",
        "subject": _SUBJECT,
        "content": [
            {
                "attachment": {
                    "contentType": "text/html",
                    "data": base64.b64encode(body.encode()).decode(),
                }
            }
        ],
        "context": {"encounter": [{"reference": "Encounter/e1"}]},
    }
    section = _record_with(enc, docref).encounters[0].sections[0]
    assert section.html is None
    assert "improving" in (section.text or "")
    assert "<script>" not in (section.text or "") and "<p>" not in (section.text or "")


def test_coverage_order_zero_does_not_become_negative() -> None:
    cov = {
        "resourceType": "Coverage",
        "id": "cov",
        "status": "active",
        "beneficiary": _SUBJECT,
        "order": 0,
        "payor": [{"display": "Acme"}],
    }
    assert _record_with(cov).coverages[0].order_of_benefits is None


def _text_docref(doc_id: str, body: str, **extra: object) -> dict:
    return {
        "resourceType": "DocumentReference",
        "id": doc_id,
        "subject": _SUBJECT,
        "content": [
            {
                "attachment": {
                    "contentType": "text/plain",
                    "data": base64.b64encode(body.encode()).decode(),
                }
            }
        ],
        **extra,
    }


def test_note_without_encounter_becomes_synthetic_encounter() -> None:
    """A clinical note with no context.encounter (routine in a $export) must not
    be dropped: it gets a synthetic encounter so the narrative still renders."""
    body = "Telephone encounter: medication refill approved."
    docref = _text_docref(
        "loose-note", body, type={"text": "Telephone note"}, date="2023-07-04T10:00:00Z"
    )
    rec = _record_with(docref)
    synthetic = [e for e in rec.encounters if e.id == "docref:loose-note"]
    assert len(synthetic) == 1
    assert body in (synthetic[0].sections[0].text or "")
    assert synthetic[0].date_of_service.isoformat() == "2023-07-04"
    assert synthetic[0].note_type == "Telephone note"


def test_note_with_dangling_encounter_ref_is_not_lost() -> None:
    """A note whose context.encounter points outside the data (a $export slice)
    still reaches a (synthetic) encounter rather than vanishing."""
    body = "Critical lab value called to provider."
    docref = _text_docref(
        "stray-note", body, context={"encounter": [{"reference": "Encounter/not-in-this-slice"}]}
    )
    rec = _record_with(docref)  # no Encounter resource present at all
    assert any(body in (s.text or "") for e in rec.encounters for s in e.sections)


def test_binary_documentreference_status_preserved() -> None:
    """A retracted (entered-in-error) PDF must not migrate as a live document —
    its top-level fields ride the artifact extensions."""
    docref = {
        "resourceType": "DocumentReference",
        "id": "pdf-1",
        "subject": _SUBJECT,
        "status": "entered-in-error",
        "type": {"text": "Scanned record"},
        "content": [
            {
                "attachment": {
                    "contentType": "application/pdf",
                    "url": "file:///x.pdf",
                    "title": "Scan",
                }
            }
        ],
    }
    art = _record_with(docref).documents[0]
    assert art.mime_type == "application/pdf"
    assert art.extensions["fhir_r4:status"] == "entered-in-error"
    assert art.extensions["fhir_r4:url"] == "file:///x.pdf"


def test_attached_note_metadata_preserved_in_record() -> None:
    """A note attached to a real encounter has nowhere on NoteSection to carry
    its status, so the docref's fields are preserved under record note_meta."""
    enc = {
        "resourceType": "Encounter",
        "id": "e1",
        "subject": _SUBJECT,
        "period": {"start": "2023-01-01"},
    }
    docref = _text_docref(
        "n1",
        "Note body here.",
        status="entered-in-error",
        context={"encounter": [{"reference": "Encounter/e1"}]},
    )
    rec = _record_with(enc, docref)
    assert any("Note body" in (s.text or "") for s in rec.encounters[0].sections)
    assert rec.extensions["fhir_r4:note_meta"]["n1"]["fhir_r4:status"] == "entered-in-error"


def test_unrenderable_documentreference_is_not_dropped() -> None:
    """A DocumentReference with no renderable content (empty/whitespace/
    undecodable) is neither a note nor an artifact, but it must still be
    preserved whole — the module promises nothing is silently dropped."""
    empty = {
        "resourceType": "DocumentReference",
        "id": "empty-note",
        "subject": _SUBJECT,
        "status": "entered-in-error",
        "type": {"text": "Void"},
        "content": [],
    }
    whitespace = _text_docref("ws-note", "   ", status="superseded")
    rec = _record_with(empty, whitespace)
    assert rec.encounters == [] and rec.documents == []
    kept = {d["id"]: d for d in rec.extensions["fhir_r4:DocumentReference"]}
    assert kept["empty-note"]["status"] == "entered-in-error"
    assert kept["ws-note"]["status"] == "superseded"


def test_minimal_patient_grouped_independently() -> None:
    rec = _by_name()["Wendell Placeholder"]
    assert rec.patient.birth_date.isoformat() == "1990-11-30"
    assert len(rec.encounters) == 1
    assert [c.snomed for c in rec.conditions] == ["195967001"]
    assert len(rec.observations) == 1  # only this patient's weight
    assert rec.medications == [] and rec.coverages == []


# --- reference resources (Practitioner / Location / Organization) ----------

PRAC_A = "feedface-000c-0000-0000-00000000000a"
PRAC_B = "feedface-000c-0000-0000-00000000000b"
LOC_A = "feedface-000d-0000-0000-00000000000a"
ORG_B = "feedface-000d-0000-0000-00000000000b"
ENC_ID = "feedface-0002-0000-0000-00000000000e"
NPI_SYSTEM = "http://hl7.org/fhir/sid/us-npi"


def _encounter_resource(**extra: object) -> dict:
    return {
        "resourceType": "Encounter",
        "id": ENC_ID,
        "subject": _SUBJECT,
        "period": {"start": "2023-05-10"},
        **extra,
    }


def _practitioner_resource(pid: str, family: str) -> dict:
    return {
        "resourceType": "Practitioner",
        "id": pid,
        "name": [{"family": family, "given": ["Avery"]}],
    }


def test_uncited_reference_resources_ride_shared() -> None:
    """A Practitioner/Location/Organization no record's encounter cites reaches
    no typed slot; without the bundle-level key it would vanish from the load
    entirely. The CITED ones stay attached and are not duplicated into it."""
    prac_a = _practitioner_resource(PRAC_A, "Cited")
    prac_b = _practitioner_resource(PRAC_B, "Uncited")
    loc_a = {"resourceType": "Location", "id": LOC_A, "name": "Example Family Clinic"}
    org_b = {"resourceType": "Organization", "id": ORG_B, "name": "Example Health System"}
    enc = _encounter_resource(
        participant=[{"individual": {"reference": f"Practitioner/{PRAC_A}"}}],
        location=[{"location": {"reference": f"Location/{LOC_A}"}}],
    )
    rec = _record_with(prac_a, prac_b, loc_a, org_b, enc)
    assert [p.id for p in rec.practitioners] == [PRAC_A]
    assert [f.id for f in rec.facilities] == [LOC_A]
    assert rec.extensions["fhir_r4:shared"] == [prac_b, org_b]  # verbatim, bundle order


def test_uncited_practitioner_rides_shared_on_every_record() -> None:
    """The key claims no attribution, so an uncited reference resource is
    preserved on BOTH records rather than guessed onto one of them."""
    prac = _practitioner_resource(PRAC_B, "Uncited")
    records = list(
        records_from_resources(
            [_patient_resource(), _patient_resource(family="Placeholder", pid=PID2), prac]
        )
    )
    assert len(records) == 2
    for record in records:
        assert record.extensions["fhir_r4:shared"] == [prac]
        assert record.practitioners == []


def test_practitioner_cited_only_by_a_dangling_encounter_is_preserved() -> None:
    """A dangling encounter never becomes a typed record, so it cites nobody: the
    practitioner it names must be treated as UNCITED and ride the shared key,
    not counted as attached and then dropped for want of a record to attach to."""
    prac = _practitioner_resource(PRAC_B, "Ghostcited")
    enc = {
        "resourceType": "Encounter",
        "id": ENC_ID,
        "subject": {"reference": "Patient/does-not-exist"},
        "participant": [{"individual": {"reference": f"Practitioner/{PRAC_B}"}}],
    }
    rec = _record_with(prac, enc)
    assert rec.practitioners == []
    assert rec.extensions["fhir_r4:shared"] == [prac]
    assert [r["id"] for r in rec.extensions["fhir_r4:unanchored"]] == [ENC_ID]


# --- partial consumption: siblings of a read element ------------------------


def test_practitioner_non_npi_identifier_and_second_name_ride_residual() -> None:
    """The lift reads one name entry's family/given[0]/text and the NPI alone —
    a second name, the prefix/use beside the one it read, and every non-NPI
    identifier are siblings that must survive at a namespaced path."""
    prac = {
        "resourceType": "Practitioner",
        "id": PRAC_A,
        "name": [
            {"family": "Marrow", "given": ["Avery"], "prefix": ["Dr"], "use": "official"},
            {"family": "SENTINEL-Maiden", "use": "maiden"},
        ],
        "identifier": [
            {"system": NPI_SYSTEM, "value": "1234567893"},
            {"system": "http://example.com/fhir/staff-id", "value": "SENTINEL-STAFF-77"},
        ],
    }
    enc = _encounter_resource(participant=[{"individual": {"reference": f"Practitioner/{PRAC_A}"}}])
    obj = _record_with(prac, enc).practitioners[0]
    assert (obj.given_name, obj.family_name, obj.npi) == ("Avery", "Marrow", "1234567893")
    assert obj.extensions["fhir_r4:identifier"] == [
        {"system": "http://example.com/fhir/staff-id", "value": "SENTINEL-STAFF-77"}
    ]
    assert obj.extensions["fhir_r4:name"] == [
        {"prefix": ["Dr"], "use": "official"},
        {"family": "SENTINEL-Maiden", "use": "maiden"},
    ]


def test_location_telecom_email_and_address_siblings_ride_residual() -> None:
    """The header lift reads two address lines, city/state/postalCode and the
    phone/fax telecom values; an email entry, the .use/.rank beside the phone it
    read, a third address line and the address country are not read at all."""
    loc = {
        "resourceType": "Location",
        "id": LOC_A,
        "name": "Example Family Clinic",
        "address": {
            "line": ["200 Clinic Way", "Suite 3", "SENTINEL-Line-3"],
            "city": "Springfield",
            "state": "IL",
            "postalCode": "60001",
            "country": "US",
        },
        "telecom": [
            {"system": "phone", "value": "555-555-0100", "use": "work", "rank": 1},
            {"system": "email", "value": "records@example.com"},
        ],
    }
    enc = _encounter_resource(location=[{"location": {"reference": f"Location/{LOC_A}"}}])
    fac = _record_with(loc, enc).facilities[0]
    assert (fac.phone, fac.address_line1, fac.address_line2) == (
        "555-555-0100",
        "200 Clinic Way",
        "Suite 3",
    )
    assert fac.extensions["fhir_r4:telecom"] == [
        {"use": "work", "rank": 1},
        {"system": "email", "value": "records@example.com"},
    ]
    assert fac.extensions["fhir_r4:address"] == {"line": ["SENTINEL-Line-3"], "country": "US"}


def test_encounter_participant_and_location_siblings_ride_residual() -> None:
    """Only the first participant's individual reference, the first location's
    reference and each diagnosis condition are read — a participant's type and
    period, a second participant or location, a location status and a diagnosis
    rank ride the residue rather than leaving with the reference that was."""
    enc = _encounter_resource(
        participant=[
            {
                "individual": {
                    "reference": f"Practitioner/{PRAC_A}",
                    "display": "SENTINEL-Display",
                },
                "type": [{"text": "primary performer"}],
                "period": {"start": "2023-05-10T09:00:00Z"},
            },
            {"individual": {"reference": f"Practitioner/{PRAC_B}"}},
        ],
        location=[
            {"location": {"reference": f"Location/{LOC_A}"}, "status": "completed"},
            {"location": {"reference": f"Location/{ORG_B}"}},
        ],
        diagnosis=[{"condition": {"reference": "Condition/dx-1"}, "rank": 1}],
    )
    obj = _record_with(enc).encounters[0]
    assert (obj.provider_id, obj.facility_id, obj.diagnosis_ids) == (PRAC_A, LOC_A, ["dx-1"])
    assert obj.extensions["fhir_r4:participant"] == [
        {
            "individual": {"display": "SENTINEL-Display"},
            "type": [{"text": "primary performer"}],
            "period": {"start": "2023-05-10T09:00:00Z"},
        },
        {"individual": {"reference": f"Practitioner/{PRAC_B}"}},
    ]
    assert obj.extensions["fhir_r4:location"] == [
        {"status": "completed"},
        {"location": {"reference": f"Location/{ORG_B}"}},
    ]
    assert obj.extensions["fhir_r4:diagnosis"] == [{"rank": 1}]


def test_observation_effective_period_end_and_code_siblings_ride_residual() -> None:
    """``effectivePeriod.end`` is never lifted (only ``.start`` is), and neither
    is an unmatched category entry, the display beside the LOINC that matched, a
    Quantity's system, or a referenceRange."""
    category_system = "http://terminology.hl7.org/CodeSystem/observation-category"
    obs = {
        "resourceType": "Observation",
        "id": "feedface-0003-0000-0000-00000000000e",
        "status": "final",
        "subject": _SUBJECT,
        "category": [
            {"coding": [{"system": category_system, "code": "SENTINEL-vendor"}]},
            {"coding": [{"system": category_system, "code": "laboratory"}]},
        ],
        "code": {
            "text": "Glucose",
            "coding": [
                {
                    "system": "http://loinc.org",
                    "code": "2339-0",
                    "display": "Glucose [Mass/volume] in Blood",
                }
            ],
        },
        "effectivePeriod": {"start": "2023-05-10T09:40:00", "end": "2023-05-10T10:00:00"},
        "valueQuantity": {"value": 99, "unit": "mg/dL", "system": "http://unitsofmeasure.org"},
        "referenceRange": [{"text": "70-110 mg/dL"}],
    }
    obj = _record_with(obs).observations[0]
    assert (obj.code, obj.display, obj.value, obj.unit) == ("2339-0", "Glucose", "99", "mg/dL")
    assert obj.category is ObservationCategory.LABORATORY
    assert obj.effective_at.isoformat() == "2023-05-10T09:40:00"
    ext = obj.extensions
    assert ext["fhir_r4:effectivePeriod"] == {"end": "2023-05-10T10:00:00"}
    assert ext["fhir_r4:code"] == {"coding": [{"display": "Glucose [Mass/volume] in Blood"}]}
    assert ext["fhir_r4:valueQuantity"] == {"system": "http://unitsofmeasure.org"}
    assert ext["fhir_r4:category"] == [
        {"coding": [{"system": category_system, "code": "SENTINEL-vendor"}]},
        {"coding": [{"system": category_system}]},
    ]
    assert ext["fhir_r4:referenceRange"] == [{"text": "70-110 mg/dL"}]


def test_allergy_resolved_status_and_unmatched_category_ride_residual() -> None:
    """A clinicalStatus that does NOT read active was never lifted: losing
    "resolved" would migrate a resolved allergy as merely inactive. The category
    entry the match scanned past, and the reaction onset, are unread too."""
    allergy = {
        "resourceType": "AllergyIntolerance",
        "id": "feedface-0006-0000-0000-00000000000e",
        "patient": _SUBJECT,
        "clinicalStatus": {
            "coding": [
                {
                    "code": "resolved",
                    "system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical",
                }
            ]
        },
        "category": ["biologic", "medication"],
        "criticality": "high",
        "code": {
            "text": "Penicillin",
            "coding": [
                {"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "7980"},
            ],
        },
        "reaction": [
            {"manifestation": [{"text": "Hives"}], "severity": "moderate", "onset": "2023-01-02"}
        ],
    }
    obj = _record_with(allergy).allergies[0]
    assert (obj.substance, obj.severity, obj.reactions) == ("Penicillin", "moderate", ["Hives"])
    assert obj.category is AllergyCategory.DRUG and obj.active is False
    ext = obj.extensions
    assert ext["fhir_r4:clinicalStatus"]["coding"][0]["code"] == "resolved"
    assert ext["fhir_r4:category"] == ["biologic"]
    assert ext["fhir_r4:reaction"] == [{"onset": "2023-01-02"}]
    assert ext["fhir_r4:code"] == {
        "coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "7980"}]
    }


def test_coverage_cancelled_status_and_second_class_ride_residual() -> None:
    """A cancelled policy must not migrate as merely inactive, so the status it
    was never lifted from rides; so do a second `class` entry and the payor
    reference beside the display the lift read."""
    cov = {
        "resourceType": "Coverage",
        "id": "feedface-0007-0000-0000-00000000000e",
        "status": "cancelled",
        "beneficiary": _SUBJECT,
        "subscriberId": "MEMBER-000999",
        "order": 1,
        "payor": [{"display": "Acme Health Plan", "reference": "Organization/acme"}],
        "period": {"start": "2023-01-01", "end": "2023-12-31"},
        "class": [
            {"type": {"coding": [{"code": "group"}]}, "value": "GRP-0001", "name": "Acme PPO"},
            {"type": {"coding": [{"code": "subplan"}]}, "value": "SENTINEL-SUBPLAN"},
        ],
    }
    obj = _record_with(cov).coverages[0]
    assert (obj.payer, obj.plan_name, obj.group_number) == (
        "Acme Health Plan",
        "Acme PPO",
        "GRP-0001",
    )
    assert obj.order_of_benefits == 0 and obj.active is False
    ext = obj.extensions
    assert ext["fhir_r4:status"] == "cancelled"
    assert ext["fhir_r4:payor"] == [{"reference": "Organization/acme"}]
    assert ext["fhir_r4:class"] == [
        {"type": {"coding": [{"code": "subplan"}]}, "value": "SENTINEL-SUBPLAN"}
    ]


def test_condition_resolved_status_and_unmatched_codings_ride_residual() -> None:
    """The meaning-reversal case: `active=False` is the lift finding no active
    coding, so the code that IS there must stay whole. The codings beside the
    ones ICD-10/SNOMED matched are unread siblings too."""
    cond = {
        "resourceType": "Condition",
        "id": "feedface-0004-0000-0000-00000000000e",
        "subject": _SUBJECT,
        "clinicalStatus": {"coding": [{"code": "resolved"}]},
        "code": {
            "text": "Essential hypertension",
            "coding": [
                {"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": "I10"},
                {"system": "http://example.com/fhir/local-codes", "code": "SENTINEL-LOCAL"},
            ],
        },
        "recordedDate": "SENTINEL-unparseable",
    }
    obj = _record_with(cond).conditions[0]
    assert (obj.icd10, obj.display, obj.active) == ("I10", "Essential hypertension", False)
    assert obj.recorded_at is None
    ext = obj.extensions
    assert ext["fhir_r4:clinicalStatus"] == {"coding": [{"code": "resolved"}]}
    assert ext["fhir_r4:code"] == {
        "coding": [{"system": "http://example.com/fhir/local-codes", "code": "SENTINEL-LOCAL"}]
    }
    assert ext["fhir_r4:recordedDate"] == "SENTINEL-unparseable"  # parsed to nothing, so kept


def test_medication_second_dosage_line_and_route_ride_residual() -> None:
    """Only ``dosageInstruction[0].text`` becomes the sig — the route beside it
    and a second dosage line are instructions a chart would otherwise lose."""
    med = {
        "resourceType": "MedicationRequest",
        "id": "feedface-0008-0000-0000-00000000000e",
        "status": "active",
        "subject": _SUBJECT,
        "authoredOn": "2023-05-10",
        "medicationCodeableConcept": {
            "text": "Metformin 500 MG Oral Tablet",
            "coding": [
                {"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "860975"},
            ],
        },
        "dosageInstruction": [
            {"text": "Take 1 tablet by mouth twice daily", "route": {"text": "Oral"}},
            {"text": "SENTINEL-TAPER-LINE"},
        ],
    }
    obj = _record_with(med).medications[0]
    assert (obj.sig, obj.rxnorm) == ("Take 1 tablet by mouth twice daily", "860975")
    assert obj.start.isoformat() == "2023-05-10"
    assert obj.extensions["fhir_r4:dosageInstruction"] == [
        {"route": {"text": "Oral"}},
        {"text": "SENTINEL-TAPER-LINE"},
    ]


def test_immunization_second_note_and_vaccine_display_ride_residual() -> None:
    """The comment comes from ``note[0].text`` alone, and the vaccineCode display
    beside the CVX the lift matched is never read."""
    imm = {
        "resourceType": "Immunization",
        "id": "feedface-0009-0000-0000-00000000000e",
        "status": "completed",
        "patient": _SUBJECT,
        "occurrenceDateTime": "2023-10-01",
        "vaccineCode": {
            "text": "Influenza, seasonal",
            "coding": [
                {"system": "http://hl7.org/fhir/sid/cvx", "code": "140", "display": "SENTINEL-CVX"}
            ],
        },
        "note": [
            {"text": "Left deltoid.", "time": "2023-10-01T11:00:00Z"},
            {"text": "SENTINEL-SECOND-NOTE"},
        ],
    }
    obj = _record_with(imm).immunizations[0]
    assert (obj.vaccine, obj.comment) == ("Influenza, seasonal", "Left deltoid.")
    assert obj.extensions["fhir_r4:cvx"] == "140"
    assert obj.extensions["fhir_r4:note"] == [
        {"time": "2023-10-01T11:00:00Z"},
        {"text": "SENTINEL-SECOND-NOTE"},
    ]
    assert obj.extensions["fhir_r4:vaccineCode"] == {"coding": [{"display": "SENTINEL-CVX"}]}


def test_goal_cancelled_lifecycle_and_description_codings_ride_residual() -> None:
    """A lifecycleStatus outside the active set was never lifted: an abandoned
    goal must not migrate as merely inactive. The description codings behind the
    ``text`` the lift preferred are unread siblings too."""
    goal = {
        "resourceType": "Goal",
        "id": "feedface-000a-0000-0000-00000000000e",
        "lifecycleStatus": "cancelled",
        "subject": _SUBJECT,
        "description": {
            "text": "Lower blood pressure to below 130/80",
            "coding": [{"system": "http://snomed.info/sct", "code": "SENTINEL-GOAL-CODE"}],
        },
    }
    obj = _record_with(goal).goals[0]
    assert obj.description == "Lower blood pressure to below 130/80" and obj.active is False
    assert obj.extensions["fhir_r4:lifecycleStatus"] == "cancelled"
    assert obj.extensions["fhir_r4:description"] == {
        "coding": [{"system": "http://snomed.info/sct", "code": "SENTINEL-GOAL-CODE"}]
    }


def test_family_history_second_condition_and_note_ride_residual() -> None:
    """Only the first condition entry is lifted; a second one and the note beside
    the code must not leave with it."""
    fam = {
        "resourceType": "FamilyMemberHistory",
        "id": "feedface-000b-0000-0000-00000000000e",
        "status": "completed",
        "patient": _SUBJECT,
        "relationship": {"text": "Father"},
        "condition": [
            {
                "code": {"text": "Type 2 diabetes mellitus"},
                "onsetString": "age 55",
                "note": [{"text": "SENTINEL-FAMILY-NOTE"}],
            },
            {"code": {"text": "SENTINEL-SECOND-CONDITION"}},
        ],
    }
    obj = _record_with(fam).family_history[0]
    assert (obj.relation, obj.diagnosis) == ("Father", "Type 2 diabetes mellitus")
    assert obj.extensions["fhir_r4:onset_string"] == "age 55"
    assert obj.extensions["fhir_r4:condition"] == [
        {"note": [{"text": "SENTINEL-FAMILY-NOTE"}]},
        {"code": {"text": "SENTINEL-SECOND-CONDITION"}},
    ]


def test_fully_read_elements_leave_no_residual_key() -> None:
    """Sentinel discipline: an element whose every sub-field was lifted yields no
    empty placeholder — partial consumption adds residue, it does not invent it."""
    prac = {
        "resourceType": "Practitioner",
        "id": PRAC_A,
        "name": [{"family": "Marrow", "given": ["Avery"], "text": "Avery Marrow, MD"}],
        "identifier": [{"system": NPI_SYSTEM, "value": "1234567893"}],
    }
    enc = _encounter_resource(participant=[{"individual": {"reference": f"Practitioner/{PRAC_A}"}}])
    rec = _record_with(prac, enc)
    obj = rec.practitioners[0]
    assert (obj.given_name, obj.family_name, obj.display_name, obj.npi) == (
        "Avery",
        "Marrow",
        "Avery Marrow, MD",
        "1234567893",
    )
    assert "fhir_r4:name" not in obj.extensions
    assert "fhir_r4:identifier" not in obj.extensions
    assert "fhir_r4:participant" not in rec.encounters[0].extensions


# --- NDJSON ($export) path -------------------------------------------------


def test_ndjson_export_matches_bundle(tmp_path: Path) -> None:
    """A Bulk-Data $export (one NDJSON file per resource type) yields the same
    records as the single Bundle — both converge on records_from_resources."""
    bundle = json.loads(BUNDLE.read_text(encoding="utf-8"))
    by_type: dict[str, list[dict]] = defaultdict(list)
    for entry in bundle["entry"]:
        res = entry["resource"]
        by_type[res["resourceType"]].append(res)
    for rtype, items in by_type.items():
        (tmp_path / f"{rtype}.ndjson").write_text(
            "\n".join(json.dumps(item) for item in items) + "\n", encoding="utf-8"
        )
    assert _adapter().detect(tmp_path) is True
    ndjson_records = list(_adapter().load(tmp_path))
    assert [r.patient.display_name for r in ndjson_records] == [
        r.patient.display_name for r in _records()
    ]
    dexter = next(r for r in ndjson_records if r.patient.family_name == "Specimen")
    assert len(dexter.encounters) == 2
    assert len(dexter.observations) == 6


# --- determinism + loud failure --------------------------------------------


def _projection(records: list) -> list:
    return [
        (
            r.patient.display_name,
            [e.date_of_service.isoformat() if e.date_of_service else None for e in r.encounters],
            [(o.code, o.value) for o in r.observations],
            [(c.icd10, c.snomed) for c in r.conditions],
        )
        for r in records
    ]


def test_load_is_deterministic() -> None:
    assert _projection(_records()) == _projection(_records())


def test_no_patient_is_a_loud_failure(tmp_path: Path) -> None:
    (tmp_path / "lonely.json").write_text(
        json.dumps(
            {
                "resourceType": "Bundle",
                "type": "collection",
                "entry": [
                    {"resource": {"resourceType": "Observation", "id": "x", "status": "final"}}
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="no Patient"):
        list(_adapter().load(tmp_path))


# --- registration + end-to-end render --------------------------------------


def test_adapter_registered_in_toolkit_info() -> None:
    from anastomosis.core.commands import get_toolkit_info

    sources = {name for name, _ in get_toolkit_info().sources}
    assert "fhir-r4" in sources


def test_end_to_end_render_through_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("pymupdf", reason="render e2e needs PyMuPDF")
    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)
    from anastomosis.core.commands import PipelineCommand, run_pipeline_command, summarize_patients

    out = tmp_path / "charts"
    result = run_pipeline_command(
        PipelineCommand(
            export_dir=FIXTURE_DIR, charts_dir=out, source="fhir-r4", pack="generic_soap"
        )
    )
    assert result.pipeline.source_name == "fhir-r4"
    # Three encounters across two patients → three rendered charts; QA passes.
    assert len(result.pipeline.render_result.rendered) == 3
    assert result.pipeline.qa_report is not None and result.pipeline.qa_report.ok
    assert len(list(out.glob("*.pdf"))) == 3
    summary = {s.display_name: s for s in summarize_patients(result.pipeline)}
    assert summary["Dexter Quill Specimen Jr."].documents == 2
    assert summary["Wendell Placeholder"].documents == 1
