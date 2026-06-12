"""GUI controller tests — headless, no pywebview, no real Chromium.

Drives :class:`anastomosis.gui.controller.GuiController` against a recording
fake sink and the FAKE Chromium renderer pattern from ``test_cli.py`` (a real
PDF carrying the chart text, so the QA stage runs for real). Asserts the event
sequence, the busy guard, the no-traceback error contract, section honoring,
deliverer invocation, and the PHI probe (no fixture patient name in any event).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

import anastomosis.reconstruct.chromium as chromium
from anastomosis.gui.controller import GuiController

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"

# The synthetic fixture's patient names — no event value may contain any of
# these (PHI probe). Confirmed against patient-demographics.tsv.
FIXTURE_NAMES = ("Ada", "Boris", "Cleo", "Fixture", "Sample", "Placeholder")


class _RecordingSink:
    """An EventSink that records every emitted event for assertions."""

    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self._lock = threading.Lock()

    def emit(self, event: dict[str, object]) -> None:
        with self._lock:
            self.events.append(event)

    def types(self) -> list[str]:
        return [str(e["type"]) for e in self.events]

    def stages_in_order(self) -> list[str]:
        return [str(e["stage"]) for e in self.events if e.get("type") == "stage"]


class _FakeChromium:
    """Writes a REAL pdf carrying the chart text (the test_cli.py pattern)."""

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


class _SlowFakeChromium(_FakeChromium):
    """A renderer that blocks long enough to test the busy guard."""

    def render(self, html: str, pdf_path: Path) -> None:
        time.sleep(0.3)
        super().render(html, pdf_path)


# --- info / detect ---------------------------------------------------------


def test_info_lists_sources_and_packs() -> None:
    controller = GuiController(_RecordingSink())
    info = controller.info()
    assert info["ok"] is True
    assert isinstance(info["version"], str) and info["version"]
    names = {s["name"] for s in info["sources"]}  # type: ignore[index, union-attr]
    assert "pf-tebra" in names
    pack_names = {p["name"] for p in info["packs"]}  # type: ignore[index, union-attr]
    assert "generic_soap" in pack_names
    assert "extras" in info and "gui" in info["extras"]  # type: ignore[operator]


def test_detect_identifies_fixture() -> None:
    controller = GuiController(_RecordingSink())
    assert controller.detect(str(FIXTURE)) == {"ok": True, "source": "pf-tebra"}


def test_detect_unknown_dir_is_none(tmp_path: Path) -> None:
    controller = GuiController(_RecordingSink())
    assert controller.detect(str(tmp_path)) == {"ok": True, "source": None}


# --- run_pipeline end to end ----------------------------------------------


def test_run_pipeline_end_to_end_emits_stage_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fitz", reason="pipeline QA e2e needs PyMuPDF")
    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)
    sink = _RecordingSink()
    controller = GuiController(sink)
    result = controller.run_pipeline(str(FIXTURE), str(tmp_path / "out"))

    assert result["ok"] is True
    # Final done event present and last.
    assert sink.events[-1]["type"] == "done"
    # Stage rail lit in pipeline order (start/done pairs per stage).
    stages = sink.stages_in_order()
    assert stages == ["ingest", "ingest", "reconstruct", "reconstruct", "qa", "qa"]
    # Exact roll-up counts from the 3-patient / 6-encounter fixture.
    done = sink.events[-1]
    assert done["records"] == 3
    assert done["rendered"] == 6
    assert done["failed"] == 0
    assert done["pass"] == 6
    assert (tmp_path / "out").glob("*.pdf")
    assert len(list((tmp_path / "out").glob("*.pdf"))) == 6


def test_run_pipeline_progress_carries_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fitz", reason="pipeline QA e2e needs PyMuPDF")
    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)
    sink = _RecordingSink()
    GuiController(sink).run_pipeline(str(FIXTURE), str(tmp_path / "out"))
    progress = [e for e in sink.events if e["type"] == "progress"]
    by_stage = {e["stage"]: e for e in progress}
    assert by_stage["ingest"]["records"] == 3
    assert by_stage["reconstruct"]["rendered"] == 6
    assert by_stage["qa"]["pass"] == 6


# --- busy guard ------------------------------------------------------------


def test_busy_guard_rejects_concurrent_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("fitz", reason="pipeline QA e2e needs PyMuPDF")
    monkeypatch.setattr(chromium, "ChromiumRenderer", _SlowFakeChromium)
    sink = _RecordingSink()
    controller = GuiController(sink)

    first_result: dict[str, object] = {}

    def _first() -> None:
        first_result.update(controller.run_pipeline(str(FIXTURE), str(tmp_path / "out")))

    worker = threading.Thread(target=_first)
    worker.start()
    # Give the first run time to enter the busy section before we collide.
    time.sleep(0.1)
    second = controller.run_pipeline(str(FIXTURE), str(tmp_path / "out2"))
    worker.join()

    assert second == {"ok": False, "error": "Busy"}
    assert first_result["ok"] is True


def test_async_returns_started_then_done(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("fitz", reason="pipeline QA e2e needs PyMuPDF")
    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)
    sink = _RecordingSink()
    controller = GuiController(sink)
    started = controller.run_pipeline_async(str(FIXTURE), str(tmp_path / "out"))
    assert started == {"ok": True, "started": True}
    # Wait for the daemon worker to finish (the done event lands).
    deadline = time.time() + 10
    while time.time() < deadline and (not sink.events or sink.events[-1]["type"] != "done"):
        time.sleep(0.05)
    assert sink.events[-1]["type"] == "done"


# --- error path ------------------------------------------------------------


def test_run_pipeline_bad_export_dir_is_clean_error(tmp_path: Path) -> None:
    sink = _RecordingSink()
    controller = GuiController(sink)
    result = controller.run_pipeline(str(tmp_path / "empty"), str(tmp_path / "out"))
    assert result["ok"] is False
    assert isinstance(result["error"], str)
    # An error event was emitted; no done event.
    assert "error" in sink.types()
    assert "done" not in sink.types()
    # No traceback leaked — the error is a PHI-free diagnosis string.
    assert "Traceback" not in str(result["error"])


def test_run_pipeline_unknown_pack_is_clean_error(tmp_path: Path) -> None:
    sink = _RecordingSink()
    result = GuiController(sink).run_pipeline(
        str(FIXTURE), str(tmp_path / "out"), pack="does_not_exist"
    )
    assert result["ok"] is False
    assert "unavailable" in str(result["error"])


# --- sections honored ------------------------------------------------------


def test_sections_flag_reaches_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("fitz", reason="needs PyMuPDF")
    captured: dict[str, dict[str, bool]] = {}
    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)

    # Capture the engine's effective section flags by wrapping the pipeline core.
    import anastomosis.pipeline as pipeline_mod

    orig = pipeline_mod.run_pipeline

    def _wrapped(**kwargs: object) -> object:
        result = orig(**kwargs)  # type: ignore[arg-type]
        captured["flags"] = result.engine.section_flags
        return result

    monkeypatch.setattr(pipeline_mod, "run_pipeline", _wrapped)

    GuiController(_RecordingSink()).run_pipeline(
        str(FIXTURE), str(tmp_path / "out"), sections={"insurance": True, "addenda": False}
    )
    assert captured["flags"]["insurance"] is True
    assert captured["flags"]["addenda"] is False


# --- deliverers ------------------------------------------------------------


def test_deliverers_invoked_when_flagged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("fitz", reason="needs PyMuPDF")
    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)
    out = tmp_path / "out"
    sink = _RecordingSink()
    result = GuiController(sink).run_pipeline(
        str(FIXTURE), str(out), archive=True, bundle=True, ccda=True
    )
    assert result["ok"] is True
    # Outputs exist in the sibling subdirectories.
    assert (out / "archive" / "index.html").is_file()
    assert any((out / "bundles").iterdir())
    assert list((out / "ccda").glob("*.xml"))
    # A deliver rail lit, with per-deliverer progress events.
    deliver_progress = [
        e for e in sink.events if e["type"] == "progress" and e["stage"] == "deliver"
    ]
    delivered = {e["deliverer"] for e in deliver_progress}
    assert delivered == {"archive", "bundle", "ccda"}
    # The roll-up carries the per-deliverer patient counts.
    done = sink.events[-1]
    assert done["archive_patients"] == 3
    assert done["bundle_patients"] == 3
    assert done["ccda_patients"] == 3


# --- routes ----------------------------------------------------------------


def test_routes_all_entries() -> None:
    controller = GuiController(_RecordingSink())
    result = controller.routes()
    assert result["ok"] is True
    routes = result["routes"]
    assert isinstance(routes, list) and routes
    names = {r["destination"] for r in routes}  # type: ignore[index, union-attr]
    assert "tebra" in names
    tebra = next(r for r in routes if r["destination"] == "tebra")  # type: ignore[index]
    assert tebra["chosen"] == "ccda_import"
    assert len(tebra["options"]) == 3


def test_routes_single_destination() -> None:
    result = GuiController(_RecordingSink()).routes("tebra")
    assert result["ok"] is True
    assert len(result["routes"]) == 1  # type: ignore[arg-type]


def test_routes_unknown_is_clean_error() -> None:
    result = GuiController(_RecordingSink()).routes("ghost")
    assert result["ok"] is False
    assert "ghost" in str(result["error"])


# --- PHI probe -------------------------------------------------------------


def test_no_event_value_contains_a_patient_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fitz", reason="needs PyMuPDF")
    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)
    sink = _RecordingSink()
    GuiController(sink).run_pipeline(
        str(FIXTURE), str(tmp_path / "out"), archive=True, bundle=True, ccda=True
    )
    blob = repr(sink.events)
    for name in FIXTURE_NAMES:
        assert name not in blob, f"event log leaked patient name {name!r}"


def test_broken_sink_never_raises_and_releases_busy(tmp_path: Path) -> None:
    """Regression: the never-raise contract holds even when the sink itself
    fails (a closed window's evaluate_js) — the run completes or fails
    cleanly, nothing propagates, and the busy guard is released."""

    class _BrokenSink:
        def emit(self, event: dict[str, object]) -> None:
            raise RuntimeError("window is gone")

    controller = GuiController(_BrokenSink())
    result = controller.run_pipeline(
        export_dir=str(tmp_path / "nonexistent"),
        out_dir=str(tmp_path / "out"),
        pack="generic_soap",
    )
    assert result["ok"] is False  # bad export dir -> clean failure dict
    # And the controller is reusable (busy released despite sink failures).
    second = controller.run_pipeline(
        export_dir=str(tmp_path / "nonexistent"),
        out_dir=str(tmp_path / "out2"),
        pack="generic_soap",
    )
    assert second["ok"] is False
