"""The bundle carries the whole record, and QA grades it.

Every layout selects by encounter, so a fact reaching no encounter (a lab
with no visit attached, a standing list a SOAP note has no section for)
must not pass clean just because no check found its kind to object to
(#239): the suite must tell "no vitals" apart from "lost the vitals". A
pack-mode run also writes a whole-patient record summary per patient
(``record-summary/``), graded in the SAME report as the charts — a kind
the record holds and the summary omits is a FAIL.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from _render_fakes import FakeChromium

import anastomosis.reconstruct.ccda_standard.renderer as ccda_renderer
import anastomosis.reconstruct.chromium as chromium
import anastomosis.sources.pf_tebra  # noqa: F401 — registers the pf-tebra adapter
from anastomosis.core.model import (
    Encounter,
    Observation,
    ObservationCategory,
    Patient,
    PatientRecord,
)
from anastomosis.pipeline import RECORD_SUMMARY_DIRNAME, PipelineError, run_pipeline
from anastomosis.qa import Verdict, whole_patient_report
from anastomosis.qa.wholepatient import DOC_GENERIC_CHECKS, ENCOUNTER_SCOPED_SKIPS

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"

#: Two invented lab names, unique enough that finding one on a page means the
#: page really carries it. One is attributed to the visit, one names no
#: encounter at all — the pair issue #239 was reopened on.
ATTRIBUTED_LAB = "Serum quillfish index"
UNATTRIBUTED_LAB = "Plasma zephyrite level"

PATIENT_ID = "feedface-0239-0000-0000-000000000001"
ENCOUNTER_ID = "feedface-0239-e000-0000-000000000001"
DOB = date(1985, 3, 14)


@pytest.fixture
def rendered(monkeypatch: pytest.MonkeyPatch) -> None:
    """A browserless run: the fake renderer writes real PDFs for QA to read."""
    pytest.importorskip("pymupdf", reason="the fake renderer writes a real PDF")
    monkeypatch.setattr(chromium, "ChromiumRenderer", FakeChromium)


def _run(out: Path, *, qa: bool = True) -> Any:
    return run_pipeline(
        export_dir=FIXTURE,
        out=out,
        source="pf-tebra",
        pack="generic_soap",
        pack_dirs=None,
        force=False,
        section=None,
        qa=qa,
    )


def _lab_record() -> PatientRecord:
    """One visit, two laboratory results: one on the visit, one on no visit.

    Both are results in the coverage vocabulary, so a page showing neither has
    lost the family — which is what the summary is graded on.
    """
    return PatientRecord(
        patient=Patient(id=PATIENT_ID, given_name="Quill", family_name="Sentinel", birth_date=DOB),
        encounters=[
            Encounter(id=ENCOUNTER_ID, patient_id=PATIENT_ID, date_of_service=date(2023, 5, 10))
        ],
        observations=[
            Observation(
                patient_id=PATIENT_ID,
                encounter_id=ENCOUNTER_ID,
                category=ObservationCategory.LABORATORY,
                display=ATTRIBUTED_LAB,
                value="4.2",
            ),
            Observation(
                patient_id=PATIENT_ID,
                encounter_id=None,
                category=ObservationCategory.LABORATORY,
                display=UNATTRIBUTED_LAB,
                value="7.1",
            ),
        ],
    )


def _write_page(path: Path, lines: list[str]) -> Path:
    """A real, non-blank Letter page carrying exactly ``lines`` — the mutation
    corpus idiom: the same record against a page that carries it and a page that
    does not."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((36, 48), "\n".join(lines), fontsize=9)
    doc.save(str(path))
    doc.close()
    return path


def _coverage(report: Any) -> Any:
    """The one ``record_coverage`` result in a single-document report."""
    (result,) = [r for doc in report.documents for r in doc.results if r.check == "record_coverage"]
    return result


