#!/usr/bin/env python3
"""Regenerate the golden rendering snapshots for the e2e golden tests.

Golden rendering tests pin *exactly* what real Chromium produces for the
``pf_tebra_v9`` fixture rendered through the ``generic_soap`` pack, so a
template or engine regression is caught as a byte-for-byte text/geometry diff
rather than slipping out as a silently-wrong chart.

Two baselines per pack, written together from one render: the text/geometry
golden (``<pack>.json``) and the per-page word-box baseline
(``<pack>.words.json``, one entry per page of every chart). The second exists
because identical text can be laid out wrongly — a CSS regression that slides
a value under the neighbouring label leaves the text layer untouched, and a
chart that reads the wrong value under the right label is exactly the failure
this project refuses to ship. Every page is covered, not just the first: a
multi-page pack (e.g. ``practice_fusion_soap``) can regress on page 4 while
page 1 stays pixel-perfect.

Regenerating the goldens is a **deliberate act**. Run this tool only when a
template, pack, or engine change *intentionally* alters the rendered output;
the resulting JSON diff is then reviewed in the pull request exactly like any
other source change. Never run it to "make the test pass" — a surprising diff
here is the signal the test exists to raise.

Usage::

    python tools/regen_goldens.py            # re-render + rewrite the JSON

The tool always uses the REAL :class:`ChromiumRenderer`; it never substitutes
the fake test renderer. If Playwright / Chromium is unavailable it exits ``2``
with a clear message rather than writing a degraded golden.

Synthetic data only: the fixture is the repo's ``feedface-`` PF/Tebra export.
PHI-safety is therefore satisfied by construction — the normalized text layer
stored in the golden is entirely synthetic fixture content.
"""

from __future__ import annotations

import json
import re
import sys
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from anastomosis.reconstruct import LoadedPack

_REPO_ROOT = Path(__file__).resolve().parents[1]
# Importable when the tool is run from a source checkout without installing.
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "pf_tebra_v9"
_GOLDEN_DIR = _REPO_ROOT / "tests" / "e2e" / "goldens"
GOLDEN_PATH = _GOLDEN_DIR / "pf_tebra_v9_generic_soap.json"
PACK_NAME = "generic_soap"
SOURCE_NAME = "pf-tebra"

# Every pack the golden suite pins, keyed by pack name → its committed golden.
# generic_soap stays the module-level default (backwards-compatible with the
# existing callers/tests that reference GOLDEN_PATH / render_goldens()).
GOLDENS: dict[str, Path] = {
    "generic_soap": GOLDEN_PATH,
    "practice_fusion_soap": _GOLDEN_DIR / "pf_tebra_v9_practice_fusion_soap.json",
}

# The spatial companion to each golden: every page's word bounding boxes,
# keyed by the same pack name. The text golden pins WHICH words were
# rendered; this pins WHERE they landed, on EVERY page, so a CSS regression
# that slides a value under the wrong label fails instead of passing with
# identical text — even when the shift is on page 2+.
WORD_BOXES: dict[str, Path] = {
    pack: path.with_suffix(".words.json") for pack, path in GOLDENS.items()
}
WORD_BOXES_PATH = WORD_BOXES[PACK_NAME]

# Points of slack allowed per coordinate. Chromium's glyph positioning is
# stable to well under a point for the same build, but exact float equality
# would make the baseline hostage to sub-pixel rounding; a few points still
# catches any move a human could see (a 12pt line is ~14pt tall).
BOX_TOLERANCE = 2.0
# Decimals kept per coordinate — enough to stay well inside the tolerance,
# few enough that the committed JSON diffs cleanly.
_BOX_PRECISION = 1
# Mismatch lines a failure report shows before it truncates.
_DIFF_LIMIT = 8

# Exit code the e2e lane / CI reads as "rendering stack unavailable, not a
# golden mismatch" — mirrors ``pytest`` collecting nothing (exit 5) being OK.
EXIT_NO_RENDERER = 2

