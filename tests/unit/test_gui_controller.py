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

import anastomosis.gui.controller as controller_module
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


# --- per-patient detail (GUI parity: names/DOB/note-counts, local display) --


def test_run_pipeline_returns_per_patient_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run return value carries a per-patient roll-up (name, DOB, #notes)
    for local dashboard display — while the event stream stays count-only."""
    pytest.importorskip("fitz", reason="needs PyMuPDF")
    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)
    sink = _RecordingSink()
    result = GuiController(sink).run_pipeline(str(FIXTURE), str(tmp_path / "out"))

    assert result["ok"] is True
    patients = result["patients"]
    assert isinstance(patients, list) and len(patients) == 3
    by_name = {p["display_name"]: p for p in patients}
    # Exact names/DOBs/counts from the 3-patient / 6-encounter fixture.
    assert by_name["Ada Q Fixture"]["birth_date"] == "1985-03-14"
    assert by_name["Ada Q Fixture"]["encounters"] == 3
    assert by_name["Ada Q Fixture"]["documents"] == 3
    assert by_name["Boris Sample Jr."]["birth_date"] == "1952-07-04"
    assert by_name["Boris Sample Jr."]["documents"] == 2
    assert by_name["Cleo Placeholder"]["birth_date"] == "2021-12-01"
    assert by_name["Cleo Placeholder"]["documents"] == 1
    assert sum(p["documents"] for p in patients) == 6
    # The names ride the RETURN value only — never the PHI-scanned event stream.
    blob = repr(sink.events)
    for name in FIXTURE_NAMES:
        assert name not in blob, f"event log leaked patient name {name!r}"


def test_last_run_summary_serves_async_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The async path returns started=True; the per-patient detail is fetched
    after the `done` event via last_run_summary (the events carry no names)."""
    pytest.importorskip("fitz", reason="needs PyMuPDF")
    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)
    sink = _RecordingSink()
    controller = GuiController(sink)
    started = controller.run_pipeline_async(str(FIXTURE), str(tmp_path / "out"))
    assert started == {"ok": True, "started": True}

    deadline = time.time() + 10
    while time.time() < deadline and (not sink.events or sink.events[-1]["type"] != "done"):
        time.sleep(0.05)
    assert sink.events[-1]["type"] == "done"

    summary = controller.last_run_summary()
    assert summary["ok"] is True
    patients = summary["patients"]
    assert isinstance(patients, list) and len(patients) == 3
    assert {p["display_name"] for p in patients} == {
        "Ada Q Fixture",
        "Boris Sample Jr.",
        "Cleo Placeholder",
    }


def test_last_run_summary_empty_before_any_run() -> None:
    assert GuiController(_RecordingSink()).last_run_summary() == {"ok": True, "patients": []}


def test_last_run_summary_cleared_after_failed_run(tmp_path: Path) -> None:
    """A failed run leaves no fetchable patient detail (no stale carry-over)."""
    controller = GuiController(_RecordingSink())
    result = controller.run_pipeline(str(tmp_path / "empty"), str(tmp_path / "out"))
    assert result["ok"] is False
    assert controller.last_run_summary() == {"ok": True, "patients": []}


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


def test_force_and_pack_dirs_reach_the_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """force and pack_dirs are no longer hard-coded off — the GUI threads them
    into the same command the CLI builds (review parity gap #1)."""
    pytest.importorskip("fitz", reason="needs PyMuPDF")
    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)
    import anastomosis.reconstruct.packtrust as packtrust

    monkeypatch.setattr(packtrust, "user_pack_trust_path", lambda: tmp_path / "trust.json")

    import anastomosis.pipeline as pipeline_mod

    orig = pipeline_mod.run_pipeline
    captured: dict[str, object] = {}

    def _wrapped(**kwargs: object) -> object:
        captured.update(kwargs)
        return orig(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(pipeline_mod, "run_pipeline", _wrapped)

    extra = tmp_path / "extra_packs"
    extra.mkdir()
    GuiController(_RecordingSink()).run_pipeline(
        str(FIXTURE),
        str(tmp_path / "out"),
        force=True,
        pack_dirs=[str(extra)],
        trust_new=True,
    )
    assert captured["force"] is True
    assert captured["pack_dirs"] == [extra]
    # trust_new threads through too, so a GUI-supplied --pack-dir can be trusted
    # on first use (the #40 hash-pin TOFU path) instead of failing untrusted.
    assert captured["trust_new"] is True


def test_async_busy_rejects_a_second_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The async race fix: the busy flag is acquired SYNCHRONOUSLY, so of two
    CONCURRENT starts exactly one wins — never two ``started``.

    The two calls must contend (a barrier releases them together); a sequential
    pair would pass even against the old TOCTOU bug, so it would not pin the fix.
    """
    pytest.importorskip("fitz", reason="needs PyMuPDF")
    monkeypatch.setattr(chromium, "ChromiumRenderer", _SlowFakeChromium)
    sink = _RecordingSink()
    controller = GuiController(sink)

    barrier = threading.Barrier(2)
    results: list[dict[str, object]] = []
    results_lock = threading.Lock()

    def _fire(out_name: str) -> None:
        barrier.wait()  # release both threads at once so they truly contend
        outcome = controller.run_pipeline_async(str(FIXTURE), str(tmp_path / out_name))
        with results_lock:
            results.append(outcome)

    threads = [threading.Thread(target=_fire, args=(f"out{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one start wins; the other is rejected Busy (order is racy).
    started = [r for r in results if r.get("started")]
    busy = [r for r in results if r.get("error") == "Busy"]
    assert len(started) == 1, results
    assert len(busy) == 1, results

    # Let the winner's worker finish so its daemon thread doesn't outlive the test.
    deadline = time.time() + 10
    while time.time() < deadline and (not sink.events or sink.events[-1]["type"] != "done"):
        time.sleep(0.05)
    assert sink.events[-1]["type"] == "done"


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


# --- run_migration (EHR-to-EHR; the wizard's run flow) ---------------------


def test_run_migration_returns_route_and_per_patient_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A migration returns ok, the resolved route, and a per-patient summary —
    while the event stream stays count-only (PHI probe holds)."""
    pytest.importorskip("fitz", reason="needs PyMuPDF")
    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)
    sink = _RecordingSink()
    controller = GuiController(sink)
    result = controller.run_migration(
        str(FIXTURE), str(tmp_path / "out"), source="pf-tebra", destination="tebra"
    )
    assert result["ok"] is True
    # The chosen route (pf→tebra → C-CDA import) rides the return value.
    route = result["route"]
    assert route["destination"] == "tebra"  # type: ignore[index]
    assert route["chosen"] == "ccda_import"  # type: ignore[index]
    # The structured-payload count rolled up.
    assert result["ccda_patients"] == 3
    # Per-patient summary present (neutral mode → full names via the pipeline).
    patients = result["patients"]
    assert isinstance(patients, list) and len(patients) == 3
    by_name = {p["display_name"]: p for p in patients}
    assert by_name["Ada Q Fixture"]["documents"] == 3
    # Names ride the RETURN value only — never the event stream.
    blob = repr(sink.events)
    for name in FIXTURE_NAMES:
        assert name not in blob, f"event log leaked patient name {name!r}"
    # The done event landed last.
    assert sink.events[-1]["type"] == "done"


