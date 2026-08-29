"""What goes into a stage comes out of it, or the run stops.

Verification in this toolkit reads ARTIFACTS: is this PDF well-formed, does it
carry the sections it should, is the right patient's name on it. Every real
defect found so far has instead been a SEAM — data crossing from one component
to the next and arriving short. Sixteen DocumentReferences built and fourteen
with ids; five attachments in the export and zero in the output; two encounters
sharing one id merging into one file while the run reported two. Nothing was
malformed. Each artifact was fine. The loss was in what never arrived, and no
check on an artifact can see an artifact that does not exist.

A conservation check asks the other question. A stage is offered N units of
work; every one of them must end in exactly one disposition — rendered,
skipped, failed, filed — and the dispositions must add back up to N. When they
do not, something crossed a boundary and vanished, and the run says so instead
of reporting the survivors as if they were everything.

Three properties this deliberately has:

* It counts, it does not sample. A check that verifies a subset cannot
  distinguish "all present" from "the ones I looked at were present".
* It refuses rather than warns. The invariant holds by construction today, so
  a violation is a bug in the stage, not a condition an operator can act on by
  reading a warning and continuing.
* It carries no values. A message names the stage, the unit, and integers —
  never a patient, a path, or an id. Counts are safe to log; the things being
  counted are the chart.
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
    """One stage's books: what it was offered, and what became of all of it.

    ``dispositions`` maps an outcome name to how many units ended there. The
    names are the stage's own vocabulary ("rendered", "skipped", "failed") and
    they appear verbatim in the failure message, so whoever reads it can tell
    which column is short without opening the code.
    """

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
