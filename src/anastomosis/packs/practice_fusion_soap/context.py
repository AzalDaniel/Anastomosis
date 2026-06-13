"""Context builder for the practice_fusion_soap pack.

Maps a canonical :class:`PatientRecord` + :class:`Encounter` into the
template's variables, reproducing the predecessor's PF SOAP-note rendering
rules (GOLD_STANDARD.md, distilled in RULES.md). The canonical model and the
``pf_tebra`` adapter already carry the data semantics (sentinels, escript
status resolution, BMI auto-calc, the PlanType join); this module is the
*presentation* layer the predecessor's single script also owned:

* drug display-name composition ``Generic (Brand) Strength Route Form``
  (GOLD §5#5), with the generic==trade paren-omission and brand-only fallback;
* the ESCRIPT/SCRIPT prescription line, escript date in Eastern, MM/DD/YY;
* insurance Active/Inactive split, OrderOfBenefits sort, the 4-col grid;
* the 17 social-history sub-categories (empty-state strings live in template);
* vitals ordering / BP combination / "as of" render-day date;
* the synthetic logo data-URI (the vendor mark is NEVER shipped — RULES §logo).

Where a template variable has no canonical source yet, a ``# LOUD:`` comment
marks it and the value falls back to the documented PF empty state rather than
inventing data.
"""

from __future__ import annotations

import base64
import datetime as _dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anastomosis.core.model import (
    Address,
    AllergyCategory,
    ContactKind,
    Coverage,
    Encounter,
    Guarantor,
    IdentifierKind,
    MedicationStatement,
    Observation,
    ObservationCategory,
    PatientRecord,
    Prescription,
    SectionKind,
)
from anastomosis.core.timeutil import age_display, to_local

# --- vitals --------------------------------------------------------------------
# Display order (GOLD §8 VITAL_ORDER). Blood Pressure is the combined sys/dia row.
_VITAL_ORDER = [
    "Height",
    "Weight",
    "BMI",
    "BMI Percentile",
    "Blood Pressure",
    "Temperature",
    "Pulse",
    "Respiratory rate",
    "O2 Saturation",
    "Pain",
    "Head Circumference",
]
# Canonical Observation.code (LOINC) -> the PF vitals display label (GOLD §8).
# Codes match core.codes.VITALS primaries + their accepted aliases so a vital
# charted under either LOINC edition lands on the right row.
_LOINC_TO_LABEL: dict[str, str] = {
    "8302-2": "Height",
    "3141-9": "Weight",
    "29463-7": "Weight",
    "39156-5": "BMI",
    "59576-9": "BMI Percentile",
    "8480-6": "Systolic BP",
    "8462-4": "Diastolic BP",
    "8867-4": "Pulse",
    "9279-1": "Respiratory rate",
    "8310-5": "Temperature",
    "2708-6": "O2 Saturation",
    "59408-5": "O2 Saturation",
    "72514-3": "Pain",
    "8287-5": "Head Circumference",
    "9843-4": "Head Circumference",
}
_FLOWSHEET_MAX_COLUMNS = 10  # GOLD §8 — most-recent 10 prior encounters


def _fmt_date_short(value: _dt.date | None) -> str | None:
    """MM/DD/YY (2-digit year) — the PF date format (GOLD §5, §8)."""
    return value.strftime("%m/%d/%y") if value else None


def _fmt_date_long(value: _dt.date | None) -> str | None:
    return value.strftime("%B %d, %Y") if value else None


def _fmt_signed_at(value: _dt.datetime | None, tz: str) -> str | None:
    """Signed/print datetime in practice-local time, no leading zeros."""
    if value is None:
        return None
    local = to_local(value, tz)
    return local.strftime("%b %d, %Y %I:%M %p").replace(" 0", " ")


def _fmt_time(value: _dt.datetime | None, tz: str) -> str | None:
    """h:mm AM/PM (no leading zero) in practice-local time (GOLD §8)."""
    if value is None:
        return None
    local = to_local(value, tz)
    return local.strftime("%I:%M %p").lstrip("0")


def _ext(obj: Any, key: str) -> Any:
    """Read a pf_tebra extension value (namespaced ``pf_tebra:<Column>``)."""
    extensions = getattr(obj, "extensions", None) or {}
    return extensions.get(f"pf_tebra:{key}")