def test_run_migration_ccda_standard_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ccda-standard mode carries the SAME per-patient detail as pack mode —
    names/DOB/encounter counts (from the retained records), one C-CDA-view doc
    per patient — while the event stream stays count-only."""
    import anastomosis.reconstruct.ccda_standard.renderer as ccda_renderer

    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)
    monkeypatch.setattr(ccda_renderer, "_default_renderer", lambda: _FakeChromium())
    sink = _RecordingSink()
    result = GuiController(sink).run_migration(
        str(FIXTURE),
        str(tmp_path / "out"),
        source="pf-tebra",
        destination="tebra",
        render="ccda-standard",
    )
    assert result["ok"] is True
    assert result["route"]["chosen"] == "ccda_import"  # type: ignore[index]
    patients = result["patients"]
    assert isinstance(patients, list) and len(patients) == 3
    assert all(p["documents"] == 1 for p in patients)
    assert result["ccda_patients"] == 3
    # Names/DOB/encounter counts are present (the GUI maps them in every mode).
    by_name = {p["display_name"]: p for p in patients}
    assert by_name["Ada Q Fixture"]["birth_date"] == "1985-03-14"
    assert by_name["Ada Q Fixture"]["encounters"] == 3
    assert {"Boris Sample Jr.", "Cleo Placeholder"} <= set(by_name)
    # ...but the names ride the return value only — never the PHI-scanned events.
    blob = repr(sink.events)
    for name in FIXTURE_NAMES:
        assert name not in blob, f"event log leaked patient name {name!r}"


def test_run_migration_unknown_destination_is_clean_error(tmp_path: Path) -> None:
    sink = _RecordingSink()
    result = GuiController(sink).run_migration(
        str(FIXTURE), str(tmp_path / "out"), source="pf-tebra", destination="ghost"
    )
    assert result["ok"] is False
    assert "ghost" in str(result["error"])
    assert "error" in sink.types()
    assert "done" not in sink.types()


def test_run_migration_forwards_all_levers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The controller threads render/sections/qa/force/pack_dirs/trust_new into the
    MigrationCommand — the levers the GUI migrate wizard now exposes (parity gap
    P1-4). Capture the command at the core boundary (a fake stops the run early)."""
    import anastomosis.core.migrate as migrate_mod
    from anastomosis.pipeline import PipelineError

    captured: dict[str, object] = {}

    def _fake_run_migration(cmd: object, on_event: object = None) -> object:
        captured["cmd"] = cmd
        raise PipelineError("stop after capture", exit_code=2, kind="bad_source")

    monkeypatch.setattr(migrate_mod, "run_migration", _fake_run_migration)
    result = GuiController(_RecordingSink()).run_migration(
        str(FIXTURE),
        str(tmp_path / "out"),
        source="pf-tebra",
        destination="tebra",
        render="practice_fusion_soap",
        sections={"insurance": False, "addenda": True},
        qa=False,
        force=True,
        pack_dirs=["/custom/packs"],
        trust_new=True,
    )
    assert result["ok"] is False  # the fake stopped the run after capturing
    cmd = captured["cmd"]
    assert cmd.render == "practice_fusion_soap"  # type: ignore[attr-defined]
    assert cmd.sections == {"insurance": False, "addenda": True}  # type: ignore[attr-defined]
    assert cmd.qa is False  # type: ignore[attr-defined]
    assert cmd.force is True  # type: ignore[attr-defined]
    assert cmd.trust_new is True  # type: ignore[attr-defined]
    # Compare Path-to-Path (str(Path) is OS-dependent: backslashes on Windows).
    assert list(cmd.pack_dirs) == [Path("/custom/packs")]  # type: ignore[attr-defined]


def test_run_migration_busy_guard_rejects_concurrent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fitz", reason="needs PyMuPDF")
    monkeypatch.setattr(chromium, "ChromiumRenderer", _SlowFakeChromium)
    controller = GuiController(_RecordingSink())

    first_result: dict[str, object] = {}

    def _first() -> None:
        first_result.update(
            controller.run_migration(
                str(FIXTURE), str(tmp_path / "out"), source="pf-tebra", destination="tebra"
            )
        )

    worker = threading.Thread(target=_first)
    worker.start()
    time.sleep(0.1)  # let the first run enter the busy section
    second = controller.run_migration(
        str(FIXTURE), str(tmp_path / "out2"), source="pf-tebra", destination="tebra"
    )
    worker.join()
    assert second == {"ok": False, "error": "Busy"}
    assert first_result["ok"] is True


