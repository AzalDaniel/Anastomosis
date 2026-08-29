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
    Patient,
    PatientRecord,
    Prescription,
    SectionKind,
)
from anastomosis.core.timeutil import age_display, to_local
from anastomosis.reconstruct.packctx import (
    format_local_dt,
    observations_by_encounter,
    record_cache_of,
)

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
    """Comma-joined line1, city, state, zip — only when line1 exists."""
    if addr is None or not addr.line1:
        return "-"
    return ", ".join(p for p in (addr.line1, addr.city, addr.state, addr.postal_code) if p)


def _payment(guarantor: Guarantor | None) -> dict[str, str]:
    """The payment-information cells and their empty states: every absent value
    renders as ``-`` except PAYMENT PREFERENCE, which PF defaults to
    ``Primary Insurance``. Never emits None — the template interpolates these
    raw."""
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


def _fold_blood_pressure(by_label: dict[str, str]) -> None:
    """Fold the two BP components into the one row the layout declares.

    RULES.md §Vitals: "Systolic + Diastolic combine into one `Blood Pressure`
    `{sys}/{dia}` row." The observations arrive as separate LOINC codes
    (8480-6 / 8462-4) and `_VITAL_ORDER` names only the combined row, so a path
    that skips this fold renders neither — which is what the flowsheet did to
    every prior-encounter blood pressure.

    In place: both callers build a label -> value map and want the same single
    key out of it.
    """
    systolic = by_label.pop("Systolic BP", None)
    diastolic = by_label.pop("Diastolic BP", None)
    if systolic or diastolic:
        # strip units off the BP components for the combined "sys/dia" cell
        sys_v = (systolic or "").split(" ")[0]
        dia_v = (diastolic or "").split(" ")[0]
        by_label["Blood Pressure"] = f"{sys_v}/{dia_v}".strip("/")


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
    _fold_blood_pressure(by_label)
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

        active_concerns: list[Any] = []
        inactive_concerns: list[Any] = []
        for concern in record.health_concerns:
            (active_concerns if concern.active else inactive_concerns).append(concern)

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
            active_concerns=active_concerns,
            inactive_concerns=inactive_concerns,
            active_goals=active_goals,
            inactive_goals=inactive_goals,
            smoking=smoking,
            sh_freetext=sh_freetext,
        )


