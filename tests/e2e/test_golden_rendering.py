"""Golden rendering tests — pin what REAL Chromium produces.

Renders ``pf_tebra_v9`` through ``generic_soap`` with the real
:class:`ChromiumRenderer`, comparing page count, geometry and normalized
text byte-for-byte against ``tests/e2e/goldens/*.json`` — plus word
bounding boxes (a few points of slack), since those alone miss a
*spatial* regression. A mismatch is a regression or a deliberate
re-baseline via ``python tools/regen_goldens.py``, never a reflex.
PHI-safe; ``e2e``, SKIPS without Chromium.
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


def test_golden_boxes_cover_every_page(
    golden: dict[str, Any], golden_boxes: dict[str, Any]
) -> None:
    # A spatial baseline that silently stopped covering a chart — or quietly
    # dropped back to page 1 only — would pass forever; pin that the word
    # baseline names the same encounters as the text golden AND the same
    # page count each, and that no page is an empty word list.
    assert sorted(k for k in golden_boxes if k != "_meta") == sorted(
        k for k in golden if k != "_meta"
    )
    for enc_id, pages in golden_boxes.items():
        if enc_id == "_meta":
            continue
        assert len(pages) == golden[enc_id]["pages"], (
            f"{enc_id}: word baseline covers {len(pages)} page(s), "
            f"golden text layer has {golden[enc_id]['pages']}"
        )
        assert all(pages), f"{enc_id}: a page has an empty word baseline"


def test_render_matches_golden_word_boxes(
    golden_boxes: dict[str, Any], rendered_boxes: dict[str, Any]
) -> None:
    """The check the text/geometry golden cannot make: same words, same
    page size, different POSITIONS — a value slid under the neighbouring
    label. Compared with :data:`regen_goldens.BOX_TOLERANCE` points of
    slack, so sub-pixel rounding never fails but a visible move always
    does."""
    for enc_id in sorted(k for k in golden_boxes if k != "_meta"):
        assert enc_id in rendered_boxes, f"{enc_id}: no word boxes were rendered"
        differences = regen_goldens.diff_word_boxes(golden_boxes[enc_id], rendered_boxes[enc_id])
        if differences:
            # Synthetic fixture text, so the words are PHI-safe to print.
            detail = "\n".join(differences)
            pytest.fail(
                f"{enc_id}: layout moved (tolerance {regen_goldens.BOX_TOLERANCE}pt). "
                f"If this change is intentional, run `python tools/regen_goldens.py` "
                f"and review the JSON diff in the PR.\n{detail}"
            )
