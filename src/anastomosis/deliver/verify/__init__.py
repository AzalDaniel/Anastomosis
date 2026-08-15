"""The L0-L6 delivery verification ladder (M2 item 11).

The layered defense that proves a reconstructed chart landed in the right
destination chart, intact and identifiable. It plugs into the browser upload
engine through the existing
:class:`~anastomosis.deliver.browser.verify.Verifier` seam — the engine,
tracking ledger, and state machine are untouched — and is also usable
standalone (a future ``anast verify``), because every level is self-contained.

* :mod:`.levels` — one small class per level (L0-L6) with ``run(...)`` and the
  shared :class:`LevelResult`/:class:`LevelStatus` and matching helpers.
* :mod:`.composite` — :class:`LayeredVerifier`, the stack behind the Verifier
  protocol: ``verify_pre`` runs L0-L4, ``verify_post`` runs L5-L6.

A sibling package to :mod:`anastomosis.deliver.browser`, deliberately: the
ladder depends on the browser package's error taxonomy and Verifier seam, never
the reverse.
"""

from __future__ import annotations

from .composite import ALL_LEVELS, LayeredVerifier
from .levels import (
    L0FileIntegrity,
    L1PageAndSize,
    L2IdentityText,
    L3HeaderFields,
    L4Banner,
    L5Metadata,
    L6RoundTrip,
    LevelResult,
    LevelStatus,
    date_renderings,
    fuzzy_contains,
)

__all__ = [
    "ALL_LEVELS",
    "L0FileIntegrity",
    "L1PageAndSize",
    "L2IdentityText",
    "L3HeaderFields",
    "L4Banner",
    "L5Metadata",
    "L6RoundTrip",
    "LayeredVerifier",
    "LevelResult",
    "LevelStatus",
    "date_renderings",
    "fuzzy_contains",
]