def _demographics(patient: Patient) -> dict[str, Any]:
    """The unified 6-column demographics table (a self-contained slice of the
    context, a pure function of the patient)."""
    home_addr = patient.addresses[0] if patient.addresses else None
    kin = patient.contacts[0] if patient.contacts else None
    telecom = {cp.kind: cp.value for cp in patient.telecom}
    return {
        "first_name": patient.given_name,
        "middle_name": patient.middle_name,
        "last_name": patient.family_name,
        "sex": patient.sex,
        "dob": patient.birth_date.strftime("%m/%d/%Y") if patient.birth_date else None,
        # DeathDate is what patient-demographics spells it; DateOfDeath was a
        # name no v9 table has, so the DATE OF DEATH cell printed "-" over an
        # export that carried the date.
        "death_date": _ext(patient, "DeathDate"),
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


#: What a section says when this pack cannot reconstruct it.
#:
#: Six sections used to print the vendor's own empty state — "No implantable
#: devices recorded", "No orders attached to this encounter." — over exports
#: that carry exactly that data. The note was not merely incomplete; it
#: asserted a negative the source contradicts, which is the one thing this
#: project promises not to do.
#:
#: The reason those sections are static is NOT that v9 has no data path. It
#: does: `patient-healthcare-devices` is a 31-column table, `patient-lab-orders`
#: and `patient-lab-order-items` carry the orders (LabType separates diagnostic
#: from imaging), and `patient-encounter-observations` carries the observations.
#: What is missing is the ROW LAYOUT — the forensic gold standard preserves no
#: populated example of these sections, so the pack does not invent one.
#:
#: So the notice claims nothing either way. It says the layout is unknown and
#: points at where the data actually is, which is true whether the export
#: carried anything or not.
#:
#: No apostrophe on purpose: autoescape turns one into ``&#39;``, and the
#: string then stops matching itself anywhere it is compared to the page.
UNRECONSTRUCTED = (
    "Not reconstructed — this layout has no verified format for this section. "
    "Whatever the export carries is preserved in the structured record for this patient."
)

#: The same statement as an inline answer, for the quality-of-care rows, where
#: the full sentence would be answering a yes/no question with a paragraph.
UNRECONSTRUCTED_SHORT = "Not reconstructed"


def _section_flags(sections: dict[str, bool]) -> dict[str, bool]:
    """The per-section show/hide flags the template gates on (all default ON)."""
    return {
        "show_insurance": sections.get("insurance", True),
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
    }


def _social_history_context(patient: Patient, index: _RecordViewIndex) -> dict[str, Any]:
    """Social-history block: smoking + the social free-text block come from the
    record (`patient-smokingstatus` and the `social`-kind `patient-med-history`
    block); the structured subcategories below stay None because they are
    VERIFIED-ABSENT from the EHI export — the predecessor emitted alcohol, drug
    use, physical activity, diet, sexual activity, stress, etc. as empty
    placeholders with no source table (issue #7), so we invent nothing."""
    smoking = index.smoking
    return {
        "smoking_status": smoking.value if smoking else None,
        "smoking_date": (
            _fmt_date_short(smoking.recorded_at.date()) if smoking and smoking.recorded_at else None
        ),
        "sh_freetext": index.sh_freetext,
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
    }


def build_record_context(
    record: PatientRecord, cfg: dict[str, Any], record_cache: dict[str, Any]
) -> dict[str, Any]:
    """The RECORD-STATIC half of the context — everything that depends only on
    the record / patient / cfg, NOT on the encounter.

    Built ONCE per record and memoized in ``record_cache`` (so a 30-encounter
    chart assembles these views once, not 30 times). ``build_context`` merges
    this with the per-encounter half; the two key sets are disjoint, so the
    merge is order-independent and the rendered output is byte-identical to the
    prior per-encounter assembly. ``meds_as_of`` is render-day (GOLD §5#9) —
    identical across a record's encounters within one render run.
    """
    cached: dict[str, Any] | None = record_cache.get("pf_record_context")
    if cached is not None:
        return cached

    tz = str(cfg.get("timezone", "America/New_York"))
    sections: dict[str, bool] = cfg.get("sections", {})
    tokens: dict[str, str] = cfg.get("tokens", {})
    pack_root: Path = Path(cfg.get("pack_root", Path(__file__).resolve().parent))
    patient = record.patient

    # Record-level groupings, one pass each (the index is itself memoized so a
    # direct caller of build_record_context still pays for it only once).
    index = record_cache.get("pf_view_index")
    if index is None:
        index = _RecordViewIndex.build(record)
        record_cache["pf_view_index"] = index

    # --- demographics (the unified 6-col table) --------------------------------
    demo = _demographics(patient)
    # PatientContactCode is the one column in v9 that carries a patient's record
    # number, and it lives on patient-superbills — a table the pf_tebra adapter
    # does not map yet, so this reads None on a PF export and the header prints
    # "-". LOUD: the alternative spelling this used to fall back to ("PRN") is a
    # column no v9 table has, and a chain over invented names is how a wrong
    # guess hides (#248). One real name, blank until the table is mapped.
    prn = _ext(patient, "PatientContactCode")

    # --- insurance / payment ---------------------------------------------------
    active_cov = index.active_coverages
    inactive_cov = index.inactive_coverages
    payment = _payment(patient.guarantor)

    # --- diagnoses -------------------------------------------------------------
    current_dx = [_dx_view(c) for c in index.active_conditions]
    historical_dx = [_dx_view(c) for c in index.historical_conditions]

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
    #
    # In the PACK's timezone, through the same `to_local` every other date in
    # this module goes through. It was `date.today()`, which is the SYSTEM local
    # date — so the same record rendered at the same instant on two machines
    # produced two different charts:
    #
    #     TZ=Pacific/Kiritimati  as-of 08/28/2026
    #     TZ=Etc/GMT+12          as-of 08/27/2026
    #
    # A chart that depends on where the operator is sitting is not reproducible,
    # and reproducibility is what the rest of this module's date handling is for.
    #
    # This does NOT settle whether render-day is the right stamp at all: it
    # collides with `DateStalenessCheck`, which reads today's date on an old
    # chart as a template calling now() by mistake, so this pack warns on every
    # document it produces. That half of #194 changes what the chart SAYS and is
    # the maintainer's call; this half is machine-dependence and is not.
    meds_as_of = to_local(_dt.datetime.now(_dt.UTC), tz).strftime("%m/%d/%Y")

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
            "recorded": format_local_dt(d.recorded_at, tz) or "",
        }
        for d in record.advance_directives
        if d.directive
    ]
    active_concerns = [_concern_view(c) for c in index.active_concerns]
    inactive_concerns = [_concern_view(c) for c in index.inactive_concerns]
    active_goals = [_concern_view(g) for g in index.active_goals]
    inactive_goals = [_concern_view(g) for g in index.inactive_goals]

    static: dict[str, Any] = {
        # patient identity (record-level)
        "patient_name": patient.display_name or "Unknown patient",
        "dob": patient.birth_date.strftime("%m/%d/%Y") if patient.birth_date else None,
        "sex": patient.sex,
        "prn": prn,
        "demo": demo,
        "patient_notes": patient.notes,
        # section flags (a pure function of cfg's sections — constant per record)
        **_section_flags(sections),
        # insurance / payment
        "active_insurance": [_coverage_view(c) for c in active_cov],
        "inactive_insurance": [_coverage_view(c) for c in inactive_cov],
        "payment": payment,
        # flowsheet patient name (the column/row data is per-encounter)
        "flowsheet_patient_name": patient.display_name or "",
        # diagnoses / allergies
        "current_diagnoses": current_dx,
        "historical_diagnoses": historical_dx,
        # No reconciliation answer is in a v9 export — the vendor's own column
        # dictionary has no such field across all 85 tables. These stay in the
        # context because a source that DOES carry the answer (a C-CDA, say)
        # would fill them; None means nobody said, and the template answers
        # "not reconstructed" rather than "No selection made", which would be a
        # claim about what the clinician did.
        "diag_recon_text": None,
        "allergy_recon_text": None,
        "drug_allergies": drug_allergies,
        "food_allergies": food_allergies,
        "env_allergies": env_allergies,
        # medications
        "active_medications": active_meds,
        "historical_medications": historical_meds,
        "meds_as_of": meds_as_of,
        "med_recon_text": None,
        # immunizations
        "immunizations": [_immunization_view(i, tz) for i in record.immunizations],
        # social history
        **_social_history_context(patient, index),
        # PMH / family / directives / devices / concerns / goals
        "pmh_sections": pmh_sections,
        "family_history": family_history,
        "family_history_freetext": None,
        "advance_directives": advance_directives,
        "active_concerns": active_concerns,
        "inactive_concerns": inactive_concerns,
        "active_goals": active_goals,
        "inactive_goals": inactive_goals,
        # what a section says when this pack cannot reconstruct it
        "unreconstructed": UNRECONSTRUCTED,
        "unreconstructed_short": UNRECONSTRUCTED_SHORT,
        # logo + tokens
        "logo_data_uri": _logo_data_uri(tokens, pack_root),
        "tokens": tokens,
    }
    record_cache["pf_record_context"] = static
    return static


