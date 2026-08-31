"""Native and OCR evidence, kept apart all the way to the emitted pack.

The measured fact behind this: all 53 sample PDFs the product has been shown —
802 pages — carried zero natively extractable words, and 52 of the 53 were
raster documents. The learner refused every one of them. These tests walk the
capability half: what each page IS, what recognizing it produces, what happens
where the two streams describe the same pixels, and what the draft pack says
about all of it afterwards.

Every PDF here is drawn by the test from invented content and rasterized in
place. Nothing is checked in and nothing is patient-derived.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf", reason="the layout learner needs the render extra")

from anastomosis.core.packinit import PackInitCommand, run_pack_init  # noqa: E402
from anastomosis.packgen import analyze  # noqa: E402
from anastomosis.packgen.emit import OCR_EVIDENCE_NAME, UNPLACED_NAME  # noqa: E402
from anastomosis.packgen.evidence import (  # noqa: E402
    AMBIGUOUS,
    CONFLICT_DISAGREEMENT,
    CONFLICT_DUPLICATE,
    EMPTY,
    IMAGE_ONLY,
    MIXED,
    NATIVE_ONLY,
    LayoutEvidence,
    OcrRegionError,
    merge_evidence,
    raster_regions,
)
from anastomosis.packgen.extract import (  # noqa: E402
    OCR_SPAN_FONT,
    OcrRequiredError,
    extract_document,
)
from anastomosis.packgen.infer import OCR_EVIDENCE_CAVEAT  # noqa: E402
from anastomosis.packgen.ocr import (  # noqa: E402
    INSTALL_HINT,
    NATIVE_OR_SYNTHETIC,
    NATIVE_TEXT,
    OCR_OBSERVATION,
    discover_worker,
)

_WORKER = discover_worker()
_NEEDS_ENGINE = pytest.mark.skipif(
    _WORKER is None, reason=f"needs a real offline OCR engine; {INSTALL_HINT}"
)

PAGE_W, PAGE_H = 612.0, 792.0

#: Four invented patients. Distinct values everywhere, so nothing patient-like
#: can recur across samples and be mistaken for the form's own wording.
_PATIENTS = (
    ("Synthia Example", "Hypertension follow up"),
    ("Maxwell Sample", "Diabetes review"),
    ("Cleo Placeholder", "Well child visit"),
    ("Dale Specimen", "Annual physical"),
)


# --------------------------------------------------------------------------- #
# Page builders
# --------------------------------------------------------------------------- #


def _note_page(page: pymupdf.Page, name: str, complaint: str) -> None:
    page.insert_text((60, 90), "SUBJECTIVE", fontsize=13, fontname="hebo")
    page.insert_text((60, 120), f"Patient {name} seen today.", fontsize=11, fontname="helv")
    page.insert_text((60, 150), "OBJECTIVE", fontsize=13, fontname="hebo")
    page.insert_text((60, 180), complaint, fontsize=11, fontname="helv")


def _pixmap_of(draw, clip=None, dpi: int = 200) -> pymupdf.Pixmap:  # type: ignore[no-untyped-def]
    """Render a freshly drawn page (or part of one) to pixels."""
    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    draw(page)
    pixmap = page.get_pixmap(dpi=dpi) if clip is None else page.get_pixmap(clip=clip, dpi=dpi)
    doc.close()
    return pixmap


def _write(path: Path, build) -> Path:  # type: ignore[no-untyped-def]
    doc = pymupdf.open()
    build(doc)
    doc.save(str(path))
    doc.close()
    return path


def _raster_note(path: Path, name: str, complaint: str) -> Path:
    """A whole note flattened to pixels — the 52-of-53 case."""
    pixmap = _pixmap_of(lambda page: _note_page(page, name, complaint))

    def build(doc: pymupdf.Document) -> None:
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_image(pymupdf.Rect(0, 0, PAGE_W, PAGE_H), pixmap=pixmap)

    return _write(path, build)


def _raster_samples(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for index, (name, complaint) in enumerate(_PATIENTS):
        _raster_note(directory / f"sample{index}.pdf", name, complaint)
    return directory


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #


def test_a_page_with_only_text_objects_is_native_only(tmp_path: Path) -> None:
    def build(doc: pymupdf.Document) -> None:
        _note_page(doc.new_page(width=PAGE_W, height=PAGE_H), "Synthia Example", "Cough")

    path = _write(tmp_path / "native.pdf", build)

    sample = extract_document(path, 0, ocr=_WORKER)

    page = sample.evidence.pages[0]
    assert page.classification == NATIVE_ONLY
    assert page.ocr_attempted is False
    assert {span.provenance for span in sample.spans} == {NATIVE_TEXT}
    assert sample.evidence.review_required is False


def test_a_page_with_neither_text_nor_raster_is_empty_and_still_refused(
    tmp_path: Path,
) -> None:
    """``empty`` is its own class, and a document of them still cannot be learned.

    The classification says what the page WAS; the refusal says what the batch
    can be used for. Conflating the two is how a blank page becomes a pack.
    """
    path = _write(tmp_path / "blank.pdf", lambda doc: doc.new_page(width=PAGE_W, height=PAGE_H))

    with pytest.raises(Exception) as excinfo:
        extract_document(path, 3, ocr=_WORKER)
    assert type(excinfo.value).__name__ == "NoExtractableTextError"


@_NEEDS_ENGINE
def test_a_page_that_is_entirely_pixels_is_image_only_and_gets_recognized(
    tmp_path: Path,
) -> None:
    """The whole point: the page the learner used to refuse now yields signal.

    Every span it yields is marked as an observation and carries the engine's
    score, so nothing downstream can read it as text that was actually read.
    """
    path = _raster_note(tmp_path / "raster.pdf", "Synthia Example", "Hypertension follow up")

    sample = extract_document(path, 0, ocr=_WORKER)

    page = sample.evidence.pages[0]
    assert page.classification == IMAGE_ONLY
    assert page.ocr_attempted is True
    assert page.ocr_accepted_count > 0
    assert {span.provenance for span in sample.spans} == {OCR_OBSERVATION}
    assert all(span.font == OCR_SPAN_FONT for span in sample.spans)
    assert all(span.confidence is not None for span in sample.spans)
    assert "SUBJECTIVE" in [span.text for span in sample.spans]
    assert sample.evidence.review_required is True


@_NEEDS_ENGINE
def test_a_large_raster_with_a_small_native_overlay_is_mixed(tmp_path: Path) -> None:
    """The case the decision record calls out by name.

    The native header is BESIDE the scan, not on it, so the page is ``mixed``:
    the header stays native evidence, the raster body becomes an observation,
    and neither is rewritten as the other.
    """
    body = _pixmap_of(
        lambda page: (
            page.insert_text((60, 300), "ASSESSMENT AND PLAN", fontsize=13, fontname="hebo"),
            page.insert_text((60, 330), "Continue current therapy.", fontsize=11, fontname="helv"),
        ),
        clip=(50, 280, 560, 360),
    )

    def build(doc: pymupdf.Document) -> None:
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_text((60, 90), "ACME CLINIC PROGRESS NOTE", fontsize=14, fontname="hebo")
        page.insert_image(pymupdf.Rect(50, 280, 560, 360), pixmap=body)

    sample = extract_document(_write(tmp_path / "mixed.pdf", build), 1, ocr=_WORKER)

    page = sample.evidence.pages[0]
    assert page.classification == MIXED
    native = [span for span in sample.spans if span.provenance == NATIVE_TEXT]
    recognized = [span for span in sample.spans if span.provenance == OCR_OBSERVATION]
    assert [span.text for span in native] == ["ACME CLINIC PROGRESS NOTE"]
    assert "ASSESSMENT" in [span.text for span in recognized]
    # The native header sits outside the raster, so nothing recognized it twice.
    assert page.duplicate_count == 0
    assert page.disagreement_count == 0


@_NEEDS_ENGINE
def test_a_text_layer_floating_on_a_page_covering_scan_is_never_called_native(
    tmp_path: Path,
) -> None:
    """A searchable scan's layer may be somebody else's OCR. Extraction
    succeeding is not evidence that its text is right, so the page is
    ``ambiguous`` and its spans are demoted to ``native_or_synthetic``."""
    scan = _pixmap_of(
        lambda page: (
            page.insert_text((60, 90), "SCANNED FORM HEADER", fontsize=13, fontname="hebo"),
            page.insert_text((60, 130), "Signature on file", fontsize=11, fontname="helv"),
        )
    )

    def build(doc: pymupdf.Document) -> None:
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_image(pymupdf.Rect(0, 0, PAGE_W, PAGE_H), pixmap=scan)
        page.insert_text((60, 90), "SCANNED FORM HEADER", fontsize=13, fontname="hebo")

    sample = extract_document(_write(tmp_path / "ambiguous.pdf", build), 2, ocr=_WORKER)

    page = sample.evidence.pages[0]
    assert page.classification == AMBIGUOUS
    layer = [span for span in sample.spans if span.provenance == NATIVE_OR_SYNTHETIC]
    assert [span.text for span in layer] == ["SCANNED FORM HEADER"]
    assert NATIVE_TEXT not in {span.provenance for span in sample.spans}


# --------------------------------------------------------------------------- #
# Holding conflicts instead of resolving them
# --------------------------------------------------------------------------- #


@_NEEDS_ENGINE
def test_a_recognized_word_over_matching_native_text_is_a_counted_duplicate(
    tmp_path: Path,
) -> None:
    """The native object is the better evidence for the same pixels — so the
    token is not promoted. It is COUNTED, because a dropped observation with no
    number beside it is the silent loss this codebase refuses."""
    scan = _pixmap_of(
        lambda page: page.insert_text((60, 90), "PROGRESS NOTE", fontsize=13, fontname="hebo")
    )

    def build(doc: pymupdf.Document) -> None:
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_image(pymupdf.Rect(0, 0, PAGE_W, PAGE_H), pixmap=scan)
        page.insert_text((60, 90), "PROGRESS NOTE", fontsize=13, fontname="hebo")

    sample = extract_document(_write(tmp_path / "dup.pdf", build), 0, ocr=_WORKER)

    evidence = sample.evidence
    assert evidence.duplicate_count == 2  # "PROGRESS" and "NOTE"
    assert evidence.disagreement_count == 0
    assert {conflict.kind for conflict in evidence.conflicts} == {CONFLICT_DUPLICATE}
    recognized = [span.text for span in sample.spans if span.provenance == OCR_OBSERVATION]
    assert "PROGRESS" not in recognized


def _scan_with_wrong_hidden_layer(path: Path) -> Path:
    """A scan whose invisible text layer disagrees with its own pixels.

    The decision record's fifth mixed-page fixture: a searchable PDF whose text
    layer is somebody else's OCR, and wrong. The layer is drawn with
    ``render_mode=3`` (invisible), so the pixels carry only ``ADDENDUM`` while
    the extractable text says ``AMENDMENT``.
    """
    scan = _pixmap_of(
        lambda page: page.insert_text((60, 90), "ADDENDUM", fontsize=13, fontname="hebo")
    )

    def build(doc: pymupdf.Document) -> None:
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_image(pymupdf.Rect(0, 0, PAGE_W, PAGE_H), pixmap=scan)
        page.insert_text((60, 90), "AMENDMENT", fontsize=13, fontname="hebo", render_mode=3)

    return _write(path, build)


@_NEEDS_ENGINE
def test_a_recognized_word_over_different_native_text_keeps_both_and_holds(
    tmp_path: Path,
) -> None:
    """Never silently choose one value when the two streams disagree.

    Both survive as spans, the disagreement is counted, and the page is held
    for review. Nothing here decides which one is right — that is a person's
    job, against the original image.
    """
    sample = extract_document(_scan_with_wrong_hidden_layer(tmp_path / "clash.pdf"), 0, ocr=_WORKER)

    evidence = sample.evidence
    assert evidence.disagreement_count == 1
    assert {conflict.kind for conflict in evidence.conflicts} == {CONFLICT_DISAGREEMENT}
    texts = {span.text for span in sample.spans}
    assert {"AMENDMENT", "ADDENDUM"} <= texts
    assert evidence.review_required is True


@_NEEDS_ENGINE
def test_a_conflict_record_carries_geometry_and_never_a_value(tmp_path: Path) -> None:
    """A disagreement is by construction about a value. The record says where
    to look, not what was said."""
    sample = extract_document(_scan_with_wrong_hidden_layer(tmp_path / "clash.pdf"), 0, ocr=_WORKER)

    conflict = sample.evidence.conflicts[0]
    fields = set(vars(conflict))
    assert "text" not in fields and "ocr_text" not in fields and "native_text" not in fields
    assert conflict.page_index == 0
    assert conflict.ocr_confidence is not None


# --------------------------------------------------------------------------- #
# Fail-closed geometry
# --------------------------------------------------------------------------- #


@_NEEDS_ENGINE
def test_a_rotated_page_is_refused_rather_than_recognized_blind(tmp_path: Path) -> None:
    """A transform nobody can name is not evidence; the page fails closed."""
    scan = _pixmap_of(
        lambda page: page.insert_text((60, 90), "ROTATED", fontsize=13, fontname="hebo")
    )

    def build(doc: pymupdf.Document) -> None:
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_image(pymupdf.Rect(0, 0, PAGE_W, PAGE_H), pixmap=scan)
        page.set_rotation(90)

    with pytest.raises(OcrRegionError, match=r"page #0 is rotated"):
        extract_document(_write(tmp_path / "rotated.pdf", build), 0, ocr=_WORKER)


def test_overlapping_raster_images_become_one_region(tmp_path: Path) -> None:
    """Two halves of a split scan are one thing to recognize; recognizing them
    twice would manufacture duplicates that no reviewer could resolve."""
    # A full-page render, so the placement rects below can share its aspect
    # ratio exactly and PyMuPDF paints them where they are put — a letterboxed
    # image would land somewhere other than the rect and prove nothing.
    tile = _pixmap_of(lambda page: page.insert_text((60, 90), "TILE", fontsize=12, fontname="hebo"))
    aspect = PAGE_H / PAGE_W
    path = tmp_path / "tiles.pdf"

    def placed(x: float, y: float, width: float) -> pymupdf.Rect:
        return pymupdf.Rect(x, y, x + width, y + width * aspect)

    def build(doc: pymupdf.Document) -> None:
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_image(placed(60, 100, 150), pixmap=tile)
        page.insert_image(placed(150, 200, 150), pixmap=tile)  # overlaps the first
        page.insert_image(placed(60, 500, 100), pixmap=tile)  # on its own

    _write(path, build)
    doc = pymupdf.open(str(path))
    regions = raster_regions(doc[0], 0, (PAGE_W, PAGE_H))
    doc.close()

    assert len(regions) == 2
    merged = regions[0]
    assert merged[0] == pytest.approx(60.0, abs=1.0)
    assert merged[1] == pytest.approx(100.0, abs=1.0)
    assert merged[2] == pytest.approx(300.0, abs=1.0)


# --------------------------------------------------------------------------- #
# The ledger
# --------------------------------------------------------------------------- #


def test_an_all_native_batch_records_nothing_to_review() -> None:
    """The default ledger is the honest one for a batch nobody recognized."""
    evidence = LayoutEvidence()

    assert evidence.review_required is False
    assert evidence.ocr_token_count == 0
    assert evidence.class_counts == dict.fromkeys(
        (NATIVE_ONLY, MIXED, IMAGE_ONLY, AMBIGUOUS, EMPTY), 0
    )


@_NEEDS_ENGINE
def test_the_batch_ledger_is_the_sum_of_its_samples(tmp_path: Path) -> None:
    """Merging keeps every page record and every conflict — nothing collapses."""
    samples = [
        extract_document(_raster_note(tmp_path / f"s{i}.pdf", name, complaint), i, ocr=_WORKER)
        for i, (name, complaint) in enumerate(_PATIENTS[:2])
    ]

    merged = merge_evidence([sample.evidence for sample in samples])

    assert len(merged.pages) == 2
    assert merged.class_counts[IMAGE_ONLY] == 2
    assert merged.ocr_token_count == sum(s.evidence.ocr_token_count for s in samples)
    assert merged.ocr_manifest == samples[0].evidence.ocr_manifest
    assert merged.review_required is True


# --------------------------------------------------------------------------- #
# The refusal, and its other half
# --------------------------------------------------------------------------- #


def test_with_no_engine_a_raster_sample_refuses_and_says_what_to_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fail-closed half, unchanged — now with a message that is actionable.

    PATH is pointed at an empty directory so the binary is genuinely
    unfindable. The refusal names the install, and promises no download.
    """
    monkeypatch.setenv("PATH", str(tmp_path / "nothing-here"))
    monkeypatch.delenv("ANAST_OCR_TESSERACT", raising=False)
    path = _raster_note(tmp_path / "raster.pdf", "Synthia Example", "Cough")

    with pytest.raises(OcrRequiredError, match=r"sample #0 page #0 requires OCR") as excinfo:
        extract_document(path, 0)

    assert "Nothing is downloaded" in str(excinfo.value)
    assert path.name not in str(excinfo.value)


