"""The ``anast source init`` CLI and the no-source "teach it" guidance.

``source init`` learns a format from an example, refuses to save a lossy
mapping, and points the operator at the right next command; an unidentifiable
export now appends the teach-it hint to the existing (unchanged) failure.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from anastomosis.cli import app

runner = CliRunner()

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "learned" / "clinic_visits.csv"


def test_source_init_learns_and_saves(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "source",
            "init",
            str(FIXTURE),
            "--name",
            "clinic_csv",
            "--out-dir",
            str(tmp_path),
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "learned source" in result.output
    saved = tmp_path / "clinic_csv" / "mapping.json"
    assert saved.is_file()
    # The PHI-safe analysis surfaced column names/types, not patient values.
    assert "900-12-3456" not in result.output
    assert "Ada" not in result.output


def test_source_init_rejects_bad_name(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["source", "init", str(FIXTURE), "--name", "Bad-Name", "--out-dir", str(tmp_path), "--yes"],
    )
    assert result.exit_code == 2
    assert "invalid mapping name" in result.output


def test_source_init_dir_with_no_structured_file(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("hi", encoding="utf-8")
    result = runner.invoke(
        app, ["source", "init", str(tmp_path), "--name", "x", "--out-dir", str(tmp_path), "--yes"]
    )
    assert result.exit_code == 2
    assert "no csv/tsv/json/ndjson file" in result.output


def test_source_init_dir_with_one_file(tmp_path: Path) -> None:
    # A directory holding exactly one structured file resolves to that file.
    import shutil

    shutil.copy(FIXTURE, tmp_path / "export.csv")
    out = tmp_path / "out"
    result = runner.invoke(
        app,
        ["source", "init", str(tmp_path), "--name", "clinic_dir", "--out-dir", str(out), "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert (out / "clinic_dir" / "mapping.json").is_file()


def test_pipeline_run_no_source_appends_teach_guidance(tmp_path: Path) -> None:
    # An empty export dir matches no adapter: the existing no_source failure
    # (exit 2) now also tells the operator how to teach the format.
    empty = tmp_path / "mystery_export"
    empty.mkdir()
    result = runner.invoke(app, ["pipeline", "run", str(empty), "--out", str(tmp_path / "out")])
    assert result.exit_code == 2
    assert "Could not identify the export format" in result.output
    assert "anast source init" in result.output
