"""Tests for the shared application/command layer (``core/commands.py``).

This is the single orchestration core both the CLI and the GUI now build on, so
these tests pin its contract directly: the toolkit-info probe, and a full
``PipelineCommand`` run with all three deliverers (the outcomes both frontends
present). The fake-Chromium pattern matches ``test_gui_controller.py`` (a real
PDF carrying the chart text, so the QA stage runs for real).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import anastomosis.reconstruct.chromium as chromium
from anastomosis.core.commands import (
    DeliveryCommand,
    PipelineCommand,
    deliver_outputs,
    get_toolkit_info,
    run_pipeline_command,
    summarize_patients,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"


class _FakeChromium:
    """Writes a REAL pdf carrying the chart text (the test_gui_controller pattern)."""

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


# --- get_toolkit_info ----------------------------------------------------------


def test_get_toolkit_info_reports_sources_packs_and_extras() -> None:
    info = get_toolkit_info()
    assert isinstance(info.version, str) and info.version
    assert "pf-tebra" in {name for name, _ in info.sources}
    pack_names = {p.name for p in info.packs}
    assert {"generic_soap", "practice_fusion_soap"} <= pack_names
    # Extras probe has all four keys with boolean values.
    assert set(info.extras) == {"render", "render-qa", "fhir", "gui"}
    assert all(isinstance(v, bool) for v in info.extras.values())
    # A built-in pack carries its section matrix (label + default per section).
    generic = next(p for p in info.packs if p.name == "generic_soap")
    assert generic.available and generic.origin == "builtin"
    assert generic.sections  # non-empty matrix
    assert all({"label", "default"} <= set(v) for v in generic.sections.values())


# --- run_pipeline_command + deliver_outputs ------------------------------------


def test_run_pipeline_command_delivers_all_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fitz", reason="needs PyMuPDF")
    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)
    charts = tmp_path / "charts"
    cmd = PipelineCommand(
        export_dir=FIXTURE,
        charts_dir=charts,
        deliveries=(
            DeliveryCommand("archive", tmp_path / "arc"),
            DeliveryCommand("bundle", tmp_path / "bun"),
            DeliveryCommand("ccda", tmp_path / "cda"),
        ),
    )
    result = run_pipeline_command(cmd)

    # The pipeline produced records and the deliverers produced the outcomes
    # both frontends present.
    assert len(result.pipeline.records) == 3
    assert set(result.deliveries) == {"archive", "bundle", "ccda"}
    assert result.deliveries["archive"].counts["patients"] == 3
    assert {"patients", "encounters", "pdfs"} <= set(result.deliveries["archive"].counts)
    assert result.deliveries["bundle"].counts == {"patients": 3}
    assert result.deliveries["ccda"].counts == {"patients": 3}
    # Files landed in the operator-chosen directories.
    assert (tmp_path / "arc" / "index.html").is_file()
    assert any((tmp_path / "bun").iterdir())
    assert list((tmp_path / "cda").glob("*.xml"))
    # Atomic writes leave no stray temp files, and the output lock is released
    # (the marker file persists, but the kernel lock is free to re-acquire).
    from anastomosis.core.locking import output_lock

    assert list(charts.glob("*.tmp")) == []
    with output_lock(charts):
        pass


def test_run_pipeline_command_refuses_a_locked_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second run against an output dir a live run already holds fails fast
    with a clean PipelineError (exit 2), before any rendering."""
    from anastomosis.core.locking import output_lock
    from anastomosis.pipeline import PipelineError

    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)
    charts = tmp_path / "charts"
    with output_lock(charts):  # simulate another live run holding the directory
        with pytest.raises(PipelineError) as excinfo:
            run_pipeline_command(PipelineCommand(export_dir=FIXTURE, charts_dir=charts))
    assert excinfo.value.exit_code == 2
    assert excinfo.value.kind == "output_locked"


def test_deliver_outputs_no_deliveries_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fitz", reason="needs PyMuPDF")
    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)
    result = run_pipeline_command(
        PipelineCommand(export_dir=FIXTURE, charts_dir=tmp_path / "charts")
    )
    assert result.deliveries == {}
    assert deliver_outputs(result.pipeline, tmp_path / "charts", ()) == {}


# --- summarize_patients --------------------------------------------------------


def test_summarize_patients_joins_records_and_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-patient roll-up carries name, DOB, encounter and rendered-doc
    counts, joined on the render result's patient attribution, in ingest order."""
    pytest.importorskip("fitz", reason="needs PyMuPDF")
    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)
    result = run_pipeline_command(
        PipelineCommand(export_dir=FIXTURE, charts_dir=tmp_path / "charts")
    )
    summaries = summarize_patients(result.pipeline)
    assert [s.display_name for s in summaries] == [
        "Ada Q Fixture",
        "Boris Sample Jr.",
        "Cleo Placeholder",
    ]
    by_name = {s.display_name: s for s in summaries}
    assert by_name["Ada Q Fixture"].birth_date == "1985-03-14"
    assert by_name["Ada Q Fixture"].encounters == 3
    assert by_name["Ada Q Fixture"].documents == 3
    assert by_name["Boris Sample Jr."].documents == 2
    assert by_name["Cleo Placeholder"].documents == 1
    assert sum(s.documents for s in summaries) == 6
