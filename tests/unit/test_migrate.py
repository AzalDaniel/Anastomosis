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
