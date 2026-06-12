"""Unit tests for the packgen statistics (infer.py).

No Chromium: a small set of synthetic 'sample' PDFs is built directly with
PyMuPDF. The samples deliberately SHARE static labels ("SUBJECTIVE", "DOB:")
at fixed positions while carrying DIFFERING fake patient values
("Synthia Example" / "Maxwell Sample"), so the static-vs-variable split, the
type-scale clustering, the column grid, and the PHI property can all be
asserted against known ground truth.

All values are synthetic (example-style names, 555 phones, feedface ids); no
patient-derived data appears anywhere.
"""

from __future__ import annotations

from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz", reason="packgen infer needs the render extra (PyMuPDF)")

from anastomosis.packgen.extract import extract_samples  # noqa: E402
from anastomosis.packgen.infer import (  # noqa: E402
    analyze,
    infer_column_grid,
    infer_page_geometry,
    infer_section_taxonomy,
    infer_static_text,
    infer_type_scale,
)

_W, _H = 612.0, 792.0
_GREY_F1 = (0.9451, 0.9451, 0.9451)  # #f1f1f1
_LABEL_X = 60.0  # left column: labels
_VALUE_X = 200.0  # right column: per-patient values

# Per-sample variable patient values (each unique → must NOT recur).
_PATIENTS = [
    ("Synthia Example", "03/14/1985", "Hypertension follow-up"),
    ("Maxwell Sample", "07/04/1952", "Diabetes review"),
    ("Cleo Placeholder", "12/01/2021", "Well child visit"),
]


def _build_sample(path: Path, name: str, dob: str, complaint: str) -> None:
    """A synthetic note sharing the static frame, differing only in values.

    Static (every sample, fixed positions): the "SUBJECTIVE" heading band, the
    "DOB:"/"Provider:" labels, the footer. Variable: the patient name, dob, and
    complaint text.
    """
    doc = fitz.open()
    page = doc.new_page(width=_W, height=_H)
    # Heading band (grey fill) + bold heading — the section-heading signal.
    page.draw_rect(fitz.Rect(_LABEL_X, 95, 560, 110), fill=_GREY_F1, color=None)
    page.insert_text((_LABEL_X, 90), "SUBJECTIVE", fontsize=13, fontname="hebo")
    # Static label column (left) + variable value column (right), aligned x0s.
    page.insert_text((_LABEL_X, 150), "DOB:", fontsize=11, fontname="helv")
    page.insert_text((_VALUE_X, 150), dob, fontsize=11, fontname="helv")
    page.insert_text((_LABEL_X, 170), "Provider:", fontsize=11, fontname="helv")
    page.insert_text((_VALUE_X, 170), "Dr. Pat Provider", fontsize=11, fontname="helv")
    # Variable body prose (patient name + complaint).
    page.insert_text((_LABEL_X, 210), f"Patient {name} seen today.", fontsize=11, fontname="helv")
    page.insert_text((_LABEL_X, 230), complaint, fontsize=11, fontname="helv")
    # Static footer.
    page.insert_text((_LABEL_X, 760), "Confidential Example Clinic", fontsize=9, fontname="helv")
    doc.save(str(path))
    doc.close()


@pytest.fixture
def samples(tmp_path: Path) -> list:
    paths = []
    for i, (name, dob, complaint) in enumerate(_PATIENTS):
        p = tmp_path / f"sample{i}.pdf"
        _build_sample(p, name, dob, complaint)
        paths.append(p)
    return extract_samples(paths)


# --------------------------------------------------------------------------- #
# Type scale
# --------------------------------------------------------------------------- #


def test_type_scale_separates_body_from_heading(samples: list) -> None:
    scale = infer_type_scale(samples)
    roles = {lvl.role for lvl in scale.levels}
    assert "body" in roles
    # The 11pt regular prose/labels dominate → body at 11pt.
    assert scale.body_size == pytest.approx(11.0, abs=0.25)
    # The 13pt bold heading is a larger-than-body heading level.
    headings = [lvl for lvl in scale.levels if lvl.role.startswith("h")]
    assert any(lvl.size == pytest.approx(13.0, abs=0.25) and lvl.bold for lvl in headings)


