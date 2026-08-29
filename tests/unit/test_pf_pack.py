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


def test_record_view_index_splits_match_naive_filtering(records: list[Any]) -> None:
    """The per-call RecordViewIndex must group each collection exactly as the
    old inline comprehensions did (order-preserving active/inactive splits,
    complete by-id maps), so build_context output is unchanged."""
    from anastomosis.packs.practice_fusion_soap.context import _RecordViewIndex

    for record in records:
        idx = _RecordViewIndex.build(record)
        assert idx.active_coverages == sorted(
            (c for c in record.coverages if c.active),
            key=lambda c: c.order_of_benefits if c.order_of_benefits is not None else 99,
        )
        assert idx.active_conditions == [c for c in record.conditions if c.active]
        assert idx.historical_conditions == [c for c in record.conditions if not c.active]
        assert idx.conditions_by_id == {c.id: c for c in record.conditions}
        assert idx.active_medications == [m for m in record.medications if m.active]
        assert idx.prescriptions_by_id == {p.id: p for p in record.prescriptions}
        assert idx.active_concerns == [c for c in record.health_concerns if c.active]
        assert idx.inactive_concerns == [c for c in record.health_concerns if not c.active]
        for category, items in idx.allergies_by_category.items():
            assert items == [a for a in record.allergies if a.category == category]


def test_record_view_index_inactive_and_duplicate_branches() -> None:
    """Exercise the splits the fixture leaves empty (inactive coverages,
    active/inactive goals) and duplicate-id last-wins — branches a
    naive-filtering bug (e.g. a swapped active/inactive) would otherwise pass."""
    from anastomosis.core.model import (
        Condition,
        Coverage,
        Goal,
        Patient,
        PatientRecord,
    )
    from anastomosis.packs.practice_fusion_soap.context import _RecordViewIndex

    pid = "feedface-0000-0000-0000-0000000000aa"
    record = PatientRecord(
        patient=Patient(id=pid),
        coverages=[
            Coverage(patient_id=pid, active=True, order_of_benefits=2),
            Coverage(patient_id=pid, active=False, order_of_benefits=1),
            Coverage(patient_id=pid, active=True, order_of_benefits=1),
        ],
        conditions=[
            Condition(patient_id=pid, id="c1", active=True),
            Condition(patient_id=pid, id="c1", active=False),  # duplicate id
        ],
        goals=[
            Goal(patient_id=pid, active=True),
            Goal(patient_id=pid, active=False),
        ],
        health_concerns=[
            Goal(patient_id=pid, description="concern-active", active=True),
            Goal(patient_id=pid, description="concern-inactive", active=False),
        ],
    )
    idx = _RecordViewIndex.build(record)
    # active coverages sorted by benefit order (tie keeps source order); inactive split out.
    assert [c.order_of_benefits for c in idx.active_coverages] == [1, 2]
    assert [c.order_of_benefits for c in idx.inactive_coverages] == [1]
    # active/inactive goals are not swapped.
    assert [g.active for g in idx.active_goals] == [True]
    assert [g.active for g in idx.inactive_goals] == [False]
    # nor are health concerns, which share the Goal shape and split the same way.
    assert [c.description for c in idx.active_concerns] == ["concern-active"]
    assert [c.description for c in idx.inactive_concerns] == ["concern-inactive"]
    # duplicate condition id: last wins, matching {c.id: c for c in conditions}.
    assert idx.conditions_by_id["c1"].active is False


def test_flowsheet_index_cached_once_and_cutoff_applied_per_encounter(
    records: list[Any],
) -> None:
    """The vital-by-encounter scan is built ONCE per record (memoized in
    record_cache) and the strictly-prior-DOS cutoff is still applied per
    encounter — so the earliest encounter sees no prior columns while a later one
    reuses the SAME cached index. (The rendered flowsheet's byte-identity is
    pinned by the e2e practice_fusion_soap golden.)"""
    from anastomosis.packs.practice_fusion_soap.context import _build_flowsheet

    record = next(r for r in records if len([e for e in r.encounters if e.date_of_service]) >= 2)
    dated = sorted(
        (e for e in record.encounters if e.date_of_service is not None),
        key=lambda e: e.date_of_service,
    )
    cache: dict[str, Any] = {}

    # The latest encounter sees the most strictly-prior columns; this populates
    # the per-record cache.
    _build_flowsheet(record, dated[-1].date_of_service, cache)
    assert "flowsheet_index" in cache, "the per-record vital scan must be memoized"
    first_index = cache["flowsheet_index"]

    # The earliest encounter has NO strictly-prior encounter → empty flowsheet,
    # and it must REUSE the cached index (built once, not rescanned per encounter).
    cols_early, rows_early = _build_flowsheet(record, dated[0].date_of_service, cache)
    assert cache["flowsheet_index"] is first_index, "the index must be reused, not rebuilt"
    assert cols_early == [] and rows_early == []  # the per-encounter cutoff still applies


