"""Classify a finished migration's delivery outcome — one verdict, two frontends.

A migration ALWAYS writes its artifacts (the structured C-CDA payload under
``<out>/ccda`` and the human-readable charts under ``<out>/charts``), but whether
the destination has a viable AUTOMATED route is a separate fact the operator must
see. The CLI prints a loud notice and exits 1 on a no-route migration; the GUI
must raise the SAME flag — an error / manual-import event, never a silent
``done``. This module is the single place that decides, so the two frontends
cannot drift (the parity the maintainer asked for: the backend CLI and the
frontend GUI never disagree about what happened).

PHI rule: a status carries only the destination NAME, the chosen route KIND, and
the (operator-chosen) output directory path — never anything patient-derived, so
it is safe to put on the PHI-scanned GUI event stream.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from anastomosis.core.migrate import MigrationResult

__all__ = [
    "MigrationOutcome",
    "MigrationStatus",
    "classify_migration",
    "manual_import_notice",
]


class MigrationOutcome(StrEnum):
    """Whether the migration's destination has a viable automated route."""

    DELIVERED = "delivered"  # a viable automated route was chosen
    MANUAL_IMPORT = "manual_import"  # artifacts written, but no automated route


@dataclass(frozen=True)
class MigrationStatus:
    """The PHI-free verdict on a finished migration, shared by both frontends.

    ``exit_code`` is the CLI's contract (0 delivered, 1 manual-import); the GUI
    reads :attr:`needs_manual_import` to decide whether to emit a ``done`` or a
    manual-import (error) event. ``ccda_dir`` is where the importable C-CDA
    payload landed; ``chosen_route`` is the resolved route kind, or ``None``.
    """

    outcome: MigrationOutcome
    destination: str
    chosen_route: str | None
    ccda_dir: str
    exit_code: int

    @property
    def needs_manual_import(self) -> bool:
        """True when no automated route exists and a human must import the C-CDA."""
        return self.outcome is MigrationOutcome.MANUAL_IMPORT


def classify_migration(result: MigrationResult) -> MigrationStatus:
    """Derive the delivery verdict from a finished migration's transit map.

    The route was resolved up front; ``transit.chosen is None`` means no viable
    automated route exists for the destination (a capability gap, not a failure
    — the artifacts ARE still written). That is the manual-import case (exit 1);
    a chosen route is a clean delivery (exit 0).
    """
    transit = result.transit
    ccda_dir = str(result.ccda_export.out_dir)
    if transit.chosen is not None:
        return MigrationStatus(
            outcome=MigrationOutcome.DELIVERED,
            destination=transit.destination,
            chosen_route=transit.chosen.kind.value,
            ccda_dir=ccda_dir,
            exit_code=0,
        )
    return MigrationStatus(
        outcome=MigrationOutcome.MANUAL_IMPORT,
        destination=transit.destination,
        chosen_route=None,
        ccda_dir=ccda_dir,
        exit_code=1,
    )


def manual_import_notice(status: MigrationStatus) -> str:
    """The operator-facing, PHI-free notice for a no-automated-route migration.

    Shared verbatim by both frontends so the guidance never drifts: the C-CDA is
    written and importable, and a browser route can be taught with ``destination
    init``.
    """
    return (
        f"no viable automated route to {status.destination!r} — import the "
        f"C-CDA at {status.ccda_dir} manually, or run "
        f"'anast destination init {status.destination}' to teach a browser route."
    )
