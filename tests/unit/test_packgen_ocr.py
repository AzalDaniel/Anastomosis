"""The offline OCR worker: the real binary, the pinned invocation, the refusal.

These tests run the ACTUAL Tesseract CLI against actual pixels wherever one is
installed — a worker that has only ever been mocked has not been tested — and
they prove the absent-binary path by making the executable genuinely
unfindable, not by imagining what would happen.

Everything recognized here is drawn by ``_ocr_pages`` from invented content.
No sample PDF is checked in and nothing is patient-derived.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf", reason="the OCR worker tests need the render extra")

from anastomosis.packgen import ocr  # noqa: E402
from anastomosis.packgen.ocr import (  # noqa: E402
    OCR_OBSERVATION,
    PROVENANCES,
    TESSERACT_BACKEND_ID,
    OcrConfig,
    OcrEngineError,
    OcrToken,
    PageImage,
    TesseractWorker,
    discover_worker,
    find_tesseract,
)

_WORKER = discover_worker()
_NEEDS_ENGINE = pytest.mark.skipif(
    _WORKER is None, reason=f"needs a real offline OCR engine; {ocr.INSTALL_HINT}"
)

PAGE_W, PAGE_H = 612.0, 792.0


def _rendered_page(draw, dpi: int = 216) -> tuple[bytes, PageImage]:  # type: ignore[no-untyped-def]
    """Draw a synthetic page and hand back the PNG plus its exact transform."""
    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    draw(page)
    pixmap = page.get_pixmap(dpi=dpi)
    png = bytes(pixmap.tobytes("png"))
    doc.close()
    return png, PageImage(
        page_index=0,
        region_id="p0r0",
        bytes_sha256=hashlib.sha256(png).hexdigest(),
        width_px=int(pixmap.width),
        height_px=int(pixmap.height),
        dpi_x=float(dpi),
        dpi_y=float(dpi),
        rotation_deg=0.0,
        page_width_pt=PAGE_W,
        page_height_pt=PAGE_H,
        clip_pt=(0.0, 0.0, PAGE_W, PAGE_H),
    )


# --- the engine, for real ---------------------------------------------------


@_NEEDS_ENGINE
def test_the_real_engine_reads_a_synthetic_page_and_places_it() -> None:
    """A genuine Tesseract run on genuine pixels: the words and the geometry.

    The page is drawn at a known position, rendered, and recognized. Both
    halves are asserted — the transcript, and that each word came back inside
    the page and near where it was drawn.
    """
    assert _WORKER is not None

    def draw(page: pymupdf.Page) -> None:
        page.insert_text((60, 90), "SUBJECTIVE", fontsize=13, fontname="hebo")
        page.insert_text((60, 130), "BP 128/82 mmHg", fontsize=11, fontname="helv")

    png, page_image = _rendered_page(draw)
    result = _WORKER.recognize(png, page_image)

    assert [token.text for token in result.tokens] == [
        "SUBJECTIVE",
        "BP",
        "128/82",
        "mmHg",
    ]
    heading = result.tokens[0]
    assert heading.source == OCR_OBSERVATION
    assert heading.confidence is not None and heading.confidence > 50
    assert heading.confidence_kind == "tesseract_conf"
    # Drawn with its baseline at y=90 starting at x=60; recognized within a
    # couple of points of that, and inside the page in both axes.
    assert 58.0 <= heading.bbox_page_pt[0] <= 63.0
    assert 76.0 <= heading.bbox_page_pt[1] <= 92.0
    assert heading.bbox_page_pt[2] <= PAGE_W and heading.bbox_page_pt[3] <= PAGE_H
    assert 11.0 <= heading.size_pt <= 15.0  # a height estimate, not a font size


@_NEEDS_ENGINE
def test_every_result_carries_a_manifest_that_states_it_was_offline() -> None:
    """The manifest is what a pack records; it must name the engine and say no.

    ``allow_network`` is in the schema precisely so this can be asserted rather
    than assumed, and the tessdata source says whether the language data was
    operator-pinned or the engine's own default — never implying a pin it does
    not have.
    """
    assert _WORKER is not None
    manifest = _WORKER.manifest()

    assert manifest["backend_id"] == TESSERACT_BACKEND_ID
    assert manifest["allow_network"] is False
    assert manifest["thread_count"] == 1
    assert manifest["tessdata"] in {"operator-pinned", "engine-default"}
    assert str(manifest["engine_version"]).startswith("tesseract ")
    assert len(str(manifest["config_sha256"])) == 64


@_NEEDS_ENGINE
def test_nothing_the_worker_returns_carries_a_filesystem_path() -> None:
    """hOCR embeds the input filename; none of it may survive into a result.

    Tesseract writes the image path it was given into the hOCR page title. The
    worker therefore renders to a fixed, non-descriptive name in a private
    temporary directory, parses, and keeps none of it — so a sample named after
    a patient can never reach a pack through this door.
    """
    assert _WORKER is not None

    def draw(page: pymupdf.Page) -> None:
        page.insert_text((60, 90), "HEADER", fontsize=12, fontname="hebo")

    png, page_image = _rendered_page(draw)
    result = _WORKER.recognize(png, page_image)

    surfaces = [
        *(token.text for token in result.tokens),
        *(token.line_id for token in result.tokens),
        *(token.block_id for token in result.tokens),
        *result.warnings,
        *(f"{key}={value}" for key, value in _WORKER.manifest().items()),
    ]
    for surface in surfaces:
        assert "/" not in surface and "\\" not in surface, surface
        assert "anast-ocr" not in surface


@_NEEDS_ENGINE
def test_an_oversized_page_image_is_refused_rather_than_recognized() -> None:
    """The pixel cap is enforced before the engine is started, not after.

    A finite, recorded limit is the decision record's resource rule; a page
    that breaches it becomes a review item and never a partial success.
    """
    assert _WORKER is not None
    tiny = TesseractWorker(_WORKER.exe, OcrConfig(max_pixels=1000))

    def draw(page: pymupdf.Page) -> None:
        page.insert_text((60, 90), "TOO BIG", fontsize=12, fontname="hebo")

    png, page_image = _rendered_page(draw)
    with pytest.raises(OcrEngineError, match=r"exceeds the configured cap 1000"):
        tiny.recognize(png, page_image)


# --- the absent binary ------------------------------------------------------


def test_no_engine_on_the_machine_is_a_refusal_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Make the executable genuinely unfindable and watch the door close.

    PATH is pointed at an empty directory and the operator override is cleared,
    so ``shutil.which`` really does fail. The answer is ``None`` — a refusal the
    caller can turn into an actionable message — never an exception out of
    discovery and never a silent skip.
    """
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv(ocr.TESSERACT_EXE_ENV, raising=False)

    assert find_tesseract() is None
    assert discover_worker() is None