def test_pack_init_without_an_engine_writes_nothing_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No engine, no partial pack, and an exception TYPE for the frontend."""
    monkeypatch.setenv("PATH", str(tmp_path / "nothing-here"))
    monkeypatch.delenv("ANAST_OCR_TESSERACT", raising=False)
    samples = _raster_samples(tmp_path / "samples")
    output = tmp_path / "packs"

    result = run_pack_init(
        PackInitCommand(samples=[str(samples)], name="synthetic", out_dir=output, confirmed=True)
    )

    assert result.ok is False
    assert result.error == "OcrRequiredError"
    assert result.pack_dir is None
    assert not (output / "synthetic").exists()


@_NEEDS_ENGINE
def test_pack_init_can_be_told_to_stay_on_native_text_only(tmp_path: Path) -> None:
    """``allow_ocr=False`` is the strict pre-OCR behaviour, still available.

    An operator who wants a pack built only from text that was READ can have
    one, and gets the same refusal as before rather than a quiet OCR pack.
    """
    samples = _raster_samples(tmp_path / "samples")
    output = tmp_path / "packs"

    result = run_pack_init(
        PackInitCommand(
            samples=[str(samples)],
            name="strict",
            out_dir=output,
            confirmed=True,
            allow_ocr=False,
        )
    )

    assert result.ok is False
    assert result.error == "OcrRequiredError"
    assert not (output / "strict").exists()


@_NEEDS_ENGINE
def test_a_raster_only_sample_set_now_produces_a_reviewable_draft(tmp_path: Path) -> None:
    """Where the learner refused 53 samples, it now emits a draft that says so.

    Every artifact a person opens carries the provenance: the manifest
    description, DRAFT.md, the quarantine file's per-string marks, and a
    dedicated OCR_EVIDENCE.md.
    """
    samples = _raster_samples(tmp_path / "samples")
    output = tmp_path / "packs"

    result = run_pack_init(
        PackInitCommand(samples=[str(samples)], name="raster_soap", out_dir=output, confirmed=True)
    )

    assert result.ok is True, result.error
    pack_dir = output / "raster_soap"
    assert result.pack_dir == pack_dir

    manifest = (pack_dir / "pack.yaml").read_text(encoding="utf-8")
    assert "OCR EVIDENCE" in manifest
    assert OCR_EVIDENCE_NAME in manifest

    draft = pack_dir / "DRAFT.md"
    assert "OCR evidence (read this second)" in draft.read_text(encoding="utf-8")

    evidence_file = (pack_dir / OCR_EVIDENCE_NAME).read_text(encoding="utf-8")
    assert "Recognized text MAY NOT establish" in evidence_file
    assert "tesseract" in evidence_file
    assert "allow_network`: False" in evidence_file

    quarantine = (pack_dir / UNPLACED_NAME).read_text(encoding="utf-8")
    assert "[OCR]" in quarantine


@_NEEDS_ENGINE
def test_the_summary_a_person_confirms_states_the_ocr_caveat(tmp_path: Path) -> None:
    """The operator confirms from the summary, so the caveat has to be in it."""
    samples = _raster_samples(tmp_path / "samples")

    result = run_pack_init(
        PackInitCommand(samples=[str(samples)], name="raster_soap", out_dir=tmp_path / "packs")
    )

    assert result.error == "ConfirmationRequired"
    assert OCR_EVIDENCE_CAVEAT in result.summary
    assert any(line.startswith("page provenance:") for line in result.summary)
    assert any("held for review" in line for line in result.summary)


@_NEEDS_ENGINE
def test_a_recognized_heading_is_labelled_as_recognized_in_the_draft(
    tmp_path: Path,
) -> None:
    """A section built from pixels is a layout hypothesis, and the pack says so
    per section rather than about itself as a whole."""
    samples = _raster_samples(tmp_path / "samples")
    from anastomosis.packgen import extract_samples

    analysis = analyze(extract_samples(sorted(samples.glob("*.pdf")), ocr=_WORKER))

    recognized = [
        candidate for candidate in analysis.sections if candidate.provenance == OCR_OBSERVATION
    ]
    assert recognized, [c.text for c in analysis.sections]
    assert {"SUBJECTIVE", "OBJECTIVE"} & {c.text for c in recognized}


@_NEEDS_ENGINE
def test_a_recognized_page_never_offers_its_sentinel_face_to_the_css(
    tmp_path: Path,
) -> None:
    """Recognition recovers no face. Emitting ``OcrObservation`` as a CSS family
    would assert exactly the thing the decision record says is unrecoverable."""
    samples = _raster_samples(tmp_path / "samples")
    output = tmp_path / "packs"

    result = run_pack_init(
        PackInitCommand(samples=[str(samples)], name="raster_soap", out_dir=output, confirmed=True)
    )

    assert result.ok is True, result.error
    for name in ("pack.yaml", "template.html"):
        assert OCR_SPAN_FONT not in (output / "raster_soap" / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# The command line
# --------------------------------------------------------------------------- #


def _flowed(output: str) -> str:
    """Console output with its wrapping removed.

    Rich wraps to the terminal width, so asserting on a phrase means asserting
    on where the wrap fell. Collapsing whitespace asks about the sentence.
    """
    return " ".join(output.split())


@_NEEDS_ENGINE
def test_the_cli_can_be_told_to_learn_only_from_text_that_was_read(
    tmp_path: Path,
) -> None:
    """``--no-ocr`` refuses a scan, and says which switch caused the refusal.

    An operator who deliberately excluded recognition should not be left
    reading an install hint for an engine they already have.
    """
    from typer.testing import CliRunner

    from anastomosis.cli import app

    samples = _raster_samples(tmp_path / "samples")
    result = CliRunner().invoke(
        app,
        [
            "pack",
            "init",
            "--from-samples",
            str(samples),
            "--name",
            "strict",
            "--out-dir",
            str(tmp_path / "packs"),
            "--no-ocr",
            "--yes",
        ],
    )

    assert result.exit_code == 1
    output = _flowed(result.output)
    assert "OcrRequiredError" in output
    assert "You passed --no-ocr" in output
    assert not (tmp_path / "packs" / "strict").exists()


@_NEEDS_ENGINE
def test_the_cli_points_at_the_evidence_file_it_just_wrote(tmp_path: Path) -> None:
    """The disclosure reaches the terminal the operator is already looking at."""
    from typer.testing import CliRunner

    from anastomosis.cli import app

    samples = _raster_samples(tmp_path / "samples")
    result = CliRunner().invoke(
        app,
        [
            "pack",
            "init",
            "--from-samples",
            str(samples),
            "--name",
            "raster_soap",
            "--out-dir",
            str(tmp_path / "packs"),
            "--yes",
        ],
    )

    assert result.exit_code == 0, result.output
    # The sentence, not the path: Rich breaks a long path wherever the terminal
    # happens to end, so the file is proved on disk instead.
    assert "recognized from page images" in _flowed(result.output)
    assert (tmp_path / "packs" / "raster_soap" / OCR_EVIDENCE_NAME).is_file()


def test_the_cli_refusal_without_an_engine_names_what_to_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No engine: the message is the install hint, and promises no download."""
    from typer.testing import CliRunner

    from anastomosis.cli import app

    samples = _raster_samples(tmp_path / "samples")
    monkeypatch.setenv("PATH", str(tmp_path / "no-binaries-here"))
    monkeypatch.delenv("ANAST_OCR_TESSERACT", raising=False)

    result = CliRunner().invoke(
        app,
        [
            "pack",
            "init",
            "--from-samples",
            str(samples),
            "--name",
            "strict",
            "--out-dir",
            str(tmp_path / "packs"),
            "--yes",
        ],
    )

    assert result.exit_code == 1
    output = _flowed(result.output)
    assert "install Tesseract 5 with the 'eng' language data" in output
    assert "Nothing is downloaded" in output