def test_type_scale_clusters_within_quarter_point(tmp_path: Path) -> None:
    # Two spans 0.2pt apart must merge into one cluster; 0.4pt apart must not.
    doc = fitz.open()
    page = doc.new_page(width=_W, height=_H)
    page.insert_text((60, 100), "AAAA", fontsize=11.0, fontname="helv")
    page.insert_text((60, 120), "BBBB", fontsize=11.2, fontname="helv")
    page.insert_text((60, 140), "CCCC", fontsize=11.5, fontname="helv")
    p = tmp_path / "scale.pdf"
    doc.save(str(p))
    doc.close()
    scale = infer_type_scale(extract_samples([p]))
    helv_sizes = sorted({lvl.size for lvl in scale.levels})
    # 11.0 and 11.2 collapse; 11.5 stays separate → two distinct sizes.
    assert len(helv_sizes) == 2


# --------------------------------------------------------------------------- #
# Static vs variable split
# --------------------------------------------------------------------------- #


def test_section_taxonomy_recovers_shared_heading(samples: list) -> None:
    candidates = infer_section_taxonomy(samples)
    texts = {c.text for c in candidates}
    assert "SUBJECTIVE" in texts
    subjective = next(c for c in candidates if c.text == "SUBJECTIVE")
    assert subjective.count == 3  # in all three samples
    assert subjective.all_pages_first is True


def test_static_text_recovers_labels_not_values(samples: list) -> None:
    static = infer_static_text(samples)
    # Shared labels recur → static.
    assert "DOB:" in static
    assert "Provider:" in static
    assert "Confidential Example Clinic" in static
    # Per-patient values appear in exactly one sample → never static.
    for name, dob, complaint in _PATIENTS:
        assert dob not in static
        assert complaint not in static
        assert not any(name in s for s in static)
    # Section headings are subtracted from static text (no double count).
    assert "SUBJECTIVE" not in static


def test_single_sample_is_low_confidence(tmp_path: Path) -> None:
    p = tmp_path / "one.pdf"
    _build_sample(p, *_PATIENTS[0])
    analysis = analyze(extract_samples([p]))
    assert analysis.low_confidence is True
    # With one sample everything "recurs" trivially: even variable text becomes
    # a candidate, flagged count==1. (More samples sharpen the split.)
    candidates = infer_section_taxonomy(extract_samples([p]))
    assert candidates, "single-sample analysis still yields candidates"
    assert all(c.count == 1 for c in candidates)


# --------------------------------------------------------------------------- #
# Column grid
# --------------------------------------------------------------------------- #


def test_column_grid_finds_two_known_columns(samples: list) -> None:
    grid = infer_column_grid(samples)
    starts = [c.x0 for c in grid.columns]
    assert any(abs(x - _LABEL_X) <= 1.0 for x in starts), starts
    assert any(abs(x - _VALUE_X) <= 1.0 for x in starts), starts


def test_column_grid_reports_fill_gutters(samples: list) -> None:
    grid = infer_column_grid(samples)
    # The heading band's left/right edges become gutters.
    assert grid.gutters, "fill-rect edges should yield gutters"


# --------------------------------------------------------------------------- #
# Page geometry
# --------------------------------------------------------------------------- #


def test_page_geometry_is_letter(samples: list) -> None:
    geom = infer_page_geometry(samples)
    assert geom.width == pytest.approx(612.0, abs=1.0)
    assert geom.height == pytest.approx(792.0, abs=1.0)
    # Left margin ~ the label column x0 (60pt), top margin above the heading.
    assert geom.margin_left == pytest.approx(_LABEL_X, abs=5.0)
    assert geom.margin_top > 0
    assert geom.margin_bottom > 0


