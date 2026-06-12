"""Tests for core.codes — vital LOINC map, pain answers, BMI math."""

from anastomosis.core.codes import PAIN_SEVERITY_LA, VITALS, bmi_imperial, bmi_metric


def test_vitals_map_core_codes() -> None:
    assert VITALS["systolic_bp"].loinc == "8480-6"
    assert VITALS["diastolic_bp"].loinc == "8462-4"
    assert VITALS["heart_rate"].loinc == "8867-4"
    assert VITALS["bmi"].loinc == "39156-5"
    assert VITALS["pain_severity"].loinc == "72514-3"


def test_vitals_map_is_internally_consistent() -> None:
    loincs = [v.loinc for v in VITALS.values()]
    assert len(loincs) == len(set(loincs)), "duplicate LOINC in vital map"
    assert all(v.display and v.unit for v in VITALS.values())


def test_pain_answer_list_covers_full_scale() -> None:
    assert sorted(PAIN_SEVERITY_LA) == list(range(11))
    codes = list(PAIN_SEVERITY_LA.values())
    assert len(codes) == len(set(codes))
    assert all(code.startswith("LA") for code in codes)


def test_bmi_imperial_cdc_formula() -> None:
    # 703 * lb / in^2 — the auto-calc used when a source charts height+weight
    # but omits BMI.
    assert bmi_imperial(150, 65) == 25.0
    assert bmi_imperial(203, 69) == 30.0


def test_bmi_metric() -> None:
    assert bmi_metric(70, 175) == 22.9


def test_bmi_not_computable_returns_none() -> None:
    assert bmi_imperial(None, 65) is None
    assert bmi_imperial(150, None) is None
    assert bmi_imperial(150, 0) is None
    assert bmi_imperial(-150, 65) is None
    assert bmi_metric(0, 175) is None
