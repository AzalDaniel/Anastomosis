"""Lightweight value types shared between the verifier and its consumers.

This module deliberately imports nothing from the rest of the project, so the
upload-report writer can depend on the verifier's TypedDict shapes without
pulling :class:`~anastomosis.deliver.verify.composite.LayeredVerifier` (which
in turn pulls :mod:`anastomosis.deliver.browser.errors`) and triggering a
circular import (verify.composite -> browser.errors -> browser/__init__ ->
browser.reports -> verify.composite) that only surfaces in a fresh
interpreter. Keeping :class:`LevelCoverage` here breaks the cycle.
"""

from __future__ import annotations

from typing import TypedDict

__all__ = ["LevelCoverage"]


class LevelCoverage(TypedDict):
    """Aggregate verification outcome for one L-level across a run.

    Carries counts and deduplicated level-shape skip-reason strings only;
    never an item key, never a patient value, never a path. Surfaced in
    the upload run report so the L0-L6 coverage claim cannot drift wider
    than the runtime.
    """

    pass_count: int
    fail_count: int
    skip_count: int
    skip_reasons: list[str]
