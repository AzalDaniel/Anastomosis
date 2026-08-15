"""Classify a finished migration's delivery outcome — one verdict, two frontends.

A migration ALWAYS writes its artifacts (the structured C-CDA payload under
``<out>/ccda``, the human-readable charts under ``<out>/charts``, and an upload
manifest) and resolves the destination's transit map. But writing artifacts and
choosing a route is PREPARATION, not delivery: a chosen route is a *plan*, never
proof that a chart landed in the destination. ``migrate`` executes no delivery
route itself, so the honest verdict for a migration whose route resolved is
PREPARED — the artifacts + the verified route plan are ready, and a human (or a
later ``anast upload`` / roadmap executor) still has to execute the move.

The invariant this module pins: a migration is never reported as DELIVERED. That
outcome is reserved for a future destination executor that returns a durable
delivery receipt (M6 roadmap) and is intentionally unreachable today — the
product's core promise is that the claim never exceeds the runtime.

Both frontends read this single classifier so they cannot drift (the parity the
maintainer asked for: the backend CLI and the frontend GUI never disagree about
what happened). The CLI prints a loud notice and exits 1 on a no-route migration
and prints the prepared notice at exit 0 on a route-resolved one; the GUI raises
the SAME flags — a manual-import (error) event for no route, a ``done`` carrying
the prepared notice + outcome for a resolved one, never a silent success.

PHI rule: a status carries only the destination NAME, the chosen route KIND, and
the (operator-chosen) output directory path — never anything patient-derived, so
it is safe to put on the PHI-scanned GUI event stream.
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
    """The PHI-free verdict on a finished migration, shared by both frontends.

    ``exit_code`` is the CLI's contract (0 prepared, 1 manual-import); the GUI
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
    """Derive the honest verdict from a finished migration's transit map.

    A chosen route is a verified PLAN, not a delivery: ``migrate`` writes the
    artifacts and resolves the route, but executes no destination route, so a
    route-resolved migration is PREPARED (exit 0 — preparation succeeded, which
    keeps the CLI exit contract callers script against). ``transit.chosen is
    None`` means no viable automated route exists for the destination (a
    capability gap, not a failure — the artifacts ARE still written); that is the
    manual-import case (exit 1).

    The verdict is NEVER :attr:`MigrationOutcome.DELIVERED`: delivered requires a
    durable receipt from an executor that does not exist yet (M6 roadmap). A
    chosen route is a plan; delivered means executed with a receipt.
    """
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
    """The operator-facing, PHI-free notice for a route-resolved (PREPARED) migration.

    Shared verbatim by both frontends so the guidance never drifts: the artifacts
    and the verified route plan are ready, but delivery has NOT run. The
    actionable tail is tailored by route kind — the vendor API route defers to
    the roadmap, the C-CDA import route points at the destination UI, the browser
    route points at ``anast upload``.
    """
    tails = {
        RouteKind.VENDOR_API.value: (
            "API delivery execution is on the roadmap; import the C-CDA "
            "manually or use the browser route"
        ),
        RouteKind.CCDA_IMPORT.value: "import the C-CDA in the destination's UI",
        RouteKind.BROWSER.value: "run 'anast upload' to file the charts via the browser route",
    }
    tail = tails.get(status.chosen_route or "", "import the C-CDA into the destination")
    return (
        f"migration artifacts prepared for {status.destination!r} — C-CDA at "
        f"{status.ccda_dir}, charts + upload manifest written; route plan: "
        f"{status.chosen_route}. Delivery has NOT been executed: {tail}."
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
