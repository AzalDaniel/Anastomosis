"""QA engine tests: a good document passes, and every mutation in the
corpus trips exactly the check built to catch it."""

from datetime import date
from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz", reason="QA tests need PyMuPDF (render extra)")

from anastomosis.core.model import (  # noqa: E402
    Encounter,
    Observation,
    ObservationCategory,
    Patient,
    PatientRecord,
)
from anastomosis.qa import (  # noqa: E402
    QAReport,
    Verdict,
    engine_checks,
    run_qa,
    write_report,
)

ENC = "feedface-e000-0000-0000-0000000000aa"

GOOD_LINES = [
    "Synthia Probe",
    "DOB 01/02/1980",
    "Date of service: May 10, 2023",
    "Blood pressure 118 / 76 mmHg",
    "Heart rate 72 bpm",
]


def _record() -> PatientRecord:
    patient = Patient(
        id="feedface-0000-0000-0000-0000000000aa",
        given_name="Synthia",
        family_name="Probe",
        birth_date=date(1980, 1, 2),
    )
    return PatientRecord(
        patient=patient,
        encounters=[Encounter(id=ENC, patient_id=patient.id, date_of_service=date(2023, 5, 10))],
        observations=[
            Observation(
                patient_id=patient.id,
                encounter_id=ENC,
                category=ObservationCategory.VITAL_SIGNS,
                code="8480-6",
                display="Systolic blood pressure",
                value="118",
            ),
            Observation(
                patient_id=patient.id,
                encounter_id=ENC,
                category=ObservationCategory.VITAL_SIGNS,
                code="8867-4",
                display="Heart rate",
                value="72",
            ),
        ],
    )


def make_pdf(
    path: Path,
    lines: list[str],
    *,
    size: tuple[float, float] = (612, 792),
    extra_blank_page: bool = False,
) -> Path:
    doc = fitz.open()
    page = doc.new_page(width=size[0], height=size[1])
    page.insert_textbox(fitz.Rect(36, 36, size[0] - 36, size[1] - 36), "\n".join(lines))
    if extra_blank_page:
        doc.new_page(width=size[0], height=size[1])
    doc.save(str(path))
    doc.close()
    return path


def _qa(pdf: Path) -> QAReport:
    record = _record()
    return run_qa([(pdf, record.encounters[0], record)])


def _result(report: QAReport, check: str) -> tuple[Verdict, list[str]]:
    result = next(r for r in report.documents[0].results if r.check == check)
    return result.verdict, result.findings


def test_engine_checks_registered() -> None:
    names = [c.name for c in engine_checks()]
    assert names == sorted(names)
    assert set(names) >= {"data_integrity", "layout_pagination", "vitals_loinc", "date_staleness"}


def test_good_document_passes_everything(tmp_path: Path) -> None:
    report = _qa(make_pdf(tmp_path / "good.pdf", GOOD_LINES))
    assert report.ok
    assert report.documents[0].verdict is Verdict.PASS


def test_mutation_missing_dob_fails_data_integrity(tmp_path: Path) -> None:
    lines = [ln for ln in GOOD_LINES if "DOB" not in ln]
    verdict, findings = _result(_qa(make_pdf(tmp_path / "m.pdf", lines)), "data_integrity")
    assert verdict is Verdict.FAIL
    assert any("date of birth" in f for f in findings)


def test_mutation_wrong_patient_fails_data_integrity(tmp_path: Path) -> None:
    lines = ["Someone Else", *GOOD_LINES[1:]]
    verdict, findings = _result(_qa(make_pdf(tmp_path / "m.pdf", lines)), "data_integrity")
    assert verdict is Verdict.FAIL
    assert any("Synthia Probe" in f for f in findings)


def test_mutation_blank_page_fails_layout(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "m.pdf", GOOD_LINES, extra_blank_page=True)
    verdict, findings = _result(_qa(pdf), "layout_pagination")
    assert verdict is Verdict.FAIL
    assert any("blank" in f for f in findings)


def test_mutation_wrong_page_size_warns_layout(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "m.pdf", GOOD_LINES, size=(595, 842))  # A4, expected Letter
    verdict, findings = _result(_qa(pdf), "layout_pagination")
    assert verdict is Verdict.WARN
    assert any("expected Letter" in f for f in findings)


def test_mutation_missing_vital_fails_vitals(tmp_path: Path) -> None:
    lines = [ln for ln in GOOD_LINES if "118" not in ln]
    verdict, findings = _result(_qa(make_pdf(tmp_path / "m.pdf", lines)), "vitals_loinc")
    assert verdict is Verdict.FAIL
    assert any("Systolic" in f for f in findings)


def test_mutation_render_day_date_warns_staleness(tmp_path: Path) -> None:
    today = date.today().strftime("%B %d, %Y")
    pdf = make_pdf(tmp_path / "m.pdf", [*GOOD_LINES, f"Printed {today}"])
    verdict, findings = _result(_qa(pdf), "date_staleness")
    assert verdict is Verdict.WARN
    assert findings


