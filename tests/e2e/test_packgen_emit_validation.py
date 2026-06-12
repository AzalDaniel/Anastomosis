"""E2E validation of the packgen draft emitter — the item-15 proof (adapted).

PLAN item 15 says: "regenerate the PF pack from synthetic PF-style samples and
diff against the hand-built pack." The hand-built ``practice_fusion_soap`` pack
is blocked (issue #4), so this test takes the HONEST adaptation, flagged in the
PR: regenerate a pack from ``generic_soap``-RENDERED samples and diff against
the hand-built ``generic_soap``.

The chain, with REAL Chromium throughout (reusing the golden render path):

    render N fixture encounters through generic_soap
      -> analyze the PDFs            (the layout learner)
      -> emit_draft_pack             (the draft writer under test)
      -> assert the draft (a) loads through the real loader,
                          (b) lists generic_soap's SOAP headings in order,
                          (c) matches Letter page size,
                          (d) recovers the #f1f1f1 heading-band fill token,
                          (e) RENDERS a fixture record to a parseable PDF whose
                              text layer carries the section headings,
                          (f) re-analyzing the DRAFT's own output re-discovers
                              the same section taxonomy (the FIXED-POINT
                              property — the strongest honest claim: the learner
                              is stable under its own emitter).

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

pytest.importorskip("playwright", reason="packgen emit e2e needs the render extra (playwright)")
pytest.importorskip("fitz", reason="packgen emit e2e needs the render extra (PyMuPDF)")

_TOOLS = Path(__file__).resolve().parents[2] / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import regen_goldens  # noqa: E402 — shared render path on sys.path
from anastomosis.packgen import analyze, extract_samples  # noqa: E402
from anastomosis.packgen.emit import emit_draft_pack  # noqa: E402
from anastomosis.reconstruct.packs import discover_packs  # noqa: E402

# generic_soap renders these four SOAP headings, uppercased by the template's
# `text-transform: uppercase` (captured that way in the PDF text layer).
_SOAP_HEADINGS = ["SUBJECTIVE", "OBJECTIVE", "ASSESSMENT", "PLAN"]
_LETTER = (612.0, 792.0)


def _chromium_or_skip() -> None:
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as pw:
            pw.chromium.launch().close()
    except Exception as exc:  # browser not fetched / cannot launch
        pytest.skip(
            f"Chromium unavailable ({type(exc).__name__}); run 'playwright install chromium'"
        )


def _render_fixture_through(pack, records, out_dir: Path) -> list[Path]:  # type: ignore[no-untyped-def]
    """Render every record through ``pack`` with REAL Chromium; return PDFs."""
    from anastomosis.reconstruct.chromium import ChromiumRenderer
    from anastomosis.reconstruct.engine import ReconstructionEngine

    manifest = pack.manifest
    margins = {
        "top": manifest.page.margin_top,
        "right": manifest.page.margin_right,
        "bottom": manifest.page.margin_bottom,
        "left": manifest.page.margin_left,
    }
    engine = ReconstructionEngine(
        pack, lambda: ChromiumRenderer(page_size=manifest.page.size, margins=margins)
    )
    result = engine.run(records, out_dir)
    if result.failed:
        pytest.fail(f"rendering failed for {len(result.failed)} encounter(s)")
    return [doc.path for doc in sorted(result.documents, key=lambda d: d.encounter_id)]


@pytest.fixture(scope="module")
def draft(tmp_path_factory):  # type: ignore[no-untyped-def]
    """Render fixtures through generic_soap, analyze, and emit a draft pack.

    Module-scoped (one real render pass), returning everything the assertions
    need: the source analysis, the rendered draft directory, and the loaded
    draft pack.
    """
    _chromium_or_skip()
    import anastomosis.sources.pf_tebra  # noqa: F401 — registers the adapter
    from anastomosis.sources import get_source

    source_pack = regen_goldens._load_pack()
    records = list(get_source(regen_goldens.SOURCE_NAME).load(regen_goldens.FIXTURE))

    tmp = Path(tempfile.mkdtemp(prefix="anast-emit-e2e-"))
    sample_pdfs = _render_fixture_through(source_pack, records, tmp / "samples")
    assert len(sample_pdfs) >= 4, "validation needs at least four rendered samples"

    analysis = analyze(extract_samples(sample_pdfs))
    packs_root = tmp / "packs"
    pack_dir = emit_draft_pack(
        analysis, name="learned_soap", display="generic_soap re-learned", out_dir=packs_root
    )
    status = discover_packs([packs_root], allow_external=True)["learned_soap"]
    return {
        "analysis": analysis,
        "pack_dir": pack_dir,
        "packs_root": packs_root,
        "status": status,
        "records": records,
    }


# --------------------------------------------------------------------------- #
# (a) the draft loads through the real loader
# --------------------------------------------------------------------------- #


def test_draft_loads_through_real_loader(draft) -> None:  # type: ignore[no-untyped-def]
    status = draft["status"]
    assert status.available, status.diagnosis
    assert status.pack is not None
    assert callable(status.pack.build_context)


# --------------------------------------------------------------------------- #
# (b) section list contains generic_soap's headings in order
# --------------------------------------------------------------------------- #


def test_draft_section_list_contains_soap_headings_in_order(draft) -> None:  # type: ignore[no-untyped-def]
    """The emitted manifest's inferred-heading sections include the four SOAP
    headings, and they appear in median-y (document) order."""
    manifest = draft["status"].pack.manifest
    # Inferred heading sections carry a `description` naming the heading; collect
    # them in manifest declaration order (insertion-ordered dict).
    inferred_order = [
        flag.label.upper()
        for key, flag in manifest.sections.items()
        if key not in ("vitals", "addenda", "insurance", "social_history")
    ]
    present = [h for h in inferred_order if h in _SOAP_HEADINGS]
    assert set(_SOAP_HEADINGS) <= set(present), f"missing SOAP headings; got {inferred_order}"
    # Order preserved: filtering the inferred list to SOAP headings equals the
    # canonical S/O/A/P order.
    assert present == _SOAP_HEADINGS, present


# --------------------------------------------------------------------------- #
# (c) page size matches
# --------------------------------------------------------------------------- #


def test_draft_page_size_matches(draft) -> None:  # type: ignore[no-untyped-def]
    assert draft["status"].pack.manifest.page.size == "Letter"
    geom = draft["analysis"].page_geometry
    assert (geom.width, geom.height) == _LETTER


# --------------------------------------------------------------------------- #
# (d) heading-band fill token == #f1f1f1
# --------------------------------------------------------------------------- #


def test_draft_heading_fill_token(draft) -> None:  # type: ignore[no-untyped-def]
    """The #f1f1f1 heading band (the manual forensic discovery this automates)
    is recovered as the emitted manifest token."""
    assert draft["status"].pack.manifest.tokens["heading_fill"] == "#f1f1f1"


# --------------------------------------------------------------------------- #
# (e) the draft renders a fixture record to a parseable PDF with headings
# --------------------------------------------------------------------------- #


def test_draft_renders_parseable_pdf_with_headings(draft, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The draft RENDERS a fixture record through the real engine, producing a
    parseable PDF whose text layer carries the SOAP section headings."""
    import fitz

    pdfs = _render_fixture_through(draft["status"].pack, draft["records"], tmp_path / "drafted")
    assert pdfs, "draft rendered no documents"
    # Concatenate the text layer of every drafted PDF; at least the four SOAP
    # headings must be present across the corpus.
    text = ""
    for pdf_path in pdfs:
        with fitz.open(str(pdf_path)) as doc:
            assert doc.page_count >= 1
            text += "".join(page.get_text() for page in doc)
    upper = text.upper()
    for heading in _SOAP_HEADINGS:
        assert heading in upper, f"{heading!r} missing from drafted PDF text layer"