@_NEEDS_ENGINE
def test_a_batch_keeps_the_region_refusals_own_type(tmp_path: Path) -> None:
    """A fail-closed geometry refusal survives the batch loop as itself.

    The generic wrapper names the sample PATH, which is right for an unreadable
    file and wrong here: this refusal already carries a page index and nothing
    else, and the frontend reports the exception TYPE.
    """
    from anastomosis.packgen import extract_samples

    scan = _pixmap_of(
        lambda page: page.insert_text((60, 90), "ROTATED", fontsize=13, fontname="hebo")
    )

    def build(doc: pymupdf.Document) -> None:
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_image(pymupdf.Rect(0, 0, PAGE_W, PAGE_H), pixmap=scan)
        page.set_rotation(90)

    path = _write(tmp_path / "rotated.pdf", build)

    with pytest.raises(OcrRegionError) as excinfo:
        extract_samples([path], ocr=_WORKER)

    assert path.name not in str(excinfo.value)


@_NEEDS_ENGINE
def test_a_truncated_reading_of_a_value_is_a_disagreement_not_a_duplicate(
    tmp_path: Path,
) -> None:
    """The safety figure has to count the cases that matter.

    Substring containment called a recognition a duplicate whenever it happened
    to sit inside the native span — which is every truncated read of a clinical
    value: ``100`` over a page whose text layer says ``1000``, ``8.6`` over
    ``98.6``, ``12`` over ``128``. Those are the two streams genuinely
    disagreeing about a number, and the disagreement count is what DRAFT.md and
    OCR_EVIDENCE.md print to an operator as the measure of how much needs a
    human eye. Here the pixels carry ``100`` and the invisible layer over them
    claims ``1000``: both readings survive, and the page is held.
    """
    scan = _pixmap_of(lambda page: page.insert_text((60, 90), "100", fontsize=13, fontname="hebo"))

    def build(doc: pymupdf.Document) -> None:
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_image(pymupdf.Rect(0, 0, PAGE_W, PAGE_H), pixmap=scan)
        page.insert_text((60, 90), "1000", fontsize=13, fontname="hebo", render_mode=3)

    sample = extract_document(_write(tmp_path / "truncated.pdf", build), 0, ocr=_WORKER)

    evidence = sample.evidence
    assert evidence.duplicate_count == 0
    assert evidence.disagreement_count == 1
    assert {conflict.kind for conflict in evidence.conflicts} == {CONFLICT_DISAGREEMENT}
    assert {"100", "1000"} <= {span.text for span in sample.spans}
    assert evidence.review_required is True


