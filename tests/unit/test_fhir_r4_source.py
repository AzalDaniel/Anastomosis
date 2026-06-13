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
from anastomosis.sources.fhir_r4.mapper import records_from_resources

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


def test_orphan_resource_not_misattributed_across_patients() -> None:
    """With several patients, an unattributable resource is omitted rather than
    misattributed to an arbitrary patient (the safer-than-corruption choice)."""
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
    for rec in records_from_resources(resources):
        assert "fhir_r4:unanchored" not in rec.extensions


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
    pytest.importorskip("fitz", reason="render e2e needs PyMuPDF")
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
