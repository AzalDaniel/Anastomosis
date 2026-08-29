"""The shape report may only emit structure, and it must count what went nowhere.

This tool exists to be run on a corpus that cannot be shared, by an operator we
cannot supervise, and its output comes back to us. So the interesting tests are
not that it counts correctly — they are that it *cannot* carry a patient value
out, and that the counters it does carry are the ones that answer the question.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_TOOL = Path(__file__).resolve().parents[2] / "tools" / "ccda_shape_report.py"
_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ccda"


def _load() -> object:
    spec = importlib.util.spec_from_file_location("ccda_shape_report", _TOOL)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool() -> object:
    return _load()


@pytest.mark.parametrize(
    "value",
    [
        "Cora Specimen",
        "cora.specimen@example.com",
        "901-65-4329",
        "Persistent cough for three weeks",
        "/home/operator/exports/Specimen_Cora.xml",
        "Specimen_Cora_2023-05-10.xml",
        "2023-05-10",
        3.5,
        None,
    ],
)
def test_a_patient_value_cannot_leave(tool: object, value: object) -> None:
    """The vocabulary IS the control.

    Nothing here is rejected by a deny-list of things that look like PHI —
    that would only catch the spellings someone thought of. It is rejected
    because a patient's name is not an element name, an OID, a LOINC code or
    an integer, and those are the only things the whitelist admits.
    """
    assert tool._safe(value) is False  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "value",
    ["observation", "codeSystem", "2.16.840.1.113883.6.1", "8716-3", "len14+tz", "PQ", 0, 2103],
)
def test_structure_can_leave(tool: object, value: object) -> None:
    assert tool._safe(value) is True  # type: ignore[attr-defined]


def test_the_whole_report_is_walked_not_just_the_top_level(tool: object) -> None:
    """A value nested three deep is still a value.

    The check that matters is the recursive one: an unsafe string reaching the
    file inside some counter's dict is exactly how this would leak in practice.
    """
    tool._assert_safe({"shapes": {"elements": {"observation": 4}}})  # type: ignore[attr-defined]
    with pytest.raises(SystemExit, match="REFUSING"):
        tool._assert_safe({"shapes": {"elements": {"Cora Specimen": 1}}})  # type: ignore[attr-defined]
    with pytest.raises(SystemExit, match="REFUSING"):
        tool._assert_safe({"totals": {"note": "Persistent cough"}})  # type: ignore[attr-defined]


def test_a_timestamp_becomes_a_precision_not_a_date(tool: object) -> None:
    """#241 was a precision the parser mis-segmented, so precision is the fact
    worth carrying — and it carries none of the date."""
    assert tool._ts_shape("20230510150405.000-0500") == "len14+tz+frac"  # type: ignore[attr-defined]
    assert tool._ts_shape("20230510") == "len8"  # type: ignore[attr-defined]
    assert tool._ts_shape("2023") == "len4"  # type: ignore[attr-defined]
    for raw in ("20230510150405.000-0500", "20230510", "2023"):
        assert "2023051" not in tool._ts_shape(raw)[4:]  # type: ignore[attr-defined]


def test_it_counts_the_facts_that_went_nowhere(tool: object, tmp_path: Path) -> None:
    """The reason to run this at all.

    A document can parse cleanly and still produce a record whose values reach
    no chart. On the shipped fixture every observation is attached to no
    encounter — the same defect the external audit reproduced independently —
    and a report that only counted `observations` would call that a success.
    """
    out = tmp_path / "report.json"
    rc = tool.main.__wrapped__ if hasattr(tool.main, "__wrapped__") else None  # noqa: F841
    import sys

    argv = sys.argv
    sys.argv = ["ccda_shape_report.py", str(_FIXTURES), "--out", str(out)]
    try:
        assert tool.main() == 0  # type: ignore[attr-defined]
    finally:
        sys.argv = argv

    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["documents_parsed"] >= 1
    totals = report["totals"]
    assert totals["observations"] > 0, "the fixture does carry observations"
    assert "observations_unattributed" in totals, "and the report must say so"
    assert totals["observations_unattributed"] == totals["observations"]

    # And the file it wrote carries no patient text — the guard ran for real.
    tool._assert_safe(report)  # type: ignore[attr-defined]