# --- medications ---------------------------------------------------------------


def _med_display_name(med: MedicationStatement) -> str:
    """Compose ``Generic (Brand) Strength Route DoseForm`` (GOLD §5#5).

    * If the adapter already stored a display_name, use it (lossless: it is the
      source's MedicationName).
    * Else build from components: omit the ``(Brand)`` parens when generic ==
      trade; fall back to brand-only when nothing else is present.
    """
    if med.display_name:
        return med.display_name
    generic = (med.generic_name or "").strip()
    brand = (med.brand_name or "").strip()
    tail = " ".join(p for p in (med.strength, med.route, med.dose_form) if p)
    if generic and brand and generic.lower() != brand.lower():
        head = f"{generic} ({brand})"
    elif generic:
        head = generic
    elif brand:
        head = brand
    else:
        head = "-"
    return " ".join(p for p in (head, tail) if p).strip() or "-"


def _start_stop(med: MedicationStatement) -> str:
    """START/STOP cell (GOLD §5#6): both, only-stop (historical), only-start, '-'."""
    start = _fmt_date_short(med.start)
    stop = _fmt_date_short(med.stop)
    if start and stop:
        return f"{start} - {stop}"
    if stop:
        return f"- {stop}"
    if start:
        return start
    return "-"


def _escript_line(rx: Prescription, record: PatientRecord, tz: str) -> dict[str, str]:
    """One ESCRIPT/SCRIPT line. Prefix + status come from the adapter's
    transaction-priority resolution (escript.py); the displayed date is the
    adapter-resolved display_date (Order-sent→Eastern for ESCRIPT) rendered
    MM/DD/YY."""
    prescriber = record.practitioner(rx.prescriber_id)
    prescriber_name = prescriber.name if prescriber else "-"
    display = rx.display_date
    if isinstance(display, _dt.datetime):
        date_str = to_local(display, tz).strftime("%m/%d/%y")
    else:
        date_str = _fmt_date_short(display) or "-"
    return {
        "prefix": rx.prefix or "ESCRIPT",
        "status": rx.status_label or "VERIFIED",
        "date": date_str,
        "prescriber": prescriber_name,
        "sig": rx.sig or "-",
        "refills": rx.refills or "0",
        "quantity": rx.quantity or "-",
    }


def _medication_view(
    med: MedicationStatement,
    rx_by_id: dict[str, Prescription],
    record: PatientRecord,
    tz: str,
) -> dict[str, Any]:
    escripts = [
        _escript_line(rx_by_id[pid], record, tz) for pid in med.prescription_ids if pid in rx_by_id
    ]
    return {
        "name": _med_display_name(med),
        "sig": med.sig,
        "start_stop": _start_stop(med),
        "assoc_dx": med.associated_dx,
        "escripts": escripts,
    }


# --- insurance -----------------------------------------------------------------


def _fmt_copay(value: str | None) -> str:
    """Copay: '-' for null sentinel/empty; integers without decimals; else the
    shortest representation (GOLD §7 "Copay formatting")."""
    if value is None or value == "":
        return "-"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    if number.is_integer():
        return str(int(number))
    return f"{number:g}"


def _coverage_view(cov: Coverage) -> dict[str, str]:
    """One insurance row. Sub-header is ``{PRIORITY} PAYER - {COVERAGE}`` (GOLD §7).
    TYPE is the adapter-resolved plan_type (superbill PlanType join), shown '-'
    when unresolved — never the generic coverage_type "Medical" (GOLD §7)."""
    priority = (cov.priority_label or "").strip()
    coverage = (cov.coverage_type or "MEDICAL").upper()
    sub_header = f"{priority} - {coverage}" if priority else coverage
    return {
        "sub_header": sub_header,
        "payer": cov.payer or "-",
        "member_id": cov.member_id or "-",
        "priority": priority or "-",
        "group_number": cov.group_number or "-",
        "type": cov.plan_type or "-",
        "employer_name": cov.employer or "-",
        "relationship": cov.relationship_to_insured or "-",
        "ins_payment_type": _ext(cov, "InsurancePaymentType") or "-",
        "start_date": _fmt_date_short(cov.start) or "-",
        "payment_type": cov.payment_type or "-",
        "end_date": _fmt_date_short(cov.end) or "-",
        "copay": _fmt_copay(cov.copay),
        "status": cov.status_label or ("Active" if cov.active else "Inactive"),
    }