def build_context(
    encounter: Encounter, record: PatientRecord, cfg: dict[str, Any]
) -> dict[str, Any]:
    tz = str(cfg.get("timezone", "America/New_York"))
    patient = record.patient
    dos = encounter.date_of_service  # calendar date — never timezone-shifted
    # CONTRACT: record_cache is per-record — the engine allocates a fresh dict
    # for each record. A caller must not share one cache across DIFFERENT records
    # (that would mis-render the second). Absent a cache, it builds locally.
    record_cache = record_cache_of(cfg)

    # Record-static views (insurance, payment, diagnoses, allergies, meds,
    # immunizations, social history, demographics, …) are independent of the
    # encounter: build them ONCE per record and reuse across every encounter.
    static = build_record_context(record, cfg, record_cache)
    index = record_cache["pf_view_index"]  # populated by build_record_context

    # --- header: patient / facility / encounter --------------------------------
    age = age_display(patient.birth_date, dos) if patient.birth_date and dos else None
    facility = record.facility(encounter.facility_id)
    seen_by = record.practitioner(encounter.provider_id)
    signer = record.practitioner(encounter.signed_by_id)
    city_state_zip = None
    if facility:
        bits = [facility.city, facility.state, facility.postal_code]
        city_state_zip = " ".join(p for p in bits if p) or None

    # --- vitals ----------------------------------------------------------------
    # observations grouped by encounter once per record (the indexed form of
    # observations_for); .get(id, []) == observations_for(id) exactly.
    obs_by_encounter = observations_by_encounter(record, record_cache)
    enc_vitals = [
        o
        for o in obs_by_encounter.get(encounter.id, [])
        if o.category == ObservationCategory.VITAL_SIGNS
    ]
    enc_vital_rows = _encounter_vital_rows(enc_vitals)
    vitals_obs_dt = next((o.effective_at for o in enc_vitals if o.effective_at), None)

    # vitals flowsheet — prior encounters only, most-recent 10 columns (GOLD §8).
    # The vital-by-encounter scan is built ONCE per record (memoized in
    # record_cache); only the per-encounter DOS cutoff is applied here.
    flowsheet_columns, flowsheet_rows = _build_flowsheet(record, dos, record_cache)

    # --- diagnoses attached to this encounter ----------------------------------
    encounter_dx = _encounter_diagnoses(index.conditions_by_id, encounter)

    # --- screenings / interventions / assessments ------------------------------
    screening_events = [
        _screening_view(e) for e in _screening_events(record, record_cache).get(encounter.id, [])
    ]

    # --- SOAP sections (sanitize_soap_html output rides NoteSection.html) -------
    soap = {s.kind: s for s in encounter.sections}
    subjective = soap.get(SectionKind.SUBJECTIVE) or soap.get(SectionKind.NARRATIVE)
    objective = soap.get(SectionKind.OBJECTIVE)
    assessment = soap.get(SectionKind.ASSESSMENT)
    plan = soap.get(SectionKind.PLAN)

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

    encounter_specific: dict[str, Any] = {
        # header / facility / encounter
        "age": age,
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
        "signed_at": format_local_dt(encounter.signed_at, tz),
        "cc_text": encounter.chief_complaint,
        # vitals
        "enc_vitals_rows": enc_vital_rows,
        "vitals_date": _fmt_date_short(dos),
        "vitals_time": _fmt_time(vitals_obs_dt, tz),
        "flowsheet_columns": flowsheet_columns,
        "flowsheet_rows": flowsheet_rows,
        "flowsheet_vitals_label": bool(flowsheet_columns),
        # diagnoses attached to this encounter
        "encounter_diagnoses": encounter_dx,
        # screenings / interventions / assessments
        "screening_events": screening_events,
        # SOAP
        "subjective_html": subjective.html if subjective else None,
        "objective_html": objective.html if objective else None,
        "assessment_html": assessment.html if assessment else None,
        "plan_html": plan.html if plan else None,
        # orders
        # addenda
        "addendums": addendums,
    }
    # Disjoint key sets: the merge is order-independent and output is unchanged.
    return {**static, **encounter_specific}


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
    """A concern or goal as the template renders it.

    Both cells fall back to the pack's "-" rather than to nothing: a row whose
    description is absent still says a concern EXISTS on this chart, and
    dropping it would lose that. The template interpolates these straight, so
    passing None through would print the literal token ``None`` where a
    clinician reads a diagnosis — a Python repr on a medical record, which is
    worse than an honest blank.
    """
    return {
        "description": obj.description or "-",
        "date": _fmt_date_short(obj.effective) or "-",
    }


