"""Every stage reconciles what it was offered against what it produced (68):
a stage is offered N units, every one must end in exactly one disposition,
and the dispositions must add back to N. It counts rather than samples, and
refuses rather than warns — the invariant holds by construction, so a
violation is a bug in the stage. A message names the stage, the unit, and
integers only, never a patient, a path, or an id.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

__all__ = ["Conservation", "ConservationError"]


class ConservationError(Exception):
    """A stage did not account for everything it was offered.

    Raised at the seam that lost the work, not at the end of the run, so the
    message names the boundary rather than the symptom three stages later.
    """


@dataclass(frozen=True)
class Conservation:
    """One stage's books (68). ``dispositions`` maps an outcome name — the
    stage's own vocabulary ("rendered", "skipped") — to how many units
    ended there, verbatim in the failure message."""

    #: The seam, named as a crossing: "canonical -> rendered".
    stage: str
    #: What is being counted, singular: "encounter", "chart", "item".
    unit: str
    offered: int
    dispositions: Mapping[str, int] = field(default_factory=dict)

    @property
    def accounted(self) -> int:
        return sum(self.dispositions.values())

    @property
    def unaccounted(self) -> int:
        """Offered minus accounted. Negative means the stage produced MORE than
        it was given, which is its own kind of wrong — two units sharing one
        slot, or one counted twice — and is just as much a refusal."""
        return self.offered - self.accounted

    @property
    def reconciles(self) -> bool:
        return self.unaccounted == 0

    def describe(self) -> str:
        """A one-line, PHI-safe summary: counts and disposition names only."""
        columns = ", ".join(f"{name}={count}" for name, count in sorted(self.dispositions.items()))
        return f"{self.stage}: {self.offered} {self.unit}(s) offered, {columns or 'nothing'}"

    def check(self) -> None:
        """Raise unless every offered unit ended in exactly one disposition."""
        if self.reconciles:
            return
        short = self.unaccounted
        direction = (
            f"{short} {self.unit}(s) went in and never came out"
            if short > 0
            else f"{-short} more {self.unit}(s) came out than went in"
        )
        raise ConservationError(f"{self.describe()} — {direction}")
