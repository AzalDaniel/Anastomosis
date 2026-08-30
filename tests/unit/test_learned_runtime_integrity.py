"""Runtime grain-integrity regressions for learned flat-source mappings."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from anastomosis.sources.learned.interpreter import LearnedSourceAdapter
from anastomosis.sources.learned.reader import header_fingerprint
from anastomosis.sources.learned.spec import (
    FieldMapping,
    Grouping,
    MappingError,
    MappingSpec,
    SourceFormat,
)


def _spec(
    columns: list[str],
    *,
    row_scope: str,
    field_mappings: list[FieldMapping] | None = None,
    encounter_key: str | None = None,
    source_type: str = "csv",
) -> MappingSpec:
    return MappingSpec(
        mapping_id="runtime_integrity",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        human_reviewed=True,
        display="Runtime integrity",
        source_format=SourceFormat(
            type=source_type,  # type: ignore[arg-type]
            delimiter="," if source_type == "csv" else None,
            header_fingerprint=header_fingerprint(columns),
            columns=columns,
        ),
        grouping=Grouping(
            patient_key="PID",
            encounter_key=encounter_key,
            row_scope=row_scope,  # type: ignore[arg-type]
        ),
        field_mappings=field_mappings or [],
    )


def _write_csv(path: Path, columns: list[str], rows: list[list[str]]) -> None:
    path.write_text("\n".join([",".join(columns), *[",".join(row) for row in rows]]) + "\n")


@pytest.mark.parametrize(
    ("source_type", "contents"),
    [
        ("csv", "PID,Name\n   ,patient-name-secret\n"),
        ("json", json.dumps([{"PID": "patient-id-secret"}, {"Name": "patient-name-secret"}])),
    ],
)
def test_blank_or_missing_patient_key_is_rejected_without_values(
    tmp_path: Path, source_type: str, contents: str
) -> None:
    source = tmp_path / f"people.{source_type}"
    source.write_text(contents, encoding="utf-8")
    spec = _spec(["PID", "Name"], row_scope="patient", source_type=source_type)

    with pytest.raises(MappingError) as excinfo:
        list(LearnedSourceAdapter(spec).load(source))

    message = str(excinfo.value)
    assert "PID" in message
    assert "patient-id-secret" not in message
    assert "patient-name-secret" not in message


def test_patient_grained_duplicate_patient_key_is_rejected_without_values(tmp_path: Path) -> None:
    source = tmp_path / "people.csv"
    _write_csv(
        source,
        ["PID", "Name"],
        [["patient-id-secret", "first-name-secret"], ["patient-id-secret", "second-name-secret"]],
    )
    spec = _spec(["PID", "Name"], row_scope="patient")

    with pytest.raises(MappingError) as excinfo:
        list(LearnedSourceAdapter(spec).load(source))

    message = str(excinfo.value)
    assert "row_scope='patient'" in message
    assert "PID" in message
    assert "patient-id-secret" not in message
    assert "first-name-secret" not in message
    assert "second-name-secret" not in message


def test_encounter_grained_conflicting_patient_values_are_rejected_without_values(
    tmp_path: Path,
) -> None:
    source = tmp_path / "visits.csv"
    _write_csv(
        source,
        ["PID", "EID", "Family"],
        [
            ["patient-id-secret", "encounter-one-secret", "Alpha-secret"],
            ["patient-id-secret", "encounter-two-secret", "Beta-secret"],
        ],
    )
    spec = _spec(
        ["PID", "EID", "Family"],
        row_scope="encounter",
        encounter_key="EID",
        field_mappings=[
            FieldMapping(source_path="Family", target_path="patient.family_name", transform="lower")
        ],
    )

    with pytest.raises(MappingError) as excinfo:
        list(LearnedSourceAdapter(spec).load(source))

    message = str(excinfo.value)
    assert "patient.family_name" in message
    assert "Family" in message
    assert "patient-id-secret" not in message
    assert "encounter-one-secret" not in message
    assert "encounter-two-secret" not in message
    assert "alpha-secret" not in message.lower()
    assert "beta-secret" not in message.lower()


def test_encounter_grained_duplicate_encounter_key_is_rejected_without_values(
    tmp_path: Path,
) -> None:
    source = tmp_path / "visits.csv"
    _write_csv(
        source,
        ["PID", "EID"],
        [
            ["patient-id-secret", "encounter-id-secret"],
            ["patient-id-secret", "encounter-id-secret"],
        ],
    )
    spec = _spec(["PID", "EID"], row_scope="encounter", encounter_key="EID")

    with pytest.raises(MappingError) as excinfo:
        list(LearnedSourceAdapter(spec).load(source))

    message = str(excinfo.value)
    assert "EID" in message
    assert "patient-id-secret" not in message
    assert "encounter-id-secret" not in message


def test_encounter_grained_identical_or_blank_patient_values_are_allowed(tmp_path: Path) -> None:
    source = tmp_path / "visits.csv"
    _write_csv(
        source,
        ["PID", "EID", "Family"],
        [
            ["patient-id", "encounter-one", "   "],
            ["patient-id", "encounter-two", "RIVERA"],
            ["patient-id", "encounter-three", "rivera"],
        ],
    )
    spec = _spec(
        ["PID", "EID", "Family"],
        row_scope="encounter",
        encounter_key="EID",
        field_mappings=[
            FieldMapping(source_path="Family", target_path="patient.family_name", transform="lower")
        ],
    )

    records = list(LearnedSourceAdapter(spec).load(source))

    assert len(records) == 1
    assert records[0].patient.family_name == "rivera"
    assert len(records[0].encounters) == 3
