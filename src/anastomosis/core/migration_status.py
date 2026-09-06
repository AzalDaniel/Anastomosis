"""Classify a finished migration's delivery outcome — one verdict, shared
by both frontends so they cannot drift. Writing artifacts and resolving a
route is PREPARATION, not delivery, so a migration is never reported
DELIVERED (53): that outcome is reserved for a future executor's durable
receipt and is intentionally unreachable today. PHI: a status carries only
the destination name, the route kind, and the output path.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from anastomosis.deliver.router import RouteKind

if TYPE_CHECKING:
    from anastomosis.core.migrate import MigrationResult

__all__ = [
    "MigrationOutcome",
    "MigrationStatus",
    "classify_migration",
    "manual_import_notice",
    "prepared_notice",
]


class MigrationOutcome(StrEnum):
    """What a finished migration produced — a PLAN, a manual step, or a receipt."""

    PREPARED = "prepared"  # route chosen + artifacts + route plan written; delivery NOT executed
    MANUAL_IMPORT = "manual_import"  # artifacts written, but no viable automated route
    # Reserved: a chart actually landed in the destination, proven by a durable
    # delivery RECEIPT from a destination executor (M6 roadmap). ``migrate`` runs
    # no delivery route, so this is intentionally unreachable today — the classifier
    # never returns it. It exists to name the outcome the executor will one day fill.
    DELIVERED = "delivered"


@dataclass(frozen=True)
class MigrationStatus:
    """The PHI-free verdict on a finished migration, shared by both
    frontends. ``exit_code`` is the CLI's contract (0 prepared, 1
    manual-import); the GUI reads :attr:`needs_manual_import` for the
    same branch."""

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
    """Contract (53): a route-resolved migration is PREPARED (exit 0,
    since ``migrate`` executes no route); ``transit.chosen is None`` — a
    capability gap, not a failure — is MANUAL_IMPORT (exit 1). Never
    :attr:`MigrationOutcome.DELIVERED`: that needs a receipt from an
    executor that does not exist yet."""
    transit = result.transit
    ccda_dir = str(result.ccda_export.out_dir)
    if transit.chosen is not None:
        return MigrationStatus(
            outcome=MigrationOutcome.PREPARED,
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


def prepared_notice(status: MigrationStatus) -> str:
    """The operator-facing, PHI-free notice for a route-resolved
    (PREPARED) migration, shared verbatim by both frontends. The
    actionable tail is tailored by route kind."""
    tails = {
        RouteKind.VENDOR_API.value: (
            "API delivery execution is on the roadmap; import the C-CDA "
            "manually or use the browser route"
        ),
        RouteKind.CCDA_IMPORT.value: "import the C-CDA in the destination's UI",
        RouteKind.BROWSER.value: "file the charts from the Uploads screen (or 'anast upload')",
    }
    tail = tails.get(status.chosen_route or "", "import the C-CDA into the destination")
    return (
        f"migration artifacts prepared for {status.destination!r} — C-CDA at "
        f"{status.ccda_dir}, charts and upload files written; route plan: "
        f"{status.chosen_route}. Delivery has NOT been executed: {tail}."
    )


def manual_import_notice(status: MigrationStatus) -> str:
    """The operator-facing, PHI-free notice for a no-automated-route
    migration, shared verbatim by both frontends."""
    return (
        f"no viable automated route to {status.destination!r} — import the "
        f"C-CDA at {status.ccda_dir} manually, or run "
        f"'anast destination init {status.destination}' to teach a browser route."
    )
