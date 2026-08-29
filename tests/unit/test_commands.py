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
        import pymupdf

        from anastomosis.core.textutil import html_to_text

        doc = pymupdf.open()
        page = doc.new_page(width=612, height=792)
        page.insert_textbox(
            pymupdf.Rect(18, 18, 594, 774), html_to_text(html) or "(empty)", fontsize=7
        )
        doc.save(str(pdf_path))
        doc.close()

    def close(self) -> None:
        pass


# --- get_toolkit_info ----------------------------------------------------------


def test_get_toolkit_info_reports_sources_packs_and_extras() -> None:
    info = get_toolkit_info()
    assert isinstance(info.version, str) and info.version
    by_name = {name: display for name, display, _desc in info.sources}
    assert "pf-tebra" in by_name
    pack_names = {p.name for p in info.packs}
    assert {"generic_soap", "practice_fusion_soap"} <= pack_names

    # Every registration carries the name a person reads, beside the id they
    # would type. `ccda` is the one that proves the point: no re-casing of the
    # id produces "C-CDA", which is why the front end had it hard-coded (#164).
    assert by_name["pf-tebra"] == "Practice Fusion / Tebra"
    assert by_name["ccda"] == "C-CDA"
    assert {p.display for p in info.packs} >= {"Generic SOAP", "Practice Fusion SOAP"}
    # Extras probe has all four keys with boolean values.
    assert set(info.extras) == {"render", "deliver-browser", "fhir", "gui"}
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
    pytest.importorskip("pymupdf", reason="needs PyMuPDF")
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
    assert result.deliveries["bundle"].counts == {"patients": 3, "missing": 0}
    assert result.deliveries["ccda"].counts == {"patients": 3, "missing": 0}
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


def test_run_pipeline_command_aliased_charts_and_delivery_dir_no_self_deadlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One physical dir used as BOTH charts and a delivery dir, spelled two ways,
    must lock once — not self-deadlock the run (the lock set dedups on resolve())."""
    pytest.importorskip("pymupdf", reason="needs PyMuPDF")
    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)
    shared = tmp_path / "both"
    alias = tmp_path / "sub" / ".." / "both"  # same physical dir, different spelling
    result = run_pipeline_command(
        PipelineCommand(
            export_dir=FIXTURE,
            charts_dir=shared,
            deliveries=(DeliveryCommand("ccda", alias),),
        )
    )
    # The run completed (no self-inflicted output_locked) and produced the C-CDA.
    assert result.deliveries["ccda"].counts["patients"] == 3
    assert list(shared.glob("*.xml"))


def test_run_pipeline_command_refuses_a_locked_delivery_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delivery dir is now locked too (not just charts): a second run sharing a
    --ccda dir with a live run fails fast (output_locked) instead of racing it."""
    from anastomosis.core.locking import output_lock
    from anastomosis.pipeline import PipelineError

    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)
    ccda = tmp_path / "shared_ccda"
    with output_lock(ccda):  # another run holds the DELIVERY dir, not charts
        with pytest.raises(PipelineError) as excinfo:
            run_pipeline_command(
                PipelineCommand(
                    export_dir=FIXTURE,
                    charts_dir=tmp_path / "charts",  # a free charts dir
                    deliveries=(DeliveryCommand("ccda", ccda),),
                )
            )
    assert excinfo.value.exit_code == 2
    assert excinfo.value.kind == "output_locked"


def test_deliver_outputs_no_deliveries_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("pymupdf", reason="needs PyMuPDF")
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
    pytest.importorskip("pymupdf", reason="needs PyMuPDF")
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


# --- the extras info names are the extras that exist -------------------------
#
# `info` printed "Double-checking charts (render-qa): not installed", and
# `render-qa` has never been an extra — `pip install "anastomosis[render-qa]"`
# warns and installs nothing. `deliver-browser` is declared and was never named.
# A hand-kept list beside a declared one drifts; this is what notices.


def _declared_extras() -> set[str]:
    import tomllib

    root = Path(__file__).resolve().parents[2]
    with (root / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    # `dev` is the contributor's install, not a capability an operator chooses.
    return set(project.get("optional-dependencies", {})) - {"dev"}


def test_every_extra_info_names_is_one_the_package_declares() -> None:
    from anastomosis.core.commands import _EXTRAS

    named = {extra for extra, _ in _EXTRAS}
    assert named == _declared_extras(), (
        "the extras `anast info` reports and the extras pyproject declares have drifted; "
        "an extra info names but pip cannot install is advice that does not work"
    )


def test_every_extra_has_a_capability_name() -> None:
    """An extra with no plain-English name prints as its packaging id, which is
    the thing `CAPABILITY_NAMES` exists to avoid."""
    from anastomosis.cli import CAPABILITY_NAMES
    from anastomosis.core.commands import _EXTRAS

    assert {extra for extra, _ in _EXTRAS} <= set(CAPABILITY_NAMES)


def test_the_gui_probe_asks_for_a_backend_not_just_the_wrapper() -> None:
    """`import webview` succeeds with neither GTK nor Qt bindings present, and
    pywebview then raises on launch — so probing the wrapper alone reported the
    desktop app as ready on a machine where `anast gui` could not start."""
    from anastomosis.core.commands import _EXTRAS

    gui = next(modules for extra, modules in _EXTRAS if extra == "gui")
    assert any("|" in requirement for requirement in gui), (
        "the gui probe must require a drawing backend, not only the wrapper"
    )


def test_probing_an_extra_does_not_execute_it() -> None:
    """The probe used `__import__`, so asking "is pymupdf installed" cost 105 ms
    of running pymupdf — inside a read-only status command."""
    import subprocess
    import sys

    probe = (
        "import sys; from anastomosis.core.commands import get_toolkit_info; "
        "get_toolkit_info(); "
        "print('pymupdf' in sys.modules or 'webview' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False", "the readiness probe imported what it asked about"


def test_the_archive_deliverer_imports_without_the_render_extra() -> None:
    """A base install has to pass its own doctor.

    `qa/checks.py` imported pymupdf at module scope, `qa/runner.py` imports
    checks, and `deliver/archive` imports `anastomosis.qa` — so on an install
    without the `render` extra the archive deliverer was unimportable, and
    `anast doctor` caught the ModuleNotFoundError and reported the archive's own
    bundled assets as MISSING. Both files it names are present and readable. The
    README tells people to run doctor after installing, so a correct install
    self-reported as broken.
    """
    import subprocess
    import sys

    # Deny pymupdf the way a base install does: a meta-path hook, so the rest of
    # the environment is untouched and nothing has to be uninstalled.
    probe = (
        "import sys\n"
        "class Deny:\n"
        "    def find_module(self, name, path=None): return None\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'pymupdf': raise ImportError('no pymupdf')\n"
        "        return None\n"
        "sys.meta_path.insert(0, Deny())\n"
        "import anastomosis.deliver.archive\n"
        "from anastomosis.core.selfcheck import check_bundled_assets\n"
        "print([c.name for c in check_bundled_assets().checks if not c.ok])\n"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "archive web assets" not in out.stdout, (
        f"a base install reports its own assets as missing: {out.stdout.strip()}"
    )
