# AI-assisted: written with Claude agents under the author's direction and review; see DESIGN.md.
"""The generic learned-source adapter: lossless flat→canonical, detect, discovery.

Exercises the interpreter end to end against a PHI-safe synthetic CSV (feedface-
ids, area-900 SSNs, 555 phones, example.com), the assembled-path builders, value
translation, the fingerprint detect/stale logic, and defensive discovery.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from anastomosis.core.model_paths import ASSEMBLED_ENCOUNTER_PATHS, ASSEMBLED_PATIENT_PATHS
from anastomosis.core.sourcelearn import analyze_source, build_mapping
from anastomosis.sources.learned import discover_learned_specs, register_learned_sources
from anastomosis.sources.learned.interpreter import LearnedSourceAdapter
from anastomosis.sources.learned.reader import header_fingerprint
from anastomosis.sources.learned.spec import (
    FieldMapping,
    Grouping,
    MappingError,
    MappingSpec,
    SourceFormat,
    load_spec,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "learned" / "clinic_visits.csv"


def _fixture_spec() -> MappingSpec:
    analysis = analyze_source(FIXTURE)
    return build_mapping(
        analysis,
        mapping_id="clinic_visits",
        display="Clinic Visits",
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _write_csv(path: Path, columns: list[str], rows: list[list[str]]) -> None:
    lines = [",".join(columns), *[",".join(r) for r in rows]]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --- end-to-end load -----------------------------------------------------------


def test_load_groups_patients_and_encounters() -> None:
    records = list(LearnedSourceAdapter(_fixture_spec()).load(FIXTURE))
    assert [r.patient.display_name for r in records] == [
        "Ada Fixture",
        "Boris Sample",
        "Cleo Placeholder",
    ]
    by_name = {r.patient.display_name: r for r in records}
    ada = by_name["Ada Fixture"]
    assert ada.patient.birth_date.isoformat() == "1985-03-14"
    assert ada.patient.sex == "F"
    assert len(ada.encounters) == 3  # one per visit row
    assert len(by_name["Boris Sample"].encounters) == 2
    assert len(by_name["Cleo Placeholder"].encounters) == 1


def test_assembled_demographics_and_sections() -> None:
    ada = next(r for r in LearnedSourceAdapter(_fixture_spec()).load(FIXTURE))
    assert ada.patient.addresses[0].city == "Testville"
    assert ada.patient.addresses[0].postal_code == "90210"
    emails = [c.value for c in ada.patient.telecom if c.kind.value == "email"]
    assert emails == ["ada@example.com"]
    ssns = [i.value for i in ada.patient.identifiers if i.kind.value == "ssn"]
    assert ssns == ["900-12-3456"]
    first = ada.encounters[0]
    assert first.date_of_service.isoformat() == "2023-01-05"
    assert first.chief_complaint == "Cough"
    kinds = {s.kind.value for s in first.sections}
    assert {"subjective", "objective", "assessment", "plan"} <= kinds


def test_unmapped_column_is_preserved_in_extensions() -> None:
    # ProviderName is not a canonical target; it must survive in extensions
    # (encounter-grained), never be dropped.
    ada = next(r for r in LearnedSourceAdapter(_fixture_spec()).load(FIXTURE))
    ext = ada.encounters[0].extensions
    assert "learned:clinic_visits:ProviderName" in ext
    assert ext["learned:clinic_visits:ProviderName"] == "Dr Placeholder"


def test_load_is_deterministic() -> None:
    spec = _fixture_spec()

    def shape() -> list[object]:
        return [
            (
                r.patient.id,
                r.patient.display_name,
                [
                    (e.id, e.date_of_service, tuple(s.text for s in e.sections))
                    for e in r.encounters
                ],
            )
            for r in LearnedSourceAdapter(spec).load(FIXTURE)
        ]

    assert shape() == shape()


def test_detect_matches_fixture_dir_and_rejects_others(tmp_path: Path) -> None:
    adapter = LearnedSourceAdapter(_fixture_spec())
    assert adapter.detect(FIXTURE.parent) is True  # dir holding the matching file
    assert adapter.detect(FIXTURE) is True  # the file itself
    assert adapter.detect(tmp_path) is False  # empty dir — no candidate


def test_stale_columns_are_not_detected_and_load_is_loud(tmp_path: Path) -> None:
    spec = _fixture_spec()
    # A CSV of the right type but DIFFERENT columns: detect must not claim it.
    stale = tmp_path / "other.csv"
    _write_csv(stale, ["Totally", "Different", "Columns"], [["a", "b", "c"]])
    adapter = LearnedSourceAdapter(spec)
    assert adapter.detect(tmp_path) is False
    with pytest.raises(MappingError):
        list(adapter.load(tmp_path))


# --- value translation (terminology, kept separate from structural mapping) ---


def test_value_translation_applied_before_transform(tmp_path: Path) -> None:
    columns = ["PID", "Gender"]
    csv_path = tmp_path / "people.csv"
    _write_csv(csv_path, columns, [["p1", "M"], ["p2", "F"]])
    spec = MappingSpec(
        mapping_id="xlate",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        human_reviewed=True,
        display="Xlate",
        source_format=SourceFormat(
            type="csv",
            delimiter=",",
            header_fingerprint=header_fingerprint(columns),
            columns=columns,
        ),
        grouping=Grouping(patient_key="PID", row_scope="patient"),
        field_mappings=[FieldMapping(source_path="Gender", target_path="patient.sex")],
        value_translations=[{"source_path": "Gender", "table": {"M": "male", "F": "female"}}],
    )
    sexes = [r.patient.sex for r in LearnedSourceAdapter(spec).load(csv_path)]
    assert sexes == ["male", "female"]


def test_interpreter_handles_every_assembled_path(tmp_path: Path) -> None:
    # One column per assembled target, proving each builder path is wired.
    assembled = sorted(ASSEMBLED_PATIENT_PATHS | ASSEMBLED_ENCOUNTER_PATHS)
    columns = ["PID", *[p.replace(".", "_") for p in assembled]]
    row = ["p1"]
    values = {
        "patient.address.postal_code": "90210",
        "patient.email": "x@example.com",
        "patient.ssn": "900-00-0000",
    }
    row += [values.get(p, f"v_{i}") for i, p in enumerate(assembled)]
    csv_path = tmp_path / "wide.csv"
    _write_csv(csv_path, columns, [row])
    field_mappings = [
        FieldMapping(source_path=p.replace(".", "_"), target_path=p) for p in assembled
    ]
    spec = MappingSpec(
        mapping_id="wide",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        human_reviewed=True,
        display="Wide",
        source_format=SourceFormat(
            type="csv",
            delimiter=",",
            header_fingerprint=header_fingerprint(columns),
            columns=columns,
        ),
        grouping=Grouping(patient_key="PID", row_scope="encounter"),
        field_mappings=field_mappings,
    )
    record = next(iter(LearnedSourceAdapter(spec).load(csv_path)))
    assert record.patient.addresses[0].postal_code == "90210"
    assert any(c.kind.value == "email" for c in record.patient.telecom)
    assert len({c.kind.value for c in record.patient.telecom if "phone" in c.kind.value}) == 4
    assert {i.kind.value for i in record.patient.identifiers} >= {
        "ssn",
        "mrn",
        "prn",
        "source_guid",
    }
    assert {s.kind.value for s in record.encounters[0].sections} == {
        "subjective",
        "objective",
        "assessment",
        "plan",
        "narrative",
    }


# --- discovery / registration --------------------------------------------------


def _save_via_dict(base: Path, mapping_id: str, *, reviewed: bool) -> None:
    import json

    columns = ["PID", "First"]
    spec = {
        "mapping_id": mapping_id,
        "spec_version": 1,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
        "human_reviewed": reviewed,
        "display": mapping_id,
        "source_format": {
            "type": "csv",
            "delimiter": ",",
            "encoding": "utf-8-sig",
            "header_fingerprint": header_fingerprint(columns),
            "columns": columns,
        },
        "grouping": {"patient_key": "PID", "encounter_key": None, "row_scope": "patient"},
        "field_mappings": [
            {"source_path": "First", "target_path": "patient.given_name", "transform": "strip"}
        ],
    }
    mapping_dir = base / mapping_id
    mapping_dir.mkdir(parents=True)
    (mapping_dir / "mapping.json").write_text(json.dumps(spec), encoding="utf-8")


def test_discover_loads_reviewed_and_skips_unreviewed_and_broken(tmp_path: Path) -> None:
    _save_via_dict(tmp_path, "disc_reviewed", reviewed=True)
    _save_via_dict(tmp_path, "disc_unreviewed", reviewed=False)
    (tmp_path / "disc_broken").mkdir()
    (tmp_path / "disc_broken" / "mapping.json").write_text("{ not json", encoding="utf-8")
    (tmp_path / "not_a_mapping").mkdir()  # no mapping.json

    discovered = {s.mapping_id for s in discover_learned_specs(tmp_path)}
    assert discovered == {"disc_reviewed"}


def test_discover_missing_dir_is_empty(tmp_path: Path) -> None:
    assert discover_learned_specs(tmp_path / "nope") == []


def test_register_adds_reviewed_and_skips_collision(tmp_path: Path) -> None:
    _save_via_dict(tmp_path, "reg_unique_src", reviewed=True)
    _save_via_dict(tmp_path, "ccda", reviewed=True)  # collides with a built-in

    added = register_learned_sources(tmp_path)
    assert "reg_unique_src" in added
    assert "ccda" not in added  # never shadows the built-in C-CDA source
    from anastomosis.sources import get_source

    assert get_source("reg_unique_src").name == "reg_unique_src"
    # Idempotent: a second pass adds nothing new.
    assert register_learned_sources(tmp_path) == []


def test_round_trip_via_saved_spec(tmp_path: Path) -> None:
    # A spec saved as JSON and reloaded loads the fixture identically.
    import json

    spec = _fixture_spec()
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(spec.model_dump(mode="json")), encoding="utf-8")
    reloaded = load_spec(path)
    names = [r.patient.display_name for r in LearnedSourceAdapter(reloaded).load(FIXTURE)]
    assert names == ["Ada Fixture", "Boris Sample", "Cleo Placeholder"]


# --- QA-review regressions -----------------------------------------------------


def _csv_spec(
    mapping_id: str,
    columns: list[str],
    *,
    row_scope: str = "patient",
    field_mappings: list[FieldMapping] | None = None,
    fmt_type: str = "csv",
) -> MappingSpec:
    return MappingSpec(
        mapping_id=mapping_id,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        human_reviewed=True,
        display=mapping_id,
        source_format=SourceFormat(
            type=fmt_type,  # type: ignore[arg-type]
            delimiter="," if fmt_type == "csv" else None,
            header_fingerprint=header_fingerprint(columns),
            columns=columns,
        ),
        grouping=Grouping(patient_key=columns[0], row_scope=row_scope),  # type: ignore[arg-type]
        field_mappings=field_mappings or [],
    )


def test_transform_error_is_value_free(tmp_path: Path) -> None:
    # A cell the transform cannot parse must raise a value-FREE MappingError —
    # never a traceback printing the offending value (PHI).
    columns = ["PID", "DOB"]
    csv_path = tmp_path / "p.csv"
    _write_csv(csv_path, columns, [["p1", "not-a-real-date-9999"]])
    spec = _csv_spec(
        "dt",
        columns,
        field_mappings=[
            FieldMapping(
                source_path="DOB", target_path="patient.birth_date", transform="parse_date"
            )
        ],
    )
    with pytest.raises(MappingError) as excinfo:
        list(LearnedSourceAdapter(spec).load(csv_path))
    assert "not-a-real-date-9999" not in str(excinfo.value)  # value must not leak
    assert "DOB" in str(excinfo.value)  # naming the column is PHI-safe


def test_malformed_json_is_loud_and_detect_does_not_crash(tmp_path: Path) -> None:
    columns = ["id", "first"]
    spec = _csv_spec(
        "j",
        columns,
        fmt_type="json",
        field_mappings=[FieldMapping(source_path="first", target_path="patient.given_name")],
    )
    (tmp_path / "broken.json").write_text("{ not valid json", encoding="utf-8")
    adapter = LearnedSourceAdapter(spec)
    # detect catches only MappingError — a raw JSONDecodeError would crash the
    # whole detect_source sweep, so the read must raise the typed error.
    assert adapter.detect(tmp_path) is False
    from anastomosis.sources.learned.reader import read_rows

    with pytest.raises(MappingError):
        read_rows(tmp_path / "broken.json", spec.source_format)


def test_encounter_grained_preserves_per_row_unmapped_values(tmp_path: Path) -> None:
    # Repeated patient key, encounter-grained, NO encounter field mapped: each row
    # must become its own encounter so distinct un-mapped values are not collapsed.
    columns = ["MRN", "LastName", "LabColor"]
    csv_path = tmp_path / "labs.csv"
    _write_csv(csv_path, columns, [["M1", "Alice", "red"], ["M1", "Alice", "blue"]])
    spec = _csv_spec(
        "labs",
        columns,
        row_scope="encounter",
        field_mappings=[FieldMapping(source_path="LastName", target_path="patient.family_name")],
    )
    records = list(LearnedSourceAdapter(spec).load(csv_path))
    assert len(records) == 1
    encounters = records[0].encounters
    assert len(encounters) == 2  # one per row, even with no encounter field mapped
    colors = {e.extensions.get("learned:labs:LabColor") for e in encounters}
    assert colors == {"red", "blue"}  # both distinct values preserved