def test_a_named_executable_that_does_not_exist_is_not_silently_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator who names a binary gets that binary or nothing.

    Falling back to whatever PATH happens to hold would silently substitute an
    engine for the pinned one, which is the opposite of a pinned worker.
    """
    monkeypatch.setenv(ocr.TESSERACT_EXE_ENV, str(tmp_path / "not-here"))

    assert find_tesseract() is None
    assert discover_worker() is None


def test_the_install_hint_says_what_to_place_and_promises_no_download() -> None:
    """The refusal has to be actionable and has to say nothing is fetched."""
    assert "Tesseract 5" in ocr.INSTALL_HINT
    assert "eng" in ocr.INSTALL_HINT
    assert ocr.TESSERACT_EXE_ENV in ocr.INSTALL_HINT
    assert "Nothing is downloaded" in ocr.INSTALL_HINT


# --- the pinned invocation --------------------------------------------------


def test_the_worker_cannot_be_configured_to_allow_network() -> None:
    """``allow_network`` exists to be recorded as False, not to be flipped."""
    with pytest.raises(ValueError, match=r"offline-only"):
        OcrConfig(allow_network=True)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"dpi": 0}, r"must be positive"),
        ({"max_pixels": 0}, r"must be positive"),
        ({"timeout_seconds": 0}, r"must be positive"),
        ({"thread_count": 4}, r"one page per process"),
    ],
)
def test_an_unbounded_or_fanned_out_configuration_is_refused(
    kwargs: dict[str, int], message: str
) -> None:
    """Every limit is finite and the worker stays one page per process."""
    with pytest.raises(ValueError, match=message):
        OcrConfig(**kwargs)


def test_the_child_environment_is_built_from_nothing() -> None:
    """No inherited proxy, no inherited tessdata, and threads pinned to one.

    The offline promise is enforced by what the child is GIVEN, so the
    environment is asserted key by key rather than trusted.
    """
    config = OcrConfig()
    env = ocr._explicit_env(config, None)

    assert env["OMP_THREAD_LIMIT"] == "1"
    assert "TESSDATA_PREFIX" not in env
    assert not [key for key in env if "PROXY" in key.upper()]
    assert set(env) <= {"OMP_THREAD_LIMIT", "LC_ALL", "SystemRoot", "windir"}


def test_an_operator_pinned_tessdata_directory_is_passed_through() -> None:
    """Naming one is the only way it is set, and then it must actually be set."""
    env = ocr._explicit_env(OcrConfig(), "/opt/tessdata")

    assert env["TESSDATA_PREFIX"] == "/opt/tessdata"


def test_the_configuration_digest_changes_with_the_configuration() -> None:
    """A manifest hash that ignored a knob would certify the wrong invocation."""
    base = OcrConfig()

    assert base.config_sha256 == OcrConfig().config_sha256
    assert base.config_sha256 != OcrConfig(dpi=300).config_sha256
    assert base.config_sha256 != OcrConfig(page_segmentation="6").config_sha256
    assert base.config_sha256 != OcrConfig(min_confidence=10.0).config_sha256


def test_the_pixel_cap_lowers_the_dpi_by_a_stated_rule() -> None:
    """Downsampling is deterministic and recorded, never an implicit resample."""
    generous = OcrConfig()
    assert generous.dpi_for(PAGE_W, PAGE_H) == generous.dpi

    tight = OcrConfig(max_pixels=100_000)
    reduced = tight.dpi_for(PAGE_W, PAGE_H)
    assert 0 < reduced < tight.dpi
    width_px = (PAGE_W / 72.0) * reduced
    height_px = (PAGE_H / 72.0) * reduced
    assert width_px * height_px <= tight.max_pixels


# --- output parsing ---------------------------------------------------------
#
# These reach for the module-private parsers on purpose. The failures they
# cover — a blank TSV row, and the two output streams disagreeing about how
# many words were read — cannot be produced on demand by asking a correct
# engine for them, and the disagreement is exactly the case that must raise
# rather than quietly prefer one stream.

_TSV_HEADER = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
)


def test_blank_and_non_word_tsv_rows_carry_no_observation() -> None:
    """Level-5 rows with text are words; everything else is structure."""
    raw = "\n".join(
        [
            _TSV_HEADER,
            "1\t1\t0\t0\t0\t0\t0\t0\t100\t100\t-1\t",
            "5\t1\t1\t1\t1\t1\t10\t20\t30\t12\t95.5\tHELLO",
            "5\t1\t1\t1\t1\t2\t50\t20\t30\t12\t-1\t   ",
        ]
    )

    rows = ocr._parse_tsv(raw)

    assert [row.text for row in rows] == ["HELLO"]
    assert rows[0].conf == 95.5


def test_hocr_line_heights_are_read_for_every_non_blank_word() -> None:
    """``x_size`` is the only size evidence either stream offers."""
    raw = (
        "<span class='ocr_line' id='line_1' title=\"bbox 0 0 10 10; x_size 36.0\">"
        "<span class='ocrx_word' id='w1' title='bbox 0 0 5 5; x_wconf 96'>ONE</span>"
        "<span class='ocrx_word' id='w2' title='bbox 6 0 9 5; x_wconf 90'> </span>"
        "</span>"
        "<span class='ocr_line' id='line_2' title=\"bbox 0 20 10 30; x_size 24.0\">"
        "<span class='ocrx_word' id='w3' title='bbox 0 20 5 25; x_wconf 88'>TWO</span>"
        "</span>"
    )

    assert ocr._parse_hocr_line_sizes(raw) == [36.0, 24.0]


@_NEEDS_ENGINE
def test_the_two_output_streams_disagreeing_is_a_loud_failure() -> None:
    """TSV and hOCR come from one run; a mismatch is a defect, not a preference.

    Silently trusting whichever stream is longer would mean pairing a word with
    another line's height, which is a wrong box reported as a right one.
    """
    assert _WORKER is not None
    _png, page_image = _rendered_page(
        lambda page: page.insert_text((60, 90), "X", fontsize=12, fontname="hebo")
    )
    rows = ocr._parse_tsv("\n".join([_TSV_HEADER, "5\t1\t1\t1\t1\t1\t10\t20\t30\t12\t95.5\tHELLO"]))

    with pytest.raises(OcrEngineError, match=r"OCR streams disagree: TSV read 1 words"):
        _WORKER._assemble(rows, [], page_image, 0)


# --- the schema -------------------------------------------------------------


def test_a_token_maps_into_page_points_through_its_regions_own_clip() -> None:
    """A clipped region's pixels resolve into the page's own frame, not the
    image's — the transform is the clip origin and the DPI, and nothing else."""
    page_image = PageImage(
        page_index=2,
        region_id="p2r0",
        bytes_sha256="0" * 64,
        width_px=300,
        height_px=150,
        dpi_x=216.0,
        dpi_y=216.0,
        rotation_deg=0.0,
        page_width_pt=PAGE_W,
        page_height_pt=PAGE_H,
        clip_pt=(100.0, 200.0, 200.0, 250.0),
    )

    assert page_image.to_page_pt((0.0, 0.0, 216.0, 108.0)) == (100.0, 200.0, 172.0, 236.0)


