from pathlib import Path

import pytest
from typer.testing import CliRunner

import anastomosis
import anastomosis.reconstruct.chromium as chromium
from anastomosis.cli import app

runner = CliRunner()

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert anastomosis.__version__ in result.output


def test_info_lists_sources_and_packs() -> None:
    result = runner.invoke(app, ["info"])
    assert result.exit_code == 0
    assert "anastomosis" in result.output
    assert "pf-tebra" in result.output
    assert "generic_soap" in result.output


class _FakeChromium:
    def __init__(self, **kwargs: object) -> None:
        pass

    def render(self, html: str, pdf_path: Path) -> None:
        pdf_path.write_bytes(b"%PDF-1.7 fake")

    def close(self) -> None:
        pass


def test_pipeline_run_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)
    out = tmp_path / "charts"
    result = runner.invoke(
        app, ["pipeline", "run", str(FIXTURE), "--out", str(out), "--section", "insurance=on"]
    )
    assert result.exit_code == 0, result.output
    assert "Detected source" in result.output and "pf-tebra" in result.output
    assert "6 rendered" in result.output
    assert len(list(out.glob("*.pdf"))) == 6
    assert (out / "_PHI_WARNING_README.txt").exists()


def test_pipeline_run_rejects_unknown_dir(tmp_path: Path) -> None:
    result = runner.invoke(app, ["pipeline", "run", str(tmp_path), "--out", str(tmp_path / "o")])
    assert result.exit_code == 2
    assert "Could not identify" in result.output


def test_pipeline_run_diagnoses_unknown_pack(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["pipeline", "run", str(FIXTURE), "--out", str(tmp_path / "o"), "--pack", "nope"],
    )
    assert result.exit_code == 2
    assert "unavailable" in result.output
