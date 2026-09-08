"""Context builder for the practice_fusion_soap pack.

Maps a canonical :class:`PatientRecord` + :class:`Encounter` onto the
template's variables, reproducing the predecessor's PF SOAP-note rendering
rules (GOLD_STANDARD.md, distilled in RULES.md). The canonical model and the
``pf_tebra`` adapter carry the data semantics; the shared view layer
(:mod:`anastomosis.reconstruct.packctx`) carries the formatters and entity
rows. What is left here is what is PF's own: the ESCRIPT/SCRIPT line, the
insurance and demographics grids (both read ``pf_tebra:``-namespaced columns),
the 17 social-history sub-categories, the section flags, and the notice a
section prints when this layout cannot reconstruct it.

The vendor logo is NEVER shipped or referenced (RULES §logo)."""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

from anastomosis.core.clock import now as _clock_now
from anastomosis.core.model import (
    AllergyCategory,
    ContactKind,
    Coverage,
    Encounter,
    IdentifierKind,
    MedicationStatement,
    ObservationCategory,
    Patient,
    PatientRecord,
    Prescription,
    SectionKind,
)
from anastomosis.core.timeutil import age_display, to_local
from anastomosis.reconstruct.packctx import (
    RecordViewIndex,
    addendum_datetime,
    addendum_status,
    allergy_views,
    concern_view,
    diagnosis_view,
    elsewhere_count,
    encounter_diagnoses,
    encounter_vital_rows,
    flowsheet,
    format_copay,
    format_date_long,
    format_date_short,
    format_local_dt,
    format_time,
    immunization_view,
    logo_data_uri,
    medication_display_name,
    observations_by_encounter,
    payment_view,
    record_cache_of,
    screening_events_by_encounter,
    screening_view,
    vitals_elsewhere_in_record,
)

#: Most-recent prior encounters the vitals flowsheet shows (GOLD §8).
_FLOWSHEET_MAX_COLUMNS = 10


def _ext(obj: Any, key: str) -> Any:
    """Read a pf_tebra extension value (namespaced ``pf_tebra:<Column>``)."""
    extensions = getattr(obj, "extensions", None) or {}
    return extensions.get(f"pf_tebra:{key}")


# --- medications ---------------------------------------------------------------


def _start_stop(med: MedicationStatement) -> str:
    """START/STOP cell (GOLD §5#6): both, only-stop (historical), only-start, '-'."""
    start = format_date_short(med.start)
    stop = format_date_short(med.stop)
    if start and stop:
        return f"{start} - {stop}"
    if stop:
        return f"- {stop}"
    return start or "-"


def _escript_line(rx: Prescription, record: PatientRecord, tz: str) -> dict[str, str]:
    """One ESCRIPT/SCRIPT line. Prefix and status come from the adapter's
    transaction-priority resolution (escript.py); the displayed date is the
    adapter-resolved display_date (Order-sent→Eastern for ESCRIPT) as MM/DD/YY."""
    prescriber = record.practitioner(rx.prescriber_id)
    display = rx.display_date
    if isinstance(display, _dt.datetime):
        date_str = to_local(display, tz).strftime("%m/%d/%y")
    else:
        date_str = format_date_short(display) or "-"
    return {
        "prefix": rx.prefix or "ESCRIPT",
        "status": rx.status_label or "VERIFIED",
        "date": date_str,
        "prescriber": prescriber.name if prescriber else "-",
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
        "name": medication_display_name(med),
        "sig": med.sig,
        "start_stop": _start_stop(med),
        "assoc_dx": med.associated_dx,
        "escripts": escripts,
    }


# --- insurance -----------------------------------------------------------------