def test_section_flags_default_on_and_honor_overrides() -> None:
    """The extracted section-flag builder defaults every section ON and honors an
    explicit False (the levers the dashboard/migrate wizards toggle)."""
    from anastomosis.packs.practice_fusion_soap.context import _section_flags

    all_on = _section_flags({})
    assert all(v is True for v in all_on.values())
    # The complete flag set (so a dropped middle flag fails here, not only in the
    # e2e golden) — every section the template gates on.
    assert set(all_on) == {
        "show_insurance",
        "show_payment",
        "show_vitals",
        "show_vitals_flowsheet",
        "show_immunizations",
        "show_social_history",
        "show_past_medical_history",
        "show_family_history",
        "show_advance_directives",
        "show_devices",
        "show_health_concerns",
        "show_goals",
        "show_orders",
        "show_addenda",
    }
    off = _section_flags({"insurance": False, "addenda": False})
    assert off["show_insurance"] is False and off["show_addenda"] is False
    assert off["show_vitals"] is True  # an unspecified section stays on


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


def _one_encounter_record(patient: Any = None, **collections: Any) -> Any:
    """A minimal renderable record: one SOAP encounter and whatever collection
    the caller is exercising."""
    from anastomosis.core.model import Encounter, NoteSection, Patient, PatientRecord, SectionKind

    return PatientRecord(
        patient=patient or Patient(given_name="Section", family_name="Coverage"),
        encounters=[
            Encounter(
                id="feedface-sect-0000-0000-000000000000",
                patient_id="feedface-sect",
                chief_complaint="Section coverage",
                encounter_type="SOAP",
                sections=[NoteSection(kind=SectionKind.SUBJECTIVE, html="<p>x</p>", text="x")],
            )
        ],
        **collections,
    )


def test_header_reads_the_columns_v9_actually_spells(pack: LoadedPack) -> None:
    """Two demographics readers were spelled against columns no v9 table has.

    ``DateOfDeath`` is ``DeathDate`` on patient-demographics, so the DATE OF
    DEATH cell printed "-" over an export that carried the date. PRN read
    ``PatientContactCode`` — the one column in the 85-table dictionary that
    carries a patient's record number — but fell back to a bare ``PRN``, a name
    nothing in v9 or in this codebase ever writes. The fallback is gone: a chain
    over invented names is how a wrong guess hides (#248).
    """
    from anastomosis.core.model import Patient

    env = _env(pack)
    template = env.get_template(pack.template_path.name)
    cfg = _cfg(pack)

    real = _one_encounter_record(
        patient=Patient(
            given_name="Deceased",
            family_name="Fixture",
            extensions={
                "pf_tebra:DeathDate": "04/09/2024",
                "pf_tebra:PatientContactCode": "PRN-4242",
            },
        )
    )
    html = template.render(**pack.build_context(real.encounters[0], real, cfg))
    assert "04/09/2024" in html
    assert "PRN-4242" in html

    invented = _one_encounter_record(
        patient=Patient(
            given_name="Invented",
            family_name="Spelling",
            extensions={"pf_tebra:DateOfDeath": "04/09/2024", "pf_tebra:PRN": "PRN-4242"},
        )
    )
    html = template.render(**pack.build_context(invented.encounters[0], invented, cfg))
    assert "04/09/2024" not in html
    assert "PRN-4242" not in html


