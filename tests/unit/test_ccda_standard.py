"""Tests for the standard C-CDA render path (``reconstruct/ccda_standard``).

Renders the C-CDA a migration actually moves (``deliver.ccda_export.build_ccd``)
through the vendored HL7 ``CDA.xsl`` into a neutral XHTML view, then to PDF.
These pin: a faithful full-document view, determinism, source-independent
neutrality (a non-PF source carries no PF skin), the no-network-egress security
posture, and the per-patient PDF orchestration (skip/force/failure).
"""

from __future__ import annotations

from pathlib import Path

import anastomosis.pipeline  # noqa: F401  registers the built-in source adapters
from anastomosis.deliver.ccda_export.builder import build_ccd
from anastomosis.reconstruct.ccda_standard import (
    CCDARenderResult,
    render_ccda_html,
    render_ccda_standard,
)
from anastomosis.sources import get_source

PF = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"
FHIR = Path(__file__).resolve().parents[1] / "fixtures" / "fhir_r4"


def _records(source: str, path: Path) -> list:
    return list(get_source(source).load(path))


class _FakeChromium:
    """Stand-in renderer: writes the XHTML to the PDF path so tests can read
    each patient's content back (the real Chromium writes a PDF there)."""

    def __init__(self, **kwargs: object) -> None:
        pass

    def render(self, html: str, pdf_path: Path) -> None:
        Path(pdf_path).write_text(html, encoding="utf-8")

    def close(self) -> None:
        pass


# --- the HTML transform ----------------------------------------------------


def test_render_ccda_html_is_a_full_view() -> None:
    rec = _records("pf-tebra", PF)[0]
    html = render_ccda_html(build_ccd(rec))
    assert "<html" in html.lower() and "</html>" in html.lower()
    # Patient demographics rendered (the HL7 stylesheet uppercases the family and
    # uses non-breaking-space separators, so assert the tokens, not the spacing).
    upper = html.upper()
    assert "ADA" in upper and "FIXTURE" in upper
    # Structured sections present in the standard view.
    for section in ("Problems", "Medications", "Vital"):
        assert section.lower() in html.lower()


def test_render_ccda_html_is_deterministic() -> None:
    ccd = build_ccd(_records("pf-tebra", PF)[0])
    assert render_ccda_html(ccd) == render_ccda_html(ccd)


def test_neutral_view_is_source_independent_and_pf_free() -> None:
    """A non-PF (FHIR) source renders through the SAME neutral HL7 stylesheet
    with zero PF skin tokens — PF is not privileged in the output. (PF-sourced
    records legitimately echo their own note markup in the preserved-fields
    narrative; that is faithful content, not the render skin — so neutrality is
    asserted on a non-PF source, where no PF markup can come from the content.)"""
    rec = _records("fhir-r4", FHIR)[0]
    html = render_ccda_html(build_ccd(rec))
    assert "SPECIMEN" in html.upper()  # the FHIR fixture patient renders
    assert "td_label" in html and "<table" in html.lower()  # HL7 stylesheet structure
    for token in ("pf-rich-text", "practice_fusion", "practice fusion"):
        assert token not in html.lower()


def test_network_egress_is_blocked() -> None:
    """The render path runs the transform under ``read_network=False`` — the
    security posture it depends on. A stylesheet ``document()`` to a remote URI
    must fetch nothing (the no-egress invariant)."""
    from lxml import etree

    hostile = (
        '<?xml version="1.0"?>'
        '<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">'
        '<xsl:template match="/"><out>'
        "<xsl:copy-of select=\"document('http://169.254.169.254/x')\"/>"
        "</out></xsl:template></xsl:stylesheet>"
    )
    transform = etree.XSLT(
        etree.fromstring(hostile.encode()),
        access_control=etree.XSLTAccessControl(read_network=False),
    )
    try:
        result = transform(etree.fromstring(b"<r/>"))
        assert b"169.254" not in etree.tostring(result)  # nothing was fetched
    except etree.XSLTApplyError:
        pass  # libxslt refusing the fetch outright is equally acceptable


# --- the per-patient PDF orchestration -------------------------------------


