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

from anastomosis.core.model import Patient, PatientRecord
from anastomosis.pipeline import PipelineError, load_records
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


def test_successful_load_returns_records() -> None:
    records = load_records(_Good(), Path("."))
    assert len(records) == 1
    assert records[0].patient.family_name == "Specimen"
