# AI-assisted: written with Claude agents under the author's direction and review; see DESIGN.md.
"""The shared learn-a-source command core (one flow, two frontends).

Pins the shared-core consolidation: ``resolve_example`` and the analyze -> confirm
-> build -> round-trip -> save flow live here once, returning enumerated codes
both frontends present. The CLI (`test_cli_source`) and GUI
(`test_gui_controller`) suites exercise the adapters; this pins the core directly.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from anastomosis.core.source_init_command import (
    SourceInitCommand,
    resolve_example,
    run_source_init_command,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "learned" / "clinic_visits.csv"


def test_resolve_example_file_dir_and_ambiguous(tmp_path: Path) -> None:
    assert resolve_example(FIXTURE) == (FIXTURE, "")
    assert resolve_example(tmp_path / "nope") == (None, "NoExampleFile")

    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "notes.txt").write_text("x", encoding="utf-8")  # not a learnable type
    assert resolve_example(empty) == (None, "NoExampleFile")

    one = tmp_path / "one"
    one.mkdir()
    shutil.copy(FIXTURE, one / "export.csv")
    assert resolve_example(one) == (one / "export.csv", "")

    two = tmp_path / "two"
    two.mkdir()
    shutil.copy(FIXTURE, two / "a.csv")
    shutil.copy(FIXTURE, two / "b.csv")
    assert resolve_example(two) == (None, "AmbiguousExample")


def test_invalid_name_is_rejected_before_analysis() -> None:
    result = run_source_init_command(SourceInitCommand(example=FIXTURE, name="Bad-Name"))
    assert result.ok is False
    assert result.error == "InvalidSourceName"
    assert result.fmt_type is None  # never analyzed


def test_confirmation_required_returns_phi_safe_proposal(tmp_path: Path) -> None:
    result = run_source_init_command(
        SourceInitCommand(example=FIXTURE, name="clinic_csv", out_dir=tmp_path, confirmed=False)
    )
    assert result.ok is False
    assert result.error == "ConfirmationRequired"
    assert result.patient_key == "PatientID"
    assert result.fmt_type is not None
    assert result.columns > 0
    assert result.suggestions  # at least one column suggestion
    # PHI probe: the proposal carries column names/types/masked shapes only.
    blob = repr(result)
    for leak in ("Ada", "900-12-3456", "ada@example.com"):
        assert leak not in blob
    assert not (tmp_path / "clinic_csv").exists()  # nothing written


def test_confirmed_builds_round_trips_and_saves(tmp_path: Path) -> None:
    result = run_source_init_command(
        SourceInitCommand(
            example=FIXTURE,
            name="clinic_csv",
            display="Clinic CSV",
            out_dir=tmp_path,
            confirmed=True,
        )
    )
    assert result.ok is True
    assert result.error is None
    assert result.mapping_dir == tmp_path / "clinic_csv"
    assert (tmp_path / "clinic_csv" / "mapping.json").is_file()
    assert result.record_count == 3
    assert "Learned source" in (result.mapping_md or "")


def test_cannot_analyze_carries_a_phi_safe_type_detail(tmp_path: Path) -> None:
    blank = tmp_path / "blank.csv"
    blank.write_text("", encoding="utf-8")  # no header/columns
    result = run_source_init_command(
        SourceInitCommand(example=blank, name="blank", confirmed=False)
    )
    assert result.ok is False
    assert result.error == "CannotAnalyze"
    assert result.detail  # an exception TYPE name (PHI-safe), not a cell value
    assert result.fmt_type is None


def test_save_failure_carries_a_phi_safe_type_detail(tmp_path: Path) -> None:
    # out_dir is a FILE, so save_mapping's mkdir beneath it raises OSError. The
    # result names the exception TYPE (the CLI prints it), never the save path.
    not_a_dir = tmp_path / "afile"
    not_a_dir.write_text("x", encoding="utf-8")
    result = run_source_init_command(
        SourceInitCommand(example=FIXTURE, name="clinic_csv", out_dir=not_a_dir, confirmed=True)
    )
    assert result.ok is False
    assert result.error == "SaveFailed"
    assert result.detail  # a PHI-safe exception type name


def test_mapping_load_failure_is_distinct_from_dropped(tmp_path: Path) -> None:
    # A mapped column whose transform chokes (DOB -> parse_date over a non-date)
    # is a fixable MappingLoadFailed, NOT an unexplained WouldDropColumns.
    bad = tmp_path / "bad.csv"
    bad.write_text("PID,DOB\np1,garbage-not-a-date\n", encoding="utf-8")
    result = run_source_init_command(
        SourceInitCommand(example=bad, name="bad_src", out_dir=tmp_path, confirmed=True)
    )
    assert result.ok is False
    assert result.error == "MappingLoadFailed"
    assert "DOB" in (result.detail or "")  # names the column (PHI-safe)
    assert "garbage-not-a-date" not in repr(result)  # the value never leaks
    assert not (tmp_path / "bad_src").exists()  # nothing written
