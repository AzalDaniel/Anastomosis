"""The per-record cache seam every template pack's ``build_context`` shares.

The engine allocates ONE ``record_cache`` dict per record and passes it in
``cfg`` (see :meth:`anastomosis.reconstruct.engine.ReconstructionEngine.run`),
so a pack can build a record-level grouping once instead of once per encounter.
Two things about that seam are pack-independent — reading the cache out of
``cfg`` at all, and the observations-by-encounter grouping every SOAP pack
needs for its vitals — and both were re-typed identically in each built-in
pack. They live here so a third pack inherits the same semantics (and the same
fallback) instead of re-deriving them.

CONTRACT (load-bearing, repeated at every call site): a ``record_cache`` is
**per record**. The engine allocates a fresh dict for each record; a caller that
shares one across DIFFERENT records mis-renders the second. A pack invoked
without a cache (a direct ``build_context`` call in a test or a tool) still
works — it just builds its groupings locally, on a throwaway dict.

Packs are exec'd from file paths but import :mod:`anastomosis` freely; this
module deliberately depends on nothing but the canonical model, so importing it
from a pack cannot drag the engine or the registry into a pack's namespace.
"""

from __future__ import annotations

from typing import Any

from anastomosis.core.model import Observation, PatientRecord

__all__ = ["observations_by_encounter", "record_cache_of"]


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