def _benefit_key(cov: Coverage) -> int:
    """Sort key: OrderOfBenefits ASC, unknown last (GOLD §7)."""
    return cov.order_of_benefits if cov.order_of_benefits is not None else 99


# --- payment / guarantor ---------------------------------------------------------


def _guarantor_addr(addr: Address | None) -> str:
    """Comma-joined line1, city, state, zip — only when line1 exists (gpdfs:944-948)."""
    if addr is None or not addr.line1:
        return "-"
    return ", ".join(p for p in (addr.line1, addr.city, addr.state, addr.postal_code) if p)


def _payment(guarantor: Guarantor | None) -> dict[str, str]:
    """The payment-information cells, with the predecessor's exact empty states
    (gpdfs:950-961): every absent value renders as ``-`` except PAYMENT
    PREFERENCE, which PF defaults to ``Primary Insurance``. Never emits None —
    the template interpolates these raw."""
    phones = guarantor.phones if guarantor else []
    by_kind = {p.kind: p.value for p in phones}
    if ContactKind.PHONE_HOME in by_kind or ContactKind.PHONE_OTHER in by_kind:
        primary = by_kind.get(ContactKind.PHONE_HOME)
        secondary = by_kind.get(ContactKind.PHONE_OTHER)
    else:  # sources that don't tag guarantor phone kinds: positional
        primary = phones[0].value if phones else None
        secondary = phones[1].value if len(phones) > 1 else None
    return {
        "preference": (guarantor.payment_preference if guarantor else None) or "Primary Insurance",
        "relationship": (guarantor.relationship_to_patient if guarantor else None) or "-",
        "guarantor_name": (guarantor.name if guarantor else None) or "-",
        "guarantor_addr": _guarantor_addr(guarantor.address if guarantor else None),
        "dob": (
            guarantor.birth_date.strftime("%m/%d/%Y") if guarantor and guarantor.birth_date else "-"
        ),
        "sex": (guarantor.sex if guarantor else None) or "-",
        "ssn": (guarantor.ssn if guarantor else None) or "-",
        "primary_phone": primary or "-",
        "secondary_phone": secondary or "-",
    }


# --- logo ----------------------------------------------------------------------


def _logo_data_uri(cfg_tokens: dict[str, str], pack_root: Path) -> str:
    """Resolve the (synthetic) logo to a data-URI.

    Operator override ``tokens.logo_data_uri`` wins; otherwise the neutral
    placeholder SVG asset is read and base64-encoded. The real PF vendor mark
    is NEVER shipped or referenced (RULES §logo)."""
    # Only inline data: URIs are honored as overrides. Anything else
    # (http/https/file) would make Chromium fetch it at render time — an
    # outbound request from a page full of PHI. Local image files go via
    # `logo_asset` instead; a non-data: override falls back to the asset.
    override = cfg_tokens.get("logo_data_uri")
    if override and override.startswith("data:"):
        return override
    asset = cfg_tokens.get("logo_asset", "assets/placeholder_logo.svg")
    path = (pack_root / asset).resolve()
    if not path.is_relative_to(pack_root.resolve()):
        return ""  # a logo_asset must live inside the pack — never embed files beyond it
    try:
        raw = path.read_bytes()
    except OSError:
        return ""  # logo is decorative; a missing asset must never crash a render
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


# --- vitals views --------------------------------------------------------------


def _encounter_vital_rows(vitals: list[Observation]) -> list[dict[str, str]]:
    """Build the per-encounter vitals rows in VITAL_ORDER, combining BP into a
    single ``Blood Pressure`` row (GOLD §8)."""
    by_label: dict[str, str] = {}
    for obs in vitals:
        label = _LOINC_TO_LABEL.get(obs.code or "", obs.display or obs.code or "")
        value = obs.value
        if value is None:
            continue
        unit = obs.unit or ""
        by_label[label] = f"{value} {unit}".strip() if unit else str(value)
    systolic = by_label.pop("Systolic BP", None)
    diastolic = by_label.pop("Diastolic BP", None)
    if systolic or diastolic:
        # strip units off the BP components for the combined "sys/dia" cell
        sys_v = (systolic or "").split(" ")[0]
        dia_v = (diastolic or "").split(" ")[0]
        by_label["Blood Pressure"] = f"{sys_v}/{dia_v}".strip("/")
    rows: list[dict[str, str]] = []
    for label in _VITAL_ORDER:
        if label in by_label:
            rows.append({"name": label, "value": by_label[label]})
    # Any vital we did not have an order slot for still renders (lossless).
    for label, value in by_label.items():
        if label not in _VITAL_ORDER:
            rows.append({"name": label, "value": value})
    return rows


