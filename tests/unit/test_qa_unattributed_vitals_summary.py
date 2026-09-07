"""#392: a vital carried by the rendered summary is on a chart. Five
vitals with no encounter link stay record-level, but the whole-patient
record summary (#239) carries them — so `UnattributedVitalsCheck` must
not grade them as being on no chart at all.

Driven synthetically with a real `ReconstructionEngine` through
`FakeChromium` and the real QA stage; reuses `_engine()` from
`test_qa_no_encounters.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _render_fakes import FakeChromium
from test_qa_no_encounters import _engine

import anastomosis.reconstruct.chromium as chromium
from anastomosis.core.model import (
    Encounter,
    Observation,
    ObservationCategory,
    Patient,
    PatientRecord,
)
from anastomosis.deliver.browser.gates import GATE_PASS, RunGates, assert_deliverable
from anastomosis.deliver.browser.persist import load_upload_manifest, write_upload_manifest
from anastomosis.pipeline import RECORD_SUMMARY_DIRNAME, _render_record_summaries, _run_qa_stage
from anastomosis.qa.base import Verdict

PATIENT_ID = "feedface-0000-0000-0000-000000000392"
ENCOUNTER_ID = "feedface-e000-0000-0000-000000000392"

#: Five vitals, LOINC-coded, exactly as the audit's own probe used elsewhere
#: in this suite — no encounter link, mirroring the linker's honest refusal
#: to attribute them to a visit with no date.
_VITAL_CODES = (
    ("8480-6", "Systolic blood pressure", "118"),
    ("8462-4", "Diastolic blood pressure", "76"),
    ("8867-4", "Heart rate", "72"),
    ("9279-1", "Respiratory rate", "16"),
    ("8310-5", "Body temperature", "98.6"),
)


@pytest.fixture
def rendered(monkeypatch: pytest.MonkeyPatch) -> None:
    """A browserless run: the fake renderer writes real PDFs for QA to read."""
    pytest.importorskip("pymupdf", reason="the fake renderer writes a real PDF")
    monkeypatch.setattr(chromium, "ChromiumRenderer", FakeChromium)


def _kareo_shaped_record() -> PatientRecord:
    """One undated encounter, five vitals attached to none of them — the
    shape #392 was filed over, entirely synthetic."""
    patient = Patient(id=PATIENT_ID, given_name="Wren", family_name="Ashgrove", birth_date=None)
    encounter = Encounter(id=ENCOUNTER_ID, patient_id=patient.id, date_of_service=None)
    observations = [
        Observation(
            patient_id=patient.id,
            encounter_id=None,
            category=ObservationCategory.VITAL_SIGNS,
            code=code,
            display=display,
            value=value,
        )
        for code, display, value in _VITAL_CODES
    ]
    return PatientRecord(patient=patient, encounters=[encounter], observations=observations)


def test_the_kareo_shaped_record_passes_qa_with_the_vitals_warned_not_failed(
    tmp_path: Path, rendered: None
) -> None:
    """2 documents FAIL on `unattributed_vitals` would read `gates.qa`
    as `fail` and refuse delivery; instead 0 fail and `gates.qa` reads
    `pass`, since `QAReport.ok` only counts FAILs and a WARN never
    blocks it."""
    record = _kareo_shaped_record()
    engine = _engine()
    out = tmp_path / "charts"

    render_result = engine.run([record], out)
    assert len(render_result.rendered) == 1  # the one undated encounter still renders
    assert not render_result.failed

    summaries = _render_record_summaries([record], out, force=False)
    assert len(list((out / RECORD_SUMMARY_DIRNAME).glob("*.pdf"))) == 1

    events: list[object] = []
    report = _run_qa_stage([record], render_result, summaries, engine, out, "Letter", events.append)

    assert report is not None
    assert report.ok  # exit 0: a WARN does not refuse a bundle that carries the value
    assert report.count(Verdict.FAIL) == 0
    # Two documents graded: the one per-encounter chart, and the record summary.
    assert len(report.documents) == 2
    assert report.count(Verdict.WARN) == 2

    for doc in report.documents:
        result = next(r for r in doc.results if r.check == "unattributed_vitals")
        assert result.verdict is Verdict.WARN
        assert result.findings == [
            "5 vital(s) are on no encounter, so no chart carries the visit link, "
            "but the value is on the record summary"
        ]

    # `gates.qa` and `assert_deliverable`, read from the real functions they
    # come from — not re-derived here — over a manifest the real writer wrote.
    gates = RunGates.from_run(qa_ok=report.ok, layout_hash=None)
    assert gates.qa == GATE_PASS
    write_upload_manifest(render_result.documents, [record], out, pack="generic_soap", gates=gates)
    manifest = load_upload_manifest(out)
    assert manifest.gates is not None
    assert manifest.gates.qa == GATE_PASS
    assert_deliverable(manifest)  # must not raise
