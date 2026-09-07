"""The markup a draft pack is written from lives in template files.

No PyMuPDF: the analysis is built by hand, so these run in a minimal install
too. Everything in it is invented (example-style labels, no values).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anastomosis.packgen.emit import (
    _TEMPLATE_DIR,
    DRAFT_TEMPLATES,
    _fill,
    emit_draft_pack,
)
from anastomosis.packgen.evidence import IMAGE_ONLY, LayoutEvidence, PageEvidence
from anastomosis.packgen.infer import (
    ColumnGrid,
    DesignTokens,
    PackAnalysis,
    PageBreakStats,
    PageGeometry,
    SectionCandidate,
    TypeScale,
    TypeScaleLevel,
)

_PACKS = Path(__file__).resolve().parents[2] / "src" / "anastomosis" / "packs"

_RECOGNIZED = LayoutEvidence(
    engine_available=True,
    ocr_manifest=(("engine", "example-engine"), ("version", "0.0")),
    pages=(
        PageEvidence(
            page_index=0,
            classification=IMAGE_ONLY,
            native_span_count=0,
            raster_region_count=1,
            ocr_token_count=9,
            ocr_accepted_count=7,
            ocr_below_confidence=1,
            duplicate_count=1,
            disagreement_count=0,
            ocr_attempted=True,
        ),
    ),
    ocr_texts=frozenset({"SUBJECTIVE"}),
)


def _analysis(evidence: LayoutEvidence | None = None) -> PackAnalysis:
    """A minimal analysis that still reaches every emitted file."""
    return PackAnalysis(
        sample_count=4,
        type_scale=TypeScale(levels=(TypeScaleLevel("Georgia", 11.0, False, 400, "body"),)),
        sections=(SectionCandidate("SUBJECTIVE", "subjective", 4, 0.2, True),),
        static_text=("DOB:", "Example Clinic"),
        column_grid=ColumnGrid(columns=(), gutters=()),
        page_breaks=PageBreakStats(
            page_count_distribution=((1, 4),), max_content_y_fraction=0.9, running_headers=()
        ),
        page_geometry=PageGeometry(612.0, 792.0, 60.0, 60.0, 60.0, 60.0),
        design_tokens=DesignTokens(fill_colors=(), stroke_widths=(), body_font="Georgia"),
        evidence=evidence if evidence is not None else LayoutEvidence(),
    )


def test_the_shipped_template_set_is_the_directory() -> None:
    """One list: a template added without naming it would ship unchecked."""
    assert set(DRAFT_TEMPLATES) == {p.name for p in _TEMPLATE_DIR.iterdir() if p.is_file()}


def test_a_template_placeholder_nobody_fills_is_a_loud_failure() -> None:
    with pytest.raises(KeyError):  # a gap would read as "nothing to say" (5)
        _fill("draft.md", name="acme_soap")


@pytest.mark.parametrize("recognized", [False, True])
def test_no_placeholder_survives_into_an_emitted_draft(recognized: bool, tmp_path: Path) -> None:
    """Read off the emitted BYTES, not off the emitter's argument list."""
    analysis = _analysis(_RECOGNIZED if recognized else None)
    pack_dir = emit_draft_pack(analysis, name="acme_soap", display="ACME", out_dir=tmp_path)

    written = sorted(p.name for p in pack_dir.iterdir() if p.is_file())
    assert "UNPLACED.txt" in written, written
    assert ("OCR_EVIDENCE.md" in written) is recognized, written
    for name in written:
        assert "$" not in (pack_dir / name).read_text(encoding="utf-8"), name


def test_the_draft_template_still_speaks_generic_soaps_contract() -> None:
    """The engine renders a draft only while it uses generic_soap's own loops
    and variables — read off that pack's template, which this change does not
    write. The two absences are named here, so a third one is loud."""
    import re

    def tokens(text: str) -> set[str]:
        return set(re.findall(r"\{%.*?%\}|\{\{.*?\}\}", text, re.S))

    shipped = tokens((_PACKS / "generic_soap" / "template.html").read_text(encoding="utf-8"))
    draft = tokens((_TEMPLATE_DIR / "draft_template.html").read_text(encoding="utf-8"))

    assert shipped - draft == {"{% elif vitals_elsewhere %}", "{{ vitals_elsewhere }}"}