@dataclass(frozen=True)
class _RecordViewIndex:
    """Record-level groupings, precomputed in one pass per collection.

    ``build_context`` previously re-scanned each collection several times
    (active/inactive splits, allergy-by-category, and — the only asymptotic
    cost — a ``{id: rx}`` prescription map rebuilt inside the per-medication
    loop, O(meds x prescriptions)). This computes each grouping once. Built per
    call (the flowsheet and per-encounter vitals stay encounter-specific); the
    splits preserve the source collection order, then coverages are sorted by
    benefit order exactly as before.
    """

    active_coverages: list[Coverage]
    inactive_coverages: list[Coverage]
    active_conditions: list[Any]
    historical_conditions: list[Any]
    conditions_by_id: dict[str, Any]
    active_medications: list[MedicationStatement]
    historical_medications: list[MedicationStatement]
    prescriptions_by_id: dict[str, Prescription]
    allergies_by_category: dict[AllergyCategory, list[Any]]
    active_concerns: list[Any]
    inactive_concerns: list[Any]
    active_goals: list[Any]
    inactive_goals: list[Any]
    smoking: Observation | None
    sh_freetext: str | None

    @classmethod
    def build(cls, record: PatientRecord) -> _RecordViewIndex:
        active_cov: list[Coverage] = []
        inactive_cov: list[Coverage] = []
        for cov in record.coverages:
            (active_cov if cov.active else inactive_cov).append(cov)
        active_cov.sort(key=_benefit_key)  # stable: ties keep source order (GOLD §7)
        inactive_cov.sort(key=_benefit_key)

        active_cond: list[Any] = []
        historical_cond: list[Any] = []
        conditions_by_id: dict[str, Any] = {}
        for cond in record.conditions:
            conditions_by_id[cond.id] = cond
            (active_cond if cond.active else historical_cond).append(cond)

        active_meds: list[MedicationStatement] = []
        historical_meds: list[MedicationStatement] = []
        for med in record.medications:
            (active_meds if med.active else historical_meds).append(med)

        allergies_by_category: dict[AllergyCategory, list[Any]] = {}
        for allergy in record.allergies:
            allergies_by_category.setdefault(allergy.category, []).append(allergy)

        active_hc: list[Any] = []
        inactive_hc: list[Any] = []
        for concern in record.health_concerns:
            (active_hc if concern.active else inactive_hc).append(concern)

        active_goals: list[Any] = []
        inactive_goals: list[Any] = []
        for goal in record.goals:
            (active_goals if goal.active else inactive_goals).append(goal)

        smoking = next(
            (
                o
                for o in record.observations
                if o.category == ObservationCategory.SOCIAL_HISTORY
                and (o.display or "").upper().startswith("TOBACCO")
            ),
            None,
        )
        sh_freetext = next(
            (
                p.text
                for p in record.past_medical_history
                if (p.kind or "").lower().startswith("social")
            ),
            None,
        )
        return cls(
            active_coverages=active_cov,
            inactive_coverages=inactive_cov,
            active_conditions=active_cond,
            historical_conditions=historical_cond,
            conditions_by_id=conditions_by_id,
            active_medications=active_meds,
            historical_medications=historical_meds,
            prescriptions_by_id={p.id: p for p in record.prescriptions},
            allergies_by_category=allergies_by_category,
            active_concerns=active_hc,
            inactive_concerns=inactive_hc,
            active_goals=active_goals,
            inactive_goals=inactive_goals,
            smoking=smoking,
            sh_freetext=sh_freetext,
        )


