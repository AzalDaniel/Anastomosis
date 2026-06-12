"""Tests for the source-adapter registry."""

from collections.abc import Iterator
from pathlib import Path

import pytest

import anastomosis.sources.base as base
from anastomosis.core.model import Patient, PatientRecord
from anastomosis.sources.base import SourceAdapter, detect_source, get_source, register


class _FakeAdapter:
    def __init__(self, name: str, *, detects: bool) -> None:
        self.name = name
        self.description = f"fake {name}"
        self._detects = detects

    def detect(self, path: Path) -> bool:
        return self._detects

    def load(self, path: Path) -> Iterator[PatientRecord]:
        yield PatientRecord(patient=Patient(given_name="Synthetic"))


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "_REGISTRY", {})


def test_register_and_lookup() -> None:
    adapter = _FakeAdapter("fake", detects=True)
    register(adapter)
    assert get_source("fake") is adapter
    assert isinstance(adapter, SourceAdapter)


def test_double_registration_is_an_error() -> None:
    register(_FakeAdapter("fake", detects=True))
    with pytest.raises(ValueError, match="already registered"):
        register(_FakeAdapter("fake", detects=True))


def test_unknown_source_diagnosis_lists_available() -> None:
    register(_FakeAdapter("pf-tebra", detects=False))
    with pytest.raises(KeyError, match="pf-tebra"):
        get_source("epic-ehi")


def test_detect_source_unique_match(tmp_path: Path) -> None:
    winner = _FakeAdapter("a", detects=True)
    register(winner)
    register(_FakeAdapter("b", detects=False))
    assert detect_source(tmp_path) is winner


def test_detect_source_ambiguity_returns_none(tmp_path: Path) -> None:
    register(_FakeAdapter("a", detects=True))
    register(_FakeAdapter("b", detects=True))
    assert detect_source(tmp_path) is None
