"""Contract: imports nothing from the project (54)."""

from __future__ import annotations

from enum import StrEnum
from typing import TypedDict

__all__ = ["LevelCoverage", "VerifyPolicy"]


class VerifyPolicy(StrEnum):
    """What may honestly be checked about one manifest item's bytes: a
    rendered chart gets the whole L0-L6 ladder, a paged source document
    gets L0 re-hash + L1 exact page count, an opaque source gets L0 only
    (46). A level that cannot honestly run says so; it never passes.
    """

    RENDERED_CHART = "rendered_chart"
    SOURCE_PAGED = "source_paged"
    SOURCE_OPAQUE = "source_opaque"

    @property
    def is_source_document(self) -> bool:
        """Whether these bytes came from the source rather than from a render."""
        return self is not VerifyPolicy.RENDERED_CHART


class LevelCoverage(TypedDict):
    """Aggregate verification outcome for one L-level across a run:
    counts and deduplicated skip-reason strings only, never an item key,
    patient value, or path (49).
    """

    pass_count: int
    fail_count: int
    skip_count: int
    skip_reasons: list[str]