def build_context(
    encounter: Encounter, record: PatientRecord, cfg: dict[str, Any]
) -> dict[str, Any]:
    tz = str(cfg.get("timezone", "America/New_York"))
    sections: dict[str, bool] = cfg.get("sections", {})
    tokens: dict[str, str] = cfg.get("tokens", {})
    pack_root: Path = Path(cfg.get("pack_root", Path(__file__).resolve().parent))
    patient = record.patient
    dos = encounter.date_of_service  # calendar date — never timezone-shifted
    index = _RecordViewIndex.build(record)  # record-level groupings, one pass each

    # --- header: patient / facility / encounter --------------------------------
    age = age_display(patient.birth_date, dos) if patient.birth_date and dos else None
    facility = record.facility(encounter.facility_id)
    seen_by = record.practitioner(encounter.provider_id)
    signer = record.practitioner(encounter.signed_by_id)
    prn = _ext(patient, "PatientContactCode") or _ext(patient, "PRN")
    city_state_zip = None
    if facility:
        bits = [facility.city, facility.state, facility.postal_code]
        city_state_zip = " ".join(p for p in bits if p) or None

    # --- demographics (the unified 6-col table) --------------------------------
    home_addr = patient.addresses[0] if patient.addresses else None
    kin = patient.contacts[0] if patient.contacts else None
    telecom = {cp.kind: cp.value for cp in patient.telecom}
    demo = {
        "first_name": patient.given_name,
        "middle_name": patient.middle_name,
        "last_name": patient.family_name,
        "sex": patient.sex,
        "dob": patient.birth_date.strftime("%m/%d/%Y") if patient.birth_date else None,
        "death_date": _ext(patient, "DateOfDeath"),
        "race": ", ".join(patient.race) or None,
        "ethnicity": ", ".join(patient.ethnicity) or None,
        "language": patient.language,
        "status": patient.status,
        "ssn": patient.identifier(IdentifierKind.SSN),
        "address1": home_addr.line1 if home_addr else None,
        "address2": home_addr.line2 if home_addr else None,
        "city": home_addr.city if home_addr else None,
        "state": home_addr.state if home_addr else None,
        "zip": home_addr.postal_code if home_addr else None,
        "contact_by": patient.contact_preference,
        "email": telecom.get(ContactKind.EMAIL),
        "phone_home": telecom.get(ContactKind.PHONE_HOME),
        "phone_mobile": telecom.get(ContactKind.PHONE_MOBILE),
        "phone_office": telecom.get(ContactKind.PHONE_WORK),
        "office_ext": _ext(patient, "OfficePhoneExtension"),
        "next_of_kin": kin.name if kin else None,
        "kin_relation": kin.relationship if kin else None,
        "kin_phone": kin.phone if kin else None,
        "kin_address": (kin.address.line1 if kin and kin.address else None),
        "mothers_maiden_name": patient.mothers_maiden_name,
    }

    # --- insurance -------------------------------------------------------------
    show_insurance = sections.get("insurance", True)
    active_cov = index.active_coverages
    inactive_cov = index.inactive_coverages

    # --- payment / guarantor ---------------------------------------------------
    payment = _payment(patient.guarantor)

    # --- vitals ----------------------------------------------------------------
    enc_vitals = [
        o
        for o in record.observations_for(encounter.id)
        if o.category == ObservationCategory.VITAL_SIGNS
    ]
    enc_vital_rows = _encounter_vital_rows(enc_vitals)
    vitals_obs_dt = next((o.effective_at for o in enc_vitals if o.effective_at), None)

    # vitals flowsheet — prior encounters only, most-recent 10 columns (GOLD §8)
    flowsheet_columns, flowsheet_rows = _build_flowsheet(record, encounter, dos)

    # --- diagnoses -------------------------------------------------------------
    current_dx = [_dx_view(c) for c in index.active_conditions]
    historical_dx = [_dx_view(c) for c in index.historical_conditions]
    encounter_dx = _encounter_diagnoses(index.conditions_by_id, encounter)

    # --- allergies -------------------------------------------------------------
    drug_allergies = _allergy_views(index.allergies_by_category.get(AllergyCategory.DRUG, []))
    food_allergies = _allergy_views(index.allergies_by_category.get(AllergyCategory.FOOD, []))
    env_allergies = _allergy_views(index.allergies_by_category.get(AllergyCategory.ENVIRONMENT, []))

    # --- medications -----------------------------------------------------------
    rx_by_id = index.prescriptions_by_id
    active_meds = [_medication_view(m, rx_by_id, record, tz) for m in index.active_medications]
    historical_meds = [
        _medication_view(m, rx_by_id, record, tz) for m in index.historical_medications
    ]
    # "as of" = render-day, NOT encounter date (GOLD §5#9).
    meds_as_of = _dt.date.today().strftime("%m/%d/%Y")  # noqa: DTZ011 — display-only render-day

    # --- social history (free-text + smoking; rest fall to template empty state)
    smoking = index.smoking
    sh_freetext = index.sh_freetext

    # --- SOAP sections (sanitize_soap_html output rides NoteSection.html) -------
    soap = {s.kind: s for s in encounter.sections}
    subjective = soap.get(SectionKind.SUBJECTIVE) or soap.get(SectionKind.NARRATIVE)
    objective = soap.get(SectionKind.OBJECTIVE)
    assessment = soap.get(SectionKind.ASSESSMENT)
    plan = soap.get(SectionKind.PLAN)

    # --- past medical history --------------------------------------------------
    pmh_sections = [
        {"type": (p.kind or "HISTORY").upper(), "text": p.text}
        for p in record.past_medical_history
        if not (p.kind or "").lower().startswith("social") and (p.text or "").strip()
    ]

    # --- family / directives / devices / concerns / goals ----------------------
    family_history = [
        {"diagnosis": f.diagnosis, "onset": _fmt_date_short(f.onset_date)}
        for f in record.family_history
        if f.diagnosis
    ]
    advance_directives = [
        {
            "directive": d.directive,
            "recorded": _fmt_signed_at(d.recorded_at, tz) or "",
        }
        for d in record.advance_directives
        if d.directive
    ]
    implantable_devices = [
        {"name": d.description, "date": _fmt_signed_at(d.recorded_at, tz)}
        for d in record.devices
        if d.description
    ]
    active_hc = [_concern_view(h) for h in index.active_concerns]
    inactive_hc = [_concern_view(h) for h in index.inactive_concerns]
    active_goals = [_concern_view(g) for g in index.active_goals]
    inactive_goals = [_concern_view(g) for g in index.inactive_goals]

    # --- orders ----------------------------------------------------------------
    lab_orders = [
        {"test_items": [{"display": i.test_name} for i in o.items if i.test_name]}
        for o in record.lab_orders
        if o.encounter_id in (encounter.id, None)
    ]

    # --- addenda (conditional) -------------------------------------------------
    addendums = [
        {
            "text": a.text or "",
            "status": _addendum_status(a),
            "source": a.source or "",
            "datetime": _addendum_datetime(a.at, tz),
        }
        for a in encounter.addenda
        if (a.text or "").strip()
    ]

    return {
        # header / patient
        "patient_name": patient.display_name or "Unknown patient",
        "dob": patient.birth_date.strftime("%m/%d/%Y") if patient.birth_date else None,
        "age": age,
        "sex": patient.sex,
        "prn": prn,
        "fac_name": facility.name if facility else None,
        "fac_phone": facility.phone if facility else None,
        "fac_fax": facility.fax if facility else None,
        "fac_addr1": facility.address_line1 if facility else None,
        "fac_addr2": facility.address_line2 if facility else None,
        "fac_city_state_zip": city_state_zip,
        "encounter_type": encounter.encounter_type,
        "note_type": encounter.note_type,
        "seen_by_name": seen_by.name if seen_by else None,
        "seen_by_credential": seen_by.credential if seen_by else None,
        "dos": _fmt_date_long(dos) or "Undated",
        "age_at_dos": age,
        "signed_by_name": signer.name if signer else None,
        "signed_by_credential": signer.credential if signer else None,
        "signed_at": _fmt_signed_at(encounter.signed_at, tz),
        "cc_text": encounter.chief_complaint,
        "demo": demo,
        "patient_notes": patient.notes,
        # section flags
        "show_insurance": show_insurance,
        "show_payment": sections.get("payment", True),
        "show_vitals": sections.get("vitals", True),
        "show_vitals_flowsheet": sections.get("vitals_flowsheet", True),
        "show_immunizations": sections.get("immunizations", True),
        "show_social_history": sections.get("social_history", True),
        "show_past_medical_history": sections.get("past_medical_history", True),
        "show_family_history": sections.get("family_history", True),
        "show_advance_directives": sections.get("advance_directives", True),
        "show_devices": sections.get("devices", True),
        "show_health_concerns": sections.get("health_concerns", True),
        "show_goals": sections.get("goals", True),
        "show_orders": sections.get("orders", True),
        "show_addenda": sections.get("addenda", True),
        # insurance / payment
        "active_insurance": [_coverage_view(c) for c in active_cov],
        "inactive_insurance": [_coverage_view(c) for c in inactive_cov],
        "payment": payment,
        # vitals
        "enc_vitals_rows": enc_vital_rows,
        "vitals_date": _fmt_date_short(dos),
        "vitals_time": _fmt_time(vitals_obs_dt, tz),
        "flowsheet_patient_name": patient.display_name or "",
        "flowsheet_columns": flowsheet_columns,
        "flowsheet_rows": flowsheet_rows,
        "flowsheet_vitals_label": bool(flowsheet_columns),
        # diagnoses / allergies
        "current_diagnoses": current_dx,
        "historical_diagnoses": historical_dx,
        "encounter_diagnoses": encounter_dx,
        "diag_recon_text": None,  # LOUD: no reconciliation column in EHI
        "allergy_recon_text": None,  # LOUD: same; falls to "No selection made"
        "drug_allergies": drug_allergies,
        "food_allergies": food_allergies,
        "env_allergies": env_allergies,
        # medications
        "active_medications": active_meds,
        "historical_medications": historical_meds,
        "meds_as_of": meds_as_of,
        "med_recon_text": "No selection made",  # GOLD §5#10 (hard-coded)
        # immunizations
        "immunizations": [_immunization_view(i, tz) for i in record.immunizations],
        # social history
        "smoking_status": smoking.value if smoking else None,
        "smoking_date": (
            _fmt_date_short(smoking.recorded_at.date()) if smoking and smoking.recorded_at else None
        ),
        "sh_freetext": sh_freetext,
        "sh_alcohol": None,
        "sh_financial": None,
        "sh_education": None,
        "sh_physical": None,
        "sh_nutrition": None,
        "sh_stress": None,
        "sh_isolation": None,
        "sh_violence": None,
        "sh_gender_identity": patient.gender_identity,
        "sh_sexual_orientation": patient.sexual_orientation,
        "sh_pregnancy_status": None,
        "sh_pregnancy_intent": None,
        "sh_tribal": None,
        "sh_occupations": None,
        "sh_food_insecurity": None,
        # SOAP
        "subjective_html": subjective.html if subjective else None,
        "objective_html": objective.html if objective else None,
        "assessment_html": assessment.html if assessment else None,
        "plan_html": plan.html if plan else None,
        # PMH / family / directives / devices / concerns / goals
        "pmh_sections": pmh_sections,
        "family_history": family_history,
        "family_history_freetext": None,
        "advance_directives": advance_directives,
        "implantable_devices": implantable_devices,
        "active_concerns": active_hc,
        "inactive_concerns": inactive_hc,
        "active_goals": active_goals,
        "inactive_goals": inactive_goals,
        # orders / screenings (events not modeled in EHI -> empty state)
        "lab_orders": lab_orders,
        "screening_events": [],  # LOUD: patient-encounter-events not modeled yet
        # addenda + logo + tokens
        "addendums": addendums,
        "logo_data_uri": _logo_data_uri(tokens, pack_root),
        "tokens": tokens,
    }