def test_screenings_render_per_encounter_and_keep_their_negation(pack: LoadedPack) -> None:
    """The Screenings/Interventions/Assessments section was starved by a
    hard-coded empty list, so it printed "No screenings/interventions/assessments
    recorded." over every export that had them.

    Two things are asserted beyond the row appearing at all. The section is
    encounter-scoped, so an event belonging to another visit must not leak into
    this one. And an event the clinician marked as not performed must not read
    as one that was — that is the row stating the opposite of the export.
    """
    from anastomosis.core.model import (
        Encounter,
        NoteSection,
        Patient,
        PatientRecord,
        ScreeningEvent,
        SectionKind,
    )

    env = _env(pack)
    template = env.get_template(pack.template_path.name)
    cfg = _cfg(pack)
    pid = "feedface-0000-0000-0000-0000000000ee"
    this_visit = "feedface-ev00-0000-0000-000000000001"
    other_visit = "feedface-ev00-0000-0000-000000000002"

    def encounter(eid: str) -> Any:
        return Encounter(
            id=eid,
            patient_id=pid,
            chief_complaint="Screening coverage",
            encounter_type="SOAP",
            sections=[NoteSection(kind=SectionKind.SUBJECTIVE, html="<p>x</p>", text="x")],
        )

    record = PatientRecord(
        patient=Patient(id=pid, given_name="Screening", family_name="Coverage"),
        encounters=[encounter(this_visit), encounter(other_visit)],
        screening_events=[
            ScreeningEvent(
                patient_id=pid,
                encounter_id=this_visit,
                name="PHQ-2",
                result="2",
                comments="Follow up next visit",
            ),
            ScreeningEvent(
                patient_id=pid,
                encounter_id=this_visit,
                name="Tobacco cessation counseling",
                negated=True,
            ),
            ScreeningEvent(patient_id=pid, encounter_id=other_visit, name="Fall risk assessment"),
        ],
    )
    html = template.render(**pack.build_context(record.encounters[0], record, cfg))
    assert "PHQ-2: 2 - Follow up next visit" in html
    assert "No screenings/interventions/assessments recorded." not in html
    # The other visit's event stays on the other visit.
    assert "Fall risk assessment" not in html
    # And the negated one cannot be read as having happened.
    assert "Not performed — Tobacco cessation counseling" in html

    # The second encounter sees its own event and neither of the first's.
    html = template.render(**pack.build_context(record.encounters[1], record, cfg))
    assert "Fall risk assessment" in html
    assert "PHQ-2" not in html

    # A record with no events at all still owes the reader the empty state.
    bare = _one_encounter_record()
    html = template.render(**pack.build_context(bare.encounters[0], bare, cfg))
    assert "No screenings/interventions/assessments recorded." in html


def test_health_concerns_render_instead_of_being_denied(pack: LoadedPack) -> None:
    """A chart over a record that HAS health concerns must show them and stop
    printing the empty state — the section used to be two hard-coded no-record
    rows with no variable behind them, so an export carrying concerns rendered
    "No active health concerns recorded." on top of them.

    The two sections are asserted independently: an active-only record must keep
    the inactive empty state, because a section with genuinely nothing in it
    still owes the chart reader that string.
    """
    from datetime import date

    from anastomosis.core.model import Goal

    env = _env(pack)
    template = env.get_template(pack.template_path.name)
    cfg = _cfg(pack)
    pid = "feedface-0000-0000-0000-0000000000cc"
    both = _one_encounter_record(
        health_concerns=[
            Goal(
                patient_id=pid,
                description="Uncontrolled blood pressure",
                effective=date(2023, 3, 4),
                active=True,
            ),
            Goal(
                patient_id=pid,
                description="Contact dermatitis, resolved",
                effective=date(2021, 3, 4),
                active=False,
            ),
        ]
    )
    html = template.render(**pack.build_context(both.encounters[0], both, cfg))
    assert "Uncontrolled blood pressure" in html
    assert "03/04/23" in html  # the effective date, in the pack's MM/DD/YY form
    assert "Contact dermatitis, resolved" in html
    assert "No active health concerns recorded." not in html
    assert "No inactive health concerns recorded" not in html
    # The headings stay outside the conditional: every static section renders.
    assert "Active health concerns" in html and "Inactive health concerns" in html

    active_only = _one_encounter_record(
        health_concerns=[Goal(patient_id=pid, description="Prediabetes", active=True)]
    )
    html = template.render(**pack.build_context(active_only.encounters[0], active_only, cfg))
    assert "Prediabetes" in html
    assert "No active health concerns recorded." not in html
    assert "No inactive health concerns recorded" in html


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


# --- payment information (guarantor empty states) -------------------------------


def _payment_ctx(pack: LoadedPack, record: Any) -> dict[str, str]:
    return dict(pack.build_context(record.encounters[0], record, _cfg(pack))["payment"])