def _screening_events(record: PatientRecord, cache: dict[str, Any]) -> dict[str | None, list[Any]]:
    """Screening events grouped by encounter id, built once per record.

    Same shape and the same reason as ``observations_by_encounter``: the pack
    asks per encounter, and a chart with thirty of them should not rescan the
    collection thirty times.
    """
    index: dict[str | None, list[Any]] | None = cache.get("screening_events_by_encounter")
    if index is None:
        index = {}
        for event in record.screening_events:
            index.setdefault(event.encounter_id, []).append(event)
        cache["screening_events_by_encounter"] = index
    return index


def _screening_view(event: Any) -> dict[str, Any]:
    """One Screenings/Interventions/Assessments row.

    ``negated`` reaches the template rather than being resolved into the text
    here: the section prints name, result and comments, and an event the
    clinician marked as not performed has to be readable as such or the row
    claims the opposite of what the export says.

    ``name`` falls back to the pack's "-" for the same reason ``_concern_view``
    does. Result and comments are each wrapped in a template conditional and
    simply vanish when absent, but the name is interpolated bare — an event
    that reached us without one would otherwise print the token ``None`` as the
    name of a screening. The row still has to appear: the export says something
    happened at this visit, and that is the fact worth keeping.
    """
    return {
        "name": event.name or "-",
        "result": event.result,
        "comments": event.comments,
        "negated": event.negated,
    }


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


