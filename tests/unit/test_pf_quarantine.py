"""Orphan rows are held, never guessed and never a reason to stop the run.

The real export behind #280: five standard tables, 369,094 rows, and 695 of
them dangling — a blank patient key here, a key naming nobody there. The old
all-or-nothing rule refused every one of the 368,399 attributable rows over
the broken 695, and the diagnostic blamed a column that was present all along.

The repair is a partition with an audit trail. A row that names a known
patient lands on that patient; a row that cannot is quarantined VERBATIM with
a PHI-free reason; a table with no path to a patient at all still refuses the
run. The quarantine reaches the operator as ``quarantine.json`` in the output
directory, its count on the INGEST event, and a yellow CLI line — visibly
non-green until someone has looked.

Every fixture here is synthetic (``feedface-`` guids, invented values).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

import anastomosis.sources.pf_tebra  # noqa: F401 — registers the adapter
from anastomosis.pipeline import QUARANTINE_FILENAME, settle_quarantine
from anastomosis.sources import get_source
from anastomosis.sources.base import QuarantinedRows
from anastomosis.sources.pf_tebra.loader import UnsupportedTablesError, read_export
from anastomosis.sources.pf_tebra.mapper import map_export

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"

P1 = "feedface-0000-0000-0000-000000000001"
P2 = "feedface-0000-0000-0000-000000000002"
GHOST = "feedface-dead-0000-0000-00000000beef"  # names no patient in the fixture

ELIGIBILITIES = "patient-insurance-eligibilities"
EXT_KEY = "pf_tebra:unmapped:patient-lab-notes"


def _mapped(export: dict) -> tuple[dict[str, Any], list[QuarantinedRows]]:
    """Run the mapper, returning records by patient id and what it held back."""
    held: list[QuarantinedRows] = []
    records = {r.patient.id: r for r in map_export(export, on_quarantine=held.extend)}
    return records, held


# --- mixed direct-key tables -------------------------------------------------


def test_a_mixed_table_keeps_the_attributable_row_and_holds_the_dangling_one() -> None:
    """The issue's own minimal shape: one row that attributes, one that cannot.

    Before #280 the dangling row took the whole table down with it. Now the
    attributable row reaches its patient and the dangling one is held — with
    the row intact, so nothing was dropped, and on no patient, so nothing was
    guessed.
    """
    export = read_export(FIXTURE)
    good = {"PatientPracticeGuid": P1, "NoteText": "an attributable lab note"}
    bad = {"PatientPracticeGuid": "", "NoteText": "a note with a blank patient key"}
    export["patient-lab-notes"] = [good, bad]

    records, held = _mapped(export)

    assert records[P1].extensions[EXT_KEY] == [good]
    (entry,) = held
    assert entry.table == "patient-lab-notes"
    assert entry.reason == "PatientPracticeGuid is blank"
    assert entry.rows == (bad,)
    # Held is not guessed: the dangling row is on NO patient's record.
    for record in records.values():
        assert bad not in record.extensions.get(EXT_KEY, [])


def test_blank_and_unknown_keys_are_held_under_their_own_reasons() -> None:
    """The diagnostic half of #280: the old message said the column was missing.

    A blank key and a key naming nobody are different repairs, so they are
    different reasons — each PHI-free (schema words and no values).
    """
    export = read_export(FIXTURE)
    export["patient-lab-notes"] = [
        {"PatientPracticeGuid": P1, "NoteText": "kept"},
        {"PatientPracticeGuid": "", "NoteText": "blank key"},
        {"PatientPracticeGuid": GHOST, "NoteText": "unknown patient"},
    ]

    _, held = _mapped(export)

    reasons = {entry.reason: entry.rows for entry in held}
    assert reasons == {
        "PatientPracticeGuid is blank": ({"PatientPracticeGuid": "", "NoteText": "blank key"},),
        "PatientPracticeGuid names no patient in this export": (
            {"PatientPracticeGuid": GHOST, "NoteText": "unknown patient"},
        ),
    }


def test_conservation_every_offered_row_is_attributed_or_held() -> None:
    """offered == attributed + quarantined, per table — the invariant a silent
    drop would break first. A mutation that discards instead of holding fails
    here before it fails anywhere subtle."""
    export = read_export(FIXTURE)
    offered = [
        {"PatientPracticeGuid": P1, "NoteText": "a"},
        {"PatientPracticeGuid": P2, "NoteText": "b"},
        {"PatientPracticeGuid": "", "NoteText": "c"},
        {"PatientPracticeGuid": GHOST, "NoteText": "d"},
        {"PatientPracticeGuid": P1, "NoteText": "e"},
    ]
    export["patient-lab-notes"] = list(offered)

    records, held = _mapped(export)

    attributed = [row for record in records.values() for row in record.extensions.get(EXT_KEY, [])]
    quarantined = [row for entry in held for row in entry.rows]
    assert len(attributed) + len(quarantined) == len(offered)
    # Not just the arithmetic: the very rows, each exactly once.
    assert sorted(map(str, attributed + quarantined)) == sorted(map(str, offered))


# --- the declared indirect join ----------------------------------------------


def _eligibility(plan: str | None) -> dict[str, str | None]:
    return {
        "PatientInsuranceEligibilityGuid": "feedface-0000-0000-0000-00000000e001",
        "PatientInsurancePlanGuid": plan,
        "CopayAmount": "25.00",
    }


def test_an_ambiguous_join_is_held_never_broadcast_and_never_first_wins() -> None:
    """Two patients' insurance rows carry the same plan guid: the eligibility
    row now names two candidate owners. Broadcasting it files a chart on the
    wrong patient; picking the first is the same sin with a smaller blast
    radius. It must land on NEITHER record and in quarantine."""
    export = read_export(FIXTURE)
    plan = "feedface-aaaa-0000-0000-00000000plan"
    base = dict(export["patient-insurances"][0])
    export["patient-insurances"] = export["patient-insurances"] + [
        {**base, "PatientInsurancePlanGuid": plan, "PatientPracticeGuid": P1},
        {**base, "PatientInsurancePlanGuid": plan, "PatientPracticeGuid": P2},
    ]
    row = _eligibility(plan)
    export[ELIGIBILITIES] = [row]

    records, held = _mapped(export)

    (entry,) = held
    assert entry.rows == (row,)
    assert "more than one patient" in entry.reason
    for record in records.values():
        assert f"pf_tebra:unmapped:{ELIGIBILITIES}" not in record.extensions


def test_a_join_matching_no_insurance_row_is_held() -> None:
    export = read_export(FIXTURE)
    row = _eligibility("feedface-aaaa-0000-0000-000000nomatch")
    export[ELIGIBILITIES] = [row]

    records, held = _mapped(export)

    (entry,) = held
    assert entry.rows == (row,)
    assert "finds no owning patient in patient-insurances" in entry.reason
    assert all(f"pf_tebra:unmapped:{ELIGIBILITIES}" not in r.extensions for r in records.values())


def test_a_blank_join_key_is_held() -> None:
    export = read_export(FIXTURE)
    row = _eligibility(None)
    export[ELIGIBILITIES] = [row]

    _, held = _mapped(export)

    (entry,) = held
    assert entry.reason == "PatientInsurancePlanGuid is blank"
    assert entry.rows == (row,)


def test_an_insurance_row_with_a_blank_patient_key_cannot_vouch_for_an_owner() -> None:
    """The join resolves through the parent row's OWN patient key. A parent
    whose key is blank names nobody — resolving through it would be a guess
    wearing a join's clothes.

    Pinned at the unit level because map_export cannot reach it for THIS
    parent: patient-insurances is a KNOWN table, so a blank patient key there
    fails the foreign-key closure first (its own fail-closed pin). The guard
    stays because _join_owners must hold for any future declared parent too.
    """
    from anastomosis.sources.pf_tebra.mapper import _join_owners, _partition_joined

    plan = "feedface-aaaa-0000-0000-00000000plan"
    owners = _join_owners(
        [{"PatientInsurancePlanGuid": plan, "PatientPracticeGuid": None}],
        "PatientInsurancePlanGuid",
    )
    assert owners == {}  # a voiceless parent contributes no owner at all

    row = _eligibility(plan)
    grouped, held = _partition_joined(
        ELIGIBILITIES,
        [row],
        "PatientInsurancePlanGuid",
        "patient-insurances",
        owners,
        frozenset({P1, P2}),
    )
    assert grouped == {}
    (entry,) = held
    assert "finds no owning patient" in entry.reason
    assert entry.rows == (row,)


def test_a_mixed_eligibility_table_resolves_the_exact_rows_and_holds_the_rest() -> None:
    """The real table's fate under the join: 773 exact resolutions would all
    attribute; here, one exact and one dangling prove the partition."""
    export = read_export(FIXTURE)
    plan_row = export["patient-insurances"][0]
    exact = _eligibility(plan_row["PatientInsurancePlanGuid"])
    dangling = {
        **_eligibility("feedface-aaaa-0000-0000-000000nomatch"),
        "PatientInsuranceEligibilityGuid": "feedface-0000-0000-0000-00000000e002",
    }
    export[ELIGIBILITIES] = [exact, dangling]

    records, held = _mapped(export)

    owner = plan_row["PatientPracticeGuid"]
    assert records[owner].extensions[f"pf_tebra:unmapped:{ELIGIBILITIES}"] == [exact]
    (entry,) = held
    assert entry.rows == (dangling,)


# --- what still refuses ------------------------------------------------------


def test_a_table_with_no_path_to_a_patient_still_refuses_and_says_so() -> None:
    """The refusal narrowed; it did not soften. And the message now describes
    the actual gap — no path at all — instead of misdiagnosing a missing
    column on tables that had one."""
    export = read_export(FIXTURE)
    export["mystery-notes"] = [{"Note": "no key, no join, no identity"}]
    # A partitionable table in the same export must NOT appear in the refusal.
    export["patient-lab-notes"] = [{"PatientPracticeGuid": "", "NoteText": "dangles"}]

    with pytest.raises(UnsupportedTablesError) as caught:
        list(map_export(export))

    message = str(caught.value)
    assert "mystery-notes" in message
    assert "patient-lab-notes" not in message
    assert "no path to a patient" in message


def test_a_clean_export_reports_an_empty_quarantine() -> None:
    """The callback fires with [] on a clean load — that emptiness is what
    resets a stateful caller (the adapter) after a previous dirty export."""
    fired: list[list[QuarantinedRows]] = []
    list(map_export(read_export(FIXTURE), on_quarantine=fired.append))
    assert fired == [[]]


# --- the adapter's per-load state --------------------------------------------


def _export_with_dangler(dst: Path) -> Path:
    shutil.copytree(FIXTURE, dst)
    (dst / "patient-lab-notes.tsv").write_text(
        "PatientPracticeGuid\tNoteText\n\tan orphaned note\n", encoding="utf-8"
    )
    return dst


def test_the_adapter_holds_and_then_resets_between_loads(tmp_path: Path) -> None:
    """The registry keeps ONE adapter instance for the process. A dirty load
    must expose its quarantine; the clean load after it must not inherit one."""
    adapter = get_source("pf-tebra")
    dirty = _export_with_dangler(tmp_path / "dirty")

    list(adapter.load(dirty))
    (entry,) = adapter.quarantine
    assert entry.table == "patient-lab-notes"
    assert len(entry.rows) == 1

    list(adapter.load(FIXTURE))
    assert adapter.quarantine == []


def test_a_refusing_load_does_not_leave_the_previous_quarantine_showing(
    tmp_path: Path,
) -> None:
    """A load that fails closed never reached the partition — its quarantine is
    nothing, not last export's something. The reset at the start of ``load``
    is what makes that true; this is its pin."""
    adapter = get_source("pf-tebra")
    dirty = _export_with_dangler(tmp_path / "dirty")
    list(adapter.load(dirty))
    assert adapter.quarantine, "precondition: the dirty load held something"

    refusing = tmp_path / "refusing"
    shutil.copytree(FIXTURE, refusing)
    (refusing / "mystery-notes.tsv").write_text("Note\nno path\n", encoding="utf-8")
    with pytest.raises(UnsupportedTablesError):
        list(adapter.load(refusing))
    assert adapter.quarantine == []


# --- the pipeline settlement -------------------------------------------------


class _FakeAdapter:
    """The narrowest thing settle_quarantine reads: a ``quarantine`` attribute."""

    def __init__(self, held: list[QuarantinedRows]) -> None:
        self.quarantine = held


def test_settle_quarantine_writes_the_artifact_and_returns_the_count(tmp_path: Path) -> None:
    held = [
        QuarantinedRows("zeta-table", "PatientPracticeGuid is blank", ({"A": "1"}, {"A": "2"})),
        QuarantinedRows("alpha-table", "PatientPracticeGuid is blank", ({"B": None},)),
    ]
    out = tmp_path / "out"

    counts = settle_quarantine(_FakeAdapter(held), out)

    assert counts == {"quarantined": 3}
    payload = json.loads((out / QUARANTINE_FILENAME).read_text(encoding="utf-8"))
    assert payload["total_rows"] == 3
    # Grouped by table (sorted — deterministic output), rows verbatim.
    assert [group["table"] for group in payload["quarantine"]] == ["alpha-table", "zeta-table"]
    assert payload["quarantine"][1]["rows"] == [{"A": "1"}, {"A": "2"}]
    assert payload["quarantine"][0]["rows"] == [{"B": None}]


def test_a_clean_settlement_adds_no_count_and_removes_the_stale_artifact(
    tmp_path: Path,
) -> None:
    """Run 1 quarantined; the operator repaired the export; run 2 into the same
    folder is clean. Last run's artifact must not survive to read as this
    run's — and the INGEST payload must be byte-identical to the pre-#280
    shape (the PHI-fence parity test counts its keys)."""
    out = tmp_path / "out"
    out.mkdir()
    (out / QUARANTINE_FILENAME).write_text("{}", encoding="utf-8")

    assert settle_quarantine(_FakeAdapter([]), out) == {}
    assert not (out / QUARANTINE_FILENAME).exists()


def test_an_adapter_without_the_attribute_settles_clean(tmp_path: Path) -> None:
    """Only pf-tebra quarantines today; every other adapter stays untouched."""
    assert settle_quarantine(object(), tmp_path / "out") == {}
    assert not (tmp_path / "out" / QUARANTINE_FILENAME).exists()


def test_the_migration_orchestrator_settles_the_same_way(tmp_path: Path) -> None:
    """Both orchestrators share the settlement: a migration over a dirty export
    writes quarantine.json at the output root and carries the count on INGEST."""
    from anastomosis.core.migrate import MigrationCommand, _resolve_source_and_load
    from anastomosis.pipeline import STAGE_INGEST, StageEvent

    dirty = _export_with_dangler(tmp_path / "dirty")
    out = tmp_path / "migration-out"
    events: list[StageEvent] = []
    cmd = MigrationCommand(export_dir=dirty, out_dir=out, source="pf-tebra", destination="tebra")

    _resolve_source_and_load(cmd, events.append)

    (ingest,) = [e for e in events if e.stage == STAGE_INGEST]
    assert ingest.counts["quarantined"] == 1
    payload = json.loads((out / QUARANTINE_FILENAME).read_text(encoding="utf-8"))
    assert payload["total_rows"] == 1
    assert payload["quarantine"][0]["table"] == "patient-lab-notes"
