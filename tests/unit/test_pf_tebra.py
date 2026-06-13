"""Tests for the PF/Tebra adapter against the synthetic v9 fixture.

Each test asserts one trap documented in tests/fixtures/pf_tebra_v9/README.md.
"""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

import anastomosis.sources.pf_tebra  # noqa: F401 — registers the adapter
from anastomosis.core.model import (
    IdentifierKind,
    ObservationCategory,
    PatientRecord,
    SectionKind,
)
from anastomosis.sources import get_source

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"

P1 = "feedface-0000-0000-0000-000000000001"
P2 = "feedface-0000-0000-0000-000000000002"
P3 = "feedface-0000-0000-0000-000000000003"
E1 = "feedface-e000-0000-0000-000000000001"
E3 = "feedface-e000-0000-0000-000000000003"
E4 = "feedface-e000-0000-0000-000000000004"
E5 = "feedface-e000-0000-0000-000000000005"
E6 = "feedface-e000-0000-0000-000000000006"
E7 = "feedface-e000-0000-0000-000000000007"  # empty-SOAP, excluded from render
E8 = "feedface-e000-0000-0000-000000000008"  # adult growth chart, excluded


@pytest.fixture(scope="module")
def records() -> dict[str, PatientRecord]:
    adapter = get_source("pf-tebra")
    assert adapter.detect(FIXTURE)
    loaded = {record.patient.id: record for record in adapter.load(FIXTURE)}
    assert len(loaded) == 3
    return loaded


def test_detect_rejects_non_pf_dirs(tmp_path: Path) -> None:
    assert not get_source("pf-tebra").detect(tmp_path)


def test_patient_demographics(records: dict[str, PatientRecord]) -> None:
    ada = records[P1].patient
    assert ada.display_name == "Ada Q Fixture"
    assert ada.birth_date == date(1985, 3, 14)
    assert ada.sex == "Female"
    assert ada.race == ["White", "Asian"]
    assert ada.gender_identity == "Identifies as Female"
    assert ada.identifier(IdentifierKind.SOURCE_GUID) == P1
    assert ada.identifier(IdentifierKind.SSN) == "900-12-3456"
    phones = {t.kind.value: t.value for t in ada.telecom}
    assert phones["phone_home"] == "(206) 555-0142"
    assert phones["phone_mobile"] == "(206) 555-0188"
    assert ada.addresses[0].line1 == "123 Example St"
    assert ada.notes is not None and "Allergy alert" in ada.notes  # pinned note folded in


def test_lossless_extensions_carry_unmapped_columns(records: dict[str, PatientRecord]) -> None:
    ada = records[P1].patient
    assert ada.extensions["pf_tebra:NamePrefix"] == "Ms."
    assert ada.extensions["pf_tebra:IsMultipleBirth"] == "false"
    assert "pf_tebra:PatientCreatedDateTimeUtc" in ada.extensions
    # Mapped columns never duplicate into extensions; sentinel cells vanish.
    assert "pf_tebra:FirstName" not in ada.extensions
    boris = records[P2].patient
    assert "pf_tebra:MothersMaidenName" not in boris.extensions  # was \N


def test_sentinel_cells_mean_absent(records: dict[str, PatientRecord]) -> None:
    boris = records[P2].patient
    assert boris.identifier(IdentifierKind.SSN) is None  # \N
    assert all(t.kind.value != "email" for t in boris.telecom)
    well_child = next(e for e in records[P3].encounters if e.id == E6)
    assert well_child.signed_at is None  # 1/1/0001 12:00:00 AM
    assert well_child.signed_by_id is None
    assert well_child.chief_complaint is None  # \N


def test_encounter_soap_sections_and_html_shadow(records: dict[str, PatientRecord]) -> None:
    encounter = next(e for e in records[P1].encounters if e.id == E1)
    assert encounter.encounter_type == "SOAP"
    subjective = encounter.section(SectionKind.SUBJECTIVE)
    assert subjective is not None
    # html is the sanitize_soap_html rendering path: rich HTML wrapped in
    # pf-rich-text (predecessor sanitize, gpdfs:1258); text is the plain shadow.
    assert subjective.html is not None
    assert subjective.html.startswith('<div class="pf-rich-text">')
    assert "<p>Reports good medication adherence. No dizziness or headache.</p>" in subjective.html
    assert subjective.text == "Reports good medication adherence. No dizziness or headache."
    assert encounter.signed_at == datetime(2023, 5, 10, 21, 32, 11, tzinfo=UTC)
    assert encounter.diagnosis_ids == ["feedface-d000-0000-0000-000000000001"]


