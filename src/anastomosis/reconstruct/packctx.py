"""The view layer every template pack's ``build_context`` shares.

Formatters, the vitals code table and its two views, the per-record groupings
and their memoizing cache seam, the entity row builders, and the pack logo
resolver. Contract: a ``record_cache`` is PER RECORD; a caller sharing one
across different records mis-renders the second, and a pack invoked with no
cache builds on a throwaway dict. Depends on nothing but the canonical model
and ``core.timeutil`` (both leaves), so a pack importing this cannot drag the
engine in — and so nothing here may know a source adapter's own namespace."""

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
    MedicationStatement,
    Observation,
    ObservationCategory,
    PatientRecord,
    Prescription,
)
from anastomosis.core.timeutil import to_local

__all__ = [
    "VITAL_LABELS",
    "VITAL_ORDER",
    "RecordViewIndex",
    "addendum_datetime",
    "addendum_status",
    "allergy_views",
    "benefit_order_key",
    "concern_view",
    "diagnosis_view",
    "elsewhere_count",
    "encounter_diagnoses",
    "encounter_vital_rows",
    "flowsheet",
    "fold_blood_pressure",
    "format_copay",
    "format_date_long",
    "format_date_short",
    "format_local_dt",
    "format_time",
    "guarantor_address",
    "immunization_view",
    "logo_data_uri",
    "medication_display_name",
    "observations_by_encounter",
    "payment_view",
    "record_cache_of",
    "screening_events_by_encounter",
    "screening_view",
    "vitals_elsewhere_in_record",
]


# --- dates and times -----------------------------------------------------------


def format_local_dt(value: _dt.datetime | None, tz: str) -> str | None:
    """Datetime in practice-local time, no leading zeros (e.g. "Aug 3, 2026 9:05 AM")."""
    if value is None:
        return None
    local = to_local(value, tz)
    return local.strftime("%b %d, %Y %I:%M %p").replace(" 0", " ")


def format_date_short(value: _dt.date | None) -> str | None:
    """MM/DD/YY, the 2-digit-year form a clinical table cell carries."""
    return value.strftime("%m/%d/%y") if value else None


def format_date_long(value: _dt.date | None) -> str | None:
    """``January 02, 2020`` — the spelled-out form a note header carries."""
    return value.strftime("%B %d, %Y") if value else None


def format_time(value: _dt.datetime | None, tz: str) -> str | None:
    """h:mm AM/PM (no leading zero) in practice-local time."""
    if value is None:
        return None
    return to_local(value, tz).strftime("%I:%M %p").lstrip("0")


def addendum_datetime(value: _dt.datetime | None, tz: str) -> str:
    """MM/DD/YYYY hh:mm am/pm, lowercase am/pm, zero-padded hour."""
    if value is None:
        return ""
    local = to_local(value, tz)
    return local.strftime("%m/%d/%Y %I:%M %p").replace("AM", "am").replace("PM", "pm")


# --- the per-record cache seam -------------------------------------------------


def record_cache_of(cfg: dict[str, Any]) -> dict[str, Any]:
    """The engine's per-record cache from ``cfg``, or a fresh throwaway dict.

    A missing (or non-dict) ``record_cache`` yields a private dict, so every
    memoizing helper below still works — it just does not outlive this one
    ``build_context`` call."""
    cache = cfg.get("record_cache")
    return cache if isinstance(cache, dict) else {}


def observations_by_encounter(
    record: PatientRecord, record_cache: dict[str, Any]
) -> dict[str | None, list[Observation]]:
    """Observations grouped by encounter id, built ONCE per record.

    ``.get(encounter_id, [])`` equals ``observations_for(encounter_id)``
    exactly, so a pack swapping to this loses nothing. A 30-encounter record
    scans its observations once rather than thirty times."""
    cached: dict[str | None, list[Observation]] | None = record_cache.get("obs_by_encounter")
    if cached is not None:
        return cached
    grouped = record.observations_by_encounter()
    record_cache["obs_by_encounter"] = grouped
    return grouped


def screening_events_by_encounter(
    record: PatientRecord, record_cache: dict[str, Any]
) -> dict[str | None, list[Any]]:
    """Screening events grouped by encounter id, built once per record —
    the same shape and the same reason as :func:`observations_by_encounter`."""
    index: dict[str | None, list[Any]] | None = record_cache.get("screening_events_by_encounter")
    if index is None:
        index = {}
        for event in record.screening_events:
            index.setdefault(event.encounter_id, []).append(event)
        record_cache["screening_events_by_encounter"] = index
    return index