# --- small view helpers --------------------------------------------------------


def _dx_view(condition: Any) -> dict[str, str | None]:
    return {
        "description": condition.display,
        "acuity": condition.acuity or "-",
        "start": _fmt_date_short(condition.onset) or "-",
        "stop": _fmt_date_short(condition.stopped) or "-",
    }


def _encounter_diagnoses(
    conditions_by_id: dict[str, Any], encounter: Encounter
) -> list[dict[str, str]]:
    """The "Diagnoses attached to this encounter" block (GOLD §9)."""
    by_id = conditions_by_id
    out: list[dict[str, str]] = []
    for dx_id in encounter.diagnosis_ids:
        condition = by_id.get(dx_id)
        if condition is None:
            continue
        codes = []
        if condition.icd10:
            codes.append(f"ICD-10: {condition.icd10}")
        if condition.snomed:
            codes.append(f"SNOMED: {condition.snomed}")
        code_str = f"[{', '.join(codes)}]" if codes else ""
        out.append({"description": condition.display or "-", "full_codes": code_str})
    return out


def _allergy_views(items: list[Any]) -> dict[str, list[dict[str, str | None]]]:
    """Split one allergy category's items into active/inactive view rows. The
    caller passes the pre-grouped list from the record index (GOLD §6)."""

    def view(a: Any) -> dict[str, str | None]:
        reactions = ", ".join(a.reactions) if a.reactions else None
        severity_reactions = " / ".join(p for p in (a.severity, reactions) if p)
        return {
            "name": a.substance,
            "severity_reactions": severity_reactions or "-",
            "onset": _fmt_date_short(a.onset) or "-",
        }

    return {
        "active": [view(a) for a in items if a.active],
        "inactive": [view(a) for a in items if not a.active],
    }


