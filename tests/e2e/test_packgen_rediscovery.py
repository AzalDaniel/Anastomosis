"""E2E validation of the packgen layout learner — the honesty loop for M3.

Renders the synthetic ``pf_tebra_v9`` encounters through the ``generic_soap``
pack with REAL Chromium (reusing the golden render path), runs
:func:`anastomosis.packgen.analyze` over the resulting PDFs, and asserts the
learner re-discovers ``generic_soap``'s ground-truth design — the four SOAP
section headings from the pack template, Letter page geometry, the heading-band
fill token, and the serif body face.

PHI-safe: the fixture is the repo's synthetic ``feedface-`` PF/Tebra export, so
every value seen here is synthetic.

Marked ``e2e``; SKIPS cleanly when Playwright/Chromium is unavailable.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

pytest.importorskip("playwright", reason="packgen e2e needs the render extra (playwright)")
pytest.importorskip("fitz", reason="packgen e2e needs the render extra (PyMuPDF)")

_TOOLS = Path(__file__).resolve().parents[2] / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import regen_goldens  # noqa: E402 — shared render path on sys.path
from anastomosis.packgen import analyze, extract_samples  # noqa: E402

# The SOAP section headings generic_soap renders (uppercased by the template's
# `text-transform: uppercase` and captured that way in the PDF text layer).
_SOAP_HEADINGS = {"SUBJECTIVE", "OBJECTIVE", "ASSESSMENT", "PLAN"}
# generic_soap pack.yaml: Letter (612x792pt), 0.6in (=43.2pt) margins,
# heading_fill #f1f1f1, serif body_font.
_LETTER = (612.0, 792.0)
_MARGIN_PT = 43.2


def _chromium_or_skip() -> None:
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as pw:
            pw.chromium.launch().close()
    except Exception as exc:  # browser not fetched / cannot launch
        pytest.skip(
            f"Chromium unavailable ({type(exc).__name__}); run 'playwright install chromium'"
        )


@pytest.fixture(scope="module")
def analysis():  # type: ignore[no-untyped-def]
    """Render every fixture encounter once, then analyze the PDFs."""
    _chromium_or_skip()
    import anastomosis.sources.pf_tebra  # noqa: F401 — registers the adapter
    from anastomosis.reconstruct.chromium import ChromiumRenderer
    from anastomosis.reconstruct.engine import ReconstructionEngine
    from anastomosis.sources import get_source

    pack = regen_goldens._load_pack()
    manifest = pack.manifest
    margins = {
        "top": manifest.page.margin_top,
        "right": manifest.page.margin_right,
        "bottom": manifest.page.margin_bottom,
        "left": manifest.page.margin_left,
    }
    records = list(get_source(regen_goldens.SOURCE_NAME).load(regen_goldens.FIXTURE))
    engine = ReconstructionEngine(
        pack,
        lambda: ChromiumRenderer(page_size=manifest.page.size, margins=margins),
    )
    with tempfile.TemporaryDirectory(prefix="anast-packgen-") as tmp:
        result = engine.run(records, Path(tmp))
        if result.failed:
            pytest.fail(f"rendering failed for {len(result.failed)} encounter(s)")
        paths = [doc.path for doc in sorted(result.documents, key=lambda d: d.encounter_id)]
        assert len(paths) >= 3, "validation needs at least three rendered samples"
        samples = extract_samples(paths)
    return analyze(samples)


def test_rediscovers_soap_section_headings(analysis) -> None:  # type: ignore[no-untyped-def]
    """The four SOAP headings appear as HIGH-confidence section candidates.

    "High confidence" = recurring across a clear majority of the six rendered
    encounters (the simple-note and well-child encounters omit some sections,
    so counts of 4-6 of 6 are expected, never 1).
    """
    by_text = {c.text: c for c in analysis.sections}
    for heading in _SOAP_HEADINGS:
        assert heading in by_text, f"{heading!r} not rediscovered; got {sorted(by_text)}"
        candidate = by_text[heading]
        assert candidate.count >= 4, f"{heading!r} low confidence ({candidate.count}/6)"
        assert candidate.role.startswith("h"), f"{heading!r} role {candidate.role!r} not heading"


def test_rediscovers_letter_page_geometry(analysis) -> None:  # type: ignore[no-untyped-def]
    geom = analysis.page_geometry
    assert (geom.width, geom.height) == _LETTER
    # Left margin is the one margin the short notes genuinely fill to — content
    # starts at the left text margin on every line. (Right/bottom margins are
    # NOT asserted: the synthetic notes are short and do not reach the right
    # edge or page bottom, so content-derived right/bottom margins legitimately
    # exceed the manifest — see the module decisions note in the PR.)
    assert abs(geom.margin_left - _MARGIN_PT) <= 10.0, geom.margin_left


def test_rediscovers_body_font_family_and_eleven_point_level(analysis) -> None:  # type: ignore[no-untyped-def]
    """The serif body face is recovered, and the 11pt prose level the template
    sets on ``body`` is present in the type scale.

    NOTE: the single most-voluminous cluster in these short, table-heavy notes
    is 9.5pt (vitals/payment tables + facility-meta), so the 11pt CSS ``body``
    rule is NOT the highest-weight cluster. The learner therefore reports 9.5pt
    as ``body`` — statistically correct for this corpus. We assert the 11pt
    prose level EXISTS as a regular level rather than that it is the body
    cluster; this honest distinction is documented in the PR.
    """
    scale = analysis.type_scale
    assert scale.body_font is not None
    assert "Serif" in (scale.body_font or ""), scale.body_font
    sizes = {round(lvl.size, 1) for lvl in scale.levels if not lvl.bold}
    assert any(abs(s - 11.0) <= 0.5 for s in sizes), sorted(sizes)


def test_rediscovers_heading_fill_token(analysis) -> None:  # type: ignore[no-untyped-def]
    """The #f1f1f1 heading-band fill (the manual forensic discovery this tool
    automates) is recovered among the fill palette."""
    hexes = {c.hex for c in analysis.design_tokens.fill_colors}
    assert "#f1f1f1" in hexes, sorted(hexes)


def test_summary_lines_are_phi_safe(analysis) -> None:  # type: ignore[no-untyped-def]
    """No per-patient value (fixture patient names) appears in the summary."""
    summary = "\n".join(analysis.summary_lines())
    for token in ("Fixture", "Sample", "Placeholder", "Ada", "Boris", "Cleo"):
        assert token not in summary, f"patient token {token!r} leaked into summary"
    # Static template content is allowed.
    assert "SUBJECTIVE" in summary
