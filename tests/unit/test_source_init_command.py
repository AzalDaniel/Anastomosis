"""The shared learn-a-source command core (one flow, two frontends).

Pins the shared-core consolidation: ``resolve_example`` and the analyze -> confirm
-> build -> round-trip -> save flow live here once, returning enumerated codes
both frontends present. The CLI (`test_cli_source`) and GUI
(`test_gui_controller`) suites exercise the adapters; this pins the core directly.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from anastomosis.core.source_init_command import (
    SourceInitCommand,
    resolve_example,
    run_source_init_command,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "learned" / "clinic_visits.csv"


def test_resolve_example_file_dir_and_ambiguous(tmp_path: Path) -> None:
    assert resolve_example(FIXTURE) == (FIXTURE, "")
    assert resolve_example(tmp_path / "nope") == (None, "NoExampleFile")

    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "notes.txt").write_text("x", encoding="utf-8")  # not a learnable type
    assert resolve_example(empty) == (None, "NoExampleFile")

    one = tmp_path / "one"
    one.mkdir()
    shutil.copy(FIXTURE, one / "export.csv")
    assert resolve_example(one) == (one / "export.csv", "")

    two = tmp_path / "two"
    two.mkdir()
    shutil.copy(FIXTURE, two / "a.csv")
    shutil.copy(FIXTURE, two / "b.csv")
    assert resolve_example(two) == (None, "AmbiguousExample")


def test_invalid_name_is_rejected_before_analysis() -> None:
    result = run_source_init_command(SourceInitCommand(example=FIXTURE, name="Bad-Name"))
    assert result.ok is False
    assert result.error == "InvalidSourceName"
    assert result.fmt_type is None  # never analyzed


def test_confirmation_required_returns_phi_safe_proposal(tmp_path: Path) -> None:
    result = run_source_init_command(
        SourceInitCommand(example=FIXTURE, name="clinic_csv", out_dir=tmp_path, confirmed=False)
    )
    assert result.ok is False
    assert result.error == "ConfirmationRequired"
    assert result.patient_key == "PatientID"
    assert result.fmt_type is not None
    assert result.columns > 0
    assert result.suggestions  # at least one column suggestion
    # PHI probe: the proposal carries column names/types/masked shapes only.
    blob = repr(result)
    for leak in ("Ada", "900-12-3456", "ada@example.com"):
        assert leak not in blob
    assert not (tmp_path / "clinic_csv").exists()  # nothing written


def test_confirmed_builds_round_trips_and_saves(tmp_path: Path) -> None:
    result = run_source_init_command(
        SourceInitCommand(
            example=FIXTURE,
            name="clinic_csv",
            display="Clinic CSV",
            out_dir=tmp_path,
            confirmed=True,
        )
    )
    assert result.ok is True
    assert result.error is None
    assert result.mapping_dir == tmp_path / "clinic_csv"
    assert (tmp_path / "clinic_csv" / "mapping.json").is_file()
    assert result.record_count == 3
    assert "Learned source" in (result.mapping_md or "")


def test_cannot_analyze_carries_a_phi_safe_type_detail(tmp_path: Path) -> None:
    blank = tmp_path / "blank.csv"
    blank.write_text("", encoding="utf-8")  # no header/columns
    result = run_source_init_command(
        SourceInitCommand(example=blank, name="blank", confirmed=False)
    )
    assert result.ok is False
    assert result.error == "CannotAnalyze"
    assert result.detail  # an exception TYPE name (PHI-safe), not a cell value
    assert result.fmt_type is None


def test_save_failure_carries_a_phi_safe_type_detail(tmp_path: Path) -> None:
    # out_dir is a FILE, so save_mapping's mkdir beneath it raises OSError. The
    # result names the exception TYPE (the CLI prints it), never the save path.
    not_a_dir = tmp_path / "afile"
    not_a_dir.write_text("x", encoding="utf-8")
    result = run_source_init_command(
        SourceInitCommand(example=FIXTURE, name="clinic_csv", out_dir=not_a_dir, confirmed=True)
    )
    assert result.ok is False
    assert result.error == "SaveFailed"
    assert result.detail  # a PHI-safe exception type name


def test_mapping_load_failure_is_distinct_from_dropped(tmp_path: Path) -> None:
    # A mapped column whose transform chokes (DOB -> parse_date over a non-date)
    # is a fixable MappingLoadFailed, NOT an unexplained WouldDropColumns.
    bad = tmp_path / "bad.csv"
    bad.write_text("PID,DOB\np1,garbage-not-a-date\n", encoding="utf-8")
    result = run_source_init_command(
        SourceInitCommand(example=bad, name="bad_src", out_dir=tmp_path, confirmed=True)
    )
    assert result.ok is False
    assert result.error == "MappingLoadFailed"
    assert "DOB" in (result.detail or "")  # names the column (PHI-safe)
    assert "garbage-not-a-date" not in repr(result)  # the value never leaks
    assert not (tmp_path / "bad_src").exists()  # nothing written


# --- an id somebody else already answers to (#334, #333) ---------------------


def test_a_builtin_id_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    """Saving a learned format as `ccda` must fail, and fail EARLY: the
    collision is knowable from the name alone (`get_source("ccda")`
    must stay the C-CDA adapter), so nothing is analysed or written
    before the refusal (#334, #333)."""
    result = run_source_init_command(
        SourceInitCommand(example=FIXTURE, name="ccda", out_dir=tmp_path, confirmed=True)
    )
    assert result.ok is False
    assert result.error == "SourceIdReserved"
    assert result.fmt_type is None, "the example was analysed despite a doomed name"
    assert list(tmp_path.iterdir()) == [], "a refused save left a partial mapping behind"


def test_an_already_learned_id_is_refused_rather_than_overwritten(tmp_path: Path) -> None:
    """A second teach under the same name must not silently replace the
    first: overwriting a reviewed mapping would discard that decision
    without asking, and leave the adapter answering for a mapping
    absent from disk. Distinct from `SourceIdReserved`: one name can
    never be used, the other is the operator's own earlier work."""
    first = run_source_init_command(
        SourceInitCommand(example=FIXTURE, name="reteach_me", out_dir=tmp_path, confirmed=True)
    )
    assert first.ok is True

    again = run_source_init_command(
        SourceInitCommand(example=FIXTURE, name="reteach_me", out_dir=tmp_path, confirmed=True)
    )
    assert again.ok is False
    assert again.error == "SourceIdInUse"
    # And the first mapping is untouched — refusing protected it.
    assert (tmp_path / "reteach_me" / "mapping.json").is_file()


def test_a_saved_format_is_selectable_without_a_restart(tmp_path: Path) -> None:
    """Teach a format and Charts can offer it NOW: the choosers populate
    from the registry, so a format taught mid-session must be
    registered immediately, not only at import. Asserted on the
    ADAPTER, not a matching name — a source answering to the right
    string could still be the wrong behaviour."""
    from anastomosis.sources import get_source
    from anastomosis.sources.learned import LearnedSourceAdapter

    result = run_source_init_command(
        SourceInitCommand(example=FIXTURE, name="selectable_now", out_dir=tmp_path, confirmed=True)
    )
    assert result.ok is True

    adapter = get_source("selectable_now")
    assert isinstance(adapter, LearnedSourceAdapter), (
        "the saved mapping is not the adapter this session would run"
    )
    assert adapter.name == "selectable_now"


# --- the correction arc (#335): observe -> correct -> save -> load -> conserve


def test_the_whole_correction_arc_conserves_what_it_loads(tmp_path: Path) -> None:
    """The #335 acceptance walk, at the layer both frontends share: a
    wrong review refuses with a structured pointer (column, target,
    transform) and no partial directory; the corrected review saves,
    reloads from disk, and every assertion runs against the loaded
    artifact, not the code that promised to produce it."""
    example = tmp_path / "visits.csv"
    example.write_text(
        "MRN,VisitId,VisitDate,Complaint,Clinic\n"
        "p1,V-001,01/05/2024,cough,north\n"
        "p1,V-002,02/06/2024,fever,north\n"
        "p2,V-003,03/07/2024,rash,south\n",
        encoding="utf-8",
    )

    def command(decisions: dict[str, tuple[str, str]]) -> SourceInitCommand:
        return SourceInitCommand(
            example=example,
            name="clinic_visits_corrected",
            out_dir=tmp_path,
            confirmed=True,
            decisions=decisions,
            patient_key="MRN",
            encounter_key="VisitId",
            row_scope="encounter",
        )

    wrong = run_source_init_command(
        command({"VisitId": ("encounter.date_of_service", "parse_date")})
    )
    assert wrong.ok is False
    assert wrong.error == "MappingLoadFailed"
    assert wrong.detail_column == "VisitId"
    assert wrong.detail_target == "encounter.date_of_service"
    assert wrong.detail_transform == "parse_date"
    assert "V-001" not in repr(wrong), "the cell value never leaks"
    assert not (tmp_path / "clinic_visits_corrected").exists(), "no partial directory"

    corrected = run_source_init_command(
        command(
            {
                "VisitDate": ("encounter.date_of_service", "parse_date"),
                "Complaint": ("encounter.chief_complaint", "strip"),
            }
        )
    )
    assert corrected.ok is True, (corrected.error, corrected.detail)

    # The restart: nothing reused from the in-process run — the spec is read
    # back off disk and driven through a fresh adapter.
    from anastomosis.sources.learned.interpreter import LearnedSourceAdapter
    from anastomosis.sources.learned.spec import load_spec

    spec = load_spec(tmp_path / "clinic_visits_corrected" / "mapping.json")
    records = list(LearnedSourceAdapter(spec).load(example))

    patients = {
        record.patient.provenance.source_id
        for record in records
        if record.patient.provenance is not None
    }
    assert len(records) == 2, "identity conservation: two distinct MRNs, two records"
    assert patients == {"p1", "p2"}

    encounters = [encounter for record in records for encounter in record.encounters]
    assert len(encounters) == 3, "encounter conservation: three rows, three visits"
    assert sorted(e.date_of_service.isoformat() for e in encounters) == [
        "2024-01-05",
        "2024-02-06",
        "2024-03-07",
    ], "the corrected transform read the DATE column, not the visit id"

    kept_values = {
        value
        for record in records
        for obj in (record.patient, *record.encounters)
        for key, value in obj.extensions.items()
        if key.endswith(":Clinic")
    }
    assert kept_values == {"north", "south"}, (
        "column conservation: the undecided column's values survive as extra data"
    )


def test_an_unreviewed_command_still_behaves_exactly_as_before(tmp_path: Path) -> None:
    """No review, no change: the four new fields default to today's behaviour.

    The correction path must be a pure addition — an operator who never touches
    it teaches formats exactly as yesterday, decided by the same scorer.
    """
    result = run_source_init_command(
        SourceInitCommand(example=FIXTURE, name="clinic_plain", out_dir=tmp_path, confirmed=True)
    )
    assert result.ok is True
    assert (tmp_path / "clinic_plain" / "mapping.json").is_file()


def test_an_override_does_not_inherit_the_scorers_stale_confidence(tmp_path: Path) -> None:
    """A number that described a different decision does not follow the
    column: the scorer's confidence describes the scorer's own pick, so
    a reviewer's override records 1.0 rather than carrying a stale
    score beside a field they deliberately chose."""
    import json

    example = tmp_path / "visits.csv"
    example.write_text(
        "MRN,VisitDate,Notes\np1,01/05/2024,ok\np2,02/06/2024,fine\n", encoding="utf-8"
    )
    result = run_source_init_command(
        SourceInitCommand(
            example=example,
            name="overridden",
            out_dir=tmp_path,
            confirmed=True,
            decisions={"Notes": ("encounter.chief_complaint", "strip")},
            patient_key="MRN",
            encounter_key=None,
            row_scope="encounter",
        )
    )
    assert result.ok is True, (result.error, result.detail)
    saved = json.loads((tmp_path / "overridden" / "mapping.json").read_text(encoding="utf-8"))
    by_source = {m["source_path"]: m for m in saved["field_mappings"]}
    assert by_source["Notes"]["confidence"] == 1.0


def test_a_lossy_read_keeps_the_cell_it_cannot_reproduce(tmp_path: Path) -> None:
    """`const:` writes the same wording over every cell — the wording is
    not the chart. A lossy column keeps its raw value in extensions,
    held to the same round-trip proof as an unmapped column, and
    MAPPING.md names which columns are covered this way."""
    example = tmp_path / "visits.csv"
    example.write_text(
        "MRN,VisitDate,Complaint\n"
        "p1,01/05/2024,crushing chest pain radiating to left arm\n"
        "p2,02/06/2024,syncope with head injury\n",
        encoding="utf-8",
    )
    result = run_source_init_command(
        SourceInitCommand(
            example=example,
            name="lossy_probe",
            out_dir=tmp_path,
            confirmed=True,
            decisions={
                "VisitDate": ("encounter.date_of_service", "parse_date"),
                "Complaint": ("encounter.chief_complaint", "const:Unknown"),
            },
            patient_key="MRN",
            encounter_key=None,
            row_scope="encounter",
        )
    )
    assert result.ok is True, (result.error, result.detail)

    from anastomosis.sources.learned.interpreter import LearnedSourceAdapter
    from anastomosis.sources.learned.spec import load_spec

    spec = load_spec(tmp_path / "lossy_probe" / "mapping.json")
    records = list(LearnedSourceAdapter(spec).load(example))
    kept = {
        value
        for record in records
        for obj in (record.patient, *record.encounters)
        for key, value in obj.extensions.items()
        if key.endswith(":Complaint")
    }
    assert kept == {
        "crushing chest pain radiating to left arm",
        "syncope with head injury",
    }, "the raw complaints survive beside the const wording"
    assert all(
        encounter.chief_complaint == "Unknown"
        for record in records
        for encounter in record.encounters
    )
    mapping_md = (tmp_path / "lossy_probe" / "MAPPING.md").read_text(encoding="utf-8")
    assert "cannot reproduce the cell" in mapping_md and "`Complaint`" in mapping_md


def test_a_per_field_refusal_is_a_mapping_error_too(tmp_path: Path) -> None:
    """Both leaves — the per-KEY validators and the per-FIELD ones —
    refuse through the one error type, and nothing echoes the
    operator's input."""
    example = tmp_path / "visits.csv"
    example.write_text("MRN,Notes\np1,fine\n", encoding="utf-8")
    for decisions in (
        {"Notes": ("patient.last_name", "strip")},  # unknown canonical target
        {"Notes": ("encounter.chief_complaint", "const:")},  # arity 0 for a 1-arg verb
    ):
        result = run_source_init_command(
            SourceInitCommand(
                example=example,
                name="badfield",
                out_dir=tmp_path,
                confirmed=True,
                decisions=decisions,
                patient_key="MRN",
                encounter_key=None,
                row_scope="encounter",
            )
        )
        assert result.ok is False
        assert result.error == "CannotBuildMapping", (result.error, result.detail)
        assert "input_value" not in (result.detail or "")
        assert not (tmp_path / "badfield").exists()


def test_a_grouping_refusal_points_at_the_grouping(tmp_path: Path) -> None:
    """A duplicate visit key is a keys-and-grain fault, not a column's read —
    the pointer says so, so the page can open the structural controls instead
    of telling the operator to change a transform that was never wrong."""
    example = tmp_path / "visits.csv"
    example.write_text("MRN,VisitId,Complaint\np1,V-1,cough\np1,V-1,fever\n", encoding="utf-8")
    result = run_source_init_command(
        SourceInitCommand(
            example=example,
            name="dupkey",
            out_dir=tmp_path,
            confirmed=True,
            decisions={"Complaint": ("encounter.chief_complaint", "strip")},
            patient_key="MRN",
            encounter_key="VisitId",
            row_scope="encounter",
        )
    )
    assert result.ok is False
    assert result.error == "MappingLoadFailed"
    assert result.detail_scope == "grouping"
    assert result.detail_column == "VisitId"


def test_a_malformed_review_is_a_failure_dict_on_both_console_doors() -> None:
    """Both console doors — sync and async — must answer a malformed
    review with this console's ordinary failure dict, naming only the
    exception TYPE, never a raw traceback across the bridge."""
    from anastomosis.gui.consoles.source import SourceConsole
    from anastomosis.gui.jobs import GuiJobRunner

    events: list[dict[str, object]] = []
    console = SourceConsole(events.append, GuiJobRunner(events.append))
    bad_review = {
        "patient_key": "MRN",
        "encounter_key": None,
        "row_scope": "encounter",
        "decisions": {"Complaint": "strip"},
    }

    sync = console.source_init("/nowhere.csv", "x_src", None, True, None, bad_review)
    assert sync == {"ok": False, "error": "TypeError"}

    async_ack = console.source_init_async("/nowhere.csv", "x_src", None, True, None, bad_review)
    assert async_ack.get("ok") in (True, False)  # the submit itself must not raise
    deadline = __import__("time").time() + 10
    while __import__("time").time() < deadline:
        if any(e.get("event") in ("done", "error") for e in events):
            break
        __import__("time").sleep(0.05)
    fetched = console.last_source_result()
    assert fetched == {"ok": False, "error": "TypeError"}, fetched