def _concern_view(obj: Any) -> dict[str, str | None]:
    return {"description": obj.description, "date": _fmt_date_short(obj.effective) or "-"}


def _immunization_view(imm: Any, tz: str) -> dict[str, str | None]:
    return {
        "date": _fmt_date_short(imm.administered_on) or "-",
        "vaccine": imm.vaccine or "-",
        "source": imm.source or "-",
        "lot": imm.lot_number or "-",
        "expires": _fmt_date_short(imm.expires) or "-",
        "comment": imm.comment or "",
    }


def _addendum_status(addendum: Any) -> str:
    """ "{Status} by {Author}\\n{Credential}" (GOLD §10)."""
    status = addendum.status or ""
    author = addendum.author_name or ""
    line1 = f"{status} by {author}".strip() if (status or author) else ""
    credential = addendum.author_credential or ""
    return f"{line1}\n{credential}".strip() if credential else line1


def _addendum_datetime(value: _dt.datetime | None, tz: str) -> str:
    """MM/DD/YYYY hh:mm am/pm, lowercase am/pm, zero-padded hour (GOLD §10)."""
    if value is None:
        return ""
    local = to_local(value, tz)
    return local.strftime("%m/%d/%Y %I:%M %p").replace("AM", "am").replace("PM", "pm")


