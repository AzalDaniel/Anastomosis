"""Lightweight value types shared between the verifier and its consumers.

This module deliberately imports nothing from the rest of the project, so the
upload-report writer can depend on the verifier's TypedDict shapes without
pulling :class:`~anastomosis.deliver.verify.composite.LayeredVerifier` (which
in turn pulls :mod:`anastomosis.deliver.browser.errors`) and triggering a
circular import (verify.composite -> browser.errors -> browser/__init__ ->
browser.reports -> verify.composite) that only surfaces in a fresh
interpreter. Keeping :class:`LevelCoverage` here breaks the cycle.

:class:`VerifyPolicy` lives here for the same reason and one more: the manifest
writer decides it and the ladder honours it, and neither of those two modules
may import the other.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TypedDict

__all__ = ["LevelCoverage", "VerifyPolicy"]


class VerifyPolicy(StrEnum):
    """What may honestly be checked about one manifest item's bytes.

    The L0-L6 ladder is calibrated for a chart THIS toolkit printed: L1 rejects
    a sub-KiB file because a Chromium print of even an empty note is several KiB,
    and L2/L3 read a name, a DOB and the pack's declared header fields off page
    one. A C-CDA Unstructured Document's scan is none of those things — it is
    the source's own bytes, often an image with no text layer at all — so
    running the chart ladder over one fails every level for reasons that say
    nothing about whether the file is intact or whose it is.

    So each item declares which kind of file it is, and a source document also
    declares whether anything here can open it and count its pages:

    * :attr:`RENDERED_CHART` — a chart this run rendered. The whole ladder, as
      before; every pre-v4 manifest item is one of these.
    * :attr:`SOURCE_PAGED` — the source's own document, DECLARED as a media type
      this toolkit pages. L0's re-hash and L1's exact page count both apply; the
      page-one text levels do not.
    * :attr:`SOURCE_OPAQUE` — the source's own bytes under a media type nothing
      here pages (a TIFF scan; a body that declared no type at all). L0's
      re-hash is the whole of what can be proven, and the levels that cannot run
      say so rather than passing.
    """

    RENDERED_CHART = "rendered_chart"
    SOURCE_PAGED = "source_paged"
    SOURCE_OPAQUE = "source_opaque"

    @property
    def is_source_document(self) -> bool:
        """Whether these bytes came from the source rather than from a render."""
        return self is not VerifyPolicy.RENDERED_CHART


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
