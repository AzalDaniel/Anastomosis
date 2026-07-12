# AI-assisted: written with Claude agents under the author's direction and review; see DESIGN.md.
"""Tests for the shared migration core (``core/migrate.py``).

A migration is a general EHR→EHR move; PF→Tebra is one instance. These pin:
the three render modes (neutral → generic_soap, a Jinja pack → that pack,
ccda-standard → one HL7-view PDF per patient), the dual output layout (charts +
the structured C-CDA payload), the resolved transit map (pf→tebra chooses
``ccda_import``), the loud ``bad_destination`` failure, profile round-trip
(config only, 0600), and route determinism. The fake-Chromium pattern matches
``test_ccda_standard.py`` / ``test_commands.py``.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

import anastomosis.reconstruct.ccda_standard.renderer as ccda_renderer
import anastomosis.reconstruct.chromium as chromium
from anastomosis.core.migrate import (
    RENDER_CCDA_STANDARD,
    RENDER_NEUTRAL,
    MigrationCommand,
    MigrationProfiles,
    run_migration,
    user_migrations_path,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"


class _FakeChromium:
    """Writes a REAL pdf carrying the chart text (the test_commands pattern), so
    the pack-mode QA stage runs for real against what was 'rendered'."""

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


def _patch_chromium(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch both Chromium seams: the pipeline factory and the ccda-standard one."""
    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)
    monkeypatch.setattr(ccda_renderer, "_default_renderer", lambda: _FakeChromium())


def _assert_manifest(charts_dir: Path, *, expected_items: int) -> None:
    """The default-on upload manifest is present, readable, and the right size."""
    from anastomosis.deliver.browser.persist import MANIFEST_NAME, read_upload_manifest

    assert (charts_dir / MANIFEST_NAME).is_file()
    items, patients = read_upload_manifest(charts_dir)
    assert len(items) == expected_items
    # Every item's patient is carried (the resolver searches by name + DOB).
    assert {item.patient_id for item in items} <= set(patients)


# --- the three render modes -------------------------------------------------


def test_migrate_neutral_uses_generic_soap_and_emits_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fitz", reason="needs PyMuPDF")
    _patch_chromium(monkeypatch)
    out = tmp_path / "out"
    result = run_migration(
        MigrationCommand(export_dir=FIXTURE, out_dir=out, source="pf-tebra", destination="tebra")
    )
    # Neutral resolves to the generic_soap pack via the full pipeline.
    assert result.render_mode == RENDER_NEUTRAL
    assert result.pack == "generic_soap"
    assert result.pipeline is not None
    assert result.ccda_view is None
    # BOTH artifacts emitted: human-readable charts AND the structured payload.
    assert len(list((out / "charts").glob("*.pdf"))) == 6  # per-encounter charts
    assert list((out / "ccda").glob("*.xml"))  # structured C-CDA payload
    assert result.ccda_export.counts["patients"] == 3
    # A migration writes the upload manifest by default (into <out>/charts).
    _assert_manifest(out / "charts", expected_items=6)