def test_run_migration_async_started_then_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fitz", reason="needs PyMuPDF")
    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)
    sink = _RecordingSink()
    controller = GuiController(sink)
    started = controller.run_migration_async(
        str(FIXTURE), str(tmp_path / "out"), source="pf-tebra", destination="tebra"
    )
    assert started == {"ok": True, "started": True}
    deadline = time.time() + 10
    while time.time() < deadline and (not sink.events or sink.events[-1]["type"] != "done"):
        time.sleep(0.05)
    assert sink.events[-1]["type"] == "done"
    # The per-patient detail is fetchable after done (names there, not in events).
    summary = controller.last_run_summary()
    assert summary["ok"] is True
    assert len(summary["patients"]) == 3  # type: ignore[arg-type]


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


# --- info() carries the section matrix (item 18b) -------------------------


def test_info_sections_shape_for_generic_soap() -> None:
    controller = GuiController(_RecordingSink())
    info = controller.info()
    packs = {p["name"]: p for p in info["packs"]}  # type: ignore[index, union-attr]
    generic = packs["generic_soap"]
    sections = generic["sections"]  # type: ignore[index]
    assert isinstance(sections, dict) and sections
    # The matrix needs label + default per section (the manifest shape).
    assert sections["vitals"] == {"label": "Vitals", "default": True}
    assert sections["insurance"]["default"] is False
    assert set(sections) >= {"vitals", "addenda", "insurance", "social_history"}


# --- destination_status (item 18a) -----------------------------------------


def test_destination_status_epic_vendor_api_chosen() -> None:
    """Epic routes by vendor API (FHIR DocumentReference) — no browser pack."""
    result = GuiController(_RecordingSink()).destination_status("epic")
    assert result["ok"] is True
    transit = result["transit"]
    assert transit["destination"] == "epic"  # type: ignore[index]
    assert transit["chosen"] == "vendor_api"  # type: ignore[index]
    assert len(transit["options"]) == 3  # type: ignore[arg-type]
    # No browser pack for an API-routed destination.
    assert result["pack"] is None


def test_destination_status_tebra_ccda_chosen_no_pack() -> None:
    """Tebra routes by C-CDA import; its browser capability is `none` for now."""
    result = GuiController(_RecordingSink()).destination_status("tebra")
    assert result["ok"] is True
    assert result["transit"]["chosen"] == "ccda_import"  # type: ignore[index]
    # tebra's registry browser kind is `none` (pack lands later), so no chip.
    assert result["pack"] is None


def test_destination_status_unknown_is_clean_error() -> None:
    result = GuiController(_RecordingSink()).destination_status("ghost")
    assert result["ok"] is False
    assert "ghost" in str(result["error"])


@pytest.mark.parametrize(("ready", "builtin"), [(True, False), (False, True)])
def test_destination_status_pack_readiness_both_states(
    ready: bool, builtin: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A destination with a viable browser pack reports ready vs needs-discovery.

    The registry ships no browser-pack destination yet, so we drive the
    readiness helper directly against a crafted loaded-pack double through a
    monkeypatched loader — exercising both `ready` states.
    """
    from types import SimpleNamespace

    import anastomosis.destinations.loader as loader_mod
    from anastomosis.deliver.router import RouteKind, RouteOption, TransitMap

    browser_opt = RouteOption(
        kind=RouteKind.BROWSER, viable=True, why="browser pack acme", requires=("pack: acme",)
    )
    transit = TransitMap(destination="acme", options=(browser_opt,), chosen=browser_opt)

    fake_pack = SimpleNamespace(name="acme", ready=ready, builtin=builtin)
    # load_destination_pack is imported inside the method; patch the loader.
    monkeypatch.setattr(loader_mod, "load_destination_pack", lambda _n, pack_dirs=None: fake_pack)

    chip = GuiController(_RecordingSink())._pack_readiness(transit)
    assert chip is not None
    assert chip["ready"] is ready
    assert chip["name"] == "acme"


# --- pack_freshness (item 19 tail) -----------------------------------------


def _write_selectors(home: Path, name: str) -> Path:
    """Write a minimal user selectors.yaml for destination ``name`` under ``home``."""
    dest = home / ".anastomosis" / "destinations" / name
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / "selectors.yaml"
    path.write_text("selectors: {}\n", encoding="utf-8")
    return path


def test_pack_freshness_stale_when_selectors_old(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A discovered selectors.yaml older than the evidence window is flagged."""
    import os
    import time

    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    # Make load_destination_pack report a ready pack for "epic" (a registry name),
    # whose selectors_source is an aged user file.
    path = _write_selectors(tmp_path, "epic")
    # Age the file ~400 days into the past (well over the 90-day evidence window
    # AND the 2026-06-11 evidence date).
    old = time.time() - 400 * 86400
    os.utime(path, (old, old))

    from types import SimpleNamespace

    import anastomosis.destinations.loader as loader_mod

    def _fake_load(name: str, pack_dirs: object = None) -> object:
        if name == "epic":
            return SimpleNamespace(name="epic", ready=True, builtin=False, selectors_source=path)
        raise loader_mod.BrowserPackError(f"no pack {name!r}")

    monkeypatch.setattr(loader_mod, "load_destination_pack", _fake_load)

    result = GuiController(_RecordingSink()).pack_freshness()
    assert result["ok"] is True
    stale = result["stale"]
    assert isinstance(stale, list)
    names = {s["destination"] for s in stale}  # type: ignore[index, union-attr]
    assert "epic" in names
    epic = next(s for s in stale if s["destination"] == "epic")  # type: ignore[index]
    assert epic["gap_days"] > 90  # type: ignore[operator]
    assert epic["advice"] == "anast destination init epic --validate"


def test_pack_freshness_fresh_when_selectors_recent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registry whose evidence is recent yields no stale entries.

    We craft a registry with an evidence date of *today* so the gap is 0; the
    selectors file's own mtime is irrelevant to staleness (the gap is measured
    against the evidence date), so nothing should flag.
    """
    from datetime import UTC, datetime
    from types import SimpleNamespace

    import anastomosis.destinations.loader as loader_mod
    from anastomosis.destinations.registry import (
        Capability,
        DestinationEntry,
        DestinationRegistry,
        Evidence,
    )

    path = _write_selectors(tmp_path, "acme")
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))

    today = datetime.now(tz=UTC).date()
    entry = DestinationEntry(
        name="acme",
        display="Acme",
        doc_write_api=Capability(
            kind="fhir_documentreference",
            evidence=Evidence(source_url="https://example.com/acme", verified=today),
        ),
        ccda_import=Capability(kind="none"),
        browser=Capability(kind="none"),
    )
    registry = DestinationRegistry(entries={"acme": entry})
    monkeypatch.setattr(DestinationRegistry, "load", classmethod(lambda _cls, path=None: registry))

    fake_pack = SimpleNamespace(name="acme", ready=True, builtin=False, selectors_source=path)
    monkeypatch.setattr(loader_mod, "load_destination_pack", lambda _n, pack_dirs=None: fake_pack)

    result = GuiController(_RecordingSink()).pack_freshness()
    assert result["ok"] is True
    assert result["checked"] == 1
    assert result["stale"] == []


def test_pack_freshness_undiscovered_pack_not_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A destination with no discovered selectors is neither checked nor stale."""
    result = GuiController(_RecordingSink()).pack_freshness()
    # The shipped registry has no discovered browser packs, so checked == 0.
    assert result["ok"] is True
    assert result["checked"] == 0
    assert result["stale"] == []


