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
    """Stands in for Chromium by writing a REAL pdf carrying the chart text,
    so the CLI's QA stage runs for real against what was 'rendered'."""

    def __init__(self, **kwargs: object) -> None:
        pass

    def render(self, html: str, pdf_path: Path) -> None:
        import fitz

        from anastomosis.core.textutil import html_to_text

        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        page.insert_textbox(
            fitz.Rect(18, 18, 594, 774), html_to_text(html) or "(empty)", fontsize=7
        )
        doc.save(str(pdf_path))
        doc.close()

    def close(self) -> None:
        pass


def test_pipeline_run_end_to_end_with_qa(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("fitz", reason="pipeline QA e2e needs PyMuPDF (render extra)")
    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)
    out = tmp_path / "charts"
    result = runner.invoke(
        app, ["pipeline", "run", str(FIXTURE), "--out", str(out), "--section", "insurance=on"]
    )
    assert result.exit_code == 0, result.output
    assert "Detected source" in result.output and "pf-tebra" in result.output
    assert "6 rendered" in result.output
    assert "QA: 6 pass" in result.output
    assert len(list(out.glob("*.pdf"))) == 6
    assert (out / "_PHI_WARNING_README.txt").exists()
    assert (out / "qa_report.json").exists()


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


def test_destination_list_shows_tebra() -> None:
    result = runner.invoke(app, ["destination", "list"])
    assert result.exit_code == 0, result.output
    assert "tebra" in result.output
    assert "unverified" in result.output


def test_destination_route_tebra_routes_via_ccda_import() -> None:
    # The shipped registry: tebra has no write API (verified-none) but does
    # have in-product C-CDA import, so a route exists.
    result = runner.invoke(app, ["destination", "route", "tebra"])
    assert result.exit_code == 0, result.output
    assert "delivery routes for tebra" in result.output
    assert "chosen: ccda_import" in result.output


def test_destination_route_unroutable_exits_1_with_map(tmp_path: Path) -> None:
    # An all-unverified destination (via --registry overlay) has no viable
    # route; the operator must see that loudly (exit 1) with the full map.
    overlay = tmp_path / "registry.yaml"
    overlay.write_text(
        "entries:\n"
        "  nowhere:\n"
        "    name: nowhere\n"
        "    display: Nowhere EHR\n"
        "    doc_write_api: {kind: unverified}\n"
        "    ccda_import: {kind: unverified}\n"
        "    browser: {kind: none}\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["destination", "route", "nowhere", "--registry", str(overlay)])
    assert result.exit_code == 1, result.output
    assert "delivery routes for nowhere" in result.output
    assert "no viable route" in result.output


def test_destination_route_unknown_is_clean_error() -> None:
    result = runner.invoke(app, ["destination", "route", "ghost"])
    assert result.exit_code == 2
    assert "unknown destination" in result.output
    assert "ghost" in result.output
    # A clean error, not a traceback.
    assert result.exception is None or isinstance(result.exception, SystemExit)