def test_simple_note_maps_to_narrative(records: dict[str, PatientRecord]) -> None:
    simple = next(e for e in records[P2].encounters if e.id == E4)
    assert simple.encounter_type == "SIMPLE"
    assert [s.kind for s in simple.sections] == [SectionKind.NARRATIVE]
    assert simple.sections[0].text is not None and "Nurse visit" in simple.sections[0].text


def test_invalid_encounters_excluded_from_render_but_preserved(
    records: dict[str, PatientRecord],
) -> None:
    # Predecessor get_valid_encounters SELECTION (gpdfs:1484): empty-SOAP and
    # adult-growth-chart encounters are not rendered. Justified divergence: we
    # keep them in record.extensions (losslessness) instead of dropping them.
    boris = records[P2]
    rendered_ids = {e.id for e in boris.encounters}
    assert E7 not in rendered_ids  # empty SOAP
    assert E8 not in rendered_ids  # adult growth chart
    assert {E4, E5} <= rendered_ids  # the valid ones still render
    skipped = boris.extensions["pf_tebra:skipped_encounters"]
    by_id = {entry["encounter"]["id"]: entry["reason"] for entry in skipped}
    assert by_id[E7] == "empty_soap"
    assert by_id[E8] == "adult_growth_chart"


def test_same_day_encounters_exist_for_collision_handling(
    records: dict[str, PatientRecord],
) -> None:
    # The fixture must keep offering the same-day pair the renderer's
    # filename-collision logic is tested against.
    dates = [e.date_of_service for e in records[P1].encounters if e.date_of_service]
    assert dates.count(date(2023, 5, 10)) == 2


def test_addendum_attached(records: dict[str, PatientRecord]) -> None:
    annual = next(e for e in records[P1].encounters if e.id == E3)
    assert len(annual.addenda) == 1
    addendum = annual.addenda[0]
    assert addendum.status == "Accepted"
    assert addendum.text is not None and "lipid panel" in addendum.text


def test_bmi_auto_calc_trigger(records: dict[str, PatientRecord]) -> None:
    obs_e1 = records[P1].observations_for(E1)
    bmi = next(o for o in obs_e1 if o.code == "39156-5")
    # 2dp matches the predecessor (gpdfs:592). Weight charted as 29463-7
    # (modern alias) still fires the trigger keyed on 3141-9-or-alias.
    assert bmi.value == "25.75"  # round(703 * 150 / 64^2, 2)
    assert bmi.extensions["pf_tebra:computed"] == "bmi_auto_calc"
    # Pediatric encounter gets one too (height+weight, no explicit BMI).
    assert any(o.code == "39156-5" for o in records[P3].observations_for(E6))


def test_explicit_bmi_is_never_recomputed(records: dict[str, PatientRecord]) -> None:
    obs_e5 = [o for o in records[P2].observations_for(E5) if o.code == "39156-5"]
    assert len(obs_e5) == 1
    assert obs_e5[0].value == "30.0"
    assert "pf_tebra:computed" not in obs_e5[0].extensions


def test_vitals_are_loinc_categorized(records: dict[str, PatientRecord]) -> None:
    obs_e1 = records[P1].observations_for(E1)
    vitals = {o.code for o in obs_e1 if o.category == ObservationCategory.VITAL_SIGNS}
    # 29463-7 (weight) and 59408-5 (O2) are charted as the MODERN aliases here
    # and still categorize as vitals (dual-map: old primary + new alias).
    assert {"8302-2", "29463-7", "8480-6", "8462-4", "8867-4", "72514-3"} <= vitals
    pain = next(o for o in obs_e1 if o.code == "72514-3")
    assert pain.value == "4"
    # Head circumference charted as the modern 9843-4 alias still categorizes.
    head_circ = next(o for o in records[P3].observations_for(E6) if o.code == "9843-4")
    assert head_circ.category == ObservationCategory.VITAL_SIGNS
    assert head_circ.value == "18.5"


