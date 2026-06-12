"""E2E rendering tests for the practice_fusion_soap pack (REAL Chromium).

Renders the synthetic ``pf_tebra_v9`` fixture's six encounters through the
``practice_fusion_soap`` pack with the real :class:`ChromiumRenderer`, then:

* asserts the PDFs are parseable and carry Letter geometry (612x792pt) — the
  forensic page size (GOLD_STANDARD §1);
* compares the stable text/geometry layer byte-for-byte against the committed
  golden ``tests/e2e/goldens/pf_tebra_v9_practice_fusion_soap.json``;
* verifies the #f1f1f1 heading-band fill is actually painted in the PDF (via
  PyMuPDF ``get_drawings()`` fill colors — the §1 forensic-token check) and
  that every PF section heading + the social-history labels survived to the
  text layer.

A text/geometry mismatch is either a regression or a deliberate re-baseline:
run ``python tools/regen_goldens.py`` and review the JSON diff in the PR.

PHI-safe: the fixture is the repo's synthetic ``feedface-`` export, so every
value diffed/printed here is synthetic. Marked ``e2e``; SKIPS cleanly when
Playwright/Chromium is unavailable.
"""

from __future__ import annotations

import difflib
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.e2e

pytest.importorskip("playwright", reason="PF rendering needs the render extra (playwright)")
pytest.importorskip("fitz", reason="PF rendering needs the render extra (PyMuPDF)")

_TOOLS = Path(__file__).resolve().parents[2] / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import regen_goldens  # noqa: E402 — shared render/extract logic on sys.path

PACK_NAME = "practice_fusion_soap"
_GOLDEN = regen_goldens.GOLDENS[PACK_NAME]
_FORENSIC_FILL = (241, 241, 241)  # #f1f1f1 grey heading band (GOLD §1)


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
def golden() -> dict[str, Any]:
    return json.loads(_GOLDEN.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def rendered() -> dict[str, Any]:
    """Render the fixture once for the whole module (Chromium launch is slow)."""
    _chromium_or_skip()
    return regen_goldens.render_goldens(PACK_NAME)


def test_golden_has_six_encounter_snapshots(golden: dict[str, Any]) -> None:
    encounters = sorted(k for k in golden if k != "_meta")
    assert len(encounters) == 6, "the pf_tebra_v9 fixture renders exactly six encounters"
    assert "_meta" in golden and golden["_meta"].get("chromium"), "missing chromium provenance"


def test_letter_geometry(golden: dict[str, Any]) -> None:
    for enc_id, props in golden.items():
        if enc_id == "_meta":
            continue
        # GOLD §1: Letter 612x792pt — forensic page size (NEVER change).
        assert props["width"] == 612, f"{enc_id}: width {props['width']} != 612"
        assert props["height"] == 792, f"{enc_id}: height {props['height']} != 792"
        assert props["pages"] >= 1


def test_render_matches_golden_geometry_and_text(
    golden: dict[str, Any], rendered: dict[str, Any]
) -> None:
    expected_keys = sorted(k for k in golden if k != "_meta")
    actual_keys = sorted(k for k in rendered if k != "_meta")
    assert actual_keys == expected_keys, f"encounter set changed: {actual_keys} != {expected_keys}"

    for enc_id in expected_keys:
        want, got = golden[enc_id], rendered[enc_id]
        assert got["pages"] == want["pages"], (
            f"{enc_id}: page count {got['pages']} != {want['pages']}"
        )
        assert got["width"] == want["width"], f"{enc_id}: width {got['width']} != {want['width']}"
        assert got["height"] == want["height"], (
            f"{enc_id}: height {got['height']} != {want['height']}"
        )
        if got["text"] != want["text"]:
            diff = "\n".join(
                difflib.unified_diff(
                    want["text"].split(" "),
                    got["text"].split(" "),
                    fromfile=f"golden:{enc_id}",
                    tofile=f"rendered:{enc_id}",
                    lineterm="",
                )
            )
            pytest.fail(
                f"{enc_id}: rendered text differs from golden. If this change is "
                f"intentional, run `python tools/regen_goldens.py` and review the "
                f"JSON diff in the PR.\n{diff}"
            )


def test_pf_headings_and_sh_labels_in_text_layer(golden: dict[str, Any]) -> None:
    blob = " ".join(str(props["text"]) for enc_id, props in golden.items() if enc_id != "_meta")
    for heading in (
        "Patient identifying details and demographics",
        "Active insurance",
        "Vitals for this encounter",
        "Diagnoses",
        "Current Medications",
        "Social history",
        "Subjective",
        "Plan",
        "Quality of care",
    ):
        assert heading in blob, f"PF heading missing from PDF text layer: {heading}"
    for label in ("TOBACCO USE", "OCCUPATIONS", "FOOD INSECURITY RISK - HVS"):
        assert label in blob, f"social-history label missing from PDF: {label}"
    # Regression: missing guarantor attributes once printed literal 'None' in
    # the payment cells. No fixture data legitimately contains the bare token.
    assert re.search(r"\bNone\b", blob) is None, "raw 'None' leaked into the PDF text layer"


def test_forensic_heading_band_fill_is_painted(rendered: dict[str, Any]) -> None:
    """Re-render and prove the #f1f1f1 grey heading band is an actual painted
    fill in the PDF (GOLD §1 print-color-adjust — the 2-sprint bug defense).
    Uses PyMuPDF get_drawings() over the live render, not the golden."""
    import fitz

    pdf_path = _render_first_pdf()
    found = False
    with fitz.open(str(pdf_path)) as doc:
        for page in doc:
            for drawing in page.get_drawings():
                fill = drawing.get("fill")
                if fill is None:
                    continue
                rgb = tuple(round(c * 255) for c in fill[:3])
                if all(abs(a - b) <= 2 for a, b in zip(rgb, _FORENSIC_FILL, strict=True)):
                    found = True
                    break
            if found:
                break
    assert found, "the #f1f1f1 heading-band fill was not painted in the PDF"


def _render_first_pdf() -> Path:
    """Render the first fixture encounter to a temp PDF and return its path."""
    import tempfile

    import anastomosis.sources.pf_tebra  # noqa: F401 — registers the adapter
    from anastomosis.reconstruct import discover_packs
    from anastomosis.reconstruct.chromium import ChromiumRenderer
    from anastomosis.reconstruct.engine import ReconstructionEngine
    from anastomosis.sources import get_source

    pack = discover_packs()[PACK_NAME].pack
    assert pack is not None
    manifest = pack.manifest
    margins = {
        "top": manifest.page.margin_top,
        "right": manifest.page.margin_right,
        "bottom": manifest.page.margin_bottom,
        "left": manifest.page.margin_left,
    }
    records = list(get_source("pf-tebra").load(regen_goldens.FIXTURE))
    engine = ReconstructionEngine(
        pack,
        lambda: ChromiumRenderer(page_size=manifest.page.size, margins=margins),
    )
    tmp = Path(tempfile.mkdtemp(prefix="anast-pf-pack-"))
    result = engine.run(records[:1], tmp)
    assert result.documents, "no document rendered"
    return result.documents[0].path