def test_migrate_pack_render_uses_named_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A render value that is neither 'neutral' nor 'ccda-standard' is a pack name."""
    pytest.importorskip("fitz", reason="needs PyMuPDF")
    _patch_chromium(monkeypatch)
    out = tmp_path / "out"
    result = run_migration(
        MigrationCommand(
            export_dir=FIXTURE,
            out_dir=out,
            source="pf-tebra",
            destination="tebra",
            render="practice_fusion_soap",
            # The PF pack's strict QA checks do not pass against the fake
            # text-only renderer; this test pins pack RESOLUTION, not QA.
            qa=False,
        )
    )
    assert result.pack == "practice_fusion_soap"
    assert result.pipeline is not None
    assert result.pipeline.render_result.documents  # the PF skin rendered charts
    assert list((out / "ccda").glob("*.xml"))


def test_migrate_ccda_standard_one_view_pdf_per_patient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_chromium(monkeypatch)
    out = tmp_path / "out"
    result = run_migration(
        MigrationCommand(
            export_dir=FIXTURE,
            out_dir=out,
            source="pf-tebra",
            destination="tebra",
            render=RENDER_CCDA_STANDARD,
            # This test pins the ARTIFACTS (one PDF per patient + payload +
            # manifest), not QA. The shared _FakeChromium overflows the large
            # whole-patient view into a blank page, which QA correctly fails;
            # the QA stage is exercised in the dedicated QA tests below with a
            # renderer that actually places text.
            qa=False,
        )
    )
    assert result.render_mode == RENDER_CCDA_STANDARD
    assert result.pack is None
    assert result.pipeline is None  # no Jinja pack in this mode
    assert result.ccda_view is not None
    # One standard-C-CDA-view PDF per patient (the 3-patient fixture).
    assert len(result.ccda_view.documents) == 3
    assert len(list((out / "charts").glob("*_ccda.pdf"))) == 3
    # Still emits the structured payload for the destination to import.
    assert result.ccda_export.counts["patients"] == 3
    assert list((out / "ccda").glob("*.xml"))
    # The upload manifest is written in ccda-standard mode too — one item per
    # patient (the whole-patient view has no per-encounter documents).
    _assert_manifest(out / "charts", expected_items=3)


# --- the transit map (the route a migration would take) ---------------------


def test_migrate_resolves_pf_tebra_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pf→tebra migration chooses the C-CDA import route (tebra's only one)."""
    _patch_chromium(monkeypatch)
    result = run_migration(
        MigrationCommand(
            export_dir=FIXTURE,
            out_dir=tmp_path / "out",
            source="pf-tebra",
            destination="tebra",
            render=RENDER_CCDA_STANDARD,
            # This test pins the ROUTE, not QA; the shared fake overflows the
            # whole-patient view into a blank page QA would fail (see the QA tests).
            qa=False,
        )
    )
    assert result.transit.destination == "tebra"
    assert result.transit.chosen is not None
    assert result.transit.chosen.kind.value == "ccda_import"


def test_migrate_route_is_deterministic() -> None:
    """Resolving the same destination twice yields the same rendered transit map."""
    from anastomosis.deliver.router import plan_route
    from anastomosis.destinations.registry import DestinationRegistry

    registry = DestinationRegistry.load()
    first = plan_route("tebra", registry)
    second = plan_route("tebra", registry)
    assert first.render() == second.render()
    assert first.chosen is not None and second.chosen is not None
    assert first.chosen.kind == second.chosen.kind


def test_migrate_unknown_destination_is_bad_destination_exit_2(tmp_path: Path) -> None:
    from anastomosis.pipeline import PipelineError

    with pytest.raises(PipelineError) as excinfo:
        run_migration(
            MigrationCommand(
                export_dir=FIXTURE,
                out_dir=tmp_path / "out",
                source="pf-tebra",
                destination="ghost",
            )
        )
    assert excinfo.value.kind == "bad_destination"
    assert excinfo.value.exit_code == 2
    assert "ghost" in str(excinfo.value)


