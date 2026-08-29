"""QA engine tests: a good document passes, and every mutation in the
corpus trips exactly the check built to catch it."""

import os
import stat
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf", reason="QA tests need PyMuPDF (render extra)")

from anastomosis.core.model import (  # noqa: E402
    Addendum,
    Encounter,
    NoteSection,
    Observation,
    ObservationCategory,
    Patient,
    PatientRecord,
    SectionKind,
)
from anastomosis.core.timeutil import to_local  # noqa: E402
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
    assert set(names) >= {
        "data_integrity",
        "date_staleness",
        "layout_pagination",
        "note_body",
        "unattributed_vitals",
        "vitals_loinc",
    }


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


# UTC+14 and UTC-12, the two ends of the inhabited offset range. They are 26
# hours apart, so at every instant at least one of them is on a different
# calendar day than the machine running the tests — which is what makes the two
# cases below asymmetric. A check that quietly reads the host's day cannot
# satisfy both, wherever the suite happens to run.
_FAR_APART_ZONES = ("Etc/GMT-14", "Etc/GMT+12")


def _day_in(zone: str) -> date:
    return to_local(datetime.now(UTC), zone).date()


@pytest.mark.parametrize("zone", _FAR_APART_ZONES)
def test_staleness_reads_the_day_the_pack_rendered_in(tmp_path: Path, zone: str) -> None:
    """A render-day stamp is stale where the chart was rendered, not where the
    operator is sitting.

    The packs stamp their "as of" dates in the pack's timezone, so a check
    reading the host's day agreed with them only by luck of geography: one
    byte-identical chart, carrying its own render-day stamp, warned on a machine
    twelve hours west and passed on one in the practice's own zone.
    """
    stamp = _day_in(zone).strftime("%B %d, %Y")
    record = _record()
    pdf = make_pdf(tmp_path / "m.pdf", [*GOOD_LINES, f"Current Medications (as of {stamp})"])
    report = run_qa([(pdf, record.encounters[0], record)], render_tz=zone)
    verdict, findings = _result(report, "date_staleness")
    assert verdict is Verdict.WARN
    assert findings


def test_staleness_stops_asking_the_host_once_it_has_the_render_clock(tmp_path: Path) -> None:
    """The same divergence seen from the other side: with the pack's clock in
    hand, a date that is today only HERE is just a date on an old chart, and
    reporting it would put the operator's location back in the verdict."""
    host_day = date.today()
    zone = next(z for z in _FAR_APART_ZONES if _day_in(z) != host_day)
    stamp = host_day.strftime("%B %d, %Y")
    record = _record()
    pdf = make_pdf(tmp_path / "m.pdf", [*GOOD_LINES, f"Printed {stamp}"])
    report = run_qa([(pdf, record.encounters[0], record)], render_tz=zone)
    verdict, findings = _result(report, "date_staleness")
    assert verdict is Verdict.PASS
    assert not findings


def test_the_pipeline_hands_qa_the_clock_the_engine_renders_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check can only share the pack's clock if the QA stage passes it, so
    the wiring is pinned here rather than assumed. Read off the engine, not off
    the manifest a second time — two readings are how the pack and its QA drift
    back apart."""
    import anastomosis.qa as qa_module
    from anastomosis.pipeline import _run_qa_stage
    from anastomosis.reconstruct import discover_packs
    from anastomosis.reconstruct.engine import ReconstructionEngine, RenderResult

    status = discover_packs()["generic_soap"]
    assert status.pack is not None, status.diagnosis
    engine = ReconstructionEngine(status.pack, lambda: None)  # never rendered here

    captured: dict[str, object] = {}

    def fake_run_qa(documents: object, **kwargs: object) -> QAReport:
        captured.update(kwargs)
        return QAReport()

    monkeypatch.setattr(qa_module, "run_qa", fake_run_qa)
    _run_qa_stage([], RenderResult(), engine, tmp_path, "Letter", lambda event: None)
    assert captured["render_tz"] == status.pack.manifest.timezone


def test_pymupdf_open_called_once_per_document_per_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every engine check shares one PDF open per document: the runner primes a
    per-document snapshot cache, so pymupdf.open fires exactly once for the whole run
    over one document instead of once per check.

    Counted on the pymupdf module itself rather than through `qa.checks`, which
    no longer holds it as an attribute — the import moved inside the one
    function that opens a PDF so that an install without the `render` extra can
    still import the archive deliverer. A function-local import resolves through
    `sys.modules`, so patching the module is what the checks actually see.
    """
    import pymupdf

    pdf = make_pdf(tmp_path / "good.pdf", GOOD_LINES)  # created BEFORE the counter
    calls = {"n": 0}
    real_open = pymupdf.open

    def counting_open(*args: object, **kwargs: object) -> object:
        calls["n"] += 1
        return real_open(*args, **kwargs)

    monkeypatch.setattr(pymupdf, "open", counting_open)
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