# --- upload_status / upload_item_keys / manifest preview (item 19) ---------


def _craft_ledger(tmp_path: Path) -> Path:
    """Build a small tracking ledger walking items into varied terminal states."""
    from anastomosis.deliver.browser.states import UploadState
    from anastomosis.deliver.browser.tracking import TrackingDB
    from anastomosis.destinations.base import UploadItem

    db_path = tmp_path / "out" / "tracking.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tracking = TrackingDB(db_path)
    run_id = tracking.begin_run("fake")

    def _item(n: int) -> UploadItem:
        return UploadItem(
            item_key=f"enc-{n}:abcdef012345",
            encounter_id=f"enc-{n}",
            patient_id=f"pat-{n}",
            file_path=Path(f"/synthetic/note-{n}.pdf"),
            sha256="0" * 64,
            size_bytes=100 + n,
        )

    # One COMPLETED item (walk the full path) and one FAILED item.
    done = _item(1)
    tracking.enqueue(done)
    tracking.transition(done.item_key, UploadState.RESOLVING_PATIENT, run_id=run_id)
    tracking.transition(done.item_key, UploadState.VERIFYING_PRE, run_id=run_id)
    tracking.transition(done.item_key, UploadState.UPLOADING, run_id=run_id)
    tracking.transition(done.item_key, UploadState.VERIFYING_POST, run_id=run_id)
    tracking.transition(done.item_key, UploadState.COMPLETED, run_id=run_id)

    bad = _item(2)
    tracking.enqueue(bad)
    tracking.transition(bad.item_key, UploadState.RESOLVING_PATIENT, run_id=run_id)
    tracking.transition(bad.item_key, UploadState.FAILED, run_id=run_id, error_type="ResolverError")

    # One still PENDING (for the item-keys palette).
    tracking.enqueue(_item(3))
    tracking.finish_run(run_id)
    tracking.close()
    return db_path


def test_upload_status_against_crafted_ledger(tmp_path: Path) -> None:
    db_path = _craft_ledger(tmp_path)
    result = GuiController(_RecordingSink()).upload_status(str(db_path))
    assert result["ok"] is True
    counts = result["counts"]
    assert counts["completed"] == 1  # type: ignore[index]
    assert counts["failed"] == 1  # type: ignore[index]
    assert counts["pending"] == 1  # type: ignore[index]
    groups = result["groups"]
    assert groups["pending"] == 1  # type: ignore[index]
    assert groups["terminal"] == 2  # type: ignore[index]
    assert result["total"] == 3
    run = result["run"]
    assert run["destination"] == "fake"  # type: ignore[index]
    assert run["finished_at"] is not None  # type: ignore[index]
    # Error TYPE histogram surfaces the failure shape (type name, not value).
    assert result["error_type_histogram"] == {"ResolverError": 1}


def test_upload_status_missing_file_is_clean_error(tmp_path: Path) -> None:
    result = GuiController(_RecordingSink()).upload_status(str(tmp_path / "nope.db"))
    assert result["ok"] is False
    assert result["error"] == "FileNotFoundError"


def test_upload_item_keys_lists_keys_never_names(tmp_path: Path) -> None:
    db_path = _craft_ledger(tmp_path)
    result = GuiController(_RecordingSink()).upload_item_keys(str(db_path))
    assert result["ok"] is True
    keys = result["item_keys"]
    assert isinstance(keys, list)
    # The PENDING item's key is present; keys are encounter:hash, never names.
    assert any(k.startswith("enc-3:") for k in keys)  # type: ignore[union-attr]
    for k in keys:  # type: ignore[union-attr]
        assert "pat-" not in k  # no patient id leaks into the palette


def test_upload_item_keys_missing_file_is_clean_error(tmp_path: Path) -> None:
    result = GuiController(_RecordingSink()).upload_item_keys(str(tmp_path / "nope.db"))
    assert result["ok"] is False
    assert result["error"] == "FileNotFoundError"


