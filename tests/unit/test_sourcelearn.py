# AI-assisted: written with Claude agents under the author's direction and review; see DESIGN.md.
"""The authoring half of learn-a-source: detect, profile, score, build, save.

Verifies the matcher proposes the right canonical fields for conventional column
names, that profiling is PHI-safe (no raw value escapes), that the round-trip
proves losslessness, and that saving is owner-only and refuses unreviewed specs.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from anastomosis.core.sourcelearn import (
    FuzzyNameScorer,
    analyze_source,
    build_mapping,
    detect_format,
    profile_columns,
    round_trip,
    save_mapping,
)
from anastomosis.sources.learned.reader import read_rows
from anastomosis.sources.learned.spec import MappingError

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "learned" / "clinic_visits.csv"


def test_detect_format_reads_csv_columns_and_fingerprint() -> None:
    fmt = detect_format(FIXTURE)
    assert fmt.type == "csv"
    assert fmt.delimiter == ","
    assert "PatientID" in fmt.columns and "ProviderName" in fmt.columns
    assert len(fmt.header_fingerprint) == 64  # sha256 hex


def test_profile_columns_infers_types_and_masks_values() -> None:
    fmt = detect_format(FIXTURE)
    rows = read_rows(FIXTURE, fmt)
    profiles = {p.name: p for p in profile_columns(rows, fmt.columns)}
    assert profiles["Email"].inferred_type == "email"
    assert profiles["DOB"].inferred_type == "date"
    assert profiles["SSN"].inferred_type == "ssn"
    assert profiles["Zip"].inferred_type == "zip"
    # PHI-safe shape: digits→N, letters→A — never the raw value.
    assert profiles["SSN"].sample_shape == "NNN-NN-NNNN"
    assert "900-12-3456" not in profiles["SSN"].sample_shape
    # patient id repeats across visit rows (distinct < non_null).
    assert profiles["PatientID"].distinct < profiles["PatientID"].non_null


def test_analysis_summary_is_phi_safe() -> None:
    analysis = analyze_source(FIXTURE)
    blob = "\n".join(analysis.summary_lines())
    # Column names and types appear; raw patient values never do.
    assert "Email" in blob and "ssn" in blob
    for leak in ("Ada", "ada@example.com", "900-12-3456", "Fixture", "Cough"):
        assert leak not in blob


def test_matcher_proposes_expected_fields() -> None:
    analysis = analyze_source(FIXTURE)
    by_source = {s.source_path: s for s in analysis.suggestions}
    assert analysis.patient_key == "PatientID"
    assert analysis.row_scope == "encounter"
    assert by_source["FirstName"].target_path == "patient.given_name"
    assert by_source["LastName"].target_path == "patient.family_name"
    assert by_source["DOB"].target_path == "patient.birth_date"
    assert by_source["DOB"].transform == "parse_date"
    assert by_source["Email"].target_path == "patient.email"
    assert by_source["MobilePhone"].target_path == "patient.phone_mobile"
    assert by_source["MobilePhone"].transform == "phone"
    assert by_source["Zip"].target_path == "patient.address.postal_code"
    assert by_source["VisitDate"].target_path == "encounter.date_of_service"
    assert by_source["ChiefComplaint"].target_path == "encounter.chief_complaint"
    assert by_source["Subjective"].target_path == "encounter.subjective"
    # An unconventional column maps to nothing (preserved in extensions later).
    assert by_source["ProviderName"].target_path is None


def test_scorer_is_deterministic() -> None:
    analysis = analyze_source(FIXTURE)
    scorer = FuzzyNameScorer()
    profile = next(p for p in analysis.profiles if p.name == "MobilePhone")
    from anastomosis.core.model_paths import canonical_target_paths

    first = scorer.score(profile, canonical_target_paths())
    second = scorer.score(profile, canonical_target_paths())
    assert first == second
    assert first[0][0] == "patient.phone_mobile"  # the right phone slot wins


def test_build_and_round_trip_loses_nothing() -> None:
    analysis = analyze_source(FIXTURE)
    spec = build_mapping(
        analysis, mapping_id="clinic", display="Clinic", now=datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert spec.human_reviewed is True
    report = round_trip(spec, FIXTURE)
    assert report.ok is True
    assert report.dropped_columns == []
    assert report.record_count == 3
    # ProviderName is recorded as unmapped (still preserved via extensions).
    assert "ProviderName" in spec.unmapped_source_fields


def test_save_mapping_is_owner_only_and_writes_artifacts(tmp_path: Path) -> None:
    analysis = analyze_source(FIXTURE)
    spec = build_mapping(
        analysis, mapping_id="clinic", display="Clinic", now=datetime(2026, 1, 1, tzinfo=UTC)
    )
    mapping_dir = save_mapping(spec, tmp_path)
    assert (mapping_dir / "mapping.json").is_file()
    assert (mapping_dir / "MAPPING.md").is_file()
    assert (mapping_dir / "source_trust.json").is_file()
    # The saved JSON re-validates.
    reloaded = json.loads((mapping_dir / "mapping.json").read_text(encoding="utf-8"))
    assert reloaded["mapping_id"] == "clinic"
    if os.name == "posix":
        assert stat.S_IMODE((mapping_dir / "mapping.json").stat().st_mode) == 0o600
        assert stat.S_IMODE(mapping_dir.stat().st_mode) == 0o700


def test_save_refuses_unreviewed(tmp_path: Path) -> None:
    analysis = analyze_source(FIXTURE)
    spec = build_mapping(
        analysis, mapping_id="clinic", display="Clinic", now=datetime(2026, 1, 1, tzinfo=UTC)
    )
    object.__setattr__(spec, "human_reviewed", False)
    with pytest.raises(MappingError):
        save_mapping(spec, tmp_path)


def test_analyze_json_array(tmp_path: Path) -> None:
    path = tmp_path / "people.json"
    path.write_text(
        json.dumps(
            [
                {"id": "p1", "first_name": "Ada", "last_name": "Fixture", "dob": "1985-03-14"},
                {"id": "p2", "first_name": "Boris", "last_name": "Sample", "dob": "1970-07-22"},
            ]
        ),
        encoding="utf-8",
    )
    analysis = analyze_source(path)
    assert analysis.fmt.type == "json"
    by_source = {s.source_path: s for s in analysis.suggestions}
    assert by_source["first_name"].target_path == "patient.given_name"
    assert by_source["last_name"].target_path == "patient.family_name"


def test_round_trip_detects_value_level_drop(tmp_path: Path) -> None:
    # row_scope=patient with duplicate patient rows collapses an un-mapped column
    # to last-value-wins. round_trip must catch the LOST VALUE, not merely confirm
    # the column name is present somewhere.
    from anastomosis.sources.learned.reader import header_fingerprint
    from anastomosis.sources.learned.spec import Grouping, MappingSpec, SourceFormat

    columns = ["PID", "Color"]
    csv_path = tmp_path / "dup.csv"
    csv_path.write_text("PID,Color\np1,red\np1,blue\n", encoding="utf-8")
    spec = MappingSpec(
        mapping_id="dup",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        human_reviewed=True,
        display="Dup",
        source_format=SourceFormat(
            type="csv",
            delimiter=",",
            header_fingerprint=header_fingerprint(columns),
            columns=columns,
        ),
        grouping=Grouping(patient_key="PID", row_scope="patient"),  # mislabeled grain
        field_mappings=[],
    )
    report = round_trip(spec, csv_path)
    assert report.ok is False
    assert "Color" in report.dropped_columns