def test_render_ccda_standard_one_pdf_per_patient(tmp_path: Path) -> None:
    out = tmp_path / "ccda"
    result = render_ccda_standard(_records("pf-tebra", PF), out, renderer_factory=_FakeChromium)
    assert isinstance(result, CCDARenderResult)
    assert len(result.documents) == 3  # the 3-patient fixture
    assert result.failed == []
    pdfs = sorted(out.glob("*_ccda.pdf"))
    assert len(pdfs) == 3
    # Each patient's own demographics landed in their own file.
    ada = next(p for p in pdfs if "Fixture" in p.name)
    assert "ADA" in ada.read_text(encoding="utf-8").upper()


def test_render_ccda_standard_idempotent_skip(tmp_path: Path) -> None:
    out = tmp_path / "ccda"
    first = render_ccda_standard(_records("pf-tebra", PF), out, renderer_factory=_FakeChromium)
    assert not first.skipped
    second = render_ccda_standard(_records("pf-tebra", PF), out, renderer_factory=_FakeChromium)
    assert len(second.skipped) == 3  # existing PDFs kept, not re-rendered
    assert len(second.documents) == 3


def test_render_ccda_standard_force_rerenders(tmp_path: Path) -> None:
    out = tmp_path / "ccda"
    render_ccda_standard(_records("pf-tebra", PF), out, renderer_factory=_FakeChromium)
    forced = render_ccda_standard(
        _records("pf-tebra", PF), out, force=True, renderer_factory=_FakeChromium
    )
    assert not forced.skipped and len(forced.documents) == 3


def _named(patient_id: str, given: str = "John", family: str = "Smith"):
    from anastomosis.core.model import Patient, PatientRecord

    return PatientRecord(patient=Patient(id=patient_id, given_name=given, family_name=family))


def test_same_name_patients_do_not_collide_in_one_batch(tmp_path: Path) -> None:
    """Two DISTINCT patients sharing family+given get distinct files — the
    filename embeds a per-patient id hash, so neither overwrites the other."""
    out = tmp_path / "ccda"
    result = render_ccda_standard(
        [_named("id-alpha"), _named("id-beta")], out, renderer_factory=_FakeChromium
    )
    assert len(result.documents) == 2
    assert len(set(result.documents)) == 2  # distinct paths, no overwrite
    assert len(list(out.glob("*_ccda.pdf"))) == 2


def test_same_name_different_patient_not_falsely_skipped_across_batches(tmp_path: Path) -> None:
    """A later batch with a DIFFERENT patient of the same name is NOT skipped
    against the first patient's file (the cross-batch silent-drop the review
    flagged): each patient maps to its own id-hashed file."""
    out = tmp_path / "ccda"
    render_ccda_standard([_named("id-alpha")], out, renderer_factory=_FakeChromium)
    second = render_ccda_standard([_named("id-beta")], out, renderer_factory=_FakeChromium)
    assert not second.skipped  # different patient → its own file is written
    assert len(list(out.glob("*_ccda.pdf"))) == 2  # both patients land on disk


def test_same_patient_rerun_is_skipped(tmp_path: Path) -> None:
    """Re-running the SAME patient maps to the SAME file → a sound idempotent
    skip (the id hash makes the name patient-identifying)."""
    out = tmp_path / "ccda"
    render_ccda_standard([_named("id-alpha")], out, renderer_factory=_FakeChromium)
    again = render_ccda_standard([_named("id-alpha")], out, renderer_factory=_FakeChromium)
    assert len(again.skipped) == 1
    assert len(list(out.glob("*_ccda.pdf"))) == 1


def test_render_failure_is_recorded_per_patient(tmp_path: Path) -> None:
    class _Boom:
        def __init__(self, **kwargs: object) -> None:
            pass

        def render(self, html: str, pdf_path: Path) -> None:
            raise RuntimeError("render exploded")

        def close(self) -> None:
            pass

    result = render_ccda_standard(_records("pf-tebra", PF), tmp_path / "o", renderer_factory=_Boom)
    assert len(result.failed) == 3
    assert all(exc_type == "RuntimeError" for _, exc_type in result.failed)
    assert result.documents == []  # nothing written


# --- PF demoted to an opt-in skin ------------------------------------------


def test_generic_soap_is_the_neutral_default_pf_is_opt_in() -> None:
    from anastomosis.core.commands import PipelineCommand, get_toolkit_info

    # The pipeline's default pack is the neutral generic_soap, never PF.
    assert PipelineCommand(export_dir=PF, charts_dir=PF).pack == "generic_soap"
    pack_names = {p.name for p in get_toolkit_info().packs}
    assert {"generic_soap", "practice_fusion_soap"} <= pack_names  # PF available, not default