def _flowsheet_record_index(
    record: PatientRecord,
) -> tuple[dict[str, dict[str, str]], dict[str, _dt.date]]:
    """The vital-by-encounter grouping for the whole record (one pass, no DOS cut).

    Built ONCE per record and memoized in ``record_cache`` (the per-encounter
    flowsheet only applies the DOS cutoff to this). Returns ``(cols, col_dates)``:
    ``cols[encounter_id]`` is ``{vital label: str(value)}`` (last value wins, in
    ``record.observations`` order) and ``col_dates[encounter_id]`` is that
    encounter's date of service — for every encounter with at least one non-null
    vital observation.
    """
    enc_by_id = {e.id: e for e in record.encounters}
    cols: dict[str, dict[str, str]] = {}
    col_dates: dict[str, _dt.date] = {}
    for obs in record.observations:
        if obs.category != ObservationCategory.VITAL_SIGNS or not obs.encounter_id:
            continue
        enc = enc_by_id.get(obs.encounter_id)
        if enc is None or enc.date_of_service is None:
            continue
        label = _LOINC_TO_LABEL.get(obs.code or "", obs.display or "")
        if obs.value is None:
            continue
        cols.setdefault(enc.id, {})[label] = str(obs.value)
        col_dates[enc.id] = enc.date_of_service
    for by_label in cols.values():
        _fold_blood_pressure(by_label)
    return cols, col_dates


def _build_flowsheet(
    record: PatientRecord, dos: _dt.date | None, record_cache: dict[str, Any]
) -> tuple[list[dict[str, str | None]], list[dict[str, Any]]]:
    """Vitals flowsheet: prior encounters only (strictly < current DOS), most
    recent ``_FLOWSHEET_MAX_COLUMNS`` columns, all 11 vital rows shown (GOLD §8).

    The record-wide vital-by-encounter scan is computed once
    (:func:`_flowsheet_record_index`) and cached; only the DOS cutoff, ordering,
    column cap, and row assembly run per encounter. Output is byte-identical to
    the prior per-encounter scan (the cutoff merely moved out of the loop).
    """
    if dos is None:
        return [], []
    cached = record_cache.get("flowsheet_index")
    if cached is None:
        cached = _flowsheet_record_index(record)
        record_cache["flowsheet_index"] = cached
    all_cols, all_col_dates = cached
    # Strictly prior encounters only (the per-encounter DOS cutoff), preserving
    # the record-order of the cached scan so the stable sort tie-breaks identically.
    col_dates = {eid: d for eid, d in all_col_dates.items() if d < dos}
    if not col_dates:
        return [], []
    ordered = sorted(col_dates, key=lambda eid: col_dates[eid], reverse=True)
    ordered = ordered[:_FLOWSHEET_MAX_COLUMNS]
    columns = [{"date": _fmt_date_short(col_dates[eid]), "time": None} for eid in ordered]
    rows: list[dict[str, Any]] = []
    for label in _VITAL_ORDER:
        vals = [all_cols[eid].get(label, "") for eid in ordered]
        if any(vals):
            rows.append({"name": label, "vals": vals})
    # Any vital we did not have an order slot for still renders (lossless), the
    # same guarantee the per-encounter rows carry. First-seen order across the
    # shown columns, so the row order is stable for the goldens.
    extra: list[str] = []
    for eid in ordered:
        for label in all_cols[eid]:
            if label not in _VITAL_ORDER and label not in extra:
                extra.append(label)
    for label in extra:
        vals = [all_cols[eid].get(label, "") for eid in ordered]
        if any(vals):
            rows.append({"name": label, "vals": vals})
    return columns, rows