def test_social_history_observations(records: dict[str, PatientRecord]) -> None:
    ada_social = [
        o for o in records[P1].observations if o.category == ObservationCategory.SOCIAL_HISTORY
    ]
    by_label = {o.display: o.value for o in ada_social}
    assert by_label["Tobacco use"] == "Former smoker"
    assert by_label["Occupation"] == "Carpenter"
    assert by_label["Industry"] == "Construction"
    boris_social = {
        o.display: o.value
        for o in records[P2].observations
        if o.category == ObservationCategory.SOCIAL_HISTORY
    }
    assert boris_social["Education"] == "High school graduate"


def test_conditions_parse_code_equivalents(records: dict[str, PatientRecord]) -> None:
    htn = next(c for c in records[P1].conditions if c.display == "Essential hypertension")
    assert (htn.icd10, htn.snomed, htn.active) == ("I10", "59621000", True)
    derm = next(c for c in records[P1].conditions if "dermatitis" in (c.display or ""))
    assert derm.active is False  # has a StopDate
    well = records[P3].conditions[0]
    assert well.icd10 == "Z00.129" and well.snomed is None


def test_allergies_with_joined_reactions(records: dict[str, PatientRecord]) -> None:
    penicillin = records[P1].allergies[0]
    assert penicillin.category.value == "drug"
    assert penicillin.severity == "Severe"
    assert penicillin.reactions == ["Hives", "Anaphylaxis"]


def test_medication_activity_and_prescription_links(
    records: dict[str, PatientRecord],
) -> None:
    lisinopril = next(m for m in records[P1].medications if m.generic_name == "lisinopril")
    assert lisinopril.active is True
    # Two scripts now link to lisinopril (Rx 1 dispensed, Rx 3 verified-only).
    assert sorted(lisinopril.prescription_ids) == [
        "feedface-0e5c-0000-0000-000000000001",
        "feedface-0e5c-0000-0000-000000000003",
    ]
    metformin = next(m for m in records[P2].medications if m.generic_name == "metformin")
    assert metformin.active is False  # stopped + discontinued reason


def test_escript_status_resolution(records: dict[str, PatientRecord]) -> None:
    # Resolution runs on the predecessor's _ESCRIPT_LABEL_MAP keyed on the
    # transaction DESCRIPTION (gpdfs:331): dispensing (100) beats the refill (10)
    # and the order-sent VERIFIED (50).
    sent_rx = next(rx for rx in records[P1].prescriptions if rx.id.endswith("000000000001"))
    assert sent_rx.prefix == "ESCRIPT"
    assert sent_rx.status_label == "DISPENSED"
    assert [t.kind for t in sent_rx.transactions] == ["Sent", "Verified", "Dispensed"]
    printed_rx = records[P2].prescriptions[0]
    assert printed_rx.prefix == "SCRIPT"  # "Prescription printed" → SCRIPT (gpdfs:333)
    assert printed_rx.status_label == "PRINTED"
    assert printed_rx.refills is None  # -1 sentinel


def test_escript_refill_does_not_override_verified(records: dict[str, PatientRecord]) -> None:
    # The §5 rule (gpdfs:355-361): Order sent + Refill request approved with no
    # dispense resolves to VERIFIED — refills (priority 10) never beat the
    # baseline VERIFIED (priority 50).
    rx = next(rx for rx in records[P1].prescriptions if rx.id.endswith("000000000003"))
    descriptions = {t.description for t in rx.transactions}
    assert descriptions == {"Order sent", "Refill request approved"}
    assert rx.status_label == "VERIFIED"


def test_escript_display_date_uses_order_sent_eastern(records: dict[str, PatientRecord]) -> None:
    # ESCRIPT display date = the Order-sent transaction datetime converted to
    # practice-local Eastern (gpdfs:408 resolve_script_display_date). Order sent
    # at 5/10/2023 9:40 PM UTC → 5:40 PM US/Eastern (EDT, UTC-4), same day.
    sent_rx = next(rx for rx in records[P1].prescriptions if rx.id.endswith("000000000001"))
    assert sent_rx.display_date is not None
    assert sent_rx.display_date.strftime("%Y-%m-%d %H:%M") == "2023-05-10 17:40"
    # SCRIPT (paper) falls back to the prescription DoS, not tz-converted.
    printed_rx = records[P2].prescriptions[0]
    assert printed_rx.display_date is not None
    assert printed_rx.display_date.date() == date(2023, 9, 15)