def _coverage_view(cov: Coverage) -> dict[str, str]:
    """One insurance row. Sub-header is ``{PRIORITY} PAYER - {COVERAGE}`` (GOLD §7).
    TYPE is the adapter-resolved plan_type (superbill PlanType join), shown '-'
    when unresolved — never the generic coverage_type "Medical"."""
    priority = (cov.priority_label or "").strip()
    coverage = (cov.coverage_type or "MEDICAL").upper()
    return {
        "sub_header": f"{priority} - {coverage}" if priority else coverage,
        "payer": cov.payer or "-",
        "member_id": cov.member_id or "-",
        "priority": priority or "-",
        "group_number": cov.group_number or "-",
        "type": cov.plan_type or "-",
        "employer_name": cov.employer or "-",
        "relationship": cov.relationship_to_insured or "-",
        "ins_payment_type": _ext(cov, "InsurancePaymentType") or "-",
        "start_date": format_date_short(cov.start) or "-",
        "payment_type": cov.payment_type or "-",
        "end_date": format_date_short(cov.end) or "-",
        "copay": format_copay(cov.copay),
        "status": cov.status_label or ("Active" if cov.active else "Inactive"),
    }


# --- demographics --------------------------------------------------------------


def _demographics(patient: Patient) -> dict[str, Any]:
    """The unified 6-column demographics table, a pure function of the patient.

    ``DeathDate`` is what patient-demographics spells it; ``DateOfDeath`` is a
    name no v9 table has, so the DATE OF DEATH cell printed "-" over an export
    that carried the date."""
    home_addr = patient.addresses[0] if patient.addresses else None
    kin = patient.contacts[0] if patient.contacts else None
    telecom = {cp.kind: cp.value for cp in patient.telecom}
    return {
        "first_name": patient.given_name,
        "middle_name": patient.middle_name,
        "last_name": patient.family_name,
        "sex": patient.sex,
        "dob": patient.birth_date.strftime("%m/%d/%Y") if patient.birth_date else None,
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


# --- sections ------------------------------------------------------------------

#: What a section says when this pack cannot reconstruct it.
#:
#: The reason these sections are static is NOT that v9 has no data path. It
#: does: `patient-healthcare-devices` is a 31-column table, `patient-lab-orders`
#: and `patient-lab-order-items` carry the orders (LabType separates diagnostic
#: from imaging), and `patient-encounter-observations` carries the observations.
#: What is missing is the ROW LAYOUT — the forensic gold standard preserves no
#: populated example of these sections, so the pack does not invent one. The
#: notice therefore claims nothing either way: it says the layout is unknown and
#: points at where the data actually is, which is true whether the export
#: carried anything or not. A vendor empty state here ("No implantable devices
#: recorded") would assert a negative the source contradicts.
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


def _social_history_context(patient: Patient, index: RecordViewIndex) -> dict[str, Any]:
    """Social-history block: smoking and the free-text block come from the
    record (`patient-smokingstatus`, the `social`-kind `patient-med-history`).
    The structured subcategories stay None because they are VERIFIED-ABSENT
    from the EHI export (issue #7): there is no source table, so nothing is
    invented for them."""
    smoking = index.smoking
    return {
        "smoking_status": smoking.value if smoking else None,
        "smoking_date": (
            format_date_short(smoking.recorded_at.date())
            if smoking and smoking.recorded_at
            else None
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


# --- the two halves of the context ---------------------------------------------


def build_record_context(
    record: PatientRecord, cfg: dict[str, Any], record_cache: dict[str, Any]
) -> dict[str, Any]:
    """The RECORD-STATIC half: everything that depends only on the record,
    patient or cfg, and not on the encounter.

    Built ONCE per record and memoized, so a 30-encounter chart assembles these
    views once. ``build_context`` merges this with the per-encounter half; the
    two key sets are disjoint, so the merge is order-independent."""
    cached: dict[str, Any] | None = record_cache.get("pf_record_context")
    if cached is not None:
        return cached

    tz = str(cfg.get("timezone", "America/New_York"))
    sections: dict[str, bool] = cfg.get("sections", {})
    tokens: dict[str, str] = cfg.get("tokens", {})
    pack_root: Path = Path(cfg.get("pack_root", Path(__file__).resolve().parent))
    patient = record.patient

    # Record-level groupings, one pass each (memoized so a direct caller of
    # build_record_context still pays for them only once).
    index = record_cache.get("pf_view_index")
    if index is None:
        index = RecordViewIndex.build(record)
        record_cache["pf_view_index"] = index

    # PatientContactCode is the one v9 column carrying a patient's record
    # number, and it lives on patient-superbills — a table the pf_tebra adapter
    # does not map yet, so this reads None on a PF export and the header prints
    # "-". LOUD: "PRN" is a column name no v9 table has, and a chain over
    # invented names is how a wrong guess hides (#248). One real name, blank
    # until the table is mapped.
    prn = _ext(patient, "PatientContactCode")

    rx_by_id = index.prescriptions_by_id
    allergies = index.allergies_by_category
    # "as of" = render-day, NOT encounter date (GOLD §5#9), in the PACK's
    # timezone through the same `to_local` every other date here goes through.
    # `date.today()` is the SYSTEM local date, so one record rendered at one
    # instant on two machines produced two different charts. This does NOT
    # settle whether render-day is the right stamp at all: it collides with
    # `DateStalenessCheck`, which reads today's date on an old chart as a
    # template calling now() by mistake, so this pack warns on every document it
    # produces. That half of #194 changes what the chart SAYS and is the
    # maintainer's call; this half is machine-dependence and is not.
    meds_as_of = to_local(_clock_now(), tz).strftime("%m/%d/%Y")

    static: dict[str, Any] = {
        # patient identity (record-level)
        "patient_name": patient.display_name or "Unknown patient",
        "dob": patient.birth_date.strftime("%m/%d/%Y") if patient.birth_date else None,
        "sex": patient.sex,
        "prn": prn,
        "demo": _demographics(patient),
        "patient_notes": patient.notes,
        # section flags (a pure function of cfg's sections — constant per record)
        **_section_flags(sections),
        # insurance / payment
        "active_insurance": [_coverage_view(c) for c in index.active_coverages],
        "inactive_insurance": [_coverage_view(c) for c in index.inactive_coverages],
        "payment": payment_view(patient.guarantor),
        # flowsheet patient name (the column/row data is per-encounter)
        "flowsheet_patient_name": patient.display_name or "",
        # diagnoses / allergies
        "current_diagnoses": [diagnosis_view(c) for c in index.active_conditions],
        "historical_diagnoses": [diagnosis_view(c) for c in index.historical_conditions],
        # No reconciliation answer is in a v9 export — the vendor's own column
        # dictionary has no such field across all 85 tables. These stay in the
        # context because a source that DOES carry the answer (a C-CDA, say)
        # would fill them; None means nobody said, and the template answers
        # "not reconstructed" rather than "No selection made", which would be a
        # claim about what the clinician did.
        "diag_recon_text": None,
        "allergy_recon_text": None,
        "drug_allergies": allergy_views(allergies.get(AllergyCategory.DRUG, [])),
        "food_allergies": allergy_views(allergies.get(AllergyCategory.FOOD, [])),
        "env_allergies": allergy_views(allergies.get(AllergyCategory.ENVIRONMENT, [])),
        # medications
        "active_medications": [
            _medication_view(m, rx_by_id, record, tz) for m in index.active_medications
        ],
        "historical_medications": [
            _medication_view(m, rx_by_id, record, tz) for m in index.historical_medications
        ],
        "meds_as_of": meds_as_of,
        "med_recon_text": None,
        # immunizations
        "immunizations": [immunization_view(i, tz) for i in record.immunizations],
        # social history
        **_social_history_context(patient, index),
        # PMH / family / directives / concerns / goals
        "pmh_sections": [
            {"type": (p.kind or "HISTORY").upper(), "text": p.text}
            for p in record.past_medical_history
            if not (p.kind or "").lower().startswith("social") and (p.text or "").strip()
        ],
        "family_history": [
            {"diagnosis": f.diagnosis, "onset": format_date_short(f.onset_date)}
            for f in record.family_history
            if f.diagnosis
        ],
        "family_history_freetext": None,
        "advance_directives": [
            {"directive": d.directive, "recorded": format_local_dt(d.recorded_at, tz) or ""}
            for d in record.advance_directives
            if d.directive
        ],
        "active_concerns": [concern_view(c) for c in index.active_concerns],
        "inactive_concerns": [concern_view(c) for c in index.inactive_concerns],
        "active_goals": [concern_view(g) for g in index.active_goals],
        "inactive_goals": [concern_view(g) for g in index.inactive_goals],
        # what a section says when this pack cannot reconstruct it
        "unreconstructed": UNRECONSTRUCTED,
        "unreconstructed_short": UNRECONSTRUCTED_SHORT,
        # logo + tokens
        "logo_data_uri": logo_data_uri(tokens, pack_root),
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
    # for each record. A caller must not share one across DIFFERENT records
    # (that would mis-render the second). Absent a cache, it builds locally.
    record_cache = record_cache_of(cfg)

    # Record-static views are independent of the encounter: built ONCE per
    # record and reused across every encounter.
    static = build_record_context(record, cfg, record_cache)
    index: RecordViewIndex = record_cache["pf_view_index"]  # set by build_record_context

    facility = record.facility(encounter.facility_id)
    seen_by = record.practitioner(encounter.provider_id)
    signer = record.practitioner(encounter.signed_by_id)
    city_state_zip = None
    if facility:
        bits = [facility.city, facility.state, facility.postal_code]
        city_state_zip = " ".join(p for p in bits if p) or None

    enc_vitals = [
        o
        for o in observations_by_encounter(record, record_cache).get(encounter.id, [])
        if o.category == ObservationCategory.VITAL_SIGNS
    ]
    vitals_obs_dt = next((o.effective_at for o in enc_vitals if o.effective_at), None)
    # Both vitals blocks print an absence when they find nothing — "No vitals
    # recorded", "No events recorded for Vitals." — and over a record that HAS
    # vitals those sentences deny what the record says. They keep their voice
    # and gain the count plus a pointer to the record summary that carries them.
    vitals_elsewhere = vitals_elsewhere_in_record(record, encounter.id, record_cache)
    flowsheet_columns, flowsheet_rows = flowsheet(
        record, dos, record_cache, max_columns=_FLOWSHEET_MAX_COLUMNS
    )

    screenings_by_encounter = screening_events_by_encounter(record, record_cache)
    soap = {s.kind: s for s in encounter.sections}
    subjective = soap.get(SectionKind.SUBJECTIVE) or soap.get(SectionKind.NARRATIVE)
    objective = soap.get(SectionKind.OBJECTIVE)
    assessment = soap.get(SectionKind.ASSESSMENT)
    plan = soap.get(SectionKind.PLAN)
    age = age_display(patient.birth_date, dos) if patient.birth_date and dos else None

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
        "dos": format_date_long(dos) or "Undated",
        "age_at_dos": age,
        "signed_by_name": signer.name if signer else None,
        "signed_by_credential": signer.credential if signer else None,
        "signed_at": format_local_dt(encounter.signed_at, tz),
        "cc_text": encounter.chief_complaint,
        # vitals
        "enc_vitals_rows": encounter_vital_rows(enc_vitals),
        "vitals_elsewhere": vitals_elsewhere,
        "vitals_date": format_date_short(dos),
        "vitals_time": format_time(vitals_obs_dt, tz),
        "flowsheet_columns": flowsheet_columns,
        "flowsheet_rows": flowsheet_rows,
        "flowsheet_vitals_label": bool(flowsheet_columns),
        # diagnoses attached to this encounter
        "encounter_diagnoses": encounter_diagnoses(index.conditions_by_id, encounter),
        # screenings / interventions / assessments
        "screening_events": [
            screening_view(e) for e in screenings_by_encounter.get(encounter.id, [])
        ],
        "screenings_elsewhere": elsewhere_count(screenings_by_encounter, encounter.id),
        # SOAP (sanitize_soap_html output rides NoteSection.html)
        "subjective_html": subjective.html if subjective else None,
        "objective_html": objective.html if objective else None,
        "assessment_html": assessment.html if assessment else None,
        "plan_html": plan.html if plan else None,
        # addenda (conditional)
        "addendums": [
            {
                "text": a.text or "",
                "status": addendum_status(a),
                "source": a.source or "",
                "datetime": addendum_datetime(a.at, tz),
            }
            for a in encounter.addenda
            if (a.text or "").strip()
        ],
    }
    # Disjoint key sets: the merge is order-independent and output is unchanged.
    return {**static, **encounter_specific}