def test_corrupt_pdf_is_check_failure_not_batch_abort(tmp_path: Path) -> None:
    bad = tmp_path / "corrupt.pdf"
    bad.write_bytes(b"this is not a pdf")
    report = _qa(bad)
    assert not report.ok
    findings = [f for r in report.documents[0].results for f in r.findings]
    assert any("CHECK CRASHED" in f for f in findings)
    # Crash findings carry the exception type only, never its message.
    assert all("not a pdf" not in f for f in findings)


def test_disabled_vitals_section_skips_vitals_check(tmp_path: Path) -> None:
    record = _record()
    pdf = make_pdf(tmp_path / "m.pdf", [ln for ln in GOOD_LINES if "118" not in ln])
    report = run_qa([(pdf, record.encounters[0], record)], section_flags={"vitals": False})
    verdict, findings = _result(report, "vitals_loinc")
    assert verdict is Verdict.PASS
    assert any("disabled" in f for f in findings)


def test_report_json(tmp_path: Path) -> None:
    import json

    report = _qa(make_pdf(tmp_path / "good.pdf", GOOD_LINES))
    target = write_report(report, tmp_path)
    payload = json.loads(target.read_text())
    assert payload["summary"]["pass"] == 1
    assert payload["documents"][0]["verdict"] == "pass"
    assert payload["documents"][0]["encounter_id"] == ENC


def test_worst_verdict_wins_mixed_warn_and_fail(tmp_path: Path) -> None:
    # Wrong page size alone → WARN; missing DOB alone → FAIL; together the
    # document verdict must be FAIL (and report.ok false): exit-code gating
    # rides on this aggregation.
    warn_only = _qa(make_pdf(tmp_path / "w.pdf", GOOD_LINES, size=(595, 842)))
    assert warn_only.documents[0].verdict is Verdict.WARN
    assert warn_only.ok  # warnings don't block

    mixed = _qa(
        make_pdf(
            tmp_path / "m.pdf",
            [ln for ln in GOOD_LINES if "DOB" not in ln],
            size=(595, 842),
        )
    )
    assert mixed.documents[0].verdict is Verdict.FAIL
    assert not mixed.ok


def test_vital_value_hiding_inside_other_numbers_is_not_found(tmp_path: Path) -> None:
    # Regression for the substring false-PASS: "72" inside "ID 9872X" and
    # inside the DOB year must not satisfy the heart-rate check.
    lines = [ln for ln in GOOD_LINES if "Heart rate" not in ln] + ["ID 9872X"]
    verdict, findings = _result(_qa(make_pdf(tmp_path / "m.pdf", lines)), "vitals_loinc")
    assert verdict is Verdict.FAIL
    assert any("Heart rate" in f for f in findings)


def test_name_embedded_in_longer_name_is_not_a_match(tmp_path: Path) -> None:
    lines = ["MarySynthia Probeworth", *GOOD_LINES[1:]]
    verdict, _ = _result(_qa(make_pdf(tmp_path / "m.pdf", lines)), "data_integrity")
    assert verdict is Verdict.FAIL


def test_unpadded_dob_inside_different_date_is_not_a_match(tmp_path: Path) -> None:
    # Record DOB 1/2/1980 must not match a chart showing 11/2/1980.
    lines = ["DOB 11/2/1980", *[ln for ln in GOOD_LINES if "DOB" not in ln]]
    verdict, findings = _result(_qa(make_pdf(tmp_path / "m.pdf", lines)), "data_integrity")
    assert verdict is Verdict.FAIL
    assert any("date of birth" in f for f in findings)


def test_staleness_catches_generic_soap_signature_format(tmp_path: Path) -> None:
    # The built-in pack renders datetimes as unpadded "%b %d, %Y" — the
    # staleness check must recognize that spelling of the render day.
    today = date.today()
    stamp = f"{today.strftime('%b')} {today.day}, {today.year}"
    pdf = make_pdf(tmp_path / "m.pdf", [*GOOD_LINES, f"Electronically signed on {stamp}"])
    verdict, findings = _result(_qa(pdf), "date_staleness")
    assert verdict is Verdict.WARN
    assert findings


def test_record_without_identity_anchors_warns(tmp_path: Path) -> None:
    from anastomosis.core.model import Encounter, Patient, PatientRecord

    anonymous = PatientRecord(
        patient=Patient(id="feedface-0000-0000-0000-0000000000ab"),
        encounters=[
            Encounter(
                id="feedface-e000-0000-0000-0000000000ab",
                patient_id="feedface-0000-0000-0000-0000000000ab",
            )
        ],
    )
    pdf = make_pdf(tmp_path / "anon.pdf", ["An unattributable document"])
    report = run_qa([(pdf, anonymous.encounters[0], anonymous)])
    result = next(r for r in report.documents[0].results if r.check == "data_integrity")
    assert result.verdict is Verdict.WARN
    assert any("identity anchors" in f for f in result.findings)
