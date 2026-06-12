"""Unit tests for the packgen draft-pack emitter (emit.py) and the wizard CLI.

No Chromium: a small set of synthetic 'sample' PDFs is built directly with
PyMuPDF (same approach as ``test_packgen_infer``), so the analysis → emit path,
the manifest schema validity, the losslessness of unplaced static text, the
same-patient caveat, determinism, and the wizard's confirm/abort/exit-code
behavior are all asserted without a browser.

All values are synthetic (example-style names, 555 phones, feedface ids); no
patient-derived data appears anywhere.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

fitz = pytest.importorskip("fitz", reason="packgen emit tests need the render extra (PyMuPDF)")

from typer.testing import CliRunner  # noqa: E402

from anastomosis.cli import app  # noqa: E402
from anastomosis.packgen import PackAnalysis, analyze, extract_samples  # noqa: E402
from anastomosis.packgen.emit import SAME_PATIENT_CAVEAT, emit_draft_pack  # noqa: E402
from anastomosis.reconstruct.packs import PackManifest, discover_packs  # noqa: E402

_W, _H = 612.0, 792.0
_GREY_F1 = (0.9451, 0.9451, 0.9451)  # #f1f1f1
_LABEL_X = 60.0
_VALUE_X = 200.0

# Distinct synthetic patients (each value unique → never recurs → never static).
_PATIENTS = [
    ("Synthia Example", "03/14/1985", "Hypertension follow-up"),
    ("Maxwell Sample", "07/04/1952", "Diabetes review"),
    ("Cleo Placeholder", "12/01/2021", "Well child visit"),
    ("Dale Specimen", "09/09/1970", "Annual physical"),
]


def _build_sample(path: Path, name: str, dob: str, complaint: str) -> None:
    """A synthetic note sharing a static frame, differing only in values."""
    doc = fitz.open()
    page = doc.new_page(width=_W, height=_H)
    page.draw_rect(fitz.Rect(_LABEL_X, 95, 560, 110), fill=_GREY_F1, color=None)
    page.insert_text((_LABEL_X, 90), "SUBJECTIVE", fontsize=13, fontname="hebo")
    page.insert_text((_LABEL_X, 130), "OBJECTIVE", fontsize=13, fontname="hebo")
    page.insert_text((_LABEL_X, 200), "DOB:", fontsize=11, fontname="helv")
    page.insert_text((_VALUE_X, 200), dob, fontsize=11, fontname="helv")
    page.insert_text((_LABEL_X, 220), "Provider:", fontsize=11, fontname="helv")
    page.insert_text((_VALUE_X, 220), "Dr. Pat Provider", fontsize=11, fontname="helv")
    page.insert_text((_LABEL_X, 260), f"Patient {name} seen today.", fontsize=11, fontname="helv")
    page.insert_text((_LABEL_X, 280), complaint, fontsize=11, fontname="helv")
    page.insert_text((_LABEL_X, 760), "Confidential Example Clinic", fontsize=9, fontname="helv")
    doc.save(str(path))
    doc.close()


def _make_samples(tmp_path: Path, n: int = 4) -> list[Path]:
    paths = []
    for i in range(n):
        name, dob, complaint = _PATIENTS[i % len(_PATIENTS)]
        p = tmp_path / f"sample{i}.pdf"
        _build_sample(p, name, dob, complaint)
        paths.append(p)
    return paths


@pytest.fixture
def analysis(tmp_path: Path) -> PackAnalysis:
    return analyze(extract_samples(_make_samples(tmp_path)))


# --------------------------------------------------------------------------- #
# Emit: structure + loadability
# --------------------------------------------------------------------------- #


def test_emit_writes_all_four_files(analysis: PackAnalysis, tmp_path: Path) -> None:
    pack_dir = emit_draft_pack(analysis, name="acme_soap", display="ACME EHR", out_dir=tmp_path)
    assert pack_dir == tmp_path / "acme_soap"
    for filename in ("pack.yaml", "template.html", "context.py", "DRAFT.md"):
        assert (pack_dir / filename).is_file(), filename


def test_emitted_pack_loads_through_discover(analysis: PackAnalysis, tmp_path: Path) -> None:
    """The draft must load through the REAL loader with allow_external."""
    out = tmp_path / "packs"
    pack_dir = emit_draft_pack(analysis, name="acme_soap", display="ACME", out_dir=out)
    statuses = discover_packs([pack_dir.parent], allow_external=True)
    status = statuses["acme_soap"]
    assert status.available, status.diagnosis
    assert status.pack is not None
    assert callable(status.pack.build_context)


def test_emitted_pack_yaml_validates_against_manifest(
    analysis: PackAnalysis, tmp_path: Path
) -> None:
    pack_dir = emit_draft_pack(analysis, name="acme_soap", display="ACME", out_dir=tmp_path)
    data = yaml.safe_load((pack_dir / "pack.yaml").read_text(encoding="utf-8"))
    manifest = PackManifest.model_validate(data)
    assert manifest.name == "acme_soap"
    assert manifest.page.size == "Letter"
    assert manifest.tokens["heading_fill"] == "#f1f1f1"


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #


def test_emit_is_byte_identical(analysis: PackAnalysis, tmp_path: Path) -> None:
    """Same analysis → byte-identical pack files."""
    a = emit_draft_pack(analysis, name="acme_soap", display="ACME", out_dir=tmp_path / "a")
    b = emit_draft_pack(analysis, name="acme_soap", display="ACME", out_dir=tmp_path / "b")
    for filename in ("pack.yaml", "template.html", "context.py", "DRAFT.md"):
        assert (a / filename).read_bytes() == (b / filename).read_bytes(), filename


# --------------------------------------------------------------------------- #
# Losslessness: unplaced static text never vanishes
# --------------------------------------------------------------------------- #


def test_unplaced_static_text_lands_in_comment_block(
    analysis: PackAnalysis, tmp_path: Path
) -> None:
    """A static string that maps to no model field must appear verbatim in the
    UNPLACED comment — never silently dropped (losslessness invariant)."""
    pack_dir = emit_draft_pack(analysis, name="acme_soap", display="ACME", out_dir=tmp_path)
    html = (pack_dir / "template.html").read_text(encoding="utf-8")
    assert "UNPLACED STATIC TEXT — position manually" in html
    # "Confidential Example Clinic" maps to no known field → must be preserved.
    comment_start = html.index("<!-- UNPLACED STATIC TEXT")
    comment_end = html.index("-->", comment_start)
    comment = html[comment_start:comment_end]
    assert "Confidential Example Clinic" in comment


def test_placed_labels_map_to_header_fields(analysis: PackAnalysis, tmp_path: Path) -> None:
    """DOB:/Provider: are recognized labels → emitted in the patient header,
    not in the unplaced comment."""
    pack_dir = emit_draft_pack(analysis, name="acme_soap", display="ACME", out_dir=tmp_path)
    html = (pack_dir / "template.html").read_text(encoding="utf-8")
    comment_start = html.index("<!-- UNPLACED STATIC TEXT")
    comment_end = html.index("-->", comment_start)
    comment = html[comment_start:comment_end]
    # The label text is placed in the header (after the comment block).
    assert "DOB:" not in comment
    assert "Provider:" not in comment
    assert "{{ dob }}" in html
    assert "provider.name" in html


def test_no_static_string_is_dropped(analysis: PackAnalysis, tmp_path: Path) -> None:
    """Every static string is either a placed header label or in the comment —
    none is silently lost."""
    pack_dir = emit_draft_pack(analysis, name="acme_soap", display="ACME", out_dir=tmp_path)
    html = (pack_dir / "template.html").read_text(encoding="utf-8")
    for static in analysis.static_text:
        # Either present verbatim, or its label stem matched a header slot.
        present = static in html
        labelish = any(
            static.lower().startswith(prefix)
            for prefix in ("dob", "date of birth", "provider", "seen by", "patient", "name", "sex")
        )
        assert present or labelish, f"static string dropped: {static!r}"


# --------------------------------------------------------------------------- #
# DRAFT.md provenance + caveat
# --------------------------------------------------------------------------- #


def test_draft_md_carries_same_patient_caveat(analysis: PackAnalysis, tmp_path: Path) -> None:
    pack_dir = emit_draft_pack(analysis, name="acme_soap", display="ACME", out_dir=tmp_path)
    draft = (pack_dir / "DRAFT.md").read_text(encoding="utf-8")
    assert SAME_PATIENT_CAVEAT in draft
    assert "DRAFT" in draft
    assert "Fidelity" in draft or "fidelity" in draft
    assert f"Samples analyzed: {analysis.sample_count}" in draft


def test_draft_md_lists_sections_and_geometry(analysis: PackAnalysis, tmp_path: Path) -> None:
    pack_dir = emit_draft_pack(analysis, name="acme_soap", display="ACME", out_dir=tmp_path)
    draft = (pack_dir / "DRAFT.md").read_text(encoding="utf-8")
    assert "612x792pt" in draft
    assert "SUBJECTIVE" in draft  # an inferred heading section


# --------------------------------------------------------------------------- #
# Single-sample low-confidence: no heading sections promoted
# --------------------------------------------------------------------------- #


def test_single_sample_emits_no_heading_sections(tmp_path: Path) -> None:
    p = tmp_path / "one.pdf"
    _build_sample(p, *_PATIENTS[0])
    analysis = analyze(extract_samples([p]))
    assert analysis.low_confidence is True
    pack_dir = emit_draft_pack(analysis, name="acme_soap", display="ACME", out_dir=tmp_path / "p")
    data = yaml.safe_load((pack_dir / "pack.yaml").read_text(encoding="utf-8"))
    manifest = PackManifest.model_validate(data)
    # Only the four always-present data sections; no inferred heading sections.
    assert set(manifest.sections) == {"vitals", "addenda", "insurance", "social_history"}
    draft = (pack_dir / "DRAFT.md").read_text(encoding="utf-8")
    assert "LOW" in draft


# --------------------------------------------------------------------------- #
# Robustness: a synthetic analysis with hostile content still emits a loadable,
# schema-valid, renderable-size pack (no PDFs needed — build PackAnalysis by hand).
# --------------------------------------------------------------------------- #


def _hostile_analysis(width: float = 500.0, height: float = 700.0) -> PackAnalysis:
    """A PackAnalysis whose inferred text contains YAML/HTML metacharacters and
    whose geometry is non-standard — the adversarial emit input."""
    from anastomosis.packgen.infer import (
        ColumnGrid,
        DesignTokens,
        PageBreakStats,
        PageGeometry,
        SectionCandidate,
        TypeScale,
    )

    return PackAnalysis(
        sample_count=3,
        type_scale=TypeScale(levels=()),
        sections=(
            SectionCandidate(
                text='WEIRD: heading "quote" -> arrow',
                role="h2",
                count=3,
                median_y_fraction=0.3,
                all_pages_first=True,
            ),
            SectionCandidate(
                text=":::", role="h2", count=2, median_y_fraction=0.5, all_pages_first=True
            ),
        ),
        static_text=("boiler --> plate", "Has: colon", "Provider:"),
        column_grid=ColumnGrid(columns=(), gutters=()),
        page_breaks=PageBreakStats(
            page_count_distribution=((1, 3),), max_content_y_fraction=0.9, running_headers=()
        ),
        page_geometry=PageGeometry(
            width=width,
            height=height,
            margin_left=36.0,
            margin_right=36.0,
            margin_top=36.0,
            margin_bottom=36.0,
        ),
        design_tokens=DesignTokens(fill_colors=(), stroke_widths=(), body_font=None),
    )


def test_yaml_active_heading_chars_still_validate(tmp_path: Path) -> None:
    """An inferred heading containing a colon/quote must not break pack.yaml —
    it is emitted as a quoted scalar, so the manifest still loads."""
    pack_dir = emit_draft_pack(_hostile_analysis(), name="hostile", display="X", out_dir=tmp_path)
    data = yaml.safe_load((pack_dir / "pack.yaml").read_text(encoding="utf-8"))
    manifest = PackManifest.model_validate(data)
    assert "hostile" == manifest.name
    status = discover_packs([pack_dir.parent], allow_external=True)["hostile"]
    assert status.available, status.diagnosis


def test_arrow_in_static_text_cannot_escape_comment(tmp_path: Path) -> None:
    """A static string containing ``-->`` must be neutralized so it cannot close
    the UNPLACED comment early (losslessness + well-formed output)."""
    pack_dir = emit_draft_pack(_hostile_analysis(), name="hostile", display="X", out_dir=tmp_path)
    html = (pack_dir / "template.html").read_text(encoding="utf-8")
    comment_start = html.index("<!-- UNPLACED STATIC TEXT")
    comment_end = html.index("-->", comment_start)
    comment = html[comment_start:comment_end]
    assert "boiler --&gt; plate" in comment
    assert "boiler --> plate" not in comment


def test_exotic_page_size_falls_back_to_renderable_named_size(tmp_path: Path) -> None:
    """A non-standard geometry must emit a NAMED page size (the engine renders
    named formats only); the true points are recorded in DRAFT.md."""
    pack_dir = emit_draft_pack(
        _hostile_analysis(500.0, 700.0), name="ex", display="X", out_dir=tmp_path
    )
    manifest = PackManifest.model_validate(
        yaml.safe_load((pack_dir / "pack.yaml").read_text(encoding="utf-8"))
    )
    assert manifest.page.size in {"Letter", "Legal", "A4", "A3", "A5"}
    draft = (pack_dir / "DRAFT.md").read_text(encoding="utf-8")
    assert "nearest standard size" in draft
    assert "500x700pt" in draft


def test_empty_analysis_emits_default_tokens(tmp_path: Path) -> None:
    """Zero fills / no body font / empty type scale must fall back to the
    documented generic_soap token defaults, never crash or emit blanks."""
    from anastomosis.packgen.infer import (
        ColumnGrid,
        DesignTokens,
        PageBreakStats,
        PageGeometry,
        TypeScale,
    )

    empty = PackAnalysis(
        sample_count=2,
        type_scale=TypeScale(levels=()),
        sections=(),
        static_text=(),
        column_grid=ColumnGrid(columns=(), gutters=()),
        page_breaks=PageBreakStats(
            page_count_distribution=((1, 2),), max_content_y_fraction=0.0, running_headers=()
        ),
        page_geometry=PageGeometry(
            width=612.0,
            height=792.0,
            margin_left=0.0,
            margin_right=0.0,
            margin_top=0.0,
            margin_bottom=0.0,
        ),
        design_tokens=DesignTokens(fill_colors=(), stroke_widths=(), body_font=None),
    )
    pack_dir = emit_draft_pack(empty, name="empty", display="X", out_dir=tmp_path)
    manifest = PackManifest.model_validate(
        yaml.safe_load((pack_dir / "pack.yaml").read_text(encoding="utf-8"))
    )
    assert manifest.tokens["heading_fill"] == "#f1f1f1"
    assert "serif" in manifest.tokens["body_font"]
    assert discover_packs([pack_dir.parent], allow_external=True)["empty"].available


# --------------------------------------------------------------------------- #
# Wizard CLI flow (CliRunner — no Chromium)
# --------------------------------------------------------------------------- #


def _runner() -> CliRunner:
    return CliRunner()


def test_wizard_happy_path_with_yes(tmp_path: Path) -> None:
    sdir = tmp_path / "samples"
    sdir.mkdir()
    _make_samples(sdir)
    out = tmp_path / "packs"
    result = _runner().invoke(
        app,
        [
            "pack",
            "init",
            "--from-samples",
            str(sdir),
            "--name",
            "acme_soap",
            "--out-dir",
            str(out),
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert (out / "acme_soap" / "pack.yaml").is_file()
    assert (out / "acme_soap" / "DRAFT.md").is_file()
    # The PHI-safe summary is shown; the same-patient caveat is echoed.
    assert "Same-patient caveat" in result.stdout
    assert "Inferred design" in result.stdout


def test_wizard_abort_on_confirm_no(tmp_path: Path) -> None:
    """Answering 'no' to the different-patients prompt aborts cleanly (exit 0,
    no pack written)."""
    sdir = tmp_path / "samples"
    sdir.mkdir()
    _make_samples(sdir)
    out = tmp_path / "packs"
    result = _runner().invoke(
        app,
        ["pack", "init", "--from-samples", str(sdir), "--name", "acme_soap", "--out-dir", str(out)],
        input="n\n",
    )
    assert result.exit_code == 0, result.stdout
    assert not (out / "acme_soap").exists()
    assert "Aborting" in result.stdout


def test_wizard_low_sample_warning(tmp_path: Path) -> None:
    sdir = tmp_path / "samples"
    sdir.mkdir()
    _make_samples(sdir, n=2)
    out = tmp_path / "packs"
    result = _runner().invoke(
        app,
        [
            "pack",
            "init",
            "--from-samples",
            str(sdir),
            "--name",
            "acme_soap",
            "--out-dir",
            str(out),
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "only 2 sample" in result.stdout
    assert "LOW" in result.stdout


def test_wizard_no_pdfs_exits_2(tmp_path: Path) -> None:
    result = _runner().invoke(
        app,
        [
            "pack",
            "init",
            "--from-samples",
            str(tmp_path / "empty"),
            "--name",
            "acme_soap",
            "--out-dir",
            str(tmp_path / "packs"),
            "--yes",
        ],
    )
    assert result.exit_code == 2, result.stdout
    assert "no sample PDFs found" in result.stdout


def test_wizard_bad_name_exits_2(tmp_path: Path) -> None:
    sdir = tmp_path / "samples"
    sdir.mkdir()
    _make_samples(sdir)
    result = _runner().invoke(
        app,
        [
            "pack",
            "init",
            "--from-samples",
            str(sdir),
            "--name",
            "Acme-Bad",
            "--out-dir",
            str(tmp_path / "packs"),
            "--yes",
        ],
    )
    assert result.exit_code == 2, result.stdout
    assert "invalid pack name" in result.stdout


def test_wizard_summary_is_phi_safe(tmp_path: Path) -> None:
    """No per-patient value (fixture names/dobs/complaints) appears in the
    wizard's printed summary."""
    sdir = tmp_path / "samples"
    sdir.mkdir()
    _make_samples(sdir)
    out = tmp_path / "packs"
    result = _runner().invoke(
        app,
        [
            "pack",
            "init",
            "--from-samples",
            str(sdir),
            "--name",
            "acme_soap",
            "--out-dir",
            str(out),
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.stdout
    for name, dob, complaint in _PATIENTS:
        assert name.split()[0] not in result.stdout
        assert dob not in result.stdout
        assert complaint not in result.stdout


# --------------------------------------------------------------------------- #
# Regression tests from adversarial review (the probes became the tests)
# --------------------------------------------------------------------------- #


def test_single_sample_wizard_never_echoes_patient_values(tmp_path: Path) -> None:
    """BLOCKER regression: with ONE sample the recurrence threshold is 1, so
    per-patient values classify as 'static' — they must be suppressed from the
    console summary AND from every emitted file."""
    sample_dir = tmp_path / "samples"
    sample_dir.mkdir()
    _build_sample(sample_dir / "one.pdf", "Synthia Maxwell-Probe", "03/14/1985", "Vertigo")
    out_dir = tmp_path / "packs"
    result = _runner().invoke(
        app,
        [
            "pack",
            "init",
            "--from-samples",
            str(sample_dir),
            "--name",
            "single_sample",
            "--out-dir",
            str(out_dir),
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Synthia Maxwell-Probe" not in result.output
    assert "03/14/1985" not in result.output
    assert "suppressed" in result.output  # the loud single-sample warning
    for emitted in (out_dir / "single_sample").rglob("*"):
        if emitted.is_file():
            content = emitted.read_text(encoding="utf-8")
            assert "Synthia Maxwell-Probe" not in content, emitted.name
            assert "03/14/1985" not in content, emitted.name


def test_display_with_newline_cannot_rekey_the_manifest(tmp_path: Path) -> None:
    """SHOULD-FIX regression: a newline in --display must not corrupt
    pack.yaml or re-key the pack under injected text."""
    analysis = _hostile_analysis()
    pack_dir = emit_draft_pack(
        analysis, name="acme_soap", display="line1\nname: injected", out_dir=tmp_path
    )
    statuses = discover_packs([pack_dir.parent], allow_external=True)
    assert "acme_soap" in statuses
    assert statuses["acme_soap"].available, statuses["acme_soap"].diagnosis
    assert "injected" not in statuses


def test_jinja_delimiters_in_static_text_stay_literal(tmp_path: Path) -> None:
    """SHOULD-FIX regression: sample-derived text containing Jinja delimiters
    must be neutralized — emitted as entities, never executable."""
    from anastomosis.packgen.infer import (
        ColumnGrid,
        DesignTokens,
        PackAnalysis,
        PageBreakStats,
        PageGeometry,
        TypeScale,
    )

    analysis = PackAnalysis(
        sample_count=3,
        type_scale=TypeScale(levels=()),
        sections=(),
        static_text=("Provider {{ 7*7 }} {% for x in y %}", "DOB:"),
        column_grid=ColumnGrid(columns=(), gutters=()),
        page_breaks=PageBreakStats(
            page_count_distribution=((1, 3),), max_content_y_fraction=0.9, running_headers=()
        ),
        page_geometry=PageGeometry(
            width=612.0,
            height=792.0,
            margin_left=36.0,
            margin_right=36.0,
            margin_top=36.0,
            margin_bottom=36.0,
        ),
        design_tokens=DesignTokens(fill_colors=(), stroke_widths=(), body_font=None),
    )
    pack_dir = emit_draft_pack(analysis, name="jinja_probe", display="X", out_dir=tmp_path)
    html = (pack_dir / "template.html").read_text(encoding="utf-8")
    assert "{{ 7*7 }}" not in html
    assert "{% for x in y %}" not in html
    assert "&#123;&#123; 7*7 &#125;&#125;" in html


def test_jinja_delimiters_in_unplaced_text_stay_literal(tmp_path: Path) -> None:
    """The UNPLACED path (non-field static text) must neutralize delimiters
    too — Jinja parses HTML comments, so raw braces there would evaluate
    against the live render context (the half the first repair missed)."""
    base = _hostile_analysis()
    analysis = PackAnalysis(
        sample_count=base.sample_count,
        type_scale=base.type_scale,
        sections=(),
        static_text=("Boilerplate {{ 7*7 }} {% if True %}RAN{% endif %}",),
        column_grid=base.column_grid,
        page_breaks=base.page_breaks,
        page_geometry=base.page_geometry,
        design_tokens=base.design_tokens,
    )
    pack_dir = emit_draft_pack(analysis, name="unplaced_probe", display="X", out_dir=tmp_path)
    html = (pack_dir / "template.html").read_text(encoding="utf-8")
    comment_start = html.index("<!-- UNPLACED STATIC TEXT")
    comment = html[comment_start : html.index("-->", comment_start)]
    assert "{{ 7*7 }}" not in comment
    assert "{% if True %}" not in comment
    assert "&#123;&#123; 7*7 &#125;&#125;" in comment