def _build_flowsheet(
    record: PatientRecord, encounter: Encounter, dos: _dt.date | None
) -> tuple[list[dict[str, str | None]], list[dict[str, Any]]]:
    """Vitals flowsheet: prior encounters only (strictly < current DOS), most
    recent ``_FLOWSHEET_MAX_COLUMNS`` columns, all 11 vital rows shown (GOLD §8).
    """
    if dos is None:
        return [], []
    enc_by_id = {e.id: e for e in record.encounters}
    # gather vital observations grouped by their encounter, prior to this DOS
    cols: dict[str, dict[str, str]] = {}
    col_dates: dict[str, _dt.date] = {}
    for obs in record.observations:
        if obs.category != ObservationCategory.VITAL_SIGNS or not obs.encounter_id:
            continue
        enc = enc_by_id.get(obs.encounter_id)
        if enc is None or enc.date_of_service is None:
            continue
        if enc.date_of_service >= dos:  # strictly prior encounters only
            continue
        label = _LOINC_TO_LABEL.get(obs.code or "", obs.display or "")
        if obs.value is None:
            continue
        cols.setdefault(enc.id, {})[label] = str(obs.value)
        col_dates[enc.id] = enc.date_of_service
    if not cols:
        return [], []
    ordered = sorted(col_dates, key=lambda eid: col_dates[eid], reverse=True)
    ordered = ordered[:_FLOWSHEET_MAX_COLUMNS]
    columns = [{"date": _fmt_date_short(col_dates[eid]), "time": None} for eid in ordered]
    rows: list[dict[str, Any]] = []
    for label in _VITAL_ORDER:
        vals = [cols[eid].get(label, "") for eid in ordered]
        if any(vals):
            rows.append({"name": label, "vals": vals})
    return columns, rows
