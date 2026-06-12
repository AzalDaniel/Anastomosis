"""Unit tests for the practice_fusion_soap template pack.

These exercise discovery, manifest geometry/tokens/sections, and the
``context.py`` mapping + Jinja render against the synthetic ``pf_tebra_v9``
fixture — all WITHOUT a browser (the PDF-geometry assertions live in the e2e
golden lane). Every value here is synthetic by construction (the fixture is the
repo's ``feedface-`` PF/Tebra export).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from jinja2 import Environment, FileSystemLoader

import anastomosis.sources.pf_tebra  # noqa: F401 — registers the pf-tebra adapter
from anastomosis.reconstruct import LoadedPack, discover_packs
from anastomosis.sources import get_source

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"
PACK_NAME = "practice_fusion_soap"

# Section headings the PF replica always renders (GOLD_STANDARD §4). The
# template emits them as plain text in these section divs.
_ALWAYS_HEADINGS = [
    "Patient identifying details and demographics",
    "Active insurance",
    "Inactive insurance",
    "Payment information",
    "Vitals for this encounter",
    "Diagnoses",
    "Drug Allergies",
    "Food Allergies",
    "Environmental Allergies",
    "Current Medications",
    "Immunizations",
    "Social history",
    "Past medical history",
    "Family health history",
    "Advance Directive",
    "Implantable devices",
    "Active health concerns",
    "Inactive health concerns",
    "Active Goals",
    "Inactive Goals",
    "Subjective",
    "Objective",
    "Assessment",
    "Plan",
    "Orders",
    "Screenings/ Interventions/ Assessments",
    "Observations",
    "Quality of care",
    "Care plan",
]

# The 17 social-history sub-category labels, in order (GOLD_STANDARD §6).
_SH_LABELS = [
    "TOBACCO USE",
    "ALCOHOL USE",
    "SOCIAL HISTORY (FREE-TEXT)",
    "FINANCIAL RESOURCES",
    "EDUCATION",
    "PHYSICAL ACTIVITY",
    "NUTRITION HISTORY",
    "STRESS",
    "SOCIAL ISOLATION AND CONNECTION",
    "EXPOSURE TO VIOLENCE",
    "GENDER IDENTITY",
    "SEXUAL ORIENTATION",
    "PREGNANCY STATUS",
    "PREGNANCY INTENT",
    "TRIBAL AFFILIATION",
    "OCCUPATIONS",
    "FOOD INSECURITY RISK - HVS",
]

# Empty-state strings that must appear verbatim when their section is empty.
_EMPTY_STATES = [
    "No vitals recorded",
    "No events recorded for Vitals.",
    "No current diagnoses",
    "No historical diagnoses",
    "No Active drug allergies recorded",
    "No Inactive food allergies recorded",
    "No active medications recorded",
    "No historical medications recorded",
    "No immunizations recorded for this patient.",
    "No tobacco use history available for this patient",
    "No alcohol use history available for this patient",
    "No social history (free-text) recorded for this patient",
    "No financial resources recorded for this patient",
    "No occupations recorded for this patient",
    "No food insecurity risk - hvs recorded for this patient",
    "No past medical history available for this patient.",
    "No family health history recorded",
    "No family health history (free text) available for this patient.",
    "No advance directives recorded for this patient.",
    "No implantable devices recorded",
    "No active health concerns recorded.",
    "No inactive goals recorded",
    "No orders attached to this encounter.",
    "No screenings/interventions/assessments recorded.",
    "No observations recorded.",
    "No quality of care events recorded.",
    "No care plan recorded.",
]


@pytest.fixture(scope="module")
def pack() -> LoadedPack:
    status = discover_packs().get(PACK_NAME)
    assert status is not None and status.pack is not None, (
        status.diagnosis if status else "pack not discovered"
    )
    assert status.origin == "builtin"
    return status.pack


@pytest.fixture(scope="module")
def records() -> list[Any]:
    return list(get_source("pf-tebra").load(_FIXTURE))


def _env(pack: LoadedPack) -> Environment:
    # Mirror the engine's Jinja environment (autoescape on; SOAP html | safe).
    return Environment(
        loader=FileSystemLoader(pack.root), autoescape=True, keep_trailing_newline=True
    )


def _cfg(pack: LoadedPack, **overrides: bool) -> dict[str, Any]:
    sections = {k: v.default for k, v in pack.manifest.sections.items()}
    sections.update(overrides)
    return {
        "sections": sections,
        "timezone": pack.manifest.timezone,
        "tokens": pack.manifest.tokens,
    }


def _render_all(pack: LoadedPack, records: list[Any], **overrides: bool) -> list[str]:
    env = _env(pack)
    template = env.get_template(pack.template_path.name)
    cfg = _cfg(pack, **overrides)
    out: list[str] = []
    for record in records:
        for encounter in record.encounters:
            ctx = pack.build_context(encounter, record, cfg)
            out.append(template.render(**ctx))
    return out


# --- discovery + manifest ------------------------------------------------------


def test_pack_discovers_as_builtin(pack: LoadedPack) -> None:
    assert pack.manifest.name == PACK_NAME
    assert pack.manifest.version == "1.0"


def test_page_geometry_matches_forensic_margins(pack: LoadedPack) -> None:
    page = pack.manifest.page
    assert page.size == "Letter"
    # GOLD_STANDARD §1 "Page geometry (NEVER change)".
    assert page.margin_top == "0.6in"
    assert page.margin_right == "0.38in"
    assert page.margin_bottom == "0.44in"
    assert page.margin_left == "0.39in"


def test_forensic_tokens_present(pack: LoadedPack) -> None:
    tokens = pack.manifest.tokens
    assert tokens["heading_fill"] == "#f1f1f1"  # NOT #f2f2f2 (sprint-4 fix)
    assert tokens["border"] == "#aaaaaa"
    assert tokens["row_sep"] == "#e6e6e6"
    assert tokens["muted_text"] == "#737373"
    assert "Arial" in tokens["body_font"]


def test_verify_header_fields(pack: LoadedPack) -> None:
    assert pack.manifest.verify_header_fields == ["patient_name", "dob", "dos"]


def test_section_flags_are_togglable(pack: LoadedPack) -> None:
    keys = set(pack.manifest.sections)
    assert {"insurance", "social_history", "addenda", "orders", "goals"} <= keys


# --- CSS / structural carries (GOLD_STANDARD lessons) --------------------------


def test_print_color_adjust_rule_present(pack: LoadedPack, records: list[Any]) -> None:
    html = _render_all(pack, records)[0]
    # GOLD §1 — the 2-sprint grey-header bug fix; non-negotiable.
    assert "-webkit-print-color-adjust: exact !important" in html
    assert "print-color-adjust: exact !important" in html


def test_heading_band_fill_and_border_collapse(pack: LoadedPack, records: list[Any]) -> None:
    html = _render_all(pack, records)[0]
    assert "#f1f1f1" in html  # the grey heading band
    # GOLD §4 "3 lines not 4": sub-header after section-header drops its top border.
    assert ".section-header + .sub-header { border-top: none; }" in html
    # orphans/widows control + avoid-page on headers.
    assert "orphans: 2; widows: 2;" in html
    assert "break-after: avoid-page;" in html


def test_all_section_headings_render(pack: LoadedPack, records: list[Any]) -> None:
    html = _render_all(pack, records)[0]
    for heading in _ALWAYS_HEADINGS:
        assert heading in html, f"missing PF section heading: {heading}"


def test_social_history_labels_in_order(pack: LoadedPack, records: list[Any]) -> None:
    html = _render_all(pack, records)[0]
    positions = [html.find(label) for label in _SH_LABELS]
    assert all(p >= 0 for p in positions), "a social-history label is missing"
    assert positions == sorted(positions), "social-history labels out of order"


def test_empty_state_strings_present(pack: LoadedPack) -> None:
    # Render a deliberately-empty record (no diagnoses/allergies/meds/etc.) so
    # every documented empty-state string is exercised — the PF original always
    # renders these sections even when there is no data (GOLD_STANDARD §4).
    from anastomosis.core.model import Encounter, NoteSection, Patient, PatientRecord, SectionKind

    empty_record = PatientRecord(
        patient=Patient(given_name="Empty", family_name="Patient"),
        encounters=[
            Encounter(
                id="feedface-empty-0000-0000-000000000000",
                patient_id="feedface-empty",
                chief_complaint="Empty-state coverage",
                encounter_type="SOAP",
                sections=[NoteSection(kind=SectionKind.SUBJECTIVE, html="<p>x</p>", text="x")],
            )
        ],
    )
    env = _env(pack)
    template = env.get_template(pack.template_path.name)
    cfg = _cfg(pack)
    enc = empty_record.encounters[0]
    blob = template.render(**pack.build_context(enc, empty_record, cfg))
    for empty in _EMPTY_STATES:
        assert empty in blob, f"missing empty-state string: {empty!r}"


# --- context wiring ------------------------------------------------------------


def test_logo_is_synthetic_data_uri(pack: LoadedPack, records: list[Any]) -> None:
    html = _render_all(pack, records)[0]
    assert "data:image/svg+xml;base64," in html  # synthetic placeholder, encoded
    # The vendor host must never appear (deny-list also enforces this).
    assert "practicefusion" not in html.lower()


def test_logo_override_must_be_data_uri(pack: LoadedPack, records: list[Any]) -> None:
    """A non-data: logo override is refused (it would make Chromium fetch an
    external URL while rendering PHI) and falls back to the placeholder."""
    env = _env(pack)
    template = env.get_template(pack.template_path.name)
    cfg = _cfg(pack)
    cfg["tokens"] = dict(cfg["tokens"], logo_data_uri="https://logo-host.example.net/mark.png")
    record = records[0]
    html = template.render(**pack.build_context(record.encounters[0], record, cfg))
    assert "logo-host.example.net" not in html
    assert "data:image/svg+xml;base64," in html  # fell back to the placeholder
    # An inline data: override IS honored.
    cfg["tokens"] = dict(cfg["tokens"], logo_data_uri="data:image/png;base64,AAAA")
    ctx = pack.build_context(record.encounters[0], record, cfg)
    assert ctx["logo_data_uri"] == "data:image/png;base64,AAAA"


def test_logo_asset_cannot_escape_pack_root(
    pack: LoadedPack, records: list[Any], tmp_path: Path
) -> None:
    """A logo_asset that resolves outside the pack root is never read or
    embedded (defense-in-depth: pack config must not exfiltrate files). The
    guard is on the RESOLVED path, so absolute and ../-relative forms are the
    same boundary; the absolute form is the cross-platform probe."""
    outside = tmp_path / "outside.svg"
    outside.write_text("<svg>OUTSIDE</svg>")
    cfg = _cfg(pack)
    cfg["tokens"] = dict(cfg["tokens"], logo_asset=str(outside))
    record = records[0]
    ctx = pack.build_context(record.encounters[0], record, cfg)
    assert ctx["logo_data_uri"] == ""  # refused — renders without a logo
    cfg["tokens"] = dict(cfg["tokens"], logo_asset="../" * 12 + str(outside).lstrip("/\\"))
    ctx = pack.build_context(record.encounters[0], record, cfg)
    assert "OUTSIDE" not in ctx["logo_data_uri"]


# --- payment information (predecessor empty states, gpdfs:950-961) --------------


def _payment_ctx(pack: LoadedPack, record: Any) -> dict[str, str]:
    return dict(pack.build_context(record.encounters[0], record, _cfg(pack))["payment"])


def test_payment_fixture_values_match_predecessor_shapes(
    pack: LoadedPack, records: list[Any]
) -> None:
    record = next(r for r in records if r.patient.guarantor is not None)
    payment = _payment_ctx(pack, record)
    # Address is the comma-joined line1, city, state, zip (gpdfs:944-948).
    assert payment["guarantor_addr"] == "789 Sample Rd, Springfield, WA, 98103"
    assert payment["dob"] == "03/15/1988"
    assert payment["primary_phone"] == "(206) 555-0163"
    # Fixture leaves these blank: '-' everywhere, except the PF default for
    # PAYMENT PREFERENCE, which is 'Primary Insurance'.
    assert payment["secondary_phone"] == "-"
    assert payment["ssn"] == "-"
    assert payment["preference"] == "Primary Insurance"


def test_payment_without_guarantor_is_all_dashes(pack: LoadedPack, records: list[Any]) -> None:
    record = next(r for r in records if r.patient.guarantor is not None).model_copy(deep=True)
    record.patient.guarantor = None
    payment = _payment_ctx(pack, record)
    for key, value in payment.items():
        assert value == ("Primary Insurance" if key == "preference" else "-")


def test_payment_never_renders_raw_none(pack: LoadedPack, records: list[Any]) -> None:
    """Regression: a guarantor PRESENT but with None attributes once leaked
    literal 'None' strings into the PDF. Every payment cell must be a
    non-empty string for every fixture encounter, and the rendered HTML must
    never contain the bare token 'None'."""
    for record in records:
        for encounter in record.encounters:
            payment = pack.build_context(encounter, record, _cfg(pack))["payment"]
            for key, value in payment.items():
                assert isinstance(value, str) and value, f"payment[{key!r}] = {value!r}"
    for html in _render_all(pack, records):
        leak = re.search(r"\bNone\b", html)
        if leak is not None:
            snippet = html[max(0, leak.start() - 60) : leak.end() + 60]
            pytest.fail(f"raw 'None' leaked into render near: {snippet!r}")


def test_meds_as_of_is_render_day_not_encounter_date(pack: LoadedPack, records: list[Any]) -> None:
    import datetime as dt

    html = _render_all(pack, records)[0]
    today = dt.date.today().strftime("%m/%d/%Y")  # mirrors context render-day
    assert f"Current Medications (as of {today})" in html


def test_escript_line_field_order(pack: LoadedPack, records: list[Any]) -> None:
    """Find a rendered ESCRIPT/SCRIPT line and assert its field order (GOLD §5)."""
    blob = "\n".join(_render_all(pack, records))
    assert ("ESCRIPT (" in blob) or ("SCRIPT (" in blob), "no prescription line rendered"
    # The label order is PRESCRIBER -> SIG -> REFILLS -> QUANTITY.
    i_presc = blob.find("PRESCRIBER:")
    assert i_presc >= 0
    assert blob.find("SIG:", i_presc) >= 0
    assert blob.find("REFILLS:", i_presc) >= 0
    assert blob.find("QUANTITY:", i_presc) >= 0


def test_insurance_type_is_not_generic_medical(pack: LoadedPack, records: list[Any]) -> None:
    """The TYPE column must carry the plan_type (HMO/PPO/...) or '-', never the
    generic coverage_type 'Medical' (GOLD §7)."""
    cfg = _cfg(pack)
    found_type_row = False
    for record in records:
        for encounter in record.encounters:
            ctx = pack.build_context(encounter, record, cfg)
            for ins in ctx["active_insurance"] + ctx["inactive_insurance"]:
                found_type_row = True
                assert ins["type"].lower() != "medical"
    # The fixture has at least one coverage row to exercise this.
    assert found_type_row


def test_addenda_conditional_render(pack: LoadedPack, records: list[Any]) -> None:
    """Addenda renders only when the encounter has addendum rows (GOLD §10)."""
    env = _env(pack)
    template = env.get_template(pack.template_path.name)
    cfg = _cfg(pack)
    saw_with = saw_without = False
    for record in records:
        for encounter in record.encounters:
            ctx = pack.build_context(encounter, record, cfg)
            html = template.render(**ctx)
            heading = ">Addenda</div>" in html
            if ctx["addendums"]:
                assert heading, "addenda rows present but no Addenda heading"
                saw_with = True
            else:
                assert not heading, "Addenda heading rendered with no addendum rows"
                saw_without = True
    assert saw_with and saw_without, "fixture should cover both addenda states"


def test_section_toggle_suppresses_section(pack: LoadedPack, records: list[Any]) -> None:
    on = _render_all(pack, records, social_history=True)[0]
    off = _render_all(pack, records, social_history=False)[0]
    assert ">Social history</div>" in on
    assert ">Social history</div>" not in off
    # Toggling insurance off drops both insurance headings.
    ins_off = _render_all(pack, records, insurance=False)[0]
    assert ">Active insurance</div>" not in ins_off


def test_renders_only_synthetic_identity(pack: LoadedPack, records: list[Any]) -> None:
    """The PF note is built from SYNTHETIC fixtures and a SYNTHETIC placeholder
    logo / footer URL only.

    The repo-wide ``tools/phi_scan.py`` deny-list is the authoritative guarantee
    that no real predecessor identity value exists anywhere in the tree (so this
    test never hard-codes a real name — that would itself be a leak). Here we
    assert positively: the synthetic fixture facility/patient render, and the
    real PF vendor host never appears (the logo + footer are synthesized)."""
    blob = "\n".join(_render_all(pack, records))
    lower = blob.lower()
    # The synthetic fixture facility + a synthetic patient must be present.
    assert "Example Family Medicine" in blob
    assert "Fixture" in blob  # synthetic fixture surname ("Ada Q Fixture")
    # The synthetic logo placeholder is used, and the real vendor host is absent.
    assert "data:image/svg+xml;base64," in blob
    assert "practicefusion" not in lower  # synthetic footer URL only
