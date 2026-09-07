"""Charts already in a folder must answer the question the run is asking.

The idempotent skip must not decide on `target.exists()` alone — not the
pack, not the section flags, nothing about what produced the file:
re-running with a section switched OFF into a folder that already holds
charts must not report "done, and verified" while every chart still
carries the suppressed section. QA alone cannot catch this: a
self-disabling check like `vitals_loinc` reads the flag, not the file,
and passes clean over stale content.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import anastomosis.reconstruct.chromium as chromium
import anastomosis.sources.pf_tebra  # noqa: F401 — registers the adapter
from anastomosis.pipeline import RENDER_SETTINGS_NAME, PipelineError, run_pipeline

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"


class _FakeChromium:
    """A real PDF without a browser — the unit lane has none."""

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


@pytest.fixture
def rendered(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("pymupdf", reason="the fake renderer writes a real PDF")
    monkeypatch.setattr(chromium, "ChromiumRenderer", _FakeChromium)


def _run(
    out: Path, *, section: list[str] | None = None, pack: str = "generic_soap", force: bool = False
) -> Any:
    return run_pipeline(
        export_dir=FIXTURE,
        out=out,
        source="pf-tebra",
        pack=pack,
        pack_dirs=None,
        force=force,
        section=section,
        qa=False,
    )


def test_a_run_records_what_it_rendered_from(rendered: None, tmp_path: Path) -> None:
    out = tmp_path / "out"
    _run(out, section=["insurance=on"])

    settings = json.loads((out / RENDER_SETTINGS_NAME).read_text(encoding="utf-8"))
    assert settings["pack"] == "generic_soap"
    assert settings["sections"]["insurance"] is True
    # The whole matrix, not only what was overridden — a default that changes
    # between releases is a difference too.
    assert set(settings["sections"]) >= {"insurance", "vitals", "addenda", "social_history"}


def test_the_same_settings_still_skip(rendered: None, tmp_path: Path) -> None:
    """The idempotent skip is the point of the directory; it must survive."""
    out = tmp_path / "out"
    first = _run(out, section=["insurance=on"])
    second = _run(out, section=["insurance=on"])

    assert len(first.render_result.rendered) == 6
    assert len(second.render_result.rendered) == 0
    assert len(second.render_result.skipped) == 6


def test_changing_a_section_refuses_instead_of_quietly_doing_nothing(
    rendered: None, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    _run(out, section=["insurance=on"])

    with pytest.raises(PipelineError) as caught:
        _run(out, section=["insurance=off"])

    assert caught.value.exit_code == 2
    # The message names WHICH setting moved and which way, so the operator does
    # not have to diff two command lines to find out.
    assert "insurance on -> off" in str(caught.value)
    assert "--force" in str(caught.value)


def test_changing_the_layout_refuses_too(rendered: None, tmp_path: Path) -> None:
    out = tmp_path / "out"
    _run(out, pack="generic_soap")

    with pytest.raises(PipelineError) as caught:
        _run(out, pack="practice_fusion_soap")
    assert "layout" in str(caught.value)


def test_force_rebuilds_with_the_new_settings(rendered: None, tmp_path: Path) -> None:
    """`--force` already means "render them all again"; it is the escape the
    refusal names, and it has to actually honour the new flags."""
    out = tmp_path / "out"
    _run(out, section=["insurance=on"])
    result = _run(out, section=["insurance=off"], force=True)

    assert len(result.render_result.rendered) == 6
    settings = json.loads((out / RENDER_SETTINGS_NAME).read_text(encoding="utf-8"))
    assert settings["sections"]["insurance"] is False


def test_an_unreadable_record_does_not_block_a_run(rendered: None, tmp_path: Path) -> None:
    """A corrupt sidecar is not a reason to refuse work. It is treated as absent
    — the same posture the render index takes toward its own unreadable file."""
    out = tmp_path / "out"
    _run(out, section=["insurance=on"])
    (out / RENDER_SETTINGS_NAME).write_text("{not json", encoding="utf-8")

    _run(out, section=["insurance=off"])  # must not raise
