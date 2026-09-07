"""A chart with no encounters is still verified.

QA must not gate solely on the per-encounter render's own output: a C-CDA
Unstructured Document renders no encounter at all (#313), so an
attachment-only export must instead be graded through the whole-patient
record summary (#239), against what actually RENDERED — and graded once
per rendered file, never once per `PatientRecord` sharing its id.

Synthetic throughout: `feedface-` ids, the 555 exchange, and PDFs from
`test_ccda_unstructured` (the same shape #381's fixture uses)."""

from __future__ import annotations

import builtins
import json
from datetime import date
from pathlib import Path

import pytest
from _render_fakes import FakeChromium
from test_ccda_unstructured import _embedded, _pdf, _write

import anastomosis.reconstruct.ccda_standard as ccda_standard
import anastomosis.reconstruct.chromium as chromium
import anastomosis.sources.pf_tebra  # noqa: F401 — registers the pf-tebra adapter
from anastomosis.core.commands import PipelineCommand, run_pipeline_command
from anastomosis.core.model import AllergyIntolerance, Condition, Patient, PatientRecord
from anastomosis.deliver.browser.gates import (
    GATE_NOT_RUN,
    GATE_PASS,
    DeliveryRefused,
    assert_deliverable,
)
from anastomosis.deliver.browser.persist import load_upload_manifest
from anastomosis.pipeline import (
    RECORD_SUMMARY_DIRNAME,
    STAGE_QA,
    PipelineError,
    PipelineResult,
    StageEvent,
    _render_record_summaries,
    _run_qa_stage,
    run_pipeline,
)
from anastomosis.qa.base import Verdict
from anastomosis.reconstruct import discover_packs
from anastomosis.reconstruct.ccda_standard import CCDARenderResult
from anastomosis.reconstruct.engine import ReconstructionEngine, RenderResult
from anastomosis.sources import get_source

PF_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"


@pytest.fixture
def rendered(monkeypatch: pytest.MonkeyPatch) -> None:
    """A browserless run: the fake renderer writes real PDFs for QA to read."""
    pytest.importorskip("pymupdf", reason="the fake renderer writes a real PDF")
    monkeypatch.setattr(chromium, "ChromiumRenderer", FakeChromium)


def _attachment_only_export(tmp_path: Path) -> Path:
    """One patient, two Unstructured Documents, zero encounters — the #383
    shape. Each document keeps a distinct embedded scan (different page counts)
    so the two carried attachments never collide on a delivered name."""
    export = tmp_path / "export"
    _write(export, _embedded(_pdf(pages=1)), name="doc1.xml")
    _write(export, _embedded(_pdf(pages=2)), name="doc2.xml")
    return export


def _run(out: Path, export: Path, *, qa: bool = True, force: bool = False) -> PipelineResult:
    return run_pipeline(
        export_dir=export,
        out=out,
        source="ccda",
        pack="generic_soap",
        pack_dirs=None,
        force=force,
        section=None,
        qa=qa,
    )


def _engine() -> ReconstructionEngine:
    """A real engine over the built-in neutral pack — QA reads its real
    section flags/carries/omits, not a mock standing in for them."""
    status = discover_packs()["generic_soap"]
    assert status.pack is not None, status.diagnosis
    return ReconstructionEngine(status.pack, lambda: FakeChromium())


# --- 1: the defect, driven end to end ----------------------------------------


def test_an_attachment_only_chart_is_verified_by_its_record_summary(
    tmp_path: Path, rendered: None
) -> None:
    """QA on, over the attachment-only export: a report is written, the
    manifest gate reads `pass`, and `assert_deliverable` accepts."""
    export = _attachment_only_export(tmp_path)
    out = tmp_path / "charts"

    run_pipeline_command(
        PipelineCommand(export_dir=export, charts_dir=out, source="ccda", write_manifest=True)
    )

    assert (out / "qa_report.json").is_file()
    manifest = load_upload_manifest(out)
    assert manifest.gates is not None
    assert manifest.gates.qa == GATE_PASS
    assert_deliverable(manifest)  # must not raise


# --- 2: one row per rendered file, not per record ----------------------------


