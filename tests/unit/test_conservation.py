"""Conservation at the stage seams: what goes in comes out, or the run stops.

Every real defect this project has found was a boundary problem — data crossing
from one component to the next and arriving short — and none was caught by
verification, because verification reads artifacts and an artifact that never
arrived has nothing to read. These tests hold the other question: was every
unit of work this stage was handed accounted for.

Each seam gets a pair: a correct run balances, and a stage that loses one unit
refuses. The losing half is what matters — a conservation check nobody has seen
fail is a comment.

Synthetic throughout (``feedface-`` ids, invented names).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from anastomosis.core.conservation import Conservation, ConservationError

# --- the primitive -----------------------------------------------------------


def test_a_balanced_stage_says_nothing() -> None:
    Conservation(
        stage="canonical -> rendered",
        unit="encounter",
        offered=6,
        dispositions={"rendered": 4, "skipped": 1, "failed": 1},
    ).check()


def test_work_that_went_in_and_never_came_out_refuses() -> None:
    with pytest.raises(ConservationError) as excinfo:
        Conservation(
            stage="canonical -> rendered",
            unit="encounter",
            offered=6,
            dispositions={"rendered": 4, "skipped": 1},
        ).check()
    assert "1 encounter(s) went in and never came out" in str(excinfo.value)


def test_more_coming_out_than_went_in_refuses_too() -> None:
    """The other direction is its own kind of wrong — one unit counted twice,
    or two sharing a slot — and reads as fine to anything that only checks for
    loss."""
    with pytest.raises(ConservationError) as excinfo:
        Conservation(
            stage="canonical -> rendered",
            unit="encounter",
            offered=2,
            dispositions={"rendered": 3},
        ).check()
    assert "1 more encounter(s) came out than went in" in str(excinfo.value)


def test_the_message_carries_counts_and_column_names_only() -> None:
    """It travels into logs and run reports, so it must be readable and hold
    nothing of the patient's."""
    message = str(
        Conservation(
            stage="canonical -> delivered",
            unit="chart",
            offered=9,
            dispositions={"delivered": 7, "missing": 1},
        ).describe()
    )
    assert message == "canonical -> delivered: 9 chart(s) offered, delivered=7, missing=1"


# --- seam: canonical -> rendered ---------------------------------------------


def _record(encounters: int) -> object:
    from anastomosis.core.model import Encounter, Patient, PatientRecord

    patient = Patient(
        id="feedface-0000-0000-0000-0000000000aa",
        given_name="Synthia",
        family_name="Probe",
        birth_date=date(1980, 1, 2),
    )
    return PatientRecord(
        patient=patient,
        encounters=[
            Encounter(
                id=f"feedface-e000-0000-0000-00000000000{n}",
                patient_id=patient.id,
                date_of_service=date(2023, 5, 10),
            )
            for n in range(encounters)
        ],
    )


def test_the_render_seam_balances_on_a_normal_run() -> None:
    from anastomosis.reconstruct.engine import RenderResult, _render_conservation

    result = RenderResult()
    result.rendered.extend([Path("a.pdf"), Path("b.pdf")])
    result.skipped.append(Path("c.pdf"))
    result.failed.append(("feedface-e000-0000-0000-000000000003", "ValueError"))
    _render_conservation(4, result).check()


def test_an_encounter_that_reached_no_column_stops_the_render() -> None:
    """The #121 shape: two encounters, one page, and the run reported two.

    An encounter that ends in none of rendered/skipped/failed has left the
    accounting — and every downstream number is then computed over the
    survivors, which is exactly how a loss reports clean.
    """
    from anastomosis.reconstruct.engine import RenderResult, _render_conservation

    result = RenderResult()
    result.rendered.append(Path("a.pdf"))
    with pytest.raises(ConservationError, match="canonical -> rendered"):
        _render_conservation(2, result).check()