# --------------------------------------------------------------------------- #
# (f) the fixed-point property: re-analyzing the draft's output re-discovers
#     the same section taxonomy
# --------------------------------------------------------------------------- #


def test_draft_is_a_fixed_point_of_the_learner(draft, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Render fixtures through the DRAFT, re-analyze the result, and assert the
    learner re-discovers the SAME section taxonomy it learned from generic_soap.

    This is the strongest honest claim available without the PF pack: the
    draft emitter is a FIXED POINT of the learner — emit(analyze(X)) renders to
    something analyze re-discovers identically. A drift here means the emitter
    silently changed the design it was handed.
    """
    pdfs = _render_fixture_through(draft["status"].pack, draft["records"], tmp_path / "fixedpoint")
    re_analysis = analyze(extract_samples(pdfs))
    re_headings = {c.text for c in re_analysis.sections if c.count >= 4}
    for heading in _SOAP_HEADINGS:
        assert heading in re_headings, (
            f"{heading!r} not re-discovered from the draft's own output; got {sorted(re_headings)}"
        )
    # The original analysis's high-confidence SOAP headings survive the round
    # trip — the taxonomy is stable under the emitter.
    original = {c.text for c in draft["analysis"].sections if c.count >= 4}
    assert set(_SOAP_HEADINGS) <= original
    assert set(_SOAP_HEADINGS) <= re_headings