# --- note_body ---------------------------------------------------------------
#
# Four checks verified the header, the geometry, the vitals and the date stamp,
# and none of them read the note. A chart with every word of Subjective,
# Objective, Assessment and Plan removed — headings and vitals table intact —
# passed all four with `6 pass, 0 warn, 0 fail` and exit 0. The note is the one
# field family the README says routinely fails to survive a migration.

_SUBJECTIVE = "Patient reports a dull ache in the left shoulder for the past three weeks."
_PLAN = "Start physiotherapy twice weekly and review in one month."


def _record_with_note() -> PatientRecord:
    record = _record()
    record.encounters[0].sections = [
        NoteSection(kind=SectionKind.SUBJECTIVE, text=_SUBJECTIVE),
        NoteSection(kind=SectionKind.PLAN, text=_PLAN),
    ]
    return record


def _qa_note(pdf: Path) -> QAReport:
    record = _record_with_note()
    return run_qa([(pdf, record.encounters[0], record)])


def test_note_body_passes_when_the_note_is_on_the_page(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "with_note.pdf", [*GOOD_LINES, _SUBJECTIVE, _PLAN])
    verdict, findings = _result(_qa_note(pdf), "note_body")
    assert (verdict, findings) == (Verdict.PASS, [])


def test_note_body_fails_when_the_note_is_gone_but_everything_else_is_there(
    tmp_path: Path,
) -> None:
    """The exact chart that used to pass every check: identity, vitals, dates and
    the section HEADINGS all present, the bodies all absent."""
    pdf = make_pdf(tmp_path / "hollow.pdf", [*GOOD_LINES, "SUBJECTIVE", "PLAN"])
    report = _qa_note(pdf)
    verdict, findings = _result(report, "note_body")
    assert verdict is Verdict.FAIL
    assert findings == [
        "the subjective section is not on the document",
        "the plan section is not on the document",
    ]
    # The document as a whole must not pass — that is the bug.
    assert not report.ok
    # And every OTHER check still passes, which is why this went unnoticed.
    for check in ("data_integrity", "layout_pagination", "vitals_loinc"):
        assert _result(report, check)[0] is Verdict.PASS


def test_a_partly_present_note_warns_rather_than_fails(tmp_path: Path) -> None:
    """A long section legitimately straddles a page break and picks up a footer
    in the extracted text, so a partial match is not proof of loss. Truncation
    looks the same from here, and telling the two apart is a person's job —
    so it is surfaced, not adjudicated."""
    long_note = " ".join(f"finding number {n} recorded at the visit" for n in range(1, 13))
    record = _record()
    record.encounters[0].sections = [NoteSection(kind=SectionKind.SUBJECTIVE, text=long_note)]
    kept = " ".join(long_note.split()[:24])  # the first three chunks of six
    pdf = make_pdf(tmp_path / "partial.pdf", [*GOOD_LINES, kept])

    report = run_qa([(pdf, record.encounters[0], record)])
    verdict, findings = _result(report, "note_body")
    assert verdict is Verdict.WARN
    assert len(findings) == 1
    assert "only partly on the document" in findings[0]


def test_note_body_findings_never_quote_the_note(tmp_path: Path) -> None:
    """A finding travels into logs and run reports. It names the section; the
    body is the patient's chart and stays on the chart."""
    pdf = make_pdf(tmp_path / "hollow.pdf", GOOD_LINES)
    _, findings = _result(_qa_note(pdf), "note_body")
    blob = " ".join(findings)
    assert findings
    for word in set(_SUBJECTIVE.split()) | set(_PLAN.split()):
        if len(word) > 4:  # skip articles and prepositions that any sentence has
            assert word not in blob, f"note text leaked into a finding: {word!r}"