def test_payment_fixture_values_match_predecessor_shapes(
    pack: LoadedPack, records: list[Any]
) -> None:
    record = next(r for r in records if r.patient.guarantor is not None)
    payment = _payment_ctx(pack, record)
    # Address is the comma-joined line1, city, state, zip.
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

    from anastomosis.core.timeutil import to_local

    html = _render_all(pack, records)[0]
    # Render-day in the PACK's timezone — the same `to_local` every other date
    # in the module goes through. `date.today()` is the system local date, which
    # is what made this stamp depend on where the operator was sitting (#194).
    today = to_local(dt.datetime.now(dt.UTC), pack.manifest.timezone).strftime("%m/%d/%Y")
    assert f"Current Medications (as of {today})" in html


def test_the_as_of_stamp_follows_the_pack_not_the_machine(
    pack: LoadedPack, records: list[Any]
) -> None:
    """Two clinics rendering the same record at the same moment got two charts.

    `date.today()` reads the SYSTEM local date; every other date in this module
    goes through `to_local(..., tz)` and lands in the practice's timezone. So
    the stamp moved with the operator, and #194 measured it across the date
    line: 08/28/2026 under `TZ=Pacific/Kiritimati`, 08/27/2026 under
    `TZ=Etc/GMT+12`, same instant, same record.

    Driven through the PACK's timezone rather than the process's, which is both
    portable — Windows has no `time.tzset()`, and setting `TZ` does not move
    its clock — and a stronger statement: the stamp has to TRACK the pack, not
    merely ignore the machine. The two zones below are 26 hours apart, so their
    local dates always differ, whenever this runs.
    """
    import datetime as dt

    from anastomosis.core.timeutil import to_local

    def stamp_for(zone: str) -> str:
        cfg = {**_cfg(pack), "timezone": zone}
        context = pack.build_context(records[0].encounters[0], records[0], cfg)
        return str(context["meds_as_of"])

    east, west = "Pacific/Kiritimati", "Etc/GMT+12"  # UTC+14 and UTC-12
    now = dt.datetime.now(dt.UTC)

    assert stamp_for(east) == to_local(now, east).strftime("%m/%d/%Y")
    assert stamp_for(west) == to_local(now, west).strftime("%m/%d/%Y")
    assert stamp_for(east) != stamp_for(west), (
        "the stamp is the same in two zones a day apart — it is not reading the pack's"
    )


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


# --- W4 perf hoist (output-preserving; the e2e goldens prove byte-identity) -----


def test_observations_by_encounter_matches_observations_for(records: list[Any]) -> None:
    """The indexed grouping equals the per-encounter linear scan exactly — same
    members, same order — so swapping the render contexts to it cannot change
    output."""
    for record in records:
        grouped = record.observations_by_encounter()
        for encounter in record.encounters:
            assert grouped.get(encounter.id, []) == record.observations_for(encounter.id)
        # Patient-level observations (no encounter id) group under None and are
        # excluded from any real encounter id.
        assert all(o.encounter_id is None for o in grouped.get(None, []))


def test_record_view_index_built_once_per_record(pack: LoadedPack, records: list[Any]) -> None:
    """With the engine's shared per-record cache, the record-level index and the
    observations grouping are built ONCE per record and reused across its
    encounters (the W4 hoist) — proven by object identity in the cache (the pack
    loads under a dynamic module name, so a monkeypatch of the class would miss)."""
    record = max(records, key=lambda r: len(r.encounters))
    assert len(record.encounters) >= 2  # a record where per-record vs per-encounter differ

    cache: dict[str, Any] = {}
    cfg = {**_cfg(pack), "record_cache": cache}
    pack.build_context(record.encounters[0], record, cfg)
    index_first = cache["pf_view_index"]
    obs_first = cache["obs_by_encounter"]
    pack.build_context(record.encounters[1], record, cfg)
    # The cached objects PERSIST across encounters — built once, not rebuilt.
    assert cache["pf_view_index"] is index_first
    assert cache["obs_by_encounter"] is obs_first


def test_build_context_without_cache_still_builds(pack: LoadedPack, records: list[Any]) -> None:
    """A direct caller that passes no record_cache still works (the fallback
    builds locally) — engine and direct-call paths both render correctly."""
    record = next(r for r in records if r.encounters)
    ctx = pack.build_context(record.encounters[0], record, _cfg(pack))  # no record_cache
    assert ctx and "payment" in ctx  # built locally via the fallback, render-ready