def test_the_selection_threshold_selects_and_never_deletes() -> None:
    """Low-scoring tokens stay in the result; only the candidate list shrinks."""
    tokens = tuple(
        OcrToken(
            text=text,
            bbox_px=(0.0, 0.0, 1.0, 1.0),
            bbox_page_pt=(0.0, 0.0, 1.0, 1.0),
            size_pt=10.0,
            line_id="l",
            block_id="b",
            confidence=score,
            confidence_kind="tesseract_conf",
            page_index=0,
            region_id="p0r0",
        )
        for text, score in (("keep", 90.0), ("drop", 5.0))
    )
    result = ocr.OcrPageResult(
        tokens=tokens,
        page_image=PageImage(
            page_index=0,
            region_id="p0r0",
            bytes_sha256="0" * 64,
            width_px=10,
            height_px=10,
            dpi_x=216.0,
            dpi_y=216.0,
            rotation_deg=0.0,
            page_width_pt=PAGE_W,
            page_height_pt=PAGE_H,
            clip_pt=(0.0, 0.0, PAGE_W, PAGE_H),
        ),
        engine_version="tesseract 5.3.4",
        config_sha256="0" * 64,
        tessdata_source="engine-default",
        below_confidence=1,
        warnings=(),
    )

    assert len(result.tokens) == 2
    assert [token.text for token in result.selected(OcrConfig())] == ["keep"]


def test_the_provenance_vocabulary_is_closed() -> None:
    """Three values, and every span in the learner carries exactly one of them."""
    assert PROVENANCES == {
        ocr.NATIVE_TEXT,
        ocr.NATIVE_OR_SYNTHETIC,
        ocr.OCR_OBSERVATION,
    }