def test_an_addendum_is_verified_like_a_section(tmp_path: Path) -> None:
    """An amendment to a signed note is the part a clinician added deliberately;
    losing it silently is the same defect as losing the note."""
    record = _record()
    record.encounters[0].addenda = [Addendum(text="Corrected: the ache is on the RIGHT shoulder.")]
    pdf = make_pdf(tmp_path / "no_addendum.pdf", GOOD_LINES)

    verdict, findings = _result(run_qa([(pdf, record.encounters[0], record)]), "note_body")
    assert verdict is Verdict.FAIL
    assert findings == ["addendum 1 is not on the document"]


def test_an_encounter_with_no_narrative_passes_and_says_it_was_vacuous(tmp_path: Path) -> None:
    """Not every encounter carries a note. Having nothing to verify is a pass,
    not a warning — the check must not become noise on ordinary charts.

    But it says so. A bare pass over a check that found nothing to check reads,
    in a report, exactly like a pass over a chart it verified, which is how a
    run that dropped a record came back with five green lines under it.
    """
    verdict, findings = _result(_qa(make_pdf(tmp_path / "plain.pdf", GOOD_LINES)), "note_body")
    assert verdict is Verdict.PASS
    assert findings == ["this encounter carries no narrative to verify"]


def test_a_section_switched_off_is_not_reported_missing(tmp_path: Path) -> None:
    """A section the operator disabled is absent on purpose.

    Caught by the gate rather than by inspection: `addenda` is a declared flag
    in the bundled packs, so a GUI run with it off rendered charts without the
    addendum and this check called every one of them a loss — turning a
    deliberate choice into a failed run.
    """
    record = _record()
    record.encounters[0].addenda = [Addendum(text="Corrected: the ache is on the RIGHT shoulder.")]
    pdf = make_pdf(tmp_path / "no_addendum.pdf", GOOD_LINES)

    on = run_qa([(pdf, record.encounters[0], record)], section_flags={"addenda": True})
    off = run_qa([(pdf, record.encounters[0], record)], section_flags={"addenda": False})

    assert _result(on, "note_body")[0] is Verdict.FAIL
    off_verdict, off_findings = _result(off, "note_body")
    assert off_verdict is Verdict.PASS
    assert off_findings == ["this encounter carries no narrative to verify"]


def test_a_vital_on_no_encounter_is_caught_by_the_check_the_others_cannot_be(
    tmp_path: Path,
) -> None:
    """The gap the external audit walked straight through.

    Every other check starts from what the encounter claims, so a record whose
    observations name no encounter gives them nothing to compare and they all
    pass. The audit's probe found exactly that: eight vitals in the record, a
    chart carrying none of them, five green ticks. So this asserts the two
    facts together — the old checks still pass, and the new one does not.
    """
    record = _record()
    record.observations[0].encounter_id = None
    pdf = make_pdf(tmp_path / "silent.pdf", GOOD_LINES)
    report = run_qa([(pdf, record.encounters[0], record)])

    assert _result(report, "vitals_loinc")[0] is Verdict.PASS, "it looks only at the encounter"
    verdict, findings = _result(report, "unattributed_vitals")
    assert verdict is Verdict.FAIL
    assert findings == ["vital Systolic blood pressure is on no encounter, so it is on no chart"]


def test_an_observation_naming_a_visit_that_is_not_there_fails(tmp_path: Path) -> None:
    """Worse than the unattached case, because it looks attached.

    The value names an encounter, so nothing about it reads as orphaned — and
    the encounter does not exist, so no chart renders it either. Caught for
    every category, not just vitals: a lab result pointing at a visit this
    record does not have is broken whatever it measures.
    """
    record = _record()
    record.observations[1].encounter_id = "feedface-dead-0000-0000-00000000beef"
    record.observations[1].category = ObservationCategory.LABORATORY
    pdf = make_pdf(tmp_path / "dangling.pdf", GOOD_LINES)

    verdict, findings = _result(
        run_qa([(pdf, record.encounters[0], record)]), "unattributed_vitals"
    )
    assert verdict is Verdict.FAIL
    assert findings == ["Heart rate names an encounter this record does not have"]


