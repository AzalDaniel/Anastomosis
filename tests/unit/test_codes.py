"""Tests for core.codes — vital LOINC map, pain answers, BMI math.

These assert the DUAL-MAP truth ported from the predecessor (generate_pdfs.py
§8): the predecessor's LOINC is PRIMARY, the modern C-CDA/Synthea sibling is an
accepted alias.
"""

from anastomosis.core.codes import (
    PAIN_LA_TO_LEVEL,
    PAIN_SEVERITY_LA,
    VITALS,
    bmi_imperial,
    bmi_metric,
    pain_display,
)


def test_vitals_map_core_codes() -> None:
    assert VITALS["systolic_bp"].loinc == "8480-6"
    assert VITALS["diastolic_bp"].loinc == "8462-4"
    assert VITALS["heart_rate"].loinc == "8867-4"
    assert VITALS["bmi"].loinc == "39156-5"
    assert VITALS["pain_severity"].loinc == "72514-3"


def test_predecessor_primary_loincs_and_modern_aliases() -> None:
    # Predecessor charted these codes (gpdfs:542,550,552); the modern siblings
    # ride along as aliases (old code first).
    assert VITALS["weight"].loinc == "3141-9"
    assert "29463-7" in VITALS["weight"].aliases
    assert VITALS["oxygen_saturation"].loinc == "2708-6"
    assert "59408-5" in VITALS["oxygen_saturation"].aliases
    assert VITALS["head_circumference"].loinc == "8287-5"
    assert "9843-4" in VITALS["head_circumference"].aliases
    # BMI Percentile (gpdfs:544) — was absent before this port.
    assert VITALS["bmi_percentile"].loinc == "59576-9"


def test_vitals_map_is_internally_consistent() -> None:
    all_codes = [code for v in VITALS.values() for code in (v.loinc, *v.aliases)]
    assert len(all_codes) == len(set(all_codes)), "duplicate LOINC across vitals"
    assert all(v.display and v.unit for v in VITALS.values())


def test_pain_answer_list_covers_full_scale_with_old_codes() -> None:
    assert sorted(PAIN_SEVERITY_LA) == list(range(11))
    # The predecessor's LA61xx run is primary for the whole 0-10 scale.
    assert PAIN_SEVERITY_LA[5] == "LA6116-3"
    assert PAIN_SEVERITY_LA[10] == "LA6121-3"
    codes = list(PAIN_SEVERITY_LA.values())
    assert len(codes) == len(set(codes))
    assert all(code.startswith("LA") for code in codes)


def test_pain_display_old_codes_primary() -> None:
    # Predecessor _PAIN_LA_MAP (gpdfs:515-518) — the 5-10 codes that previously
    # leaked raw to the chart.
    assert pain_display("LA6116-3") == "5"
    assert pain_display("LA6121-3") == "10"
    # LOINC: prefix stripped (gpdfs:526).
    assert pain_display("LOINC:LA6118-9") == "7"
    # Modern 5-10 answer list accepted as a fallback alias.
    assert pain_display("LA10137-0") == "5"
    assert pain_display("LA13942-0") == "10"
    # Raw numeric passes through; unknown values fall back to raw (lossless).
    assert pain_display("3") == "3"
    assert pain_display("not-a-code") == "not-a-code"
    assert pain_display(None) is None
    assert pain_display("") is None


def test_pain_inverse_map_includes_old_and_alias() -> None:
    assert PAIN_LA_TO_LEVEL["LA6116-3"] == 5  # old primary
    assert PAIN_LA_TO_LEVEL["LA10137-0"] == 5  # modern alias


def test_bmi_imperial_cdc_formula_two_decimals() -> None:
    # 703 * lb / in^2 — the auto-calc used when a source charts height+weight
    # but omits BMI. 2dp matches the predecessor (gpdfs:543,592).
    assert bmi_imperial(150, 65) == 24.96
    assert bmi_imperial(203, 69) == 29.97


def test_bmi_metric_two_decimals() -> None:
    assert bmi_metric(70, 175) == 22.86


def test_bmi_not_computable_returns_none() -> None:
    assert bmi_imperial(None, 65) is None
    assert bmi_imperial(150, None) is None
    assert bmi_imperial(150, 0) is None
    assert bmi_imperial(-150, 65) is None
    assert bmi_metric(0, 175) is None