def test_the_summary_population_is_graded_once_per_rendered_file(
    tmp_path: Path, rendered: None
) -> None:
    """Two records sharing one patient id must be graded via the one
    summary path they share, never once per record — re-deriving the path
    per record would grade the same rendered file twice."""
    export = _attachment_only_export(tmp_path)
    out = tmp_path / "charts"

    result = _run(out, export)

    (summary,) = (out / RECORD_SUMMARY_DIRNAME).glob("*.pdf")
    assert result.qa_report is not None
    assert [doc.path for doc in result.qa_report.documents] == [summary]


# --- 2b: round-two BLOCKER — the graded row is the record that wrote it ------
#
# Test 2 above only proves ONE row per file; it never asks WHICH record backs
# that row, so keeping the FIRST record in `render_ccda_standard`'s dedup
# instead of the LAST left it green either way. `force=False` means the FIRST
# record processed WRITES the summary page and every later one sharing its
# patient id takes the renderer's idempotent-skip branch — so a caller that
# re-zips `documents`/`records` with a plain `dict(zip(...))` (last entry
# wins) associates the page with the SKIP, not the WRITER, whose bytes were
# never rendered onto it.


def _dob_record(patient_id: str, birth_date: date) -> PatientRecord:
    """A minimal whole-patient record, distinguished only by its own DOB —
    the identity anchor `whole_patient_report`'s `_anchor_record` leaves after
    blanking the name (see `qa/wholepatient.py`)."""
    return PatientRecord(
        patient=Patient(
            id=patient_id, given_name="Wren", family_name="Ashgrove", birth_date=birth_date
        )
    )


def test_the_graded_summary_is_the_record_that_wrote_the_page(
    tmp_path: Path, rendered: None
) -> None:
    """Two records share one patient id at `force=False`: the FIRST
    (`writer`) renders and writes the page, the SECOND takes the
    idempotent skip. Only the writer's DOB is on the rendered page, so
    `data_integrity` must be graded against the WRITER, never whichever
    record a naive last-wins lookup returns."""
    pid = "feedface-0000-0000-0000-000000000201"
    writer = _dob_record(pid, date(1970, 1, 1))
    skipped = _dob_record(pid, date(1985, 6, 15))
    out = tmp_path / "charts"

    summaries = _render_record_summaries([writer, skipped], out, force=False)
    assert len(summaries.documents) == 2  # one write, one idempotent skip
    (summary_path,) = set(summaries.documents)  # both name the same file
    assert summaries.by_path[summary_path] is writer

    report = _run_qa_stage(
        [writer, skipped], RenderResult(), summaries, _engine(), out, "Letter", lambda _e: None
    )

    assert report is not None
    (doc,) = report.documents
    integrity = next(r for r in doc.results if r.check == "data_integrity")
    assert integrity.verdict is Verdict.PASS, integrity.findings
    assert integrity.findings == []


def test_a_summary_graded_against_the_wrong_record_would_disarm_its_coverage(
    tmp_path: Path, rendered: None
) -> None:
    """One record carries clinical content, one carries none, sharing a
    patient id. Graded against the wrong record, `record_coverage` would
    silently become a no-op (nothing to compare — never wrong, never
    useful) while `data_integrity` raises a spurious failure on a DOB
    never on this page — graded against the WRITER instead, both pass."""
    pid = "feedface-0000-0000-0000-000000000301"
    structured = _dob_record(pid, date(1970, 1, 1))
    structured.conditions.append(Condition(patient_id=pid, display="Feedface Sinusitis"))
    structured.allergies.append(AllergyIntolerance(patient_id=pid, substance="Feedface Latex"))
    attachment_only = _dob_record(pid, date(1985, 6, 15))
    out = tmp_path / "charts"

    summaries = _render_record_summaries([structured, attachment_only], out, force=False)

    report = _run_qa_stage(
        [structured, attachment_only],
        RenderResult(),
        summaries,
        _engine(),
        out,
        "Letter",
        lambda _e: None,
    )  # must not raise — a spurious qa_failed is exactly the regression

    assert report is not None
    assert report.ok  # exit 0
    (doc,) = report.documents  # exactly one graded summary row
    results = {r.check: r for r in doc.results}
    assert results["record_coverage"].verdict is Verdict.PASS
    assert results["record_coverage"].findings == []  # a real comparison, not the no-op
    assert results["data_integrity"].verdict is Verdict.PASS
    assert results["data_integrity"].findings == []


