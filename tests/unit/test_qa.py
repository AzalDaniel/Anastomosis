"""QA engine tests: a good document passes, and every mutation in the
corpus trips exactly the check built to catch it."""

import os
import stat
from datetime import date
from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf", reason="QA tests need PyMuPDF (render extra)")

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
    doc = pymupdf.open()
    page = doc.new_page(width=size[0], height=size[1])
    page.insert_textbox(pymupdf.Rect(36, 36, size[0] - 36, size[1] - 36), "\n".join(lines))
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


def test_write_report_hardens_its_output_dir(tmp_path: Path) -> None:
    """The report embeds findings that can carry patient names, so write_report
    secures its own output dir to 0o700 rather than trusting the caller — a
    direct caller passing an un-hardened dir must not expose PHI."""
    out = tmp_path / "unhardened"
    out.mkdir()  # default perms, NOT secured
    report = _qa(make_pdf(out / "good.pdf", GOOD_LINES))
    write_report(report, out)
    if os.name == "posix":
        assert stat.S_IMODE(out.stat().st_mode) == 0o700


def test_write_report_leaves_no_orphan_tmp_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash between the write and the atomic replace must not leave a stray
    ``.qa_report.json.<pid>.tmp`` file behind — the atomic_write_text safety
    net write_report now shares with every other atomic-write site."""
    import anastomosis.core.atomic as atomic

    report = _qa(make_pdf(tmp_path / "good.pdf", GOOD_LINES))

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(atomic.os, "replace", _boom)
    with pytest.raises(OSError):
        write_report(report, tmp_path)
    leftover = list(tmp_path.glob(".qa_report.json.*.tmp"))
    assert leftover == [], f"orphan tmp file(s) left behind: {leftover}"


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


def test_pymupdf_open_called_once_per_document_per_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All four engine checks share one PDF open per document: the runner primes a
    per-document snapshot cache, so pymupdf.open fires exactly once for the whole run
    over one document instead of once per check."""
    from anastomosis.qa import checks as qa_checks

    pdf = make_pdf(tmp_path / "good.pdf", GOOD_LINES)  # created BEFORE the counter
    calls = {"n": 0}
    real_open = qa_checks.pymupdf.open

    def counting_open(*args: object, **kwargs: object) -> object:
        calls["n"] += 1
        return real_open(*args, **kwargs)

    monkeypatch.setattr(qa_checks.pymupdf, "open", counting_open)
    report = _qa(pdf)
    assert report.documents[0].verdict is Verdict.PASS
    assert calls["n"] == 1


def test_bare_ctx_without_primed_cache_falls_back_to_opening(tmp_path: Path) -> None:
    """A third-party QA pack that builds its own QAContext (never primed by the
    runner) has no snapshot cache — the check must fall back to opening the file
    itself rather than break."""
    from anastomosis.qa.base import QAContext
    from anastomosis.qa.checks import DataIntegrityCheck

    record = _record()
    pdf = make_pdf(tmp_path / "good.pdf", GOOD_LINES)
    ctx = QAContext(encounter=record.encounters[0], record=record)  # not primed
    result = DataIntegrityCheck().run(pdf, ctx)
    assert result.verdict is Verdict.PASS


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


def test_a_chart_for_a_different_patient_does_not_pass_the_wrong_chart_check() -> None:
    """The one check whose entire job is to catch a misfiled chart.

    It matched the patient NAME with the value-boundary predicate, so a chart
    for "Mary-Ann Li-Wong" verified clean against a record for "Ann Li" — a
    different patient's chart, marked verified. The identity module keeps a
    separate name-boundary family precisely because intra-name joiners have to
    count as embedding, and both sibling verifiers (the L2/L3/L6 delivery
    verifier and the browser pack) already used it.

    The docstring above `_present` claimed the predicate "cannot drift into a
    substring-loose variant in one place and not another". Nothing enforced
    that, and it had already drifted, so the claim is now a test.

    Every name here is invented.
    """
    from anastomosis.qa.checks import _date_present, _name_present, _present

    other_patients_chart = "Chart for Mary-Ann Li-Wong. DOB 01-02-1980. Visit 05-10-2023."
    assert not _name_present("Ann Li", other_patients_chart), (
        "a chart bearing a different patient's name passed the wrong-chart check"
    )
    assert _present("Ann Li", other_patients_chart), (
        "the VALUE predicate is what let it through — if this stops being true "
        "the test above no longer proves anything"
    )

    # The right chart still passes, on either spelling of the date.
    own_chart = "Chart for Ann Li. DOB 01-02-1980. Visit 5-10-2023."
    assert _name_present("Ann Li", own_chart)
    assert _date_present("01-02-1980", own_chart)


def test_qa_matches_a_name_the_same_way_every_other_verifier_does() -> None:
    """One family per kind of field, across all three consumers.

    A comment is not a mechanism. This reads the imports off the syntax tree,
    so a fourth consumer — or a regression in one of these three — shows up
    here rather than as a chart filed under the wrong patient.
    """
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src" / "anastomosis"
    consumers = {
        "qa/checks.py": {"name_fragment_present", "date_token_present"},
        "deliver/verify/levels.py": {"name_fragment_present", "date_token_present"},
        "destinations/browserpack.py": {"name_parts_present", "date_token_present"},
    }
    for relative, required in consumers.items():
        tree = ast.parse((src / relative).read_text(encoding="utf-8"))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "anastomosis.core.identity"
            for alias in node.names
        }
        missing = required - imported
        assert not missing, (
            f"{relative} no longer matches through {sorted(missing)} — a name "
            "matched with the value predicate is how a chart for one patient "
            "verifies clean against another"
        )