#: Enough of a whole-patient page to satisfy the identity anchor and geometry
#: checks, so the coverage verdict is the only thing under test.
_HEADER = ["Whole-patient record summary", "DOB 03/14/1985", "Quill Sentinel"]


# --- A: the bundle carries the record ---------------------------------------


def test_a_run_writes_one_record_summary_per_patient(tmp_path: Path, rendered: None) -> None:
    out = tmp_path / "charts"
    _run(out, qa=False)
    summaries = sorted((out / RECORD_SUMMARY_DIRNAME).glob("*.pdf"))
    # 3 patients in the fixture, 6 encounters: the charts are per visit, the
    # summaries are per patient.
    assert len(summaries) == 3
    assert len(list(out.glob("*.pdf"))) == 6


def test_a_summary_that_cannot_render_stops_the_run(
    tmp_path: Path, rendered: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A patient whose record reached no whole-record page is a patient whose
    bundle is silently partial — the run refuses rather than reporting success."""

    class _Boom:
        def __init__(self, **kwargs: object) -> None:
            pass

        def render(self, html: str, pdf_path: Path) -> None:
            raise RuntimeError("no browser here")

        def close(self) -> None:
            pass

    monkeypatch.setattr(ccda_renderer, "_default_renderer", lambda: _Boom())
    with pytest.raises(PipelineError) as excinfo:
        _run(tmp_path / "charts", qa=False)
    assert excinfo.value.kind == "render_failed"
    assert excinfo.value.exit_code == 1
    # PHI-safe: pseudonymous ids and exception TYPE names, never exception text.
    assert [tag for _id, tag in excinfo.value.failed] == ["RuntimeError"] * 3
    assert "no browser here" not in str(excinfo.value)


# --- B: one report, both populations -----------------------------------------


def test_the_qa_report_covers_the_summaries_as_well_as_the_charts(
    tmp_path: Path, rendered: None
) -> None:
    out = tmp_path / "charts"
    result = _run(out)
    report = json.loads((out / "qa_report.json").read_text(encoding="utf-8"))
    files = {doc["file"] for doc in report["documents"]}
    summaries = {p.name for p in (out / RECORD_SUMMARY_DIRNAME).glob("*.pdf")}
    assert summaries and summaries <= files
    assert len(report["documents"]) == 6 + len(summaries)
    assert result.qa_report is not None and result.qa_report.ok


def test_every_document_reports_the_same_check_set(tmp_path: Path, rendered: None) -> None:
    """Skip-with-reason, not omission: a check missing from a report reads
    exactly like a check that passed."""
    out = tmp_path / "charts"
    _run(out)
    report = json.loads((out / "qa_report.json").read_text(encoding="utf-8"))
    summaries = {p.name for p in (out / RECORD_SUMMARY_DIRNAME).glob("*.pdf")}
    expected = set(DOC_GENERIC_CHECKS) | set(ENCOUNTER_SCOPED_SKIPS)
    for doc in report["documents"]:
        assert {check["check"] for check in doc["checks"]} == expected
        if doc["file"] not in summaries:
            continue
        skipped = {
            check["check"]
            for check in doc["checks"]
            if any(finding.startswith("skipped:") for finding in check["findings"])
        }
        assert skipped == set(ENCOUNTER_SCOPED_SKIPS)


def test_the_merged_report_still_sums_what_the_layout_did_not_carry(
    tmp_path: Path, rendered: None
) -> None:
    """``not_carried`` is a run-level fact, and merging must not dilute it: the
    charts contribute the counts their layout has no place for, the summaries
    contribute nothing because they carry every kind."""
    out = tmp_path / "charts"
    result = _run(out)
    report = result.qa_report
    assert report is not None
    summaries = {p.name for p in (out / RECORD_SUMMARY_DIRNAME).glob("*.pdf")}
    chart_total = sum(
        check.not_carried
        for doc in report.documents
        if doc.path.name not in summaries
        for check in doc.results
    )
    summary_total = sum(
        check.not_carried
        for doc in report.documents
        if doc.path.name in summaries
        for check in doc.results
    )
    assert chart_total > 0, "generic_soap omits every chartable kind; the count must survive"
    assert summary_total == 0
    assert report.not_carried == chart_total
    assert (
        json.loads((out / "qa_report.json").read_text(encoding="utf-8"))["summary"]["not_carried"]
        == chart_total
    )


# --- C: the conservation check, on the pair issue #239 was reopened on --------


def test_a_summary_that_lost_both_labs_fails_coverage() -> None:
    """The criterion this issue was reopened on: a laboratory result attributed
    to the visit AND one with no encounter at all, neither on the page. The
    check that must not pass is ``record_coverage`` — and it must FAIL, not
    warn, because a whole-patient view declares it carries every kind."""
    pytest.importorskip("pymupdf", reason="QA reads the PDF back")
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        pdf = _write_page(Path(tmp) / "summary.pdf", _HEADER)
        report = whole_patient_report([(pdf, _lab_record())])
    coverage = _coverage(report)
    assert coverage.verdict is Verdict.FAIL
    assert not report.ok
    assert any("results" in finding for finding in coverage.findings)
    # A count and a kind name, never the value that was lost.
    assert not any(ATTRIBUTED_LAB in finding for finding in coverage.findings)


def test_a_summary_that_carries_both_labs_passes_coverage() -> None:
    """The other half of the pair: the same record against a page that DOES
    carry them. A check that cannot tell these two apart is the bug, not the
    fix."""
    pytest.importorskip("pymupdf", reason="QA reads the PDF back")
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        pdf = _write_page(Path(tmp) / "summary.pdf", [*_HEADER, ATTRIBUTED_LAB, UNATTRIBUTED_LAB])
        report = whole_patient_report([(pdf, _lab_record())])
    coverage = _coverage(report)
    assert coverage.verdict is Verdict.PASS
    assert report.ok


def test_carrying_only_the_attributed_lab_is_not_enough_to_hide_the_other() -> None:
    """The some/none boundary is per KIND, not per result: a page carrying
    one of two results passes coverage, so an unattributed lab (#239) needs
    the summary to exist at all, not a sharper coverage rule."""
    pytest.importorskip("pymupdf", reason="QA reads the PDF back")
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        pdf = _write_page(Path(tmp) / "summary.pdf", [*_HEADER, ATTRIBUTED_LAB])
        report = whole_patient_report([(pdf, _lab_record())])
    coverage = _coverage(report)
    assert coverage.verdict is Verdict.PASS


def test_the_summary_batch_declares_every_chartable_kind_carried() -> None:
    """The declaration turns an absence into a FAIL instead of a WARN; a
    kind missing from it is invisible in the verdicts, since
    ``record_coverage`` keys on "was coverage declared" plus ``omits``
    excuses. The set is the promise; pin the set."""
    from anastomosis.core.model import CHARTABLE_KINDS
    from anastomosis.qa.wholepatient import WHOLE_PATIENT_CARRIES, whole_patient_batch

    assert WHOLE_PATIENT_CARRIES == frozenset(CHARTABLE_KINDS)

    record = _lab_record()
    ((_path, encounter, anchored),) = whole_patient_batch([(Path("summary.pdf"), record)])
    # The synthetic encounter stands for the patient, not for a visit.
    assert encounter.id == record.patient.id and encounter.date_of_service is None
    # The name is blanked (the HL7 stylesheet canonicalizes it); the DOB stays,
    # so data_integrity still has an anchor to find.
    assert not anchored.patient.display_name
    assert anchored.patient.birth_date == DOB
    assert "record_coverage" in DOC_GENERIC_CHECKS
    assert set(CHARTABLE_KINDS) == {
        "conditions",
        "allergies",
        "medications",
        "immunizations",
        "results",
    }