__all__ = [
    "BOX_TOLERANCE",
    "EXIT_NO_RENDERER",
    "GOLDENS",
    "GOLDEN_PATH",
    "WORD_BOXES",
    "WORD_BOXES_PATH",
    "PdfProps",
    "diff_word_boxes",
    "dump_word_boxes",
    "extract_pdf_props",
    "extract_word_boxes",
    "meta_block",
    "normalize_text",
    "render_goldens",
    "render_snapshots",
]


# The PF pack renders "Current Medications (as of <render-day>)" — a date that
# is TODAY, by design (GOLD §5#9). Baking it into the golden would make the
# snapshot expire the day after it was regenerated, so we neutralize just that
# one render-day token (every other date — DOB, encounter, escript — is real
# data and stays, so a genuine date regression is still caught).
_RENDER_DAY_RE = re.compile(r"\(as of \d{1,2}/\d{1,2}/\d{4}\)")
_RENDER_DAY_PLACEHOLDER = "(as of <render-day>)"


def normalize_text(text: str) -> str:
    """Collapse every run of whitespace to a single space, strip, and neutralize
    the render-day "(as of …)" date.

    Chromium's text layer carries layout-dependent newlines and runs of
    spaces; normalizing makes the golden robust to cosmetic reflow while still
    catching any real change in the *words* that were rendered. The render-day
    date is replaced by a stable token so the golden does not expire daily.
    """
    collapsed = re.sub(r"\s+", " ", text).strip()
    return _RENDER_DAY_RE.sub(_RENDER_DAY_PLACEHOLDER, collapsed)


class PdfProps(dict[str, object]):
    """The stable, comparable properties extracted from one rendered PDF:
    ``pages`` (int), ``width``/``height`` (points, rounded int), ``text``
    (normalized full text layer). A plain dict so it serializes directly."""


def extract_pdf_props(pdf_path: Path) -> PdfProps:
    """Read a rendered PDF and return its stable golden properties.

    Geometry is taken from the first page (the pack renders a single uniform
    page size); the text layer is the concatenation of every page, normalized.
    """
    import pymupdf  # provided by the render extra.

    with pymupdf.open(str(pdf_path)) as doc:
        first = doc[0]
        text = "".join(page.get_text() for page in doc)
        return PdfProps(
            pages=doc.page_count,
            width=round(first.rect.width),
            height=round(first.rect.height),
            text=normalize_text(text),
        )


def extract_word_boxes(pdf_path: Path) -> list[list[list[object]]]:
    """Every page's word bounding boxes: one page per outer element, each an
    ``[x0, y0, x1, y1, word]`` list.

    PyMuPDF's ``page.get_text("words")`` yields one tuple per word in block →
    line → word order, which is stable for a given PDF, so each page's
    baseline is compared positionally. ALL pages are covered, not just the
    header page: a chart's header (patient, DOB, date of service, facility)
    carries the clearest clinical consequence if a value slides under the
    wrong label, but a diagnosis or medication sliding under the wrong
    heading on page 4 is exactly as unsafe.
    """
    import pymupdf  # provided by the render extra.

    with pymupdf.open(str(pdf_path)) as doc:
        return [
            [
                [
                    round(x0, _BOX_PRECISION),
                    round(y0, _BOX_PRECISION),
                    round(x1, _BOX_PRECISION),
                    round(y1, _BOX_PRECISION),
                    word,
                ]
                for x0, y0, x1, y1, word, *_rest in page.get_text("words")
            ]
            for page in doc
        ]


