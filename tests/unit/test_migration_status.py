"""The shared migration-status classifier (one verdict, two frontends).

These pin the CLI/GUI parity fix (codex P0-2): a no-viable-route migration is a
manual-import outcome (exit 1, loud notice) even though its artifacts are still
written — and both the CLI and the GUI read this single classifier, so they
cannot disagree about whether a migration "succeeded".
"""

from __future__ import annotations

from pathlib import Path

from anastomosis.core.commands import DeliveryOutcome
from anastomosis.core.migrate import MigrationResult
from anastomosis.core.migration_status import (
    MigrationOutcome,
    classify_migration,
    manual_import_notice,
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


def test_chosen_route_classifies_as_delivered(tmp_path: Path) -> None:
    status = classify_migration(_result("tebra", tmp_path))  # tebra → C-CDA import
    assert status.outcome is MigrationOutcome.DELIVERED
    assert status.needs_manual_import is False
    assert status.chosen_route == "ccda_import"
    assert status.destination == "tebra"
    assert status.exit_code == 0


def test_no_route_classifies_as_manual_import(tmp_path: Path) -> None:
    status = classify_migration(_result("advancedmd", tmp_path))  # no viable route
    assert status.outcome is MigrationOutcome.MANUAL_IMPORT
    assert status.needs_manual_import is True
    assert status.chosen_route is None
    assert status.destination == "advancedmd"
    assert status.exit_code == 1  # the CLI's no-route contract


def test_manual_import_notice_is_actionable_and_phi_free(tmp_path: Path) -> None:
    status = classify_migration(_result("advancedmd", tmp_path))
    notice = manual_import_notice(status)
    assert "no viable automated route" in notice
    assert "advancedmd" in notice
    assert str(tmp_path / "ccda") in notice  # points at the written C-CDA payload
    assert "destination init" in notice  # the path to teach a browser route
