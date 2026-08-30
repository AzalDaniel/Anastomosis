"""Context builder for the generic_soap pack."""

from __future__ import annotations

from typing import Any

from anastomosis.core.model import Encounter, ObservationCategory, PatientRecord
from anastomosis.core.timeutil import age_display
from anastomosis.reconstruct.packctx import (
    format_local_dt,
    observations_by_encounter,
    record_cache_of,
    vitals_elsewhere_in_record,
)


def _vitals_elsewhere(
    encounter: Encounter, record: PatientRecord, sections: dict[str, bool], record_cache: Any
) -> int:
    """How many vitals this visit did not claim, or zero if the section is off.

    The layout drops the whole section when a visit has none, and a dropped
    section over a record that holds eight measurements reads as "this patient
    has no vitals" — the same denial an empty-state sentence would make, in a
    quieter voice.

    Zero when the operator switched the section off: a suppressed section makes
    no claim to correct, and disclosing a count there would put back exactly what
    they asked to keep off the page.
    """
    if not sections.get("vitals", True):
        return 0
    return vitals_elsewhere_in_record(record, encounter.id, record_cache)


def build_context(
    encounter: Encounter, record: PatientRecord, cfg: dict[str, Any]
) -> dict[str, Any]:
    tz = str(cfg.get("timezone", "UTC"))
    sections: dict[str, bool] = cfg.get("sections", {})
    patient = record.patient

    dos = encounter.date_of_service  # calendar date — never timezone-shifted
    # Observations grouped by encounter once per record and memoized in the
    # engine's per-record cache (built once per record, not per encounter);
    # .get(id, []) == observations_for(id) exactly. CONTRACT: record_cache is
    # per-record (the engine allocates a fresh dict per record) — do not share
    # one across DIFFERENT records. Falls back to a local build when absent.
    record_cache = record_cache_of(cfg)
    obs_by_encounter = observations_by_encounter(record, record_cache)
    vitals = [
        observation
        for observation in obs_by_encounter.get(encounter.id, [])
        if observation.category == ObservationCategory.VITAL_SIGNS
    ]
    age = age_display(patient.birth_date, dos) if patient.birth_date and dos else None
    signer = record.practitioner(encounter.signed_by_id)

    return {
        "patient": patient,
        "patient_name": patient.display_name or "Unknown patient",
        "dob": patient.birth_date.strftime("%m/%d/%Y") if patient.birth_date else None,
        "age": age,
        "encounter": encounter,
        "dos": dos.strftime("%B %d, %Y") if dos else "Undated",
        "note_sections": [s for s in encounter.sections if not s.is_empty],
        "vitals": vitals if sections.get("vitals", True) else [],
        "vitals_elsewhere": _vitals_elsewhere(encounter, record, sections, record_cache),
        "addenda": encounter.addenda if sections.get("addenda", True) else [],
        "coverages": record.coverages if sections.get("insurance", False) else [],
        "social_history": (
            [o for o in record.observations if o.category == ObservationCategory.SOCIAL_HISTORY]
            if sections.get("social_history", False)
            else []
        ),
        "provider": record.practitioner(encounter.provider_id),
        "signer": signer,
        "signed_at": format_local_dt(encounter.signed_at, tz),
        "facility": record.facility(encounter.facility_id),
        "tokens": cfg.get("tokens", {}),
    }
