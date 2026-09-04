"""Tests for the pipeline-core input guard ``load_records`` (review P0-2/P0-3).

A malformed export must become a clean, PHI-safe ``bad_input`` error (exit 2)
rather than a raw traceback, and a load that yields zero records must become a
loud ``empty_export`` error rather than a silent 0-document "success" — while a
``PipelineError`` an adapter raises itself passes through unchanged. An
adapter's fail-closed ``SourceDataError`` keeps its own message: the tables and
counts it names ARE the repair instructions, and they are PHI-safe.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from anastomosis.core.conservation import Conservation
from anastomosis.core.model import Patient, PatientRecord
from anastomosis.pipeline import (
    LOSS_LEDGER_FILENAME,
    PipelineError,
    load_records,
    settle_source_ledger,
)
from anastomosis.sources.fhir_r4.mapper import AmbiguousUnanchoredError
from anastomosis.sources.pf_tebra.loader import OrphanRowsError


class _Adapter:
    name = "fake-src"
    description = "fake source"

    def detect(self, path: Path) -> bool:
        return True


class _Raises(_Adapter):
    def load(self, path: Path) -> Iterator[PatientRecord]:
        raise ValueError("a malformed 2020-99-99 value")


class _Empty(_Adapter):
    def load(self, path: Path) -> Iterator[PatientRecord]:
        return iter(())


class _Good(_Adapter):
    def load(self, path: Path) -> Iterator[PatientRecord]:
        yield PatientRecord(patient=Patient(family_name="Specimen"))


class _RaisesPipelineError(_Adapter):
    def load(self, path: Path) -> Iterator[PatientRecord]:
        raise PipelineError("adapter rejected the export", exit_code=2, kind="bad_source")


class _RaisesOrphanRows(_Adapter):
    def load(self, path: Path) -> Iterator[PatientRecord]:
        raise OrphanRowsError({"patient-medications": 3})


class _RaisesConservation(_Adapter):
    def load(self, path: Path) -> Iterator[PatientRecord]:
        # The shape the C-CDA adapter produces when its ledger cannot balance
        # a document's books mid-load (#315): a bare ConservationError out of
        # the iterator, counts and column names only.
        Conservation(stage="ccda xml -> canonical", unit="construct", offered=3).check()
        return iter(())  # pragma: no cover — check() above raises


class _RaisesAmbiguous(_Adapter):
    def load(self, path: Path) -> Iterator[PatientRecord]:
        raise AmbiguousUnanchoredError({"Condition": 2})


def test_malformed_export_becomes_bad_input() -> None:
    with pytest.raises(PipelineError) as excinfo:
        load_records(_Raises(), Path("."))
    assert excinfo.value.kind == "bad_input"
    assert excinfo.value.exit_code == 2
    # PHI-safe: names the source + the exception TYPE only, never the value.
    assert "ValueError" in str(excinfo.value)
    assert "2020-99-99" not in str(excinfo.value)


def test_zero_records_becomes_empty_export() -> None:
    with pytest.raises(PipelineError) as excinfo:
        load_records(_Empty(), Path("."))
    assert excinfo.value.kind == "empty_export"
    assert excinfo.value.exit_code == 2
    # No `skipped_files` on this fake adapter (`getattr` defaults to 0), so the
    # generic question is still the right one — the branch this test guards.
    assert "is this a fake-src export?" in str(excinfo.value)


def test_adapter_pipeline_error_passes_through() -> None:
    # An adapter's own clean PipelineError is not re-wrapped as bad_input.
    with pytest.raises(PipelineError) as excinfo:
        load_records(_RaisesPipelineError(), Path("."))
    assert excinfo.value.kind == "bad_source"


def test_orphan_rows_message_reaches_the_operator() -> None:
    """The refusal's diagnosis — which TABLE, how many rows — is the whole
    operational value of failing closed, so it must survive the wrap into
    ``PipelineError`` verbatim rather than collapsing to an exception type."""
    with pytest.raises(PipelineError) as excinfo:
        load_records(_RaisesOrphanRows(), Path("."))
    message = str(excinfo.value)
    assert excinfo.value.kind == "bad_input"
    assert excinfo.value.exit_code == 2
    assert "patient-medications" in message
    assert "3 row(s)" in message
    # Still PHI-safe: schema names and counts only, and the source is named.
    assert "fake-src" in message


def test_unanchored_resource_types_and_counts_reach_the_operator() -> None:
    with pytest.raises(PipelineError) as excinfo:
        load_records(_RaisesAmbiguous(), Path("."))
    message = str(excinfo.value)
    assert excinfo.value.kind == "bad_input"
    assert "Condition (2)" in message


def test_an_unbalanced_ledger_is_a_conservation_refusal() -> None:
    """The load's own instrument failing to account for a document refuses the
    run under its own name — the message that says which column went short is
    the repair instruction, and "(ConservationError)" is not."""
    with pytest.raises(PipelineError) as excinfo:
        load_records(_RaisesConservation(), Path("."))
    assert excinfo.value.kind == "conservation_failed"
    assert excinfo.value.exit_code == 1
    assert "3 construct(s) went in and never came out" in str(excinfo.value)


def test_successful_load_returns_records() -> None:
    records = load_records(_Good(), Path("."))
    assert len(records) == 1
    assert records[0].patient.family_name == "Specimen"


# --- a bad export path must be named as a bad path, not a bad format ----------


def test_a_missing_export_folder_says_so(tmp_path: Path) -> None:
    """Detection answers "which format is this?", so it cannot tell a folder of
    unrecognised files from a folder that is not there. Blaming the format sent
    the person off to pick one, which then failed too."""
    from anastomosis.pipeline import PipelineError, resolve_source

    with pytest.raises(PipelineError) as excinfo:
        resolve_source(tmp_path / "not-here", None)
    assert "no folder at" in str(excinfo.value)
    assert "export format" not in str(excinfo.value)


def test_a_file_given_as_the_export_folder_says_so(tmp_path: Path) -> None:
    from anastomosis.pipeline import PipelineError, resolve_source

    a_file = tmp_path / "export.tsv"
    a_file.write_text("PatientPracticeGuid\n", encoding="utf-8")
    with pytest.raises(PipelineError) as excinfo:
        resolve_source(a_file, None)
    assert "is a file, not a folder" in str(excinfo.value)


# --- the source ledger settles beside the charts (#315) -----------------------


def test_a_ccda_load_settles_its_ledger_and_reading(tmp_path: Path) -> None:
    """The full account lands in loss_ledger.json, PHI-vetted at the point of
    writing, and the reading comes back for the frontends — measured off a
    real load of the reference fixture, not a canned corpus."""
    import json

    from anastomosis.sources import get_source

    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "ccda"
    adapter = get_source("ccda")
    records = load_records(adapter, fixture)
    assert len(records) == 1
    reading = settle_source_ledger(adapter, tmp_path)
    assert reading and all(isinstance(line, str) for line in reading)
    assert any("became data" in line for line in reading)
    report = json.loads((tmp_path / LOSS_LEDGER_FILENAME).read_text(encoding="utf-8"))
    assert report["documents"] == 1
    # The artifact is the vetted corpus report: construct rows, no free text.
    assert {row["construct"] for row in report["constructs"]} >= {"participation:author"}


def test_a_skipped_file_reaches_the_reading_and_the_ledger(tmp_path: Path) -> None:
    """#384: a document the adapter's own sniff recognises as a CDA but whose
    extension it does not read (``.txt`` here) is never opened, but the LOSS
    must not be silent the way ``.ccd`` itself used to be. The count rides the
    same settlement as every other construct — into ``loss_ledger.json`` and
    the physician reading — never a channel of its own."""
    import json
    import shutil

    from anastomosis.sources import get_source

    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "ccda"
    export = tmp_path / "export"
    export.mkdir()
    shutil.copy(fixture / "feedface_ccd.xml", export / "summary.xml")
    shutil.copy(fixture / "feedface_ccd.xml", export / "extra.txt")

    adapter = get_source("ccda")
    records = load_records(adapter, export)
    assert len(records) == 1  # the .txt copy was never opened
    assert adapter.skipped_files == 1

    out = tmp_path / "out"
    out.mkdir()
    reading = settle_source_ledger(adapter, out)
    assert any("1 file" in line and "skipped, not read" in line for line in reading)
    report = json.loads((out / LOSS_LEDGER_FILENAME).read_text(encoding="utf-8"))
    assert report["skipped_files"] == 1
    assert report["documents"] == 1  # the one document that WAS opened still balances


def test_an_export_of_only_wrongly_named_cda_documents_names_the_count(
    tmp_path: Path,
) -> None:
    """#384 round two, finding 2: ``load_records`` raises ``empty_export``
    before ``settle_source_ledger`` ever runs, so an export holding NOTHING
    but wrongly-extensioned CDA documents used to report the count nowhere
    and tell the operator "is this a ccda export?" — wrong in the one case
    that matters, since the adapter's own sniff recognised them. The refusal
    now carries the count and the three extensions instead, in the reading's
    own wording, and names no filename."""
    import shutil

    from anastomosis.sources import get_source

    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "ccda"
    export = tmp_path / "export"
    export.mkdir()
    shutil.copy(fixture / "feedface_ccd.xml", export / "chart.txt")
    shutil.copy(fixture / "feedface_ccd.xml", export / "chart2.txt")

    adapter = get_source("ccda")
    with pytest.raises(PipelineError) as excinfo:
        load_records(adapter, export)
    assert excinfo.value.kind == "empty_export"
    assert excinfo.value.exit_code == 2
    message = str(excinfo.value)
    assert "2 files" in message
    assert ".xml" in message and ".ccd" in message and ".ccda" in message
    assert "is this a ccda export?" not in message  # the sniff already answered yes
    assert "chart" not in message  # never a filename
    # No loss_ledger.json either: settle_source_ledger never runs on this path.
    assert not (tmp_path / LOSS_LEDGER_FILENAME).exists()


def test_a_source_without_a_ledger_settles_to_nothing(tmp_path: Path) -> None:
    """Every non-C-CDA adapter: empty reading, no artifact — and a STALE
    artifact from a previous run into the same folder is removed, so last
    run's account cannot read as this run's."""
    stale = tmp_path / LOSS_LEDGER_FILENAME
    stale.write_text("{}", encoding="utf-8")
    assert settle_source_ledger(_Good(), tmp_path) == ()
    assert not stale.exists()