def test_record_static_context_built_once_and_merge_preserves_output(
    pack: LoadedPack, records: list[Any]
) -> None:
    """The RECORD-STATIC views (insurance, meds, allergies, diagnoses,
    immunizations, demographics, social history) are assembled ONCE per record
    and reused across encounters — proven by object identity of the memoized
    static dict — and merging static + per-encounter must reproduce the full
    context exactly (per-encounter keys win on the disjoint partition)."""
    from anastomosis.packs.practice_fusion_soap.context import build_record_context

    record = max(records, key=lambda r: len(r.encounters))
    assert len(record.encounters) >= 2  # static-vs-encounter only differ with >=2 encounters

    cache: dict[str, Any] = {}
    cfg = {**_cfg(pack), "record_cache": cache}
    ctx0 = pack.build_context(record.encounters[0], record, cfg)
    static_first = cache["pf_record_context"]
    # A representative record-static value is present and identical across encounters.
    assert "active_medications" in static_first
    ctx1 = pack.build_context(record.encounters[1], record, cfg)
    # The static dict is the SAME object on the second encounter (built once).
    assert cache["pf_record_context"] is static_first
    # Every record-static key is byte-identical across the two encounters.
    for key in static_first:
        assert ctx0[key] == ctx1[key], f"record-static key drifted across encounters: {key}"

    # The merged context equals static-keys-from-static + the rest from the
    # per-encounter build (no key is silently dropped by the merge).
    no_cache_ctx = pack.build_context(record.encounters[0], record, _cfg(pack))
    assert set(no_cache_ctx) == set(ctx0)  # the cached and uncached paths agree on keys
    assert build_record_context(record, cfg, cache) is static_first  # idempotent + cached


def test_the_flowsheet_shows_blood_pressure_like_the_per_encounter_rows_do() -> None:
    """A vitals flowsheet exists to show a trend, and BP is the trend.

    Observations arrive as two separate LOINC codes (8480-6 systolic, 8462-4
    diastolic) and `_VITAL_ORDER` names only the combined "Blood Pressure" row,
    so a path that does not fold them renders neither. The per-encounter rows
    folded them; the flowsheet did not, and every prior-encounter blood
    pressure was dropped from the archival copy of the chart with the run
    reporting pass.

    The flowsheet also lacked the per-encounter path's documented lossless
    fallback, so a vital with no order slot vanished the same way.

    Every value here is invented.
    """
    import datetime as dt

    from anastomosis.core.model import (
        Encounter,
        Observation,
        ObservationCategory,
        Patient,
        PatientRecord,
    )
    from anastomosis.packs.practice_fusion_soap.context import build_context

    patient = Patient(
        id="feedface-0000-0000-0000-000000000001",
        given_name="Fixture",
        family_name="Patient",
        birth_date=dt.date(1980, 1, 2),
    )
    early = Encounter(
        id="feedface-enc-1", patient_id=patient.id, date_of_service=dt.date(2023, 1, 10)
    )
    later = Encounter(
        id="feedface-enc-2", patient_id=patient.id, date_of_service=dt.date(2023, 6, 1)
    )

    def vital(enc: Encounter, code: str, value: str, unit: str | None = None) -> Observation:
        return Observation(
            id=f"feedface-obs-{code}-{enc.id}",
            patient_id=patient.id,
            encounter_id=enc.id,
            category=ObservationCategory.VITAL_SIGNS,
            code=code,
            value=value,
            unit=unit,
        )

    record = PatientRecord(
        patient=patient,
        encounters=[early, later],
        observations=[
            vital(early, "8480-6", "162", "mmHg"),
            vital(early, "8462-4", "104", "mmHg"),
            vital(early, "29463-7", "180", "lb"),
            vital(later, "8480-6", "118", "mmHg"),
            vital(later, "8462-4", "76", "mmHg"),
        ],
    )

    rows = build_context(later, record, {})["flowsheet_rows"]
    by_name = {row["name"]: row["vals"] for row in rows}
    assert "Blood Pressure" in by_name, (
        f"the prior encounter's blood pressure is not in the flowsheet: {rows}"
    )
    assert by_name["Blood Pressure"] == ["162/104"]
    # The declared display order puts BP between Weight and Temperature.
    assert [row["name"] for row in rows] == ["Weight", "Blood Pressure"]