def test_plan_type_superbill_join_with_regex_fallback(
    records: dict[str, PatientRecord],
) -> None:
    # The predecessor's three-tier superbill PlanType join (gpdfs:266-279):
    # PIPG tier-1, plan-name tier-2, payer tier-3, then the "(PPO)" regex as the
    # heuristic of last resort.
    ada_coverages = {c.plan_name: c for c in records[P1].coverages}
    # Cascadia has NO superbill row → regex last-resort on "(PPO)".
    ppo = ada_coverages["Cascadia Choice (PPO)"]
    assert ppo.plan_type == "PPO"
    assert ppo.order_of_benefits == 0
    assert ppo.priority_label == "PRIMARY PAYER"
    # Evergreen Basic resolves via the superbill PLAN-NAME tier (no "(PPO)" in
    # the name) — proving the join beats the name-regex heuristic.
    basic = ada_coverages["Evergreen Basic"]
    assert basic.plan_type == "HMO"
    assert basic.order_of_benefits == 1
    # Medicare resolves via the superbill PIPG tier — TYPE is never "Medical"
    # (the predecessor's insurance QA fails on "Medical").
    medicare = records[P2].coverages[0]
    assert medicare.plan_type == "Medicare"
    assert medicare.coverage_type == "Medical"
    assert records[P3].coverages == []  # self-pay


def test_family_history_immunizations_directives(
    records: dict[str, PatientRecord],
) -> None:
    family = records[P1].family_history[0]
    assert (family.relation, family.diagnosis) == ("Mother", "Type 2 diabetes mellitus")
    cleo = records[P3]
    flu = next(i for i in cleo.immunizations if "Influenza" in (i.vaccine or ""))
    assert flu.administered_on == date(2022, 10, 3)
    assert flu.lot_number == "FLU2023A"
    dnr = records[P2].advance_directives[0]
    assert dnr.directive is not None and dnr.directive.startswith("Do not resuscitate")


def test_guarantor_and_shared_actors(records: dict[str, PatientRecord]) -> None:
    cleo = records[P3]
    guarantor = cleo.patient.guarantor
    assert guarantor is not None
    assert guarantor.name == "Gus Placeholder"
    # The Billing* / bare City-State-Zip columns are this table's real names
    # (gpdfs:940-961) — regression for the invented Address*/RelationshipTo
    # names that silently mapped nothing.
    assert guarantor.relationship_to_patient == "Parent"
    assert guarantor.birth_date == date(1988, 3, 15)
    assert guarantor.sex == "Male"
    assert guarantor.ssn is None  # empty SSNumber cell stays None, never ""
    assert guarantor.payment_preference is None  # empty BillingPaymentType
    assert guarantor.address is not None and guarantor.address.city == "Springfield"
    assert [(p.kind.value, p.value) for p in guarantor.phones] == [("phone_home", "(206) 555-0163")]
    # Columns the mapping doesn't consume land in extensions (losslessness).
    assert guarantor.extensions == {"pf_tebra:MiddleName": "Q"}
    record = records[P1]
    encounter = next(e for e in record.encounters if e.id == E1)
    provider = record.practitioner(encounter.provider_id)
    assert provider is not None and provider.name == "Paige Providerson"
    facility = record.facility(encounter.facility_id)
    assert facility is not None and facility.name == "Example Family Medicine"
    assert facility.phone == "(206) 555-0199"


def test_provenance_traces_to_source(records: dict[str, PatientRecord]) -> None:
    ada = records[P1]
    assert ada.patient.provenance is not None
    assert ada.patient.provenance.source_file == "patient-demographics.tsv"
    assert ada.patient.provenance.source_id == P1
    assert ada.encounters[0].provenance is not None
    assert ada.encounters[0].provenance.source_file == "patient-encounters.tsv"