def test_a_social_history_observation_on_no_encounter_is_not_a_finding(tmp_path: Path) -> None:
    """The normal case, and the reason this check is narrow.

    A smoking status is a fact about the patient rather than something measured
    at an appointment, and the C-CDA linker leaves it unattached on purpose.
    Reporting it would put a finding on the chart of every patient ever asked
    about tobacco — and a check that fires on the ordinary case is one an
    operator stops reading, which costs more than it catches.
    """
    record = _record()
    record.observations.append(
        Observation(
            patient_id=record.patient.id,
            encounter_id=None,
            category=ObservationCategory.SOCIAL_HISTORY,
            display="Tobacco use",
            value="Never smoker",
        )
    )
    pdf = make_pdf(tmp_path / "social.pdf", GOOD_LINES)

    assert _result(run_qa([(pdf, record.encounters[0], record)]), "unattributed_vitals") == (
        Verdict.PASS,
        [],
    )


def test_the_ccda_view_reports_every_check_the_neutral_path_does() -> None:
    """A check must never fall out of the standard-C-CDA report unnoticed.

    That path runs two document-generic checks and records the encounter-scoped
    ones as skipped-with-reason, and its own comment promises the report shows
    the same check set as the neutral path. Nothing enforced it, so when
    `note_body` was registered it landed in neither table and was silently
    omitted from every whole-patient report — the exact thing the promise ruled
    out. Registering a check is now enough to be reminded to place it.
    """
    from anastomosis.core.migrate import _CCDA_DOC_CHECKS, _CCDA_SKIPPED_CHECKS

    placed = set(_CCDA_DOC_CHECKS) | set(_CCDA_SKIPPED_CHECKS)
    registered = {check.name for check in engine_checks()}
    assert registered - placed == set(), "a registered check named in neither table"
    assert placed - registered == set(), "a table names a check that is not registered"
    assert set(_CCDA_DOC_CHECKS).isdisjoint(_CCDA_SKIPPED_CHECKS), "run it or skip it, not both"


# --- record coverage: did the chart carry the record? ------------------------
#
# The gap this closes: every other check reads the page and asks whether what is
# there is well-formed, so a chart that dropped almost everything passed all of
# them. These tests come in pairs — the same record against a page that carries
# it and a page that does not — because a coverage check that cannot tell those
# two apart is the bug, not the fix.

COVERAGE_LINES = [
    *GOOD_LINES,
    "Type 2 diabetes mellitus",
    "Penicillin G",
    "Lisinopril 10 mg tablet",
    "Influenza, seasonal, injectable",
    "Hemoglobin A1c",
]


def _covered_record() -> PatientRecord:
    from anastomosis.core.model import (
        AllergyIntolerance,
        Condition,
        Immunization,
        MedicationStatement,
    )

    record = _record()
    pid = record.patient.id
    record.conditions.append(Condition(patient_id=pid, display="Type 2 diabetes mellitus"))
    record.allergies.append(AllergyIntolerance(patient_id=pid, substance="Penicillin G"))
    record.medications.append(
        MedicationStatement(patient_id=pid, display_name="Lisinopril 10 mg tablet")
    )
    record.immunizations.append(
        Immunization(patient_id=pid, vaccine="Influenza, seasonal, injectable")
    )
    record.observations.append(
        Observation(
            patient_id=pid,
            encounter_id=ENC,
            category=ObservationCategory.LABORATORY,
            code="4548-4",
            display="Hemoglobin A1c",
            value="6.1",
        )
    )
    return record


ALL_KINDS = frozenset({"conditions", "allergies", "medications", "immunizations", "results"})


def _coverage(
    pdf: Path,
    record: PatientRecord,
    *,
    carries: frozenset[str] | None = None,
    omits: dict[str, str] | None = None,
) -> tuple[Verdict, list[str], int]:
    report = run_qa(
        [(pdf, record.encounters[0], record)],
        carries=carries,
        omits=omits,
        checks=[c for c in engine_checks() if c.name == "record_coverage"],
    )
    result = report.documents[0].results[0]
    return result.verdict, result.findings, report.not_carried


def test_coverage_passes_when_the_chart_carries_the_record(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "full.pdf", COVERAGE_LINES)
    verdict, findings, not_carried = _coverage(pdf, _covered_record(), carries=ALL_KINDS)
    assert verdict is Verdict.PASS, findings
    assert findings == []
    assert not_carried == 0


