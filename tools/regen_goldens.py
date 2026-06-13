#!/usr/bin/env python3
"""Regenerate the golden rendering snapshots for the e2e golden tests.

Golden rendering tests pin *exactly* what real Chromium produces for the
``pf_tebra_v9`` fixture rendered through the ``generic_soap`` pack, so a
template or engine regression is caught as a byte-for-byte text/geometry diff
rather than slipping out as a silently-wrong chart.

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

# Exit code the e2e lane / CI reads as "rendering stack unavailable, not a
# golden mismatch" — mirrors ``pytest`` collecting nothing (exit 5) being OK.
EXIT_NO_RENDERER = 2

__all__ = [
    "EXIT_NO_RENDERER",
    "GOLDENS",
    "GOLDEN_PATH",
    "PdfProps",
    "extract_pdf_props",
    "meta_block",
    "normalize_text",
    "render_goldens",
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
    import fitz  # PyMuPDF — provided by the render extra.

    with fitz.open(str(pdf_path)) as doc:
        first = doc[0]
        text = "".join(page.get_text() for page in doc)
        return PdfProps(
            pages=doc.page_count,
            width=round(first.rect.width),
            height=round(first.rect.height),
            text=normalize_text(text),
        )


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
    """Render every fixture encounter with the REAL Chromium renderer through
    ``pack_name`` and return the golden mapping
    ``{"_meta": {...}, "<encounter_id>": {pages, width, height, text}, ...}``.

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
        for doc in sorted(result.documents, key=lambda d: d.encounter_id):
            props[doc.encounter_id] = dict(extract_pdf_props(doc.path))

    golden: dict[str, object] = {"_meta": meta_block()}
    golden.update(props)
    return golden


def _renderer_available() -> str | None:
    """Return ``None`` if the real Chromium renderer can launch, else a reason
    string. Never substitutes the fake renderer (the whole point of a golden)."""
    try:
        import fitz  # noqa: F401
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
    # Regenerate every registered pack's golden (generic_soap + practice_fusion_soap).
    for pack_name, golden_path in GOLDENS.items():
        golden = render_goldens(pack_name)
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        # Deterministic key order (sort_keys) so the committed diff is reviewable;
        # trailing newline so the file is POSIX-clean.
        golden_path.write_text(
            json.dumps(golden, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        encounters = [k for k in golden if k != "_meta"]
        print(
            f"regen_goldens: wrote {len(encounters)} encounter snapshot(s) for "
            f"{pack_name!r} to {golden_path.relative_to(_REPO_ROOT)} "
            f"(chromium {golden['_meta']['chromium']}, playwright {golden['_meta']['playwright']})"  # type: ignore[index]
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