def test_migrate_output_collision_is_clean_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An out dir whose charts target is a FILE is a clean exit-2 PipelineError
    (ccda-standard mode validates both targets up front)."""
    _patch_chromium(monkeypatch)
    from anastomosis.pipeline import PipelineError

    out = tmp_path / "out"
    (out).mkdir()
    (out / "charts").write_text("x", encoding="utf-8")  # charts target is a file
    with pytest.raises(PipelineError) as excinfo:
        run_migration(
            MigrationCommand(
                export_dir=FIXTURE,
                out_dir=out,
                source="pf-tebra",
                destination="tebra",
                render=RENDER_CCDA_STANDARD,
            )
        )
    assert excinfo.value.exit_code == 2
    assert excinfo.value.kind == "bad_output"


# --- profile persistence (config only, no paths, 0600) ----------------------


def test_profile_round_trip_and_permissions(tmp_path: Path) -> None:
    path = tmp_path / "migrations.json"
    store = MigrationProfiles(path)
    profile = {
        "source": "pf-tebra",
        "destination": "tebra",
        "render": "ccda-standard",
        "sections": {"insurance": True},
        "qa": False,
    }
    store.save("pf_to_tebra", profile)

    # A fresh store loads the same config back.
    reloaded = MigrationProfiles(path).get("pf_to_tebra")
    assert reloaded == profile
    assert MigrationProfiles(path).names() == ["pf_to_tebra"]

    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_profile_stores_config_only_no_paths(tmp_path: Path) -> None:
    """A profile drops any stray non-config keys (paths/PHI never persist)."""
    path = tmp_path / "migrations.json"
    store = MigrationProfiles(path)
    store.save(
        "p",
        {
            "source": "pf-tebra",
            "destination": "tebra",
            "render": "neutral",
            "sections": {},
            "qa": True,
            "export_dir": "/some/phi/path",  # must NOT persist
            "out_dir": "/another/path",
        },
    )
    saved = MigrationProfiles(path).get("p")
    assert saved is not None
    assert set(saved) == {"source", "destination", "render", "sections", "qa"}
    assert "export_dir" not in saved
    assert "out_dir" not in saved


def test_profile_missing_or_garbage_starts_empty(tmp_path: Path) -> None:
    assert MigrationProfiles(tmp_path / "absent.json").names() == []
    garbage = tmp_path / "garbage.json"
    garbage.write_text("{not json", encoding="utf-8")
    assert MigrationProfiles(garbage).get("anything") is None


def test_user_migrations_path_under_anastomosis_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: Path("/home/example")))
    assert user_migrations_path() == Path("/home/example/.anastomosis/migrations.json")


# --- stage-contract parity --------------------------------------------------


def test_migrate_pack_and_ccda_standard_share_stage_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both render modes must emit DETECT, INGEST, and MANIFEST in the same
    order, with the same PHI-safe payload shapes. Both the ccda-standard and
    the pack path route these emissions through three shared
    helpers, but ``_run_pack_mode`` still reaches them via
    ``run_pipeline_command``. This test is the contract that the two paths
    keep emitting the same events — drift would silently break the CLI/GUI
    presenters that consume the stream.
    """
    pytest.importorskip("fitz", reason="needs PyMuPDF")
    _patch_chromium(monkeypatch)

    from anastomosis.pipeline import STAGE_DETECT, STAGE_INGEST, STAGE_MANIFEST, StageEvent

    def _collect(events: list[StageEvent]) -> list[StageEvent]:
        return events

    pack_events: list[StageEvent] = []
    ccda_events: list[StageEvent] = []

    run_migration(
        MigrationCommand(
            export_dir=FIXTURE,
            out_dir=tmp_path / "pack",
            source="pf-tebra",
            destination="tebra",
            # qa=False so the parity is on the shared stages only; we are not
            # testing the QA stage here, just the contract that both modes
            # emit DETECT/INGEST/MANIFEST identically.
            qa=False,
        ),
        on_event=pack_events.append,
    )
    run_migration(
        MigrationCommand(
            export_dir=FIXTURE,
            out_dir=tmp_path / "ccda",
            source="pf-tebra",
            destination="tebra",
            render=RENDER_CCDA_STANDARD,
            # qa=False here too: the shared fake overflows the whole-patient view
            # into a blank page that QA fails; this test pins the DETECT/INGEST/
            # MANIFEST stage contract, not QA (QA has its own tests).
            qa=False,
        ),
        on_event=ccda_events.append,
    )

    def _by_stage(events: list[StageEvent], stage: str) -> StageEvent | None:
        for ev in events:
            if ev.stage == stage:
                return ev
        return None

    # Both modes emit DETECT with the same adapter name (the PHI-safe detail).
    pack_detect = _by_stage(pack_events, STAGE_DETECT)
    ccda_detect = _by_stage(ccda_events, STAGE_DETECT)
    assert pack_detect is not None and ccda_detect is not None
    assert pack_detect.detail == ccda_detect.detail == "pf-tebra"

    # Both modes emit INGEST with the same record-count payload SHAPE.
    pack_ingest = _by_stage(pack_events, STAGE_INGEST)
    ccda_ingest = _by_stage(ccda_events, STAGE_INGEST)
    assert pack_ingest is not None and ccda_ingest is not None
    assert pack_ingest.counts == ccda_ingest.counts  # {"records": 3}
    assert set(pack_ingest.counts) == {"records"}  # nothing else (PHI fence)

    # Both modes emit MANIFEST with the same ``items`` count-payload key (and
    # the same numeric count, since the same fixture renders six encounters
    # per pack mode / one C-CDA per patient — three patients * two encounters
    # per patient on average vs. three per-patient C-CDAs).
    pack_manifest = _by_stage(pack_events, STAGE_MANIFEST)
    ccda_manifest = _by_stage(ccda_events, STAGE_MANIFEST)
    assert pack_manifest is not None and ccda_manifest is not None
    assert set(pack_manifest.counts) == set(ccda_manifest.counts) == {"items"}
    assert pack_manifest.counts["items"] > 0
    assert ccda_manifest.counts["items"] > 0

    # Both modes emit DETECT BEFORE INGEST BEFORE MANIFEST — the order the
    # CLI/GUI presenters render. Drift here breaks both frontends.
    def _index(events: list[StageEvent], stage: str) -> int:
        for i, ev in enumerate(events):
            if ev.stage == stage:
                return i
        return -1

    for events in (pack_events, ccda_events):
        d = _index(events, STAGE_DETECT)
        i = _index(events, STAGE_INGEST)
        m = _index(events, STAGE_MANIFEST)
        assert -1 < d < i < m, f"stage order broke: detect={d} ingest={i} manifest={m}"

    # Sanity: _collect is unused but pins the test's intent (events are
    # collected by appending to a list, not interpreted as messages).
    assert _collect(pack_events) is pack_events