# --- 3: empty populations, driven at the function that gates them -----------


def test_a_run_that_graded_nothing_records_not_run_rather_than_pass(tmp_path: Path) -> None:
    """Unreachable through `run_pipeline`'s own gate (an empty per-encounter
    AND empty summary population means nothing rendered at all, which the
    CLI refuses earlier with `kind=empty_export`) but drivable directly
    here: the emptiness rule must live in `_run_qa_stage` itself."""
    pytest.importorskip("pymupdf", reason="_run_qa_stage imports it before grading")
    events: list[StageEvent] = []

    report = _run_qa_stage(
        [], RenderResult(), CCDARenderResult(), _engine(), tmp_path, "Letter", events.append
    )

    assert report is None
    assert not (tmp_path / "qa_report.json").exists()
    assert events == [
        StageEvent(STAGE_QA, detail="skipped: nothing rendered to verify", skipped=True)
    ]


# --- 4: a summary that fails its coverage check refuses the run -------------


def test_a_summary_that_fails_its_coverage_check_refuses_the_run(
    tmp_path: Path, rendered: None
) -> None:
    """No record can hold a chartable kind the whole-patient view fails
    to show, so this drives the fallback instead: delete the summary file
    after render. Three checks CRASH on the missing file, and CHECK
    CRASHED findings must outrank every PASS and refuse the run
    (`qa_failed`, exit 1)."""
    export = _attachment_only_export(tmp_path)
    out = tmp_path / "charts"
    out.mkdir()
    records = list(get_source("ccda").load(export))

    summaries = _render_record_summaries(records, out, force=False)
    (summary_path,) = set(summaries.documents)
    summary_path.unlink()

    events: list[StageEvent] = []
    with pytest.raises(PipelineError) as excinfo:
        _run_qa_stage(records, RenderResult(), summaries, _engine(), out, "Letter", events.append)

    assert excinfo.value.kind == "qa_failed"
    assert excinfo.value.exit_code == 1

    written = json.loads((out / "qa_report.json").read_text(encoding="utf-8"))
    crashed = sorted(
        check["check"]
        for doc in written["documents"]
        for check in doc["checks"]
        if any("CHECK CRASHED: FileNotFoundError" in f for f in check["findings"])
    )
    assert crashed == ["data_integrity", "layout_pagination", "record_coverage"]


# --- 5: regression guard — a mixed record still merges into one report ------


def test_a_mixed_record_grades_both_populations_into_one_report(
    tmp_path: Path, rendered: None
) -> None:
    """#383 must not regress the ordinary case: a source WITH encounters
    still merges the per-encounter charts and the whole-patient summaries
    into one report, neither diluting the other. Drives `run_pipeline`
    itself — the public surface — rather than the private helpers the
    other tests here reach into."""
    out = tmp_path / "charts"

    result = run_pipeline(
        export_dir=PF_FIXTURE,
        out=out,
        source="pf-tebra",
        pack="generic_soap",
        pack_dirs=None,
        force=False,
        section=None,
        qa=True,
    )

    summaries = set((out / RECORD_SUMMARY_DIRNAME).glob("*.pdf"))
    assert len(summaries) == 3
    assert result.qa_report is not None
    graded = [doc.path for doc in result.qa_report.documents]
    assert len(graded) == 9  # 6 encounter charts + 3 whole-patient summaries
    assert len(set(graded)) == 9  # nothing graded twice
    assert summaries <= set(graded)  # the summary population is not dropped


# --- 6: --no-qa is still not_run and still refused ---------------------------


def test_no_qa_is_still_not_run_and_still_refused(tmp_path: Path, rendered: None) -> None:
    """Regression guard: passes on main. `--no-qa` leaves the bundle unverified
    regardless of which population would otherwise have graded it."""
    export = _attachment_only_export(tmp_path)
    out = tmp_path / "charts"

    run_pipeline_command(
        PipelineCommand(
            export_dir=export, charts_dir=out, source="ccda", qa=False, write_manifest=True
        )
    )

    assert not (out / "qa_report.json").exists()
    manifest = load_upload_manifest(out)
    assert manifest.gates is not None
    assert manifest.gates.qa == GATE_NOT_RUN
    with pytest.raises(DeliveryRefused, match="never verified"):
        assert_deliverable(manifest)