# --------------------------------------------------------------------------- #
# PHI property — the load-bearing invariant
# --------------------------------------------------------------------------- #


def test_summary_lines_never_leak_per_patient_values(samples: list) -> None:
    analysis = analyze(samples)
    summary = "\n".join(analysis.summary_lines())
    for name, dob, complaint in _PATIENTS:
        assert name.split()[0] not in summary, f"patient given-name leaked: {summary!r}"
        assert dob not in summary, "DOB leaked into summary"
        assert complaint not in summary, "chief complaint leaked into summary"
    # Static template text is allowed (it is not patient data).
    assert "SUBJECTIVE" in summary
    assert "612x792" in summary


def test_summary_lines_phi_property_holds_for_any_unique_value(samples: list) -> None:
    """A value present in only one sample must never appear in the summary."""
    analysis = analyze(samples)
    summary_words = set("\n".join(analysis.summary_lines()).split())
    from collections import Counter

    sample_count_for = Counter()
    for sample in samples:
        seen_this_sample = {s.text.strip() for s in sample.spans}
        for text in seen_this_sample:
            sample_count_for[text] += 1
    for text, n in sample_count_for.items():
        if n == 1 and len(text) > 4:
            assert text not in "\n".join(analysis.summary_lines()), (
                f"single-sample value leaked: {text!r}"
            )
    assert summary_words  # sanity


# --------------------------------------------------------------------------- #
# Aggregate + determinism
# --------------------------------------------------------------------------- #


def test_analyze_is_deterministic(samples: list) -> None:
    first = analyze(samples)
    second = analyze(samples)
    assert first.summary_lines() == second.summary_lines()
    assert first.sections == second.sections
    assert first.static_text == second.static_text


def test_design_tokens_capture_fill_palette(samples: list) -> None:
    analysis = analyze(samples)
    hexes = {c.hex for c in analysis.design_tokens.fill_colors}
    assert "#f1f1f1" in hexes
    assert analysis.design_tokens.body_font is not None


def test_bold_heading_smaller_than_body_earns_heading_role(tmp_path: Path) -> None:
    """The load-bearing branch of the heading rule: a 10.5pt BOLD heading under
    an 11pt regular body must still classify as a heading (generic_soap's real
    design does exactly this; larger-only rules miss it)."""
    paths = []
    for i in range(2):
        doc = fitz.open()
        page = doc.new_page(width=_W, height=_H)
        page.insert_text((72, 90), "ASSESSMENT", fontsize=10.5, fontname="hebo")
        page.insert_text(
            (72, 120), f"Prose body line {i} with running text.", fontsize=11, fontname="helv"
        )
        page.insert_text(
            (72, 140), f"More body prose for sample {i} here.", fontsize=11, fontname="helv"
        )
        path = tmp_path / f"bold_{i}.pdf"
        doc.save(str(path))
        doc.close()
        paths.append(path)
    analysis = analyze(extract_samples(paths))
    heads = [c for c in analysis.sections if c.text == "ASSESSMENT"]
    assert heads, "bold-under-body heading must be a section candidate"
    assert heads[0].role.startswith("h")


def test_mixed_page_sizes_resolve_to_modal_geometry(tmp_path: Path) -> None:
    # 2x Letter + 1x A4: the modal (Letter) geometry wins, documented behavior.
    paths = []
    for i, (w, h) in enumerate([(612, 792), (612, 792), (595, 842)]):
        doc = fitz.open()
        page = doc.new_page(width=w, height=h)
        page.insert_text((72, 90), "DOB:", fontsize=11, fontname="helv")
        path = tmp_path / f"geo_{i}.pdf"
        doc.save(str(path))
        doc.close()
        paths.append(path)
    geometry = analyze(extract_samples(paths)).page_geometry
    assert (round(geometry.width), round(geometry.height)) == (612, 792)
