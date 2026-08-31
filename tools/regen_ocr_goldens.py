#!/usr/bin/env python3
"""Regenerate the OCR layout-evidence goldens for the packgen fixture cases.

The eight cases the OCR decision record names — short, long, empty, multiline,
table, attachment, pagination, font fallback — are DRAWN by
``tests/unit/_ocr_pages.py``, rasterized so the resulting PDF has no text
objects at all, and recognized by the real offline worker. This tool records
what came back.

Two families of number, kept apart on purpose and never averaged together:

* **semantic** — the sequence similarity between the words the page was drawn
  with and the words the engine read.
* **visual** — the median box overlap and the median centre offset in points
  between a recognized box and the native word box it corresponds to.

The thresholds are the gate; the measured values sit beside them so a drift is
readable as a number rather than as a pass/fail. Exact word lists are recorded
too, and the test compares them only when the engine version matches the one
that produced the golden — cross-version and cross-OS output is compared
against its own baseline, never promised byte-identical.

Regenerating is a **deliberate act**. Run it when a fixture page or the worker
changes on purpose, and review the JSON diff like any other source change.
Never run it to make a failing test pass: a surprising diff here is the signal
the goldens exist to raise.

Usage::

    python tools/regen_ocr_goldens.py

Exits 2 when no offline OCR engine is installed rather than writing a degraded
golden. Synthetic data only: every page is invented by the fixture module and
nothing patient-derived exists anywhere in this path.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "tests" / "unit"))

GOLDEN = _REPO_ROOT / "tests" / "unit" / "goldens" / "packgen_ocr_layout.json"

#: The gate. Set from measurement, with margin, and explained where it is not
#: the number a reader would expect:
#:
#: * ``semantic_min`` is near-exact because these pages are clean synthetic
#:   renders, not scans of paper. It is a regression gate for THIS fixture
#:   pack, and says nothing about a real scanned chart.
#: * ``visual_min_iou`` is 0.45 against a measured ~0.54 because the metric has
#:   a ceiling around 0.55 by construction: a native word box spans the font's
#:   ascender-to-descender metric and a recognized box hugs the ink, so the two
#:   differ vertically even when the horizontal agreement is exact. The
#:   decision record's 0.90 target assumes boxes drawn to one convention;
#:   raising this number would require changing the metric, not the engine.
#: * ``visual_max_center_offset_pt`` is the metric with no such ceiling, and it
#:   is what actually gates "in the same place": measured at or under 1.4pt.
THRESHOLDS = {
    "semantic_min_ratio": 0.95,
    "visual_min_median_iou": 0.45,
    "visual_max_median_center_offset_pt": 2.5,
}


def _measure(case, worker, workdir: Path) -> dict[str, object]:  # type: ignore[no-untyped-def]
    from _ocr_pages import (
        case_digest,
        draw_pdf,
        median_center_offset,
        median_iou,
        native_words,
        rasterize,
        semantic_ratio,
    )

    from anastomosis.packgen.extract import NoExtractableTextError, extract_document
    from anastomosis.packgen.ocr import OCR_OBSERVATION

    native = draw_pdf(case, workdir / f"{case.key}.pdf")
    raster = rasterize(native, workdir / f"{case.key}.raster.pdf")
    truth = native_words(native)
    refusal: str | None = None
    spans: list[object] = []
    try:
        sample = extract_document(raster, 0, ocr=worker)
        spans = [span for span in sample.spans if span.provenance == OCR_OBSERVATION]
    except NoExtractableTextError:
        refusal = "NoExtractableTextError"
    observed = [span.text for span in spans]  # type: ignore[attr-defined]
    boxes = [(span.text, span.bbox) for span in spans]  # type: ignore[attr-defined]
    return {
        "note": case.note,
        "tags": list(case.tags),
        "fixture_digest": case_digest(case),
        "native_word_count": len(truth),
        "refusal": refusal,
        "words": observed,
        "measured": {
            "semantic_ratio": round(semantic_ratio([text for text, _ in truth], observed), 3),
            "median_iou": median_iou(truth, boxes),
            "median_center_offset_pt": median_center_offset(truth, boxes),
        },
    }


def main() -> int:
    from _ocr_pages import CASES

    from anastomosis.packgen.ocr import INSTALL_HINT, discover_worker

    worker = discover_worker()
    if worker is None:
        print(f"OCR goldens NOT regenerated: {INSTALL_HINT}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="anast-ocr-goldens-") as tmp:
        workdir = Path(tmp)
        cases = {case.key: _measure(case, worker, workdir) for case in CASES}

    payload = {
        "engine_version": worker.engine_version,
        "config_sha256": worker.config.config_sha256,
        "thresholds": THRESHOLDS,
        "cases": cases,
    }
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {GOLDEN.relative_to(_REPO_ROOT)} for {worker.engine_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