def diff_word_boxes(
    expected: list[list[list[object]]],
    actual: list[list[list[object]]],
    *,
    tolerance: float = BOX_TOLERANCE,
    limit: int = _DIFF_LIMIT,
) -> list[str]:
    """Human-readable mismatch lines; empty when every page's layout agrees.

    Names the page, the word, and both boxes for every disagreement, because
    "the layout changed" is not actionable — "page 3: ``DOB`` moved 31pt
    down" is. A page-count change is reported first, then each page's
    positional comparison walks its common word prefix so the first
    divergence is visible rather than a wall of shifted rows.
    """
    lines: list[str] = []
    if len(expected) != len(actual):
        lines.append(f"page count {len(actual)} != {len(expected)}")
    for page_index in range(min(len(expected), len(actual))):
        want_page, got_page = expected[page_index], actual[page_index]
        if len(want_page) != len(got_page):
            lines.append(f"page {page_index}: word count {len(got_page)} != {len(want_page)}")
        for word_index in range(min(len(want_page), len(got_page))):
            want, got = want_page[word_index], got_page[word_index]
            if want[4] != got[4]:
                lines.append(f"page {page_index} word {word_index}: {got[4]!r} != {want[4]!r}")
            elif any(
                abs(float(g) - float(w)) > tolerance  # type: ignore[arg-type]
                for w, g in zip(want[:4], got[:4], strict=True)
            ):
                lines.append(
                    f"page {page_index} word {word_index} {want[4]!r} moved: "
                    f"expected {tuple(want[:4])} got {tuple(got[:4])}"
                )
            if len(lines) >= limit:
                lines.append("… (further differences not listed)")
                return lines
    return lines


def _dump_pages(pages: list[object]) -> str:
    """Render one encounter's per-page word boxes as ``[page, page, ...]``
    with one box per line inside each page — see :func:`dump_word_boxes`."""
    if not pages:
        return "[]"
    page_blocks: list[str] = []
    for page in pages:
        assert isinstance(page, list), f"expected a page (list of boxes), got {type(page).__name__}"
        if not page:
            page_blocks.append("    []")
            continue
        rows = ",\n".join(f"      {json.dumps(box)}" for box in page)
        page_blocks.append(f"    [\n{rows}\n    ]")
    return "[\n" + ",\n".join(page_blocks) + "\n  ]"


def dump_word_boxes(word_boxes: dict[str, object]) -> str:
    """Serialize a word-box baseline with ONE word per line, grouped by page.

    ``json.dumps(indent=2)`` spreads every box over seven lines, which turns a
    multi-page baseline into tens of thousands of unreviewable lines — and an
    unreviewable baseline is one that gets re-generated instead of read. Each
    ``[x0, y0, x1, y1, word]`` is emitted compactly, so one word that moved is
    one line that changed. Keys are sorted, exactly like the text golden.
    """
    chunks: list[str] = []
    for key in sorted(word_boxes):
        value = word_boxes[key]
        if isinstance(value, list):
            chunks.append(f"  {json.dumps(key)}: {_dump_pages(value)}")
        else:
            chunks.append(f"  {json.dumps(key)}: {json.dumps(value, sort_keys=True)}")
    return "{\n" + ",\n".join(chunks) + "\n}\n"


