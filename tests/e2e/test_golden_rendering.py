"""Golden rendering tests — pin what REAL Chromium produces.

These tests render the ``pf_tebra_v9`` fixture's six encounters through the
``generic_soap`` pack with the real :class:`ChromiumRenderer`, extract stable
properties with PyMuPDF (page count, page geometry in points, and the full
text layer normalized to single-spaced text), and compare them byte-for-byte
against the committed golden snapshot in
``tests/e2e/goldens/pf_tebra_v9_generic_soap.json``.

Text and geometry alone cannot catch a *spatial* regression: a stylesheet
change that slides a value out from under its label leaves the page count, the
page size, and the extracted words all identical, and ships a chart that reads
the wrong value against the right heading. So page 1 of every golden chart also
pins the word bounding boxes PyMuPDF reports (``page.get_text("words")``)
against ``tests/e2e/goldens/pf_tebra_v9_generic_soap.words.json``, compared with
a few points of slack rather than exact floats.

A mismatch means the rendered output changed. That is either a regression to
fix, or — if the template/pack/engine changed *intentionally* — a deliberate
re-baseline: run ``python tools/regen_goldens.py`` (it rewrites both baselines
from one render) and review the JSON diff in the pull request. Regenerating is
never a reflex to make the test pass; the diff is the whole point.

The ``_meta`` block (Playwright + Chromium versions) is recorded for
diagnosability and is IGNORED by the comparison, so a browser bump alone does
not fail the suite — only a change in rendered text or geometry does.

PHI-safe: the fixture is the repo's synthetic ``feedface-`` PF/Tebra export,
so the normalized text stored and diffed here is entirely synthetic.

Marked ``e2e`` so the unit lanes never collect it; it SKIPS cleanly (no import
crash) when Playwright/Chromium is unavailable.
"""

from __future__ import annotations

import difflib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_TOOLS = Path(__file__).resolve().parents[2] / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

pytestmark = pytest.mark.e2e

# The render extra (playwright + pymupdf) must be importable to even define the
# rendering helpers; skip the whole module cleanly when it is not.
pytest.importorskip("playwright", reason="golden rendering needs the render extra (playwright)")
pytest.importorskip("pymupdf", reason="golden rendering needs the render extra (PyMuPDF)")

import regen_goldens  # noqa: E402 — tool module on sys.path, shared render/extract logic


def _chromium_or_skip() -> None:
    """Skip (do not error) when Chromium cannot launch, so this lane is inert
    on machines without the browser fetched."""
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
    text = regen_goldens.GOLDEN_PATH.read_text(encoding="utf-8")
    return json.loads(text)


@pytest.fixture(scope="module")
def golden_boxes() -> dict[str, Any]:
    text = regen_goldens.WORD_BOXES_PATH.read_text(encoding="utf-8")
    return json.loads(text)


@pytest.fixture(scope="module")
def snapshots() -> tuple[dict[str, Any], dict[str, Any]]:
    """Render the fixture once for the whole module (Chromium launch is slow);
    both baselines come out of that single render."""
    _chromium_or_skip()
    return regen_goldens.render_snapshots()


@pytest.fixture(scope="module")
def rendered(snapshots: tuple[dict[str, Any], dict[str, Any]]) -> dict[str, Any]:
    return snapshots[0]


@pytest.fixture(scope="module")
def rendered_boxes(snapshots: tuple[dict[str, Any], dict[str, Any]]) -> dict[str, Any]:
    return snapshots[1]


def test_golden_has_six_encounter_snapshots(golden: dict[str, Any]) -> None:
    encounters = sorted(k for k in golden if k != "_meta")
    assert len(encounters) == 6, "the pf_tebra_v9 fixture renders exactly six encounters"
    assert "_meta" in golden and golden["_meta"].get("chromium"), "missing chromium provenance"


def test_render_matches_golden_geometry_and_text(
    golden: dict[str, Any], rendered: dict[str, Any]
) -> None:
    expected_keys = sorted(k for k in golden if k != "_meta")
    actual_keys = sorted(k for k in rendered if k != "_meta")
    assert actual_keys == expected_keys, f"encounter set changed: {actual_keys} != {expected_keys}"

    for enc_id in expected_keys:
        want = golden[enc_id]
        got = rendered[enc_id]
        # Geometry is exact (rounded points) — a layout/page-size regression
        # must fail loudly, never round away.
        assert got["pages"] == want["pages"], (
            f"{enc_id}: page count {got['pages']} != {want['pages']}"
        )
        assert got["width"] == want["width"], f"{enc_id}: width {got['width']} != {want['width']}"
        assert got["height"] == want["height"], (
            f"{enc_id}: height {got['height']} != {want['height']}"
        )
        # Text is exact on the normalized layer; show a unified diff on mismatch
        # (synthetic fixture text, so PHI-safe to print).
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


def test_golden_boxes_cover_every_encounter(
    golden: dict[str, Any], golden_boxes: dict[str, Any]
) -> None:
    # A spatial baseline that silently stopped covering a chart would pass
    # forever; pin that both files describe the same encounters.
    assert sorted(k for k in golden_boxes if k != "_meta") == sorted(
        k for k in golden if k != "_meta"
    )
    assert all(golden_boxes[k] for k in golden_boxes if k != "_meta"), "an empty word baseline"


def test_render_matches_golden_word_boxes(
    golden_boxes: dict[str, Any], rendered_boxes: dict[str, Any]
) -> None:
    """Page 1 of every chart lands where it landed when the baseline was cut.

    This is the check the text/geometry golden cannot make: same words, same
    page size, different POSITIONS — a value slid under the neighbouring label.
    Compared with :data:`regen_goldens.BOX_TOLERANCE` points of slack, so
    sub-pixel rounding never fails the suite but a visible move always does.
    """
    for enc_id in sorted(k for k in golden_boxes if k != "_meta"):
        assert enc_id in rendered_boxes, f"{enc_id}: no page-1 word boxes were rendered"
        differences = regen_goldens.diff_word_boxes(golden_boxes[enc_id], rendered_boxes[enc_id])
        if differences:
            # Synthetic fixture text, so the words are PHI-safe to print.
            detail = "\n".join(differences)
            pytest.fail(
                f"{enc_id}: page-1 layout moved (tolerance "
                f"{regen_goldens.BOX_TOLERANCE}pt). If this change is intentional, run "
                f"`python tools/regen_goldens.py` and review the JSON diff in the PR.\n"
                f"{detail}"
            )
