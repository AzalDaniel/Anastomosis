"""The per-record cache seam every template pack's ``build_context`` shares.

The engine allocates ONE ``record_cache`` dict per record and passes it in
``cfg`` (see :meth:`anastomosis.reconstruct.engine.ReconstructionEngine.run`),
so a pack can build a record-level grouping once instead of once per encounter.
Two things about that seam are pack-independent — reading the cache out of
``cfg`` at all, and the observations-by-encounter grouping every SOAP pack
needs for its vitals. They live here so a third pack inherits the same
semantics instead of re-deriving them.

CONTRACT (load-bearing, repeated at every call site): a ``record_cache`` is
**per record**. The engine allocates a fresh dict for each record; a caller that
shares one across DIFFERENT records mis-renders the second. A pack invoked
without a cache (a direct ``build_context`` call in a test or a tool) still
works — it just builds its groupings locally, on a throwaway dict.

A third pack-independent helper lives here too: :func:`format_local_dt`, the
practice-local datetime formatter every built-in SOAP pack's ``signed_at``
field uses, re-typed identically in each before it moved here.

Packs are exec'd from file paths but import :mod:`anastomosis` freely; this
module deliberately depends on nothing but the canonical model and
:mod:`anastomosis.core.timeutil` — both leaf modules — so importing it from a
pack cannot drag the engine or the registry into a pack's namespace.
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