# --- ccda-standard QA stage -------------------------------------------------


class _ViewChromium:
    """A fake renderer that writes a REAL, non-blank Letter PDF carrying the top
    of the standard C-CDA view (patient header incl. DOB) via point-insertion.

    The whole-patient view is far larger than one insert_textbox can hold — the
    shared _FakeChromium overflows it into a blank page (which QA correctly
    fails) — so the QA-stage tests need a renderer that actually places text. The
    patient DOB (the identity anchor data_integrity checks) lives in the header,
    so the first lines are enough."""

    def __init__(self, **kwargs: object) -> None:
        pass

    def render(self, html: str, pdf_path: Path) -> None:
        import fitz

        from anastomosis.core.textutil import html_to_text

        lines = (html_to_text(html) or "(empty)").splitlines()
        doc = fitz.open()
        page = doc.new_page(width=612, height=792)  # Letter, as the view declares
        page.insert_text((36, 48), "\n".join(lines[:80]), fontsize=8)
        doc.save(str(pdf_path))
        doc.close()

    def close(self) -> None:
        pass


class _NoAnchorChromium:
    """Writes a non-blank Letter page that OMITS the patient identity anchor — a
    mutation the ``data_integrity`` check catches (the DOB is missing) while
    ``layout_pagination`` passes (the page is not blank), mirroring the QA
    engine's mutation-corpus idiom."""

    def __init__(self, **kwargs: object) -> None:
        pass

    def render(self, html: str, pdf_path: Path) -> None:
        import fitz

        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        page.insert_text((36, 48), "Standard C-CDA view placeholder page.", fontsize=10)
        doc.save(str(pdf_path))
        doc.close()

    def close(self) -> None:
        pass


def _patch_ccda_renderer(monkeypatch: pytest.MonkeyPatch, factory: object) -> None:
    """Patch ONLY the ccda-standard renderer seam (no pipeline pack renderer)."""
    monkeypatch.setattr(ccda_renderer, "_default_renderer", factory)


