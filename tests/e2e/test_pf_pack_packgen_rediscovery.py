"""The fixed-point honesty check for the practice_fusion_soap pack (M3.15).

The original PF pack took five manual forensic sprints. This test closes the
loop the predecessor never could: render the synthetic ``pf_tebra_v9``
encounters through the hand-built ``practice_fusion_soap`` pack with REAL
Chromium, then run the deterministic ``packgen`` layout learner over those
PDFs and assert it RE-DISCOVERS the pack's own ground-truth design — the PF
section-heading taxonomy, Letter page geometry, and the #f1f1f1 heading-band
fill that the learner is meant to automate (the manual ``get_drawings()``
discovery).

If the learner can recover the pack's taxonomy and band fill from the pack's
own output, the pack is self-consistent and the learner is honest about a real
PF-shaped document — not just the simpler ``generic_soap`` note.

PHI-safe: the fixture is the repo's synthetic ``feedface-`` export. Marked
``e2e``; SKIPS cleanly when Playwright/Chromium is unavailable.
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

PACK_NAME = "practice_fusion_soap"
_LETTER = (612.0, 792.0)
# PF MAIN section headings that recur on (nearly) every encounter — the four
# SOAP headings plus the always-rendered structural sections (GOLD §4).
_EXPECTED_HEADINGS = {
    "Subjective",
    "Objective",
    "Assessment",
    "Plan",
    "Active insurance",
    "Diagnoses",
    "Social history",
    "Quality of care",
    "Care plan",
}


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
    """Render every fixture encounter through the PF pack, then analyze the PDFs."""
    _chromium_or_skip()
    import anastomosis.sources.pf_tebra  # noqa: F401 — registers the adapter
    from anastomosis.reconstruct.chromium import ChromiumRenderer
    from anastomosis.reconstruct.engine import ReconstructionEngine
    from anastomosis.sources import get_source

    pack = regen_goldens._load_pack(PACK_NAME)
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
    with tempfile.TemporaryDirectory(prefix="anast-pf-packgen-") as tmp:
        result = engine.run(records, Path(tmp))
        if result.failed:
            pytest.fail(f"rendering failed for {len(result.failed)} encounter(s)")
        paths = [doc.path for doc in sorted(result.documents, key=lambda d: d.encounter_id)]
        assert len(paths) >= 3, "validation needs at least three rendered samples"
        samples = extract_samples(paths)
    return analyze(samples)


def test_rediscovers_pf_section_taxonomy(analysis) -> None:  # type: ignore[no-untyped-def]
    """The learner recovers the PF section headings as recurring candidates."""
    by_text = {c.text: c for c in analysis.sections}
    recovered = _EXPECTED_HEADINGS & set(by_text)
    # Require a clear majority of the always-rendered PF headings to surface
    # (some sections sit mid-page and may not cluster as headings on every
    # encounter; SOAP + the hard-break sections reliably do).
    assert len(recovered) >= 6, f"only re-discovered {sorted(recovered)}"
    for heading in ("Subjective", "Plan", "Social history"):
        assert heading in by_text, f"{heading!r} not rediscovered"
        assert by_text[heading].count >= 4, f"{heading!r} low confidence"


def test_rediscovers_letter_page_geometry(analysis) -> None:  # type: ignore[no-untyped-def]
    geom = analysis.page_geometry
    assert (geom.width, geom.height) == _LETTER


def test_rediscovers_heading_band_fill(analysis) -> None:  # type: ignore[no-untyped-def]
    """The #f1f1f1 grey heading band — the manual forensic discovery this tool
    automates — is recovered from the PF pack's own renders."""
    hexes = {c.hex for c in analysis.design_tokens.fill_colors}
    assert "#f1f1f1" in hexes, sorted(hexes)


def test_summary_is_phi_safe(analysis) -> None:  # type: ignore[no-untyped-def]
    """The human-readable analysis summary carries only static template text
    (PF section labels), never synthetic patient values."""
    summary = "\n".join(analysis.summary_lines())
    # Per-patient values (fixture patient names) must NOT appear as "static".
    for patient_token in ("Ada", "Boris", "Cleo", "Fixture", "Sample", "Placeholder"):
        assert patient_token not in summary, f"per-patient value leaked: {patient_token}"