def test_upload_manifest_preview_counts_pdfs(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "a.pdf").write_bytes(b"%PDF-1.4 a")
    (out / "b.pdf").write_bytes(b"%PDF-1.4 bb")
    (out / "notes.txt").write_text("ignored", encoding="utf-8")
    result = GuiController(_RecordingSink()).upload_manifest_preview(str(out))
    assert result["ok"] is True
    assert result["renderable"] == 2
    assert result["total_bytes"] == len(b"%PDF-1.4 a") + len(b"%PDF-1.4 bb")


def test_upload_manifest_preview_missing_dir_is_clean_error(tmp_path: Path) -> None:
    result = GuiController(_RecordingSink()).upload_manifest_preview(str(tmp_path / "ghost"))
    assert result["ok"] is False
    assert result["error"] == "NotADirectoryError"


# --- pack_init (item 19, the pack-from-samples wizard backend) -------------


def _packgen_samples(tmp_path: Path, n: int = 4) -> Path:
    """A directory of distinct-patient synthetic sample PDFs (needs PyMuPDF)."""
    import fitz

    patients = [
        ("Synthia Example", "03/14/1985", "Hypertension follow-up"),
        ("Maxwell Sample", "07/04/1952", "Diabetes review"),
        ("Cleo Placeholder", "12/01/2021", "Well child visit"),
        ("Dale Specimen", "09/09/1970", "Annual physical"),
    ]
    samples = tmp_path / "samples"
    samples.mkdir()
    for i in range(n):
        name, dob, complaint = patients[i % len(patients)]
        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        page.draw_rect(fitz.Rect(60, 95, 560, 110), fill=(0.9451, 0.9451, 0.9451), color=None)
        page.insert_text((60, 90), "SUBJECTIVE", fontsize=13, fontname="hebo")
        page.insert_text((60, 130), "OBJECTIVE", fontsize=13, fontname="hebo")
        page.insert_text((60, 200), "DOB:", fontsize=11, fontname="helv")
        page.insert_text((200, 200), dob, fontsize=11, fontname="helv")
        page.insert_text((60, 260), f"Patient {name} seen today.", fontsize=11, fontname="helv")
        page.insert_text((60, 280), complaint, fontsize=11, fontname="helv")
        page.insert_text((60, 760), "Confidential Example Clinic", fontsize=9, fontname="helv")
        doc.save(str(samples / f"sample{i}.pdf"))
        doc.close()
    return samples


def test_pack_init_happy_writes_draft(tmp_path: Path) -> None:
    pytest.importorskip("fitz", reason="packgen needs PyMuPDF")
    samples = _packgen_samples(tmp_path)
    result = GuiController(_RecordingSink()).pack_init(
        str(samples),
        name="acme_soap",
        display="Acme SOAP",
        confirmed_distinct_patients=True,
        out_dir=str(tmp_path / "packs"),
    )
    assert result["ok"] is True, result.get("error")
    pack_dir = Path(str(result["pack_dir"]))
    assert pack_dir.is_dir()
    assert (pack_dir / "pack.yaml").is_file()
    assert (pack_dir / "DRAFT.md").is_file()
    assert "DRAFT pack" in str(result["draft_md"])
    assert isinstance(result["summary"], list) and result["summary"]


def test_pack_init_refuses_without_confirmation(tmp_path: Path) -> None:
    pytest.importorskip("fitz", reason="packgen needs PyMuPDF")
    samples = _packgen_samples(tmp_path)
    result = GuiController(_RecordingSink()).pack_init(
        str(samples),
        name="acme_soap",
        confirmed_distinct_patients=False,
        out_dir=str(tmp_path / "packs"),
    )
    assert result["ok"] is False
    assert result["error"] == "ConfirmationRequired"
    # The refusal still surfaces the caveat + the PHI-safe summary to confirm.
    assert isinstance(result["caveat"], str) and result["caveat"]
    assert isinstance(result["summary"], list) and result["summary"]
    # And it wrote NOTHING.
    assert not (tmp_path / "packs" / "acme_soap").exists()


def test_pack_init_single_sample_suppresses_text(tmp_path: Path) -> None:
    """The single-sample text-suppression behavior is inherited from summary_lines."""
    pytest.importorskip("fitz", reason="packgen needs PyMuPDF")
    samples = _packgen_samples(tmp_path, n=1)
    result = GuiController(_RecordingSink()).pack_init(
        str(samples), name="acme_soap", confirmed_distinct_patients=False
    )
    assert result["low_confidence"] is True
    summary = result["summary"]
    blob = " ".join(summary)  # type: ignore[arg-type]
    # Single sample: static-vs-per-patient is indistinguishable, so span text is
    # suppressed — the loud "text suppressed" markers appear and no sample value.
    assert "text suppressed" in blob
    for value in ("Synthia", "Hypertension", "1985"):
        assert value not in blob, f"single-sample summary leaked {value!r}"


def test_pack_init_invalid_name_is_clean_error(tmp_path: Path) -> None:
    result = GuiController(_RecordingSink()).pack_init(
        str(tmp_path), name="Bad-Name", confirmed_distinct_patients=True
    )
    assert result["ok"] is False
    assert result["error"] == "InvalidPackName"