# --- 7: a missing PyMuPDF still downgrades QA with the skip event -----------


def test_a_missing_pymupdf_still_downgrades_qa_with_the_skip_event(
    tmp_path: Path, rendered: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one `ImportError` this stage is allowed to soften: `exc.name ==
    'pymupdf'` downgrades to a skip event and `not_run`, mirroring the
    CLI's base-install behavior. Asserts the skip EVENT itself, and the
    gate/refusal it drives — not just the report's absence, which a
    missing skip event would satisfy just as well."""
    real_import = builtins.__import__

    def _no_pymupdf_qa(name: str, *args: object, **kwargs: object) -> object:
        if name == "anastomosis.qa":
            raise ImportError("No module named 'pymupdf'", name="pymupdf")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_pymupdf_qa)
    export = _attachment_only_export(tmp_path)
    out = tmp_path / "charts"
    events: list[StageEvent] = []

    run_pipeline_command(
        PipelineCommand(export_dir=export, charts_dir=out, source="ccda", write_manifest=True),
        on_event=events.append,
    )

    qa_events = [e for e in events if e.stage == STAGE_QA]
    assert qa_events == [
        StageEvent(
            STAGE_QA, detail="skipped: install anastomosis[render] for PyMuPDF", skipped=True
        )
    ]
    assert not (out / "qa_report.json").exists()
    manifest = load_upload_manifest(out)
    assert manifest.gates is not None
    assert manifest.gates.qa == GATE_NOT_RUN
    with pytest.raises(DeliveryRefused, match="never verified"):
        assert_deliverable(manifest)


# --- the gate's per-encounter arm ------------------------------------------
#
# `_qa_gate_applies`'s `result.documents or summaries.documents` disjunction
# has exactly one live arm: `_render_record_summaries` raises on any render
# failure and otherwise returns exactly one `documents` entry per record, so
# `summaries.documents` is truthy on every real run and the per-encounter
# operand never decides anything — the gate must not refuse entry before
# `_run_qa_stage`'s own emptiness rule gets a chance to emit its skip event.


def test_a_run_that_rendered_nothing_says_so_on_the_rail(
    tmp_path: Path, rendered: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`render_ccda_standard` stubbed to return nothing, over an export
    that already renders zero per-encounter documents
    (`_attachment_only_export`): both populations are genuinely empty, and
    the gate must still enter `_run_qa_stage` and emit a skip event —
    never silently skip the stage without one."""
    monkeypatch.setattr(ccda_standard, "render_ccda_standard", lambda *_a, **_k: CCDARenderResult())
    export = _attachment_only_export(tmp_path)
    out = tmp_path / "charts"
    events: list[StageEvent] = []

    run_pipeline_command(
        PipelineCommand(export_dir=export, charts_dir=out, source="ccda", write_manifest=True),
        on_event=events.append,
    )

    qa_events = [e for e in events if e.stage == STAGE_QA]
    assert qa_events == [
        StageEvent(STAGE_QA, detail="skipped: nothing rendered to verify", skipped=True)
    ]
    assert not (out / "qa_report.json").exists()
    manifest = load_upload_manifest(out)
    assert manifest.gates is not None
    assert manifest.gates.qa == GATE_NOT_RUN
    with pytest.raises(DeliveryRefused, match="never verified"):
        assert_deliverable(manifest)


def test_a_different_import_error_is_not_softened(
    tmp_path: Path, rendered: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Companion to the test above: only `pymupdf` may downgrade QA, and
    any other `ImportError` out of the `anastomosis.qa` import must still
    propagate, never be swallowed as the same case."""
    real_import = builtins.__import__

    def _bogus_dependency(name: str, *args: object, **kwargs: object) -> object:
        if name == "anastomosis.qa":
            raise ImportError("No module named 'not_pymupdf'", name="not_pymupdf")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _bogus_dependency)
    export = _attachment_only_export(tmp_path)
    out = tmp_path / "charts"

    with pytest.raises(ImportError) as excinfo:
        _run(out, export, qa=True)
    assert excinfo.value.name == "not_pymupdf"