@_NEEDS_ENGINE
@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in engine is a POSIX shell script")
def test_a_faulting_engine_reaches_the_operator_as_an_engine_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole path, from a broken installation to the words on the terminal.

    An engine that exits non-zero used to arrive as ``analysis failed
    (ValueError)`` with nothing after it — an operator reading that goes and
    looks at their samples, which are fine. Driven here through the shipped
    ``anast pack init`` so the seam being proved is the one a person uses.
    """
    from typer.testing import CliRunner

    from anastomosis.cli import app

    assert _WORKER is not None
    broken = tmp_path / "broken-engine"
    broken.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        f'  --version|--list-langs) exec "{_WORKER.exe}" "$@" ;;\n'
        "esac\n"
        "exit 3\n",
        encoding="utf-8",
    )
    broken.chmod(0o755)
    monkeypatch.setenv("ANAST_OCR_TESSERACT", str(broken))
    samples = _raster_samples(tmp_path / "samples")
    output = tmp_path / "packs"

    result = CliRunner().invoke(
        app,
        [
            "pack",
            "init",
            "--from-samples",
            str(samples),
            "--name",
            "faulted",
            "--out-dir",
            str(output),
            "--yes",
        ],
    )
    capsys.readouterr()

    assert result.exit_code == 1
    assert "analysis failed (OcrEngineError)" in result.output
    assert "Your samples are not implicated" in result.output
    assert not (output / "faulted").exists()
