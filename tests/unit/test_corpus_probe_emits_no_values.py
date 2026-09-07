"""The corpus probe reports shape, never content — proven, not asserted.

The probe reads real charts, so "it only prints counts" cannot rest on
inspection: every key it can print is a string literal declared in the
probe itself, and the only thing it takes from a chart is a yes/no and a
length. This file proves both ends: the declared vocabulary is the schema
(no silent drift from a model's fields), and a chart parsed by the real
parser contributes none of its own strings to the output.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from anastomosis.core.model import Encounter, Patient, PatientRecord
from anastomosis.sources.ccda.parser import parse_document

REPO_ROOT = Path(__file__).resolve().parents[2]
_PROBE = REPO_ROOT / "docs" / "audits" / "learned-source" / "tools" / "probe_ccda_corpus.py"
_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "ccda" / "feedface_ccd.xml"
#: Short strings are excluded from the leak check: a two- or three-letter value
#: collides with ordinary JSON punctuation and field names by accident, which
#: would make the test fail for a reason that is not a leak.
_MIN_LEAKABLE = 4


@pytest.fixture(scope="module")
def probe() -> ModuleType:
    """Import the probe by path; it lives under docs/, not in the package."""
    spec = importlib.util.spec_from_file_location("probe_ccda_corpus", _PROBE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _strings(value: Any, found: set[str]) -> None:
    """Every string anywhere in a dumped record, however deeply nested."""
    if isinstance(value, str):
        found.add(value)
    elif isinstance(value, dict):
        for item in value.values():
            _strings(item, found)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _strings(item, found)


def test_the_declared_vocabulary_is_the_schema(probe: ModuleType) -> None:
    """The probe's literal field names match the models exactly, both
    ways: written out rather than read off the model at runtime, they
    could otherwise drift, letting a field added to Patient go unreported
    without anyone noticing."""
    assert probe.PATIENT_FIELDS == tuple(Patient.model_fields)
    assert probe.ENCOUNTER_FIELDS == tuple(Encounter.model_fields)
    empty = PatientRecord(patient=Patient(id="x"))
    for name in probe.COLLECTION_FIELDS:
        assert isinstance(getattr(empty, name), list), f"{name} is not a collection on the record"


def test_a_filled_field_is_answered_yes_or_no_and_nothing_more(probe: ModuleType) -> None:
    """The one function a chart's values reach returns a bool, so a caller
    cannot accidentally carry the value forward."""
    for empty in (None, "", [], {}):
        assert probe._is_filled(empty) is False
    for filled in ("zzsentinel", ["x"], {"k": "v"}, 0):
        assert probe._is_filled(filled) is True


def test_no_string_from_a_parsed_chart_appears_in_the_probes_output(
    probe: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Runs the probe over an archive holding a real (synthetic) C-CDA,
    takes every string the parser found in that chart, and requires that
    none of them reach what the probe printed."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    with zipfile.ZipFile(corpus / "bundle.zip", "w") as bundle:
        bundle.write(_FIXTURE, arcname="chart.xml")

    argv = sys.argv
    sys.argv = ["probe_ccda_corpus", str(corpus), str(tmp_path / "scratch")]
    try:
        assert probe.main() == 0
    finally:
        sys.argv = argv

    printed = capsys.readouterr().out.strip()
    result = json.loads(printed)
    assert result["counts"]["parsed"] == 1, "the fixture did not parse; the test proves nothing"

    declared = set(probe.PATIENT_FIELDS) | set(probe.ENCOUNTER_FIELDS)
    assert set(result["patient_field_presence"]) <= declared
    assert set(result["encounter_field_presence"]) <= declared
    assert set(result["collection_totals"]) == set(probe.COLLECTION_FIELDS)
    assert all(isinstance(count, int) for count in result["collection_totals"].values())

    chart: set[str] = set()
    _strings(parse_document(_FIXTURE).model_dump(mode="json"), chart)
    leakable = {
        text
        for text in chart
        if len(text) >= _MIN_LEAKABLE and text not in declared | set(probe.COLLECTION_FIELDS)
    }
    assert leakable, "the fixture carries no distinctive strings; the test proves nothing"
    for text in sorted(leakable):
        assert text not in printed, "a value from the chart reached the probe's output"
