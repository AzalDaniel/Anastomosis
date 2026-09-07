"""The per-record cache seam every template pack's ``build_context`` shares.

Contract: a ``record_cache`` is PER RECORD; a caller sharing one across
different records mis-renders the second. A pack invoked with no cache
still works, building groupings on a throwaway dict. Also carries
:func:`format_local_dt`, the datetime formatter every SOAP pack's
``signed_at`` uses. Depends on nothing but the canonical model and
``core.timeutil`` (both leaves), so importing this cannot drag the engine in.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from anastomosis.core.model import Observation, ObservationCategory, PatientRecord
from anastomosis.core.timeutil import to_local

__all__ = [
    "format_local_dt",
    "observations_by_encounter",
    "record_cache_of",
    "vitals_elsewhere_in_record",
]


def format_local_dt(value: datetime | None, tz: str) -> str | None:
    """Datetime in practice-local time, no leading zeros (e.g. "Aug 3, 2026 9:05 AM")."""
    if value is None:
        return None
    local = to_local(value, tz)
    return local.strftime("%b %d, %Y %I:%M %p").replace(" 0", " ")


def record_cache_of(cfg: dict[str, Any]) -> dict[str, Any]:
    """The engine's per-record cache from ``cfg``, or a fresh throwaway dict.

    A missing (or non-dict) ``record_cache`` yields a private dict, so every
    memoizing helper below still works — it just does not outlive this one
    ``build_context`` call.
    """
    cache = cfg.get("record_cache")
    return cache if isinstance(cache, dict) else {}


def observations_by_encounter(
    record: PatientRecord, record_cache: dict[str, Any]
) -> dict[str | None, list[Observation]]:
    """Observations grouped by encounter id, built ONCE per record.

    The indexed form of repeated ``record.observations_for(id)`` calls:
    ``.get(encounter_id, [])`` equals ``observations_for(encounter_id)``
    exactly, so a pack swapping to this loses nothing. Memoized in
    ``record_cache`` under ``"obs_by_encounter"`` — a 30-encounter record scans
    its observations once rather than thirty times.
    """
    cached: dict[str | None, list[Observation]] | None = record_cache.get("obs_by_encounter")
    if cached is not None:
        return cached
    grouped = record.observations_by_encounter()
    record_cache["obs_by_encounter"] = grouped
    return grouped


def vitals_elsewhere_in_record(
    record: PatientRecord, encounter_id: str, record_cache: dict[str, Any]
) -> int:
    """Vital signs the record holds that THIS visit did not claim.

    A visit note selects by encounter, so a section that finds nothing prints
    its empty state — and an empty state is a claim. "No vitals recorded" over
    a record holding eight of them, taken at another visit or at none, is the
    chart denying what the record says, which is the one thing this project
    promises not to do. A section cannot fix that by rendering the other
    visits' measurements (they are not this visit's), so it says how many there
    are and points at the record summary that carries them.

    A count, never a value: this number travels into a rendered chart, a golden
    snapshot and a test, and a measurement is the chart.

    Observations attached to NO encounter count too — they are the ones with no
    visit to reach and therefore the ones most at risk of appearing nowhere.
    """
    grouped = observations_by_encounter(record, record_cache)
    return sum(
        1
        for eid, observations in grouped.items()
        if eid != encounter_id
        for observation in observations
        if observation.category == ObservationCategory.VITAL_SIGNS
    )