def test_pack_init_no_samples_is_clean_error(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result = GuiController(_RecordingSink()).pack_init(
        str(empty), name="acme_soap", confirmed_distinct_patients=True
    )
    assert result["ok"] is False
    assert result["error"] == "NoSamplesFound"


# --- pack_init_async (W3-1: the async wizard backend) ----------------------


def test_pack_init_async_writes_draft(tmp_path: Path) -> None:
    """The async path returns started=True; the draft result is fetchable via
    last_pack_result once the packgen done event lands."""
    pytest.importorskip("fitz", reason="packgen needs PyMuPDF")
    samples = _packgen_samples(tmp_path)
    sink = _RecordingSink()
    controller = GuiController(sink)
    started = controller.pack_init_async(
        str(samples),
        name="acme_soap",
        display="Acme SOAP",
        confirmed_distinct_patients=True,
        out_dir=str(tmp_path / "packs"),
    )
    assert started == {"ok": True, "started": True}

    # Poll until the packgen done/error stage event lands (or ~10s).
    deadline = time.time() + 10
    while time.time() < deadline and not any(
        e.get("type") in ("stage", "error")
        and (e.get("state") == "done" or e.get("type") == "error")
        for e in sink.events
    ):
        time.sleep(0.05)

    result = controller.last_pack_result()
    assert result["ok"] is True, result.get("error")
    pack_dir = Path(str(result["pack_dir"]))
    assert pack_dir.is_dir()
    assert (pack_dir / "DRAFT.md").is_file()


def test_pack_init_async_busy_guard(tmp_path: Path) -> None:
    """A second async run while the first holds the busy flag is rejected.

    Acquire the busy flag directly (deterministic), then assert a second
    pack_init_async returns Busy without spawning a worker; release after.
    """
    pytest.importorskip("fitz", reason="packgen needs PyMuPDF")
    samples = _packgen_samples(tmp_path)
    controller = GuiController(_RecordingSink())
    assert controller._acquire() is True  # simulate an in-flight run
    try:
        second = controller.pack_init_async(
            str(samples), name="acme_soap", confirmed_distinct_patients=True
        )
        assert second == {"ok": False, "error": "Busy"}
    finally:
        controller._release()


def test_last_pack_result_empty_before_any_run() -> None:
    assert GuiController(_RecordingSink()).last_pack_result() == {"ok": False, "error": "NoResult"}


def test_pack_init_async_failure_emits_single_packgen_error(tmp_path: Path) -> None:
    """An emit failure on the async path emits exactly ONE error event, on the
    packgen channel — not a doubled, stage-mismatched pair."""
    pytest.importorskip("fitz", reason="packgen needs PyMuPDF")
    samples = _packgen_samples(tmp_path)
    out_file = tmp_path / "not_a_dir"
    out_file.write_text("x", encoding="utf-8")  # out_dir is a FILE → emit fails
    sink = _RecordingSink()
    controller = GuiController(sink)
    started = controller.pack_init_async(
        str(samples), name="acme_soap", confirmed_distinct_patients=True, out_dir=str(out_file)
    )
    assert started == {"ok": True, "started": True}

    deadline = time.time() + 10
    while time.time() < deadline and not any(e.get("type") == "error" for e in sink.events):
        time.sleep(0.05)

    errors = [e for e in sink.events if e.get("type") == "error"]
    assert len(errors) == 1  # not a doubled pack_init + packgen pair
    assert errors[0]["stage"] == "packgen"
    assert controller.last_pack_result()["ok"] is False


# --- JSON-safety of every new method ---------------------------------------


def test_new_methods_return_json_safe_dicts(tmp_path: Path) -> None:
    import json

    db_path = _craft_ledger(tmp_path)
    controller = GuiController(_RecordingSink())
    out = tmp_path / "out"  # the ledger's parent (has no pdfs but is a dir)
    payloads = [
        controller.destination_status("epic"),
        controller.destination_status("ghost"),
        controller.pack_freshness(),
        controller.upload_status(str(db_path)),
        controller.upload_status(str(tmp_path / "nope.db")),
        controller.upload_item_keys(str(db_path)),
        controller.upload_manifest_preview(str(out)),
        controller.pack_init(str(tmp_path), name="Bad-Name"),
    ]
    for payload in payloads:
        # round-trips through JSON with no custom encoder → JSON-safe.
        json.loads(json.dumps(payload))
        assert "ok" in payload


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


# --- source_init (learn-a-source wizard backend) -------------------------------

LEARNED_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "learned" / "clinic_visits.csv"


def test_source_init_refuses_without_confirmation(tmp_path: Path) -> None:
    """confirmed=False returns the PHI-safe proposed mapping and writes nothing."""
    result = GuiController(_RecordingSink()).source_init(
        str(LEARNED_FIXTURE),
        name="clinic_csv",
        confirmed=False,
        out_dir=str(tmp_path),
    )
    assert result["ok"] is False
    assert result["error"] == "ConfirmationRequired"
    assert result["patient_key"] == "PatientID"
    assert isinstance(result["suggestions"], list) and result["suggestions"]
    # PHI probe: the proposed mapping carries column names/types only.
    blob = repr(result)
    for leak in (*FIXTURE_NAMES, "900-12-3456", "ada@example.com"):
        assert leak not in blob
    assert not (tmp_path / "clinic_csv").exists()  # no write


def test_source_init_happy_saves_and_round_trips(tmp_path: Path) -> None:
    """confirmed=True builds, round-trips, and saves the learned mapping."""
    result = GuiController(_RecordingSink()).source_init(
        str(LEARNED_FIXTURE),
        name="clinic_csv",
        display="Clinic CSV",
        confirmed=True,
        out_dir=str(tmp_path),
    )
    assert result["ok"] is True, result
    assert Path(str(result["mapping_dir"])).is_dir()
    assert (tmp_path / "clinic_csv" / "mapping.json").is_file()
    assert result["record_count"] == 3
    assert "Learned source" in str(result["mapping_md"])


def test_source_init_rejects_bad_name(tmp_path: Path) -> None:
    result = GuiController(_RecordingSink()).source_init(
        str(LEARNED_FIXTURE), name="Bad-Name", confirmed=True, out_dir=str(tmp_path)
    )
    assert result == {"ok": False, "error": "InvalidSourceName"}


def test_source_init_no_example_file(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result = GuiController(_RecordingSink()).source_init(
        str(empty), name="x", confirmed=False, out_dir=str(tmp_path)
    )
    assert result == {"ok": False, "error": "NoExampleFile"}


def test_source_init_load_failure_is_distinct_from_dropped(tmp_path: Path) -> None:
    """A mapped column whose transform chokes is a fixable mapping mistake
    (MappingLoadFailed), NOT an unexplained empty-list 'WouldDropColumns'."""
    bad = tmp_path / "bad.csv"
    bad.write_text("PID,DOB\np1,garbage-not-a-date\n", encoding="utf-8")  # DOB→parse_date fails
    result = GuiController(_RecordingSink()).source_init(
        str(bad), name="bad_src", confirmed=True, out_dir=str(tmp_path)
    )
    assert result["ok"] is False
    assert result["error"] == "MappingLoadFailed"
    assert "DOB" in str(result["detail"])  # names the column (PHI-safe)
    assert "garbage-not-a-date" not in repr(result)  # the value never leaks
    assert not (tmp_path / "bad_src").exists()  # nothing written


def test_source_init_unanalyzable_returns_enumerated_code(tmp_path: Path) -> None:
    headerless = tmp_path / "blank.csv"
    headerless.write_text("", encoding="utf-8")
    result = GuiController(_RecordingSink()).source_init(
        str(headerless), name="blank", confirmed=False, out_dir=str(tmp_path)
    )
    assert result == {"ok": False, "error": "CannotAnalyze"}


# --- upload_start / upload_stop (W5/PR-6b: live driving, no browser) -------
#
# Mirrors tests/unit/test_cli_upload.py exactly: a manifest written into out_dir,
# a ready destination pack dir, and the destination SEAM monkeypatched to a
# FakeDestination so the whole flow drives with no Playwright/Chromium. The seam
# here is the controller module's _attach_destination (not the CLI's).

_LOOPBACK = "http://127.0.0.1:9222"
_UPLOAD_DEST = "testdest"
# Three distinct synthetic patients (feedface- GUIDs), one chart each.
_UPLOAD_PATS = [f"feedface-0000-0000-0000-00000000020{i}" for i in range(3)]


def _upload_pack_dir(tmp_path: Path) -> Path:
    """A ready destination pack dir (real selectors, no DISCOVER placeholders)."""
    from anastomosis.destinations.browserpack import SelectorMap

    root = tmp_path / "packs"
    pack = root / _UPLOAD_DEST
    pack.mkdir(parents=True)
    selectors = {slot: f"#{slot}" for slot in SelectorMap.required_slots()}
    lines = [f"name: {_UPLOAD_DEST}", "display: Test Destination", "selectors:"]
    lines += [f"  {slot}: '{sel}'" for slot, sel in selectors.items()]
    (pack / "pack.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


def _write_upload_manifest(tmp_path: Path, n: int = 3) -> Path:
    """Write a manifest of ``n`` charts into out_dir; return out_dir.

    The chart PDFs land INTO out_dir (where a real render puts them) because the
    manifest stores basenames re-absolutized against the manifest root, so the
    engine's preflight (existence + re-hash) resolves them there.
    """
    import datetime

    from anastomosis.core.model import Patient, PatientRecord
    from anastomosis.deliver.browser.persist import write_upload_manifest
    from anastomosis.reconstruct.engine import RenderedDoc

    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    docs: list[RenderedDoc] = []
    records: list[PatientRecord] = []
    for i in range(n):
        pid = _UPLOAD_PATS[i]
        path = out_dir / f"note-{i}.pdf"
        path.write_bytes(f"chart-{i}".encode())
        docs.append(RenderedDoc(path=path, encounter_id=f"enc-{i}", patient_id=pid))
        patient = Patient(
            id=pid,
            family_name="Family",
            given_name="Given",
            birth_date=datetime.date(1980, 1, 1 + i),
        )
        records.append(PatientRecord(id=pid, patient=patient))
    write_upload_manifest(docs, records, out_dir)
    return out_dir


def _upload_known(n: int = 3) -> dict[str, str]:
    return {_UPLOAD_PATS[i]: f"dest-{i}" for i in range(n)}


def _ledger_counts(out_dir: Path) -> dict[str, int]:
    """The controller writes its ledger to <out_dir>/upload_ledger.sqlite — the
    SAME file the CLI uses, so a run resumes/monitors across both frontends."""
    from anastomosis.deliver.browser.tracking import TrackingDB

    tracking = TrackingDB(out_dir / "upload_ledger.sqlite")
    try:
        return dict(tracking.counts())
    finally:
        tracking.close()


def _wait_for_terminal_upload(
    sink: _RecordingSink, *, deadline_s: float = 10.0
) -> dict[str, object]:
    """Poll until a terminal upload event lands (stage done OR error); return it.

    Time-bounded like test_last_run_summary_serves_async_run. A terminal event is
    a ``stage`` event with ``stage==upload`` and ``state==done`` OR an ``error``
    event with ``stage==upload``.
    """

    def _terminal(e: dict[str, object]) -> bool:
        if e.get("type") == "stage" and e.get("stage") == "upload" and e.get("state") == "done":
            return True
        return e.get("type") == "error" and e.get("stage") == "upload"

    deadline = time.time() + deadline_s
    while time.time() < deadline:
        for e in list(sink.events):
            if _terminal(e):
                return e
        time.sleep(0.05)
    raise AssertionError(f"no terminal upload event landed; events={sink.events!r}")


def test_upload_start_drives_to_terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole drive flow runs with no browser: the seam yields a FakeDestination,
    the engine drives every item to COMPLETED, and a terminal `done` event lands.
    The PHI probe holds — no patient name appears in any event."""
    from anastomosis.deliver.browser.fake import FakeDestination
    from anastomosis.deliver.browser.states import UploadState

    out_dir = _write_upload_manifest(tmp_path)
    pack_root = _upload_pack_dir(tmp_path)
    monkeypatch.setattr(
        controller_module,
        "_attach_destination",
        lambda cdp, loaded: FakeDestination(_upload_known()),
    )

    sink = _RecordingSink()
    controller = GuiController(sink)
    started = controller.upload_start(
        str(out_dir), _LOOPBACK, _UPLOAD_DEST, pack_dirs=[str(pack_root)]
    )
    assert started == {"ok": True, "started": True}

    terminal = _wait_for_terminal_upload(sink)
    assert terminal["type"] == "stage", f"expected a clean done, got {terminal!r}"
    assert terminal["state"] == "done"

    # The ledger reached all-COMPLETED (read via the shared ledger path).
    counts = _ledger_counts(out_dir)
    assert counts.get(UploadState.COMPLETED.value) == 3
    assert sum(counts.values()) == 3

    # upload_status agrees the terminal bucket is full.
    status = controller.upload_status(str(out_dir / "upload_ledger.sqlite"))
    assert status["ok"] is True
    assert status["groups"]["terminal"] == 3  # type: ignore[index]

    # PHI probe: no patient name rides any event (events are stage/state names).
    blob = repr(sink.events)
    for name in ("Family", "Given", *FIXTURE_NAMES):
        assert name not in blob, f"event log leaked patient value {name!r}"


def test_upload_start_rejects_non_loopback_cdp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-loopback CDP host is a hard refusal BEFORE the busy guard is taken —
    a subsequent start is not 'Busy' (the guard was never held)."""
    from anastomosis.deliver.browser.fake import FakeDestination

    out_dir = _write_upload_manifest(tmp_path)
    pack_root = _upload_pack_dir(tmp_path)
    seam_calls = {"n": 0}

    def _spy(cdp: str, loaded: object) -> object:
        seam_calls["n"] += 1
        return FakeDestination(_upload_known())

    monkeypatch.setattr(controller_module, "_attach_destination", _spy)
    sink = _RecordingSink()
    controller = GuiController(sink)
    result = controller.upload_start(
        str(out_dir), "http://evil.example.com:9222", _UPLOAD_DEST, pack_dirs=[str(pack_root)]
    )
    assert result == {"ok": False, "error": "BadCdpEndpoint"}
    assert seam_calls["n"] == 0  # the seam is never reached past the loopback gate
    # The busy guard was never held — a clean loopback start now succeeds.
    second = controller.upload_start(
        str(out_dir), _LOOPBACK, _UPLOAD_DEST, pack_dirs=[str(pack_root)]
    )
    assert second == {"ok": True, "started": True}
    # Drain the spawned worker so its daemon thread does not outlive the test.
    _wait_for_terminal_upload(sink)


def test_upload_start_bad_manifest(tmp_path: Path) -> None:
    """An out_dir with no manifest is a clean BadManifest (no busy guard, no spawn)."""
    out_dir = tmp_path / "empty"
    out_dir.mkdir()
    result = GuiController(_RecordingSink()).upload_start(str(out_dir), _LOOPBACK, _UPLOAD_DEST)
    assert result == {"ok": False, "error": "BadManifest"}


def test_upload_stop_without_run() -> None:
    """A stop with no run in flight is a clean NoRun (never raises)."""
    assert GuiController(_RecordingSink()).upload_stop() == {"ok": False, "error": "NoRun"}


def test_upload_start_busy_guard(tmp_path: Path) -> None:
    """A start while the busy flag is held is rejected Busy (deterministic).

    Acquire the busy flag directly, then assert upload_start returns Busy. The
    pre-flight is clean (real manifest + ready pack) so the only block is Busy;
    release after.
    """
    out_dir = _write_upload_manifest(tmp_path)
    pack_root = _upload_pack_dir(tmp_path)
    controller = GuiController(_RecordingSink())
    assert controller._acquire() is True  # simulate an in-flight run
    try:
        result = controller.upload_start(
            str(out_dir), _LOOPBACK, _UPLOAD_DEST, pack_dirs=[str(pack_root)]
        )
        assert result == {"ok": False, "error": "Busy"}
    finally:
        controller._release()


def test_upload_start_never_raises_on_bad_args() -> None:
    """The never-raise contract holds for a non-string out_dir — a returned
    error dict, not a propagated TypeError, and the busy guard stays free."""
    controller = GuiController(_RecordingSink())
    result = controller.upload_start(None, _LOOPBACK, _UPLOAD_DEST)  # type: ignore[arg-type]
    assert result["ok"] is False  # a dict, never a raised TypeError
    # The pre-flight failure never took the busy guard (a later start is not Busy).
    assert controller._acquire() is True
    controller._release()


def test_upload_start_refuses_when_output_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run already holding the output dir is refused (OutputLocked), not raced
    into the shared ledger — the GUI honors the CLI's output lock."""
    from anastomosis.core.locking import output_lock
    from anastomosis.deliver.browser.fake import FakeDestination

    out_dir = _write_upload_manifest(tmp_path)
    pack_root = _upload_pack_dir(tmp_path)
    monkeypatch.setattr(
        controller_module,
        "_attach_destination",
        lambda cdp, loaded: FakeDestination(_upload_known()),
    )
    sink = _RecordingSink()
    controller = GuiController(sink)
    with output_lock(out_dir):  # another run (CLI or GUI) holds the dir
        started = controller.upload_start(
            str(out_dir), _LOOPBACK, _UPLOAD_DEST, pack_dirs=[str(pack_root)]
        )
        assert started == {"ok": True, "started": True}
        terminal = _wait_for_terminal_upload(sink)
    assert terminal["type"] == "error"
    assert terminal["error"] == "OutputLocked"  # refused, never drove the engine
