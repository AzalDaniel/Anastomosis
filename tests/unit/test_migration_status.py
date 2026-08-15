"""The shared migration-status classifier (one verdict, two frontends).

These pin CLI/GUI parity AND the honesty invariant: a route-resolved migration is
PREPARED (exit 0 — artifacts + a verified route plan are written, but `migrate`
executes no delivery route), a no-viable-route migration is MANUAL_IMPORT (exit 1,
loud notice), and the classifier NEVER returns DELIVERED (that needs a durable
receipt no executor produces yet). Both the CLI and the GUI read this single
classifier, so they cannot disagree about whether a migration "succeeded".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anastomosis.core.commands import DeliveryOutcome
from anastomosis.core.migrate import MigrationResult
from anastomosis.core.migration_status import (
    MigrationOutcome,
    classify_migration,
    manual_import_notice,
    prepared_notice,
)
from anastomosis.deliver.router import plan_route
from anastomosis.destinations.registry import DestinationRegistry


def _result(destination: str, out_dir: Path) -> MigrationResult:
    """A MigrationResult carrying a REAL transit map (the only field the
    classifier reads, besides ``ccda_export.out_dir``)."""
    transit = plan_route(destination, DestinationRegistry.load())
    return MigrationResult(
        transit=transit,
        pipeline=None,
        ccda_view=None,
        ccda_export=DeliveryOutcome(kind="ccda", out_dir=out_dir / "ccda", counts={"patients": 3}),
        render_mode="neutral",
        pack="generic_soap",
        records=[],
    )


def test_chosen_route_classifies_as_prepared(tmp_path: Path) -> None:
    status = classify_migration(_result("tebra", tmp_path))  # tebra → C-CDA import
    assert status.outcome is MigrationOutcome.PREPARED
    assert status.needs_manual_import is False
    assert status.chosen_route == "ccda_import"
    assert status.destination == "tebra"
    assert status.exit_code == 0  # preparation succeeded — keeps the CLI exit contract


def test_no_route_classifies_as_manual_import(tmp_path: Path) -> None:
    status = classify_migration(_result("advancedmd", tmp_path))  # no viable route
    assert status.outcome is MigrationOutcome.MANUAL_IMPORT
    assert status.needs_manual_import is True
    assert status.chosen_route is None
    assert status.destination == "advancedmd"
    assert status.exit_code == 1  # the CLI's no-route contract


def test_classify_never_returns_delivered(tmp_path: Path) -> None:
    """The honesty invariant: `migrate` executes no delivery route, so a chosen
    route is a PLAN, not proof a chart landed — classify NEVER returns DELIVERED
    (reserved for a future executor that returns a durable receipt). Pin it for
    both fixture shapes (a chosen route AND no route) until such an executor
    exists."""
    for destination in ("tebra", "epic", "advancedmd"):
        status = classify_migration(_result(destination, tmp_path))
        assert status.outcome is not MigrationOutcome.DELIVERED


def test_manual_import_notice_is_actionable_and_phi_free(tmp_path: Path) -> None:
    status = classify_migration(_result("advancedmd", tmp_path))
    notice = manual_import_notice(status)
    assert "no viable automated route" in notice
    assert "advancedmd" in notice
    assert str(tmp_path / "ccda") in notice  # points at the written C-CDA payload
    assert "destination init" in notice  # the path to teach a browser route


@pytest.mark.parametrize(
    ("destination", "route", "tail_marker"),
    [
        ("tebra", "ccda_import", "import the C-CDA in the destination's UI"),
        ("epic", "vendor_api", "on the roadmap"),
    ],
)
def test_prepared_notice_is_actionable_and_phi_free(
    tmp_path: Path, destination: str, route: str, tail_marker: str
) -> None:
    status = classify_migration(_result(destination, tmp_path))
    assert status.chosen_route == route
    notice = prepared_notice(status)
    assert "prepared" in notice
    assert destination in notice
    assert str(tmp_path / "ccda") in notice  # points at the written C-CDA payload
    assert route in notice  # the resolved route PLAN is named
    assert "NOT been executed" in notice  # delivery is not claimed done
    assert tail_marker in notice  # the route-tailored actionable tail
    assert "delivered" not in notice.lower()  # honesty: prepared is not delivered