def _ccda_command(out: Path, *, qa: bool = True) -> MigrationCommand:
    return MigrationCommand(
        export_dir=FIXTURE,
        out_dir=out,
        source="pf-tebra",
        destination="tebra",
        render=RENDER_CCDA_STANDARD,
        qa=qa,
    )


def test_migrate_ccda_standard_runs_qa_and_writes_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default QA in ccda-standard mode writes qa_report.json next to the
    per-patient PDFs, fires the STAGE_QA event, runs the document-generic checks,
    and records the encounter-scoped checks as skipped-with-reason."""
    pytest.importorskip("fitz", reason="needs PyMuPDF")
    from anastomosis.pipeline import STAGE_QA
    from anastomosis.qa.runner import REPORT_NAME

    _patch_ccda_renderer(monkeypatch, lambda: _ViewChromium())
    out = tmp_path / "out"
    events: list[object] = []
    run_migration(_ccda_command(out), on_event=events.append)

    # qa_report.json lands next to the per-patient PDFs (in <out>/charts).
    report_path = out / "charts" / REPORT_NAME
    assert report_path.is_file()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert len(report["documents"]) == 3  # the 3-patient fixture

    # The STAGE_QA event fired exactly once with the pass/warn/fail count payload.
    qa_events = [e for e in events if getattr(e, "stage", None) == STAGE_QA]
    assert len(qa_events) == 1
    assert set(qa_events[0].counts) == {"pass", "warn", "fail"}

    # Every document PASSES (DOB identity anchor present, non-blank Letter page).
    assert report["summary"]["fail"] == 0

    # All four engine checks appear per document: the two document-generic checks
    # RAN; the two encounter-scoped checks are recorded as skipped WITH A REASON
    # (never silently omitted).
    for doc in report["documents"]:
        by_check = {c["check"]: c for c in doc["checks"]}
        assert set(by_check) == {
            "data_integrity",
            "layout_pagination",
            "vitals_loinc",
            "date_staleness",
        }
        assert by_check["data_integrity"]["verdict"] == "pass"
        assert by_check["layout_pagination"]["verdict"] == "pass"
        for skipped in ("vitals_loinc", "date_staleness"):
            assert by_check[skipped]["verdict"] == "pass"
            assert any("skipped" in f for f in by_check[skipped]["findings"])


def test_migrate_ccda_standard_qa_fail_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rendered view MISSING the identity anchor fails data_integrity → the
    migration raises PipelineError(exit 1, qa_failed), exactly like run_pipeline;
    the report is still written for the operator to inspect."""
    pytest.importorskip("fitz", reason="needs PyMuPDF")
    from anastomosis.pipeline import PipelineError
    from anastomosis.qa.runner import REPORT_NAME

    _patch_ccda_renderer(monkeypatch, lambda: _NoAnchorChromium())
    out = tmp_path / "out"
    with pytest.raises(PipelineError) as excinfo:
        run_migration(_ccda_command(out))
    assert excinfo.value.exit_code == 1
    assert excinfo.value.kind == "qa_failed"
    # The report is written (QA runs before the raise), recording the failure.
    report = json.loads((out / "charts" / REPORT_NAME).read_text(encoding="utf-8"))
    assert report["summary"]["fail"] == 3
    fails = {
        c["check"] for doc in report["documents"] for c in doc["checks"] if c["verdict"] == "fail"
    }
    assert fails == {"data_integrity"}


def test_migrate_ccda_standard_no_qa_writes_no_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--no-qa` (qa=False) skips the QA stage entirely — no qa_report.json."""
    pytest.importorskip("fitz", reason="needs PyMuPDF")
    from anastomosis.qa.runner import REPORT_NAME

    _patch_ccda_renderer(monkeypatch, lambda: _ViewChromium())
    out = tmp_path / "out"
    run_migration(_ccda_command(out, qa=False))
    assert not (out / "charts" / REPORT_NAME).exists()
