"""The L0-L6 delivery verification ladder (M2 item 11).

Proves a reconstructed chart landed in the right destination chart, intact
and identifiable, plugging into the browser upload engine through the
:class:`~anastomosis.deliver.browser.verify.Verifier` seam; every level is
self-contained, so it is also usable standalone. A sibling package to
:mod:`anastomosis.deliver.browser`: the ladder depends on it, never the
reverse (54).
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
    "fuzzy_contains",
]