def elsewhere_count(by_encounter: dict[str | None, list[Any]], encounter_id: str) -> int:
    """How many items of an encounter-grouped family belong to some OTHER visit.

    A section that finds nothing here makes a claim, true only when the record
    holds nothing of that family either. Items under ``None`` count: they have
    no visit to reach. A count, never a value — this is rendered onto a chart."""
    return sum(len(items) for eid, items in by_encounter.items() if eid != encounter_id)


def vitals_elsewhere_in_record(
    record: PatientRecord, encounter_id: str, record_cache: dict[str, Any]
) -> int:
    """Vital signs the record holds that THIS visit did not claim.

    "No vitals recorded" over a record holding eight of them, taken at another
    visit or at none, is the chart denying what the record says. A section
    cannot fix that by rendering another visit's measurements, so it says how
    many there are and points at the record summary. A count, never a value."""
    grouped = observations_by_encounter(record, record_cache)
    return sum(
        1
        for eid, observations in grouped.items()
        if eid != encounter_id
        for observation in observations
        if observation.category == ObservationCategory.VITAL_SIGNS
    )


# --- vitals --------------------------------------------------------------------

#: Display order for a vitals table. Blood Pressure is the combined sys/dia row.
VITAL_ORDER = [
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

#: Canonical ``Observation.code`` (LOINC) -> vitals display label. The codes are
#: ``core.codes.VITALS``' primaries plus their accepted aliases, so a vital
#: charted under either LOINC edition lands on the right row.
VITAL_LABELS: dict[str, str] = {
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


def fold_blood_pressure(by_label: dict[str, str]) -> None:
    """Fold the two BP components into the one row :data:`VITAL_ORDER` names.

    The observations arrive as separate LOINC codes (8480-6 / 8462-4) and the
    order names only the combined row, so a path that skips this fold renders
    neither. In place: every caller wants one key out of its label map."""
    systolic = by_label.pop("Systolic BP", None)
    diastolic = by_label.pop("Diastolic BP", None)
    if systolic or diastolic:
        # strip units off the BP components for the combined "sys/dia" cell
        sys_v = (systolic or "").split(" ")[0]
        dia_v = (diastolic or "").split(" ")[0]
        by_label["Blood Pressure"] = f"{sys_v}/{dia_v}".strip("/")


def encounter_vital_rows(vitals: list[Observation]) -> list[dict[str, str]]:
    """One visit's vitals rows in :data:`VITAL_ORDER`, blood pressure combined.

    A vital with no order slot still renders, after the ordered ones: the row is
    lossless before it is tidy."""
    by_label: dict[str, str] = {}
    for obs in vitals:
        label = VITAL_LABELS.get(obs.code or "", obs.display or obs.code or "")
        value = obs.value
        if value is None:
            continue
        unit = obs.unit or ""
        by_label[label] = f"{value} {unit}".strip() if unit else str(value)
    fold_blood_pressure(by_label)
    rows: list[dict[str, str]] = [
        {"name": label, "value": by_label[label]} for label in VITAL_ORDER if label in by_label
    ]
    rows.extend(
        {"name": label, "value": value}
        for label, value in by_label.items()
        if label not in VITAL_ORDER
    )
    return rows


def _flowsheet_index(
    record: PatientRecord,
) -> tuple[dict[str, dict[str, str]], dict[str, _dt.date]]:
    """The vital-by-encounter grouping for the whole record, one pass, no cutoff.

    ``cols[encounter_id]`` is ``{vital label: value}`` (last value wins, in
    ``record.observations`` order) and ``col_dates[encounter_id]`` its date of
    service, for every encounter carrying at least one non-null vital."""
    enc_by_id = {e.id: e for e in record.encounters}
    cols: dict[str, dict[str, str]] = {}
    col_dates: dict[str, _dt.date] = {}
    for obs in record.observations:
        if obs.category != ObservationCategory.VITAL_SIGNS or not obs.encounter_id:
            continue
        enc = enc_by_id.get(obs.encounter_id)
        if enc is None or enc.date_of_service is None or obs.value is None:
            continue
        label = VITAL_LABELS.get(obs.code or "", obs.display or "")
        cols.setdefault(enc.id, {})[label] = str(obs.value)
        col_dates[enc.id] = enc.date_of_service
    for by_label in cols.values():
        fold_blood_pressure(by_label)
    return cols, col_dates


def flowsheet(
    record: PatientRecord,
    dos: _dt.date | None,
    record_cache: dict[str, Any],
    *,
    max_columns: int,
) -> tuple[list[dict[str, str | None]], list[dict[str, Any]]]:
    """Vitals flowsheet: strictly prior encounters, most recent ``max_columns``.

    The record-wide scan is cached; only the cutoff, ordering, cap and row
    assembly run per encounter. A vital with no order slot still renders, in
    first-seen order across the shown columns, so a golden stays stable."""
    if dos is None:
        return [], []
    cached = record_cache.get("flowsheet_index")
    if cached is None:
        cached = _flowsheet_index(record)
        record_cache["flowsheet_index"] = cached
    all_cols, all_col_dates = cached
    # Record order preserved, so the stable sort below tie-breaks identically.
    col_dates = {eid: d for eid, d in all_col_dates.items() if d < dos}
    if not col_dates:
        return [], []
    ordered = sorted(col_dates, key=lambda eid: col_dates[eid], reverse=True)[:max_columns]
    columns = [{"date": format_date_short(col_dates[eid]), "time": None} for eid in ordered]
    extra: list[str] = []
    for eid in ordered:
        extra.extend(
            label for label in all_cols[eid] if label not in VITAL_ORDER and label not in extra
        )
    rows: list[dict[str, Any]] = []
    for label in [*VITAL_ORDER, *extra]:
        vals = [all_cols[eid].get(label, "") for eid in ordered]
        if any(vals):
            rows.append({"name": label, "vals": vals})
    return columns, rows


# --- record-level groupings ----------------------------------------------------


def benefit_order_key(cov: Coverage) -> int:
    """Coverage sort key: OrderOfBenefits ascending, unknown last."""
    return cov.order_of_benefits if cov.order_of_benefits is not None else 99


@dataclass(frozen=True)
class RecordViewIndex:
    """Record-level groupings, precomputed in one pass per collection.

    Built per call; the flowsheet and the per-encounter vitals stay
    encounter-specific. Each split preserves its source collection's order,
    then the coverages are sorted by benefit order."""

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
    def build(cls, record: PatientRecord) -> RecordViewIndex:
        active_cov: list[Coverage] = []
        inactive_cov: list[Coverage] = []
        for cov in record.coverages:
            (active_cov if cov.active else inactive_cov).append(cov)
        active_cov.sort(key=benefit_order_key)  # stable: ties keep source order
        inactive_cov.sort(key=benefit_order_key)

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


# --- entity rows ---------------------------------------------------------------


def medication_display_name(med: MedicationStatement) -> str:
    """``Generic (Brand) Strength Route DoseForm``.

    A display name the adapter already stored wins: it is the source's own
    MedicationName. Otherwise the parens are omitted when generic == trade, and
    a brand-only name is the last fallback."""
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


def diagnosis_view(condition: Any) -> dict[str, str | None]:
    """One diagnosis row: description, acuity, start and stop."""
    return {
        "description": condition.display,
        "acuity": condition.acuity or "-",
        "start": format_date_short(condition.onset) or "-",
        "stop": format_date_short(condition.stopped) or "-",
    }


def encounter_diagnoses(
    conditions_by_id: dict[str, Any], encounter: Encounter
) -> list[dict[str, str]]:
    """The diagnoses this encounter attaches, with their codes in one cell."""
    out: list[dict[str, str]] = []
    for dx_id in encounter.diagnosis_ids:
        condition = conditions_by_id.get(dx_id)
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


def allergy_views(items: list[Any]) -> dict[str, list[dict[str, str | None]]]:
    """Split one allergy category's items into active/inactive view rows. The
    caller passes the pre-grouped list from :class:`RecordViewIndex`."""

    def view(a: Any) -> dict[str, str | None]:
        reactions = ", ".join(a.reactions) if a.reactions else None
        severity_reactions = " / ".join(p for p in (a.severity, reactions) if p)
        return {
            "name": a.substance,
            "severity_reactions": severity_reactions or "-",
            "onset": format_date_short(a.onset) or "-",
        }

    return {
        "active": [view(a) for a in items if a.active],
        "inactive": [view(a) for a in items if not a.active],
    }


def concern_view(obj: Any) -> dict[str, str | None]:
    """A health concern or goal as a two-cell row.

    Both cells fall back to "-": a row with no description still says a concern
    EXISTS on this chart, and a template interpolating None would print the
    literal token ``None`` where a clinician reads a diagnosis."""
    return {
        "description": obj.description or "-",
        "date": format_date_short(obj.effective) or "-",
    }


def screening_view(event: Any) -> dict[str, Any]:
    """One Screenings/Interventions/Assessments row.

    ``negated`` reaches the template rather than being resolved into the text
    here: an event marked as not performed has to be readable as such or the
    row claims the opposite of the export. ``name`` falls back like a concern."""
    return {
        "name": event.name or "-",
        "result": event.result,
        "comments": event.comments,
        "negated": event.negated,
    }


def immunization_view(imm: Any, tz: str) -> dict[str, str | None]:
    """One immunization row: date, vaccine, source, lot, expiry, comment."""
    return {
        "date": format_date_short(imm.administered_on) or "-",
        "vaccine": imm.vaccine or "-",
        "source": imm.source or "-",
        "lot": imm.lot_number or "-",
        "expires": format_date_short(imm.expires) or "-",
        "comment": imm.comment or "",
    }


def addendum_status(addendum: Any) -> str:
    """``{Status} by {Author}\\n{Credential}``, empty parts dropped."""
    status = addendum.status or ""
    author = addendum.author_name or ""
    line1 = f"{status} by {author}".strip() if (status or author) else ""
    credential = addendum.author_credential or ""
    return f"{line1}\n{credential}".strip() if credential else line1


# --- guarantor and payment -----------------------------------------------------


def format_copay(value: str | None) -> str:
    """Copay: "-" for the null sentinel or empty, integers without decimals,
    otherwise the shortest representation."""
    if value is None or value == "":
        return "-"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    if number.is_integer():
        return str(int(number))
    return f"{number:g}"


def guarantor_address(addr: Address | None) -> str:
    """Comma-joined line1, city, state, zip — only when line1 exists."""
    if addr is None or not addr.line1:
        return "-"
    return ", ".join(p for p in (addr.line1, addr.city, addr.state, addr.postal_code) if p)


def payment_view(guarantor: Guarantor | None) -> dict[str, str]:
    """The payment-information cells and their empty states.

    Every absent value renders "-" except PAYMENT PREFERENCE, which defaults to
    ``Primary Insurance``. Never emits None: a template interpolates these raw.
    A source that does not tag guarantor phone kinds is read positionally."""
    phones = guarantor.phones if guarantor else []
    by_kind = {p.kind: p.value for p in phones}
    if ContactKind.PHONE_HOME in by_kind or ContactKind.PHONE_OTHER in by_kind:
        primary = by_kind.get(ContactKind.PHONE_HOME)
        secondary = by_kind.get(ContactKind.PHONE_OTHER)
    else:
        primary = phones[0].value if phones else None
        secondary = phones[1].value if len(phones) > 1 else None
    return {
        "preference": (guarantor.payment_preference if guarantor else None) or "Primary Insurance",
        "relationship": (guarantor.relationship_to_patient if guarantor else None) or "-",
        "guarantor_name": (guarantor.name if guarantor else None) or "-",
        "guarantor_addr": guarantor_address(guarantor.address if guarantor else None),
        "dob": (
            guarantor.birth_date.strftime("%m/%d/%Y") if guarantor and guarantor.birth_date else "-"
        ),
        "sex": (guarantor.sex if guarantor else None) or "-",
        "ssn": (guarantor.ssn if guarantor else None) or "-",
        "primary_phone": primary or "-",
        "secondary_phone": secondary or "-",
    }


# --- pack assets ---------------------------------------------------------------


def logo_data_uri(cfg_tokens: dict[str, str], pack_root: Path) -> str:
    """Resolve a pack's logo to a data URI; "" when there is none to read.

    Only an inline ``data:`` override is honoured — http/https/file would make
    Chromium fetch it at render time, an outbound request from a page full of
    PHI. An asset outside the pack root is refused; an unreadable one is empty."""
    override = cfg_tokens.get("logo_data_uri")
    if override and override.startswith("data:"):
        return override
    asset = cfg_tokens.get("logo_asset", "assets/placeholder_logo.svg")
    path = (pack_root / asset).resolve()
    if not path.is_relative_to(pack_root.resolve()):
        return ""
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    return f"data:image/svg+xml;base64,{base64.b64encode(raw).decode('ascii')}"
