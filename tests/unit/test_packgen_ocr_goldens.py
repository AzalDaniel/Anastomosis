"""The OCR layout-evidence goldens: eight synthetic cases, two metric families.

The decision record names the fixture set a learned style pack has to clear
before a human approves it — short, long, empty, multiline, table, attachment,
pagination and font fallback — and requires that visual similarity be measured
SEPARATELY from semantic fidelity, so neither can waive the other. These tests
are that gate.

Every page is drawn by ``_ocr_pages`` and then rasterized, so the PDF handed to
the worker genuinely has no text objects: this exercises the real Tesseract
binary against real pixels. No sample PDF is checked in; nothing here is
patient-derived.

Regenerate with ``python tools/regen_ocr_goldens.py`` — deliberately, in a
commit that intends the change.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf", reason="the OCR goldens need the render extra")

from _ocr_pages import (  # noqa: E402
    CASES,
    OcrCase,
    case_digest,
    draw_pdf,
    median_center_offset,
    median_iou,
    native_words,
    rasterize,
    semantic_ratio,
)

from anastomosis.packgen.extract import NoExtractableTextError, extract_document  # noqa: E402
from anastomosis.packgen.ocr import (  # noqa: E402
    INSTALL_HINT,
    OCR_OBSERVATION,
    discover_worker,
)

GOLDEN = Path(__file__).resolve().parent / "goldens" / "packgen_ocr_layout.json"

_WORKER = discover_worker()
pytestmark = pytest.mark.skipif(
    _WORKER is None,
    reason=f"the OCR goldens run the real engine; {INSTALL_HINT}",
)


@pytest.fixture(scope="module")
def golden() -> dict[str, object]:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _observe(
    case: OcrCase, tmp_path: Path
) -> tuple[
    list[tuple[str, tuple[float, float, float, float]]],
    list[str],
    list[tuple[str, tuple[float, float, float, float]]],
    str | None,
]:
    """Draw, rasterize, recognize: the native truth and what came back."""
    native = draw_pdf(case, tmp_path / f"{case.key}.pdf")
    raster = rasterize(native, tmp_path / f"{case.key}.raster.pdf")
    truth = native_words(native)
    refusal: str | None = None
    spans = []
    try:
        sample = extract_document(raster, 0, ocr=_WORKER)
        spans = [span for span in sample.spans if span.provenance == OCR_OBSERVATION]
    except NoExtractableTextError:
        refusal = "NoExtractableTextError"
    return truth, [s.text for s in spans], [(s.text, s.bbox) for s in spans], refusal


def test_every_named_fixture_case_has_a_golden(golden: dict[str, object]) -> None:
    """All eight cases the decision record names are present, and only those.

    A case quietly dropped from the fixture pack would take its gate with it.
    """
    cases = golden["cases"]
    assert isinstance(cases, dict)
    assert set(cases) == {case.key for case in CASES}
    tags = {tag for case in cases.values() for tag in case["tags"]}
    assert tags == {
        "short",
        "long",
        "empty",
        "multiline",
        "table",
        "attachment",
        "pagination",
        "font-fallback",
    }


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.key)
def test_fixture_page_still_draws_what_the_golden_scored(
    case: OcrCase, golden: dict[str, object]
) -> None:
    """The golden belongs to THIS page. Edit the page, regenerate the golden.

    Without this, changing a fixture's wording would silently re-point a
    recorded score at a page it never measured.
    """
    recorded = golden["cases"][case.key]  # type: ignore[index]
    assert case_digest(case) == recorded["fixture_digest"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.key)
def test_semantic_fidelity_clears_its_own_threshold(
    case: OcrCase, golden: dict[str, object], tmp_path: Path
) -> None:
    """Did the engine read the words the page was drawn with?

    Reported and gated on its own. A page can score perfectly here and still
    have every word in the wrong place, which is why the visual test exists
    beside it and neither is allowed to stand in for the other.
    """
    recorded = golden["cases"][case.key]  # type: ignore[index]
    floor = golden["thresholds"]["semantic_min_ratio"]  # type: ignore[index]
    truth, observed, _boxes, refusal = _observe(case, tmp_path)

    assert refusal == recorded["refusal"]
    ratio = semantic_ratio([text for text, _ in truth], observed)
    assert ratio >= floor, (
        f"{case.key}: semantic ratio {ratio:.3f} below the {floor} threshold "
        f"(golden recorded {recorded['measured']['semantic_ratio']})"
    )


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.key)
def test_visual_geometry_clears_its_own_thresholds(
    case: OcrCase, golden: dict[str, object], tmp_path: Path
) -> None:
    """Did it put them where they were drawn?

    Two numbers, because the overlap metric has a ceiling near 0.55 by
    construction — a native word box spans the font's ascender-to-descender
    metric while a recognized box hugs the ink — and the centre offset does
    not. The blank page measures neither, and reports so rather than passing by
    default.
    """
    recorded = golden["cases"][case.key]  # type: ignore[index]
    thresholds = golden["thresholds"]
    truth, _observed, boxes, _refusal = _observe(case, tmp_path)
    iou = median_iou(truth, boxes)
    offset = median_center_offset(truth, boxes)

    if recorded["measured"]["median_iou"] is None:
        assert iou is None and offset is None, f"{case.key}: nothing was drawn to measure"
        return
    assert iou is not None and offset is not None
    assert iou >= thresholds["visual_min_median_iou"], (  # type: ignore[index]
        f"{case.key}: median box overlap {iou} below threshold"
    )
    assert offset <= thresholds["visual_max_median_center_offset_pt"], (  # type: ignore[index]
        f"{case.key}: median centre offset {offset}pt above threshold"
    )


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.key)
def test_recorded_words_match_on_the_engine_that_produced_them(
    case: OcrCase, golden: dict[str, object], tmp_path: Path
) -> None:
    """Exact-match the recorded transcript — on the SAME engine version only.

    The decision record refuses to promise byte-identical output across engine
    versions or operating systems, so a different version is compared against
    the threshold tests above and its exact transcript is not asserted. On the
    version that produced the golden, drift is a diff.
    """
    if _WORKER is None or _WORKER.engine_version != golden["engine_version"]:
        pytest.skip(f"golden baseline is {golden['engine_version']}; this host differs")
    recorded = golden["cases"][case.key]  # type: ignore[index]
    _truth, observed, _boxes, _refusal = _observe(case, tmp_path)
    assert observed == recorded["words"]


def test_the_blank_page_is_a_refusal_and_not_an_empty_success(tmp_path: Path) -> None:
    """A rasterized blank page recognizes as nothing, and nothing is refused.

    This is the distinction the whole feature turns on. Asking the engine and
    getting no words is not the same as never asking: the page was observed,
    the observation was empty, and a sample with no text at all still cannot be
    learned from — so it raises rather than emitting a pack with a hole in it.
    """
    blank = next(case for case in CASES if case.key == "empty")
    native = draw_pdf(blank, tmp_path / "blank.pdf")
    raster = rasterize(native, tmp_path / "blank.raster.pdf")

    with pytest.raises(NoExtractableTextError, match=r"sample #0"):
        extract_document(raster, 0, ocr=_WORKER)