def meta_block() -> dict[str, str]:
    """Chromium-version provenance for the golden, so a future mismatch is
    diagnosable. The comparison in the test IGNORES this block."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            chromium_version = browser.version
        finally:
            browser.close()
    return {
        "playwright": metadata.version("playwright"),
        "chromium": chromium_version,
    }


def _load_pack(pack_name: str = PACK_NAME) -> LoadedPack:
    from anastomosis.reconstruct import discover_packs

    status = discover_packs().get(pack_name)
    if status is None or status.pack is None:
        diagnosis = status.diagnosis if status else "pack not discovered"
        raise RuntimeError(f"pack {pack_name!r} unavailable: {diagnosis}")
    return status.pack


def render_goldens(pack_name: str = PACK_NAME) -> dict[str, object]:
    """The text/geometry golden alone — see :func:`render_snapshots`."""
    return render_snapshots(pack_name)[0]


def render_snapshots(pack_name: str = PACK_NAME) -> tuple[dict[str, object], dict[str, object]]:
    """Render every fixture encounter with the REAL Chromium renderer through
    ``pack_name`` and return BOTH committed baselines from that one pass:

    * the golden mapping ``{"_meta": {...}, "<encounter_id>": {pages, width,
      height, text}, ...}``;
    * the word-box baseline ``{"_meta": {...}, "<encounter_id>": [[[x0, y0,
      x1, y1, word], ...], ...], ...}`` — one page per outer element, for
      EVERY page of each chart.

    One render feeds both, because launching Chromium twice to snapshot the
    same PDFs would double the slowest part of the e2e lane — and could
    snapshot two different renders.

    Mirrors the real pipeline wiring (``cli._run_pipeline``): pack page
    geometry → renderer; manifest section defaults → engine.
    """
    import anastomosis.sources.pf_tebra  # noqa: F401 — registers the adapter
    from anastomosis.reconstruct.chromium import ChromiumRenderer
    from anastomosis.reconstruct.engine import ReconstructionEngine
    from anastomosis.sources import get_source

    pack = _load_pack(pack_name)
    manifest = pack.manifest
    margins = {
        "top": manifest.page.margin_top,
        "right": manifest.page.margin_right,
        "bottom": manifest.page.margin_bottom,
        "left": manifest.page.margin_left,
    }
    records = list(get_source(SOURCE_NAME).load(FIXTURE))
    engine = ReconstructionEngine(
        pack,
        lambda: ChromiumRenderer(page_size=manifest.page.size, margins=margins),
    )
    import tempfile

    with tempfile.TemporaryDirectory(prefix="anast-goldens-") as tmp:
        out_dir = Path(tmp)
        result = engine.run(records, out_dir)
        if result.failed:
            raise RuntimeError(f"rendering failed for {len(result.failed)} encounter(s)")
        # Map encounter id -> rendered PDF path via the engine's RenderedDoc list.
        props: dict[str, object] = {}
        boxes: dict[str, object] = {}
        for doc in sorted(result.documents, key=lambda d: d.encounter_id):
            props[doc.encounter_id] = dict(extract_pdf_props(doc.path))
            boxes[doc.encounter_id] = extract_word_boxes(doc.path)

    meta = meta_block()  # one Chromium probe, shared by both baselines
    golden: dict[str, object] = {"_meta": meta}
    golden.update(props)
    word_boxes: dict[str, object] = {"_meta": meta}
    word_boxes.update(boxes)
    return golden, word_boxes


def _renderer_available() -> str | None:
    """Return ``None`` if the real Chromium renderer can launch, else a reason
    string. Never substitutes the fake renderer (the whole point of a golden)."""
    try:
        import pymupdf  # noqa: F401
    except ImportError:
        return "PyMuPDF missing: install 'anastomosis[render]'"
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "Playwright missing: install 'anastomosis[render]'"
    try:
        with sync_playwright() as pw:
            pw.chromium.launch().close()
    except Exception as exc:  # browser not fetched / cannot launch
        return f"Chromium unavailable ({type(exc).__name__}): run 'playwright install chromium'"
    return None


def main() -> int:
    reason = _renderer_available()
    if reason is not None:
        print(f"regen_goldens: cannot regenerate — {reason}", file=sys.stderr)
        return EXIT_NO_RENDERER
    # Regenerate every registered pack's golden (generic_soap + practice_fusion_soap),
    # text/geometry and every-page word boxes together — a Chromium bump
    # re-baselines both from the same render, never one without the other.
    for pack_name, golden_path in GOLDENS.items():
        golden, word_boxes = render_snapshots(pack_name)
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        # Deterministic key order (sort_keys) so the committed diff is reviewable;
        # trailing newline so the file is POSIX-clean.
        golden_path.write_text(
            json.dumps(golden, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        boxes_path = WORD_BOXES[pack_name]
        boxes_path.write_text(dump_word_boxes(word_boxes), encoding="utf-8")
        encounters = [k for k in golden if k != "_meta"]
        print(
            f"regen_goldens: wrote {len(encounters)} encounter snapshot(s) for "
            f"{pack_name!r} to {golden_path.relative_to(_REPO_ROOT)} + "
            f"{boxes_path.relative_to(_REPO_ROOT)} "
            f"(chromium {golden['_meta']['chromium']}, playwright {golden['_meta']['playwright']})"  # type: ignore[index]
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