def test_the_engine_itself_refuses_a_run_it_cannot_account_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not the helper in isolation — the engine, driven end to end, with a
    ``_render_one`` that swallows one encounter the way a real regression
    would."""
    from anastomosis.reconstruct import discover_packs
    from anastomosis.reconstruct.engine import ReconstructionEngine

    pack = discover_packs()["generic_soap"].pack
    assert pack is not None

    def _renderer() -> object:  # never reached: _render_one is stubbed out
        raise AssertionError("no renderer should be needed")

    engine = ReconstructionEngine(pack, _renderer)  # type: ignore[arg-type]
    monkeypatch.setattr(
        ReconstructionEngine, "_render_one", lambda *args, **kwargs: None, raising=True
    )
    with pytest.raises(ConservationError, match="2 encounter\\(s\\) went in"):
        engine.run([_record(2)], tmp_path / "charts")  # type: ignore[list-item]


# --- seam: offered -> ledger --------------------------------------------------


def test_an_item_that_never_got_a_ledger_row_stops_the_upload() -> None:
    """Every state the upload engine reports comes from the ledger, so an item
    with no row is invisible to all of them: the run finishes, the counts add
    up among the rows that exist, and a chart nobody filed reports as
    nothing."""
    from anastomosis.deliver.browser.engine import _ledger_conservation

    keys = ["item-a", "item-b", "item-c"]
    _ledger_conservation(keys, 3).check()
    with pytest.raises(ConservationError, match="offered -> ledger"):
        _ledger_conservation(keys, 2).check()


def test_the_same_item_offered_twice_is_one_obligation() -> None:
    """Enqueue is idempotent, so a duplicated key is one row and must be one
    unit — counting it twice would refuse a correct run."""
    from anastomosis.deliver.browser.engine import _ledger_conservation

    _ledger_conservation(["item-a", "item-a", "item-b"], 2).check()


# --- seam: canonical -> delivered ---------------------------------------------


def test_the_archive_seam_balances_and_notices_a_chart_that_reached_nobody(
    tmp_path: Path,
) -> None:
    """#110's shape: nothing malformed, and a chart that arrived nowhere.

    The obligation is the union of what the render index NAMES and what is
    actually sitting in the charts directory, so a chart the index forgot is
    still owed an answer — which is what stops it being left behind.
    """
    from anastomosis.deliver.archive.archive import _chart_conservation
    from anastomosis.deliver.render_index import RenderEntry, RenderIndex

    record = _record(2)
    charts = tmp_path / "charts"
    charts.mkdir()
    names = ["chart_a.pdf", "chart_b.pdf"]
    for name in names:
        (charts / name).write_bytes(b"%PDF-1.4\n")
    index = RenderIndex.from_entries(
        RenderEntry(
            pdf=name,
            patient_id=record.patient.id,  # type: ignore[attr-defined]
            encounter_id=enc.id,
        )
        for name, enc in zip(names, record.encounters, strict=True)  # type: ignore[attr-defined]
    )

    _chart_conservation(index, [record], charts, delivered=2, unattributed=0, missing=0).check()  # type: ignore[list-item]

    # One chart delivered, one nowhere: not copied, not swept, not counted.
    with pytest.raises(ConservationError, match="canonical -> delivered"):
        _chart_conservation(index, [record], charts, delivered=1, unattributed=0, missing=0).check()  # type: ignore[list-item]


def test_a_chart_the_index_never_named_is_still_owed_an_answer(tmp_path: Path) -> None:
    """A file in the charts directory that nothing indexes has to end
    somewhere — swept into unattributed/ — rather than being quietly left where
    it is because no index entry asked after it."""
    from anastomosis.deliver.archive.archive import _chart_conservation
    from anastomosis.deliver.render_index import RenderIndex

    charts = tmp_path / "charts"
    charts.mkdir()
    (charts / "stray.pdf").write_bytes(b"%PDF-1.4\n")
    empty = RenderIndex.from_entries([])

    _chart_conservation(empty, [], charts, delivered=0, unattributed=1, missing=0).check()
    with pytest.raises(ConservationError, match="1 chart\\(s\\) went in"):
        _chart_conservation(empty, [], charts, delivered=0, unattributed=0, missing=0).check()
