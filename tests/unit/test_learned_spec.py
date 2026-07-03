# AI-assisted: written with Claude agents under the author's direction and review; see DESIGN.md.
"""Validation of the learned-mapping spec — the safety boundary for run-a-mapping.

The spec is the whole contract: it must reject unknown keys, unknown canonical
targets, unknown transforms, and internally inconsistent grouping/columns, and
it must parse from JSON (never yaml/code).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from anastomosis.sources.learned.reader import header_fingerprint
from anastomosis.sources.learned.spec import MappingError, MappingSpec, load_spec

COLUMNS = ["PatientID", "First", "Last", "DOB"]


def _valid_spec_dict(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "mapping_id": "acme_csv",
        "spec_version": 1,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
        "human_reviewed": True,
        "display": "Acme CSV",
        "source_format": {
            "type": "csv",
            "delimiter": ",",
            "encoding": "utf-8-sig",
            "header_fingerprint": header_fingerprint(COLUMNS),
            "columns": COLUMNS,
        },
        "grouping": {"patient_key": "PatientID", "encounter_key": None, "row_scope": "patient"},
        "field_mappings": [
            {"source_path": "First", "target_path": "patient.given_name", "transform": "strip"},
            {"source_path": "Last", "target_path": "patient.family_name", "transform": "strip"},
            {"source_path": "DOB", "target_path": "patient.birth_date", "transform": "parse_date"},
        ],
    }
    base.update(overrides)
    return base


def test_valid_spec_round_trips() -> None:
    spec = MappingSpec.model_validate(_valid_spec_dict())
    assert spec.mapping_id == "acme_csv"
    assert len(spec.field_mappings) == 3


def test_extra_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        MappingSpec.model_validate(_valid_spec_dict(surprise="boom"))


def test_unknown_target_path_is_rejected() -> None:
    bad = _valid_spec_dict()
    bad["field_mappings"][0]["target_path"] = "patient.not_a_field"
    with pytest.raises(ValidationError):
        MappingSpec.model_validate(bad)


def test_unknown_transform_is_rejected() -> None:
    bad = _valid_spec_dict()
    bad["field_mappings"][0]["transform"] = "teleport"
    with pytest.raises(ValidationError):
        MappingSpec.model_validate(bad)


def test_bad_mapping_id_is_rejected() -> None:
    with pytest.raises(ValidationError):
        MappingSpec.model_validate(_valid_spec_dict(mapping_id="Acme-CSV"))


def test_patient_key_must_be_a_column() -> None:
    bad = _valid_spec_dict()
    bad["grouping"]["patient_key"] = "Nonexistent"
    with pytest.raises(ValidationError):
        MappingSpec.model_validate(bad)


def test_source_path_must_be_a_column() -> None:
    bad = _valid_spec_dict()
    bad["field_mappings"][0]["source_path"] = "Ghost"
    with pytest.raises(ValidationError):
        MappingSpec.model_validate(bad)


def test_duplicate_target_path_is_rejected() -> None:
    bad = _valid_spec_dict()
    bad["field_mappings"][1]["target_path"] = "patient.given_name"  # same as [0]
    with pytest.raises(ValidationError):
        MappingSpec.model_validate(bad)


def test_encounter_key_equal_to_patient_key_is_rejected() -> None:
    bad = _valid_spec_dict()
    bad["grouping"]["encounter_key"] = "PatientID"  # same as patient_key → collapses encounters
    with pytest.raises(ValidationError):
        MappingSpec.model_validate(bad)


def test_load_spec_reads_json(tmp_path: Path) -> None:
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(_valid_spec_dict()), encoding="utf-8")
    spec = load_spec(path)
    assert spec.display == "Acme CSV"


def test_load_spec_loud_on_missing_file(tmp_path: Path) -> None:
    with pytest.raises(MappingError):
        load_spec(tmp_path / "nope.json")


def test_load_spec_loud_on_non_json(tmp_path: Path) -> None:
    path = tmp_path / "mapping.json"
    path.write_text("not json {{{", encoding="utf-8")
    with pytest.raises(MappingError):
        load_spec(path)


def test_load_spec_loud_on_non_object(tmp_path: Path) -> None:
    path = tmp_path / "mapping.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(MappingError):
        load_spec(path)


def test_load_spec_loud_on_invalid_spec(tmp_path: Path) -> None:
    path = tmp_path / "mapping.json"
    bad = _valid_spec_dict()
    bad["field_mappings"][0]["target_path"] = "patient.not_a_field"
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(MappingError):
        load_spec(path)