def test_coverage_fails_when_a_carried_kind_reaches_no_page(tmp_path: Path) -> None:
    """The #239 shape: the record holds five kinds, the page holds none of them,
    and before this check every verdict was green."""
    pdf = make_pdf(tmp_path / "empty.pdf", GOOD_LINES)  # header + vitals only
    verdict, findings, _ = _coverage(pdf, _covered_record(), carries=ALL_KINDS)
    assert verdict is Verdict.FAIL
    for kind in sorted(ALL_KINDS):
        assert any(f"none of the 1 {kind}" in f for f in findings), (kind, findings)


def test_coverage_of_an_undeclared_pack_warns_rather_than_fails(tmp_path: Path) -> None:
    """No statement from the pack means the check cannot tell a lost section
    from a layout that never had one — so it says both, loudly, and softens."""
    pdf = make_pdf(tmp_path / "empty.pdf", GOOD_LINES)
    verdict, findings, _ = _coverage(pdf, _covered_record())
    assert verdict is Verdict.WARN
    assert any("does not say what its layout carries" in f for f in findings)


def test_coverage_counts_a_declared_omission_instead_of_grading_it_clean(tmp_path: Path) -> None:
    """A layout with no problem list is not a defect. It is also not nothing:
    the count travels to the run summary so a clean report cannot mean a chart
    that dropped the record."""
    pdf = make_pdf(tmp_path / "note.pdf", GOOD_LINES)
    omits = {kind: f"no {kind} block in this layout" for kind in sorted(ALL_KINDS)}
    verdict, findings, not_carried = _coverage(pdf, _covered_record(), omits=omits)
    assert verdict is Verdict.PASS
    assert not_carried == 5
    assert any("1 conditions not carried by this layout" in f for f in findings)


def test_coverage_says_so_when_the_record_holds_nothing_to_compare(tmp_path: Path) -> None:
    """A check that finds nothing to check must say so, or absence of data
    reads as presence of quality."""
    pdf = make_pdf(tmp_path / "bare.pdf", GOOD_LINES)
    verdict, findings, _ = _coverage(pdf, _record(), carries=ALL_KINDS)
    assert verdict is Verdict.PASS
    assert any("nothing to compare" in f for f in findings)


def test_coverage_reports_items_it_cannot_look_for(tmp_path: Path) -> None:
    """A medication with no name at all is still a medication the record holds.
    Counting only the nameable ones would let a source that lost every label
    report full coverage."""
    from anastomosis.core.model import MedicationStatement

    record = _record()
    record.medications.append(MedicationStatement(patient_id=record.patient.id))
    pdf = make_pdf(tmp_path / "nameless.pdf", GOOD_LINES)
    verdict, findings, _ = _coverage(pdf, record, carries=ALL_KINDS)
    assert verdict is Verdict.PASS  # nothing to look for is not a failure to find
    assert any("carry no description or code" in f for f in findings)


def test_coverage_does_not_accept_a_label_embedded_in_another_word(tmp_path: Path) -> None:
    """ "Fever" must not be satisfied by "Fever blister" — the same boundary
    reasoning as the identity predicates, one step down in stakes."""
    from anastomosis.core.model import Condition

    record = _record()
    record.conditions.append(Condition(patient_id=record.patient.id, display="Fever"))
    pdf = make_pdf(tmp_path / "embedded.pdf", [*GOOD_LINES, "Fevers blistering"])
    verdict, findings, _ = _coverage(pdf, record, carries=ALL_KINDS)
    assert verdict is Verdict.FAIL
    assert any("none of the 1 conditions" in f for f in findings)


def test_vitals_check_says_when_it_had_nothing_to_verify(tmp_path: Path) -> None:
    """The vacuous pass #239 named: the check for vitals graded a chart with no
    vitals on it as correct, because it validated the values it found and found
    none."""
    record = PatientRecord(patient=_record().patient, encounters=_record().encounters)
    report = run_qa(
        [(make_pdf(tmp_path / "novitals.pdf", GOOD_LINES), record.encounters[0], record)]
    )
    verdict, findings = _result(report, "vitals_loinc")
    assert verdict is Verdict.PASS
    assert findings == ["no vital signs on this encounter to verify"]
