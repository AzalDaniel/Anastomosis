"""The render-selection rules are a run's choice, not the product's opinion.

The pf-tebra adapter keeps two shapes of encounter out of the render — a SOAP
note whose four sections are all empty, and a growth-chart visit for a patient
who was an adult at the time — and parks them losslessly in
``extensions["pf_tebra:skipped_encounters"]``. That was one practice's call.
An archivist retaining everything wants the empty visit in the pile, and a
paediatric practice whose patients grew up wants the growth chart.

So each rule is now a per-run option, in the shape the section flags already
have: a repeatable CLI flag, a strict parse shared with the GUI, validation
against what the resolved end (there, the pack; here, the source) actually
offers, and a record in the output of what was asked. Every rule is on by
default, which is exactly what they were, so an existing run is unchanged —
``test_a_default_run_selects_exactly_what_it_always_did`` is the pin.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from _render_fakes import FakeChromium
from typer.testing import CliRunner

import anastomosis.reconstruct.chromium as chromium
import anastomosis.sources.pf_tebra  # noqa: F401 — registers the adapter
from anastomosis.cli import app
from anastomosis.pipeline import (
    RENDER_SETTINGS_NAME,
    SELECTION_REPORT_NAME,
    PipelineError,
    parse_selection_includes,
    run_pipeline,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"

#: Boris's adult growth-chart visit, and the empty-SOAP records request.
GROWTH_CHART = "feedface-e000-0000-0000-000000000008"
EMPTY_SOAP = "feedface-e000-0000-0000-000000000007"

runner = CliRunner()


@pytest.fixture
def rendered(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("pymupdf", reason="the fake renderer writes a real PDF")
    monkeypatch.setattr(chromium, "ChromiumRenderer", FakeChromium)


def _run(out: Path, *, include: list[str] | None = None, qa: bool = False) -> Any:
    return run_pipeline(
        export_dir=FIXTURE,
        out=out,
        source="pf-tebra",
        pack="generic_soap",
        pack_dirs=None,
        force=False,
        section=None,
        qa=qa,
        include=include,
    )


def _report(out: Path) -> dict[str, Any]:
    return json.loads((out / SELECTION_REPORT_NAME).read_text(encoding="utf-8"))


def _rendered_ids(result: Any) -> set[str]:
    return {enc.id for record in result.records for enc in record.encounters}


def _skipped_ids(result: Any) -> set[str]:
    return {
        entry["encounter"]["id"]
        for record in result.records
        for entry in record.extensions.get("pf_tebra:skipped_encounters", [])
    }


# --- (a) the default -----------------------------------------------------------


def test_a_default_run_selects_exactly_what_it_always_did(rendered: None, tmp_path: Path) -> None:
    """No ``--include``: both rules run, the same two encounters are held back,
    and the settings record carries no trace of an option nobody used."""
    out = tmp_path / "out"
    result = _run(out)

    assert len(result.render_result.rendered) == 6
    assert _skipped_ids(result) == {EMPTY_SOAP, GROWTH_CHART}
    assert GROWTH_CHART not in _rendered_ids(result)
    settings = json.loads((out / RENDER_SETTINGS_NAME).read_text(encoding="utf-8"))
    # The key rides only when a rule was switched off, so a folder built by a
    # build that had no such option is still readable by this one, and this
    # one's record of a default run is identical to that build's.
    assert "included" not in settings


# --- (b) the option ------------------------------------------------------------


def test_including_growth_charts_renders_the_visit_the_rule_kept_out(
    rendered: None, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    result = _run(out, include=["growth-charts"])

    assert GROWTH_CHART in _rendered_ids(result)
    # And it is not BOTH rendered and parked as excluded: the rule did not run,
    # so there is nothing for it to have kept out.
    assert _skipped_ids(result) == {EMPTY_SOAP}
    assert len(result.render_result.rendered) == 7
    # The other rule is untouched — switching one off is not switching them off.
    assert EMPTY_SOAP not in _rendered_ids(result)


def test_qa_counts_an_included_growth_chart_as_carried_not_as_a_fact_on_no_chart(
    rendered: None, tmp_path: Path
) -> None:
    """The accounting has to follow the option, or the option is cosmetic.

    A growth chart is a height and a weight, so this copy of the fixture puts a
    body height on that visit — the shape the real thing has. Under the rule,
    the visit is not in the record's encounters and the measurement names an
    encounter that is not there: ``unattributed_vitals`` says so on every
    chart, correctly, and the run FAILS, because the value reached no page at
    all. With the rule switched off the same measurement is on a chart, and the
    same check passes.
    """
    export = tmp_path / "export"
    shutil.copytree(FIXTURE, export)
    observations = export / "patient-encounter-observations.tsv"
    columns = observations.read_text(encoding="utf-8").splitlines()[0].split("\t")
    row = dict.fromkeys(columns, "")
    row.update(
        {
            "PatientPracticeGuid": "feedface-0000-0000-0000-000000000002",
            "EncounterGuid": GROWTH_CHART,
            "ObservationSetGuid": "feedface-0b50-0000-0000-000000000001",
            "ObservationCodeSystem": "LOINC",
            "ObservationCode": "8302-2",  # body height
            "ValueType": "Numeric",
            "Value": "70",
            "UnitOfObservation": "in",
            "ObservationDateTimeUtc": "3/2/2023 6:30:00 PM",
            "LastModifiedDateTimeUtc": "3/2/2023 6:30:00 PM",
        }
    )
    with observations.open("a", encoding="utf-8") as handle:
        handle.write("\t".join(row[column] for column in columns) + "\n")

    def _run_export(out: Path, include: list[str] | None) -> dict[str, Any]:
        try:
            run_pipeline(
                export_dir=export,
                out=out,
                source="pf-tebra",
                pack="generic_soap",
                pack_dirs=None,
                force=False,
                section=None,
                qa=True,
                include=include,
            )
        except PipelineError as exc:
            assert exc.kind == "qa_failed"
        report: dict[str, Any] = json.loads((out / "qa_report.json").read_text(encoding="utf-8"))
        return report

    def _unattributed(report: dict[str, Any]) -> list[str]:
        return [
            finding
            for document in report["documents"]
            for check in document["checks"]
            if check["check"] == "unattributed_vitals"
            for finding in check["findings"]
        ]

    applied = _run_export(tmp_path / "applied", None)
    assert applied["summary"]["fail"] == 3
    assert (
        _unattributed(applied) == ["Body height names an encounter this record does not have"] * 3
    )

    included = _run_export(tmp_path / "included", ["growth-charts"])
    assert included["summary"]["fail"] == 0
    assert _unattributed(included) == []
    # One more graded document than the default run, and it is the growth chart.
    assert GROWTH_CHART in {document["encounter_id"] for document in included["documents"]}
    assert len(included["documents"]) == len(applied["documents"]) + 1


# --- (c) the report ------------------------------------------------------------


def test_the_report_names_every_rule_and_whether_this_run_applied_it(
    rendered: None, tmp_path: Path
) -> None:
    """Two runs made under different options have to be readable against each
    other. Without this block an empty ``excluded`` means either "the rules
    found nothing" or "no rule was running", which are opposite answers."""
    default_out = tmp_path / "default"
    included_out = tmp_path / "included"
    _run(default_out)
    _run(included_out, include=["growth-charts"])
    default = _report(default_out)
    included = _report(included_out)

    assert default["version"] == 2
    assert [(rule["rule"], rule["applied"]) for rule in default["rules"]] == [
        ("empty-soap", True),
        ("growth-charts", True),
    ]
    assert [(rule["rule"], rule["applied"]) for rule in included["rules"]] == [
        ("empty-soap", True),
        ("growth-charts", False),
    ]
    # The rule that did not run took its encounter out of `excluded` with it.
    assert {entry["encounter_id"] for entry in default["excluded"]} == {EMPTY_SOAP, GROWTH_CHART}
    assert {entry["encounter_id"] for entry in included["excluded"]} == {EMPTY_SOAP}
    # Each rule still names the reason its exclusions carry, so an older
    # report's `reason` strings are readable against a newer report's rules.
    assert {rule["reason"] for rule in default["rules"]} == {"empty_soap", "adult_growth_chart"}


def test_the_report_names_rules_without_naming_anything_they_read(
    rendered: None, tmp_path: Path
) -> None:
    """The block is schema — rule names, reasons, labels — and sits beside the
    charts, so it must carry no word of the encounters the rules looked at."""
    out = tmp_path / "out"
    _run(out, include=["growth-charts"])
    blob = json.dumps(_report(out)["rules"])

    from anastomosis.sources import get_source

    for record in get_source("pf-tebra").load(FIXTURE):
        for value in (record.patient.family_name, record.patient.given_name):
            if value:
                assert value not in blob
        for encounter in record.encounters:
            if chief_complaint := (encounter.chief_complaint or "").strip():
                assert chief_complaint not in blob


# --- loud refusals -------------------------------------------------------------


def test_an_unknown_rule_name_is_refused_and_lists_what_the_source_has(
    rendered: None, tmp_path: Path
) -> None:
    """The one thing worse than not being able to switch a rule off is thinking
    you did. Checked against the source's own rules, before the export is read."""
    with pytest.raises(PipelineError) as caught:
        _run(tmp_path / "out", include=["growth_charts"])  # underscore, not hyphen

    assert caught.value.kind == "bad_selection"
    assert caught.value.exit_code == 2
    message = str(caught.value)
    assert "growth_charts" in message
    assert "empty-soap, growth-charts" in message
    assert not (tmp_path / "out" / SELECTION_REPORT_NAME).exists()


def test_a_source_with_no_selection_rules_refuses_every_include(
    rendered: None, tmp_path: Path
) -> None:
    """ "Known: (none)" rather than a silently ignored flag: the C-CDA adapter
    keeps nothing out of the render, so there is nothing to switch off."""
    import anastomosis.sources.ccda  # noqa: F401 — registers the adapter

    with pytest.raises(PipelineError) as caught:
        run_pipeline(
            export_dir=FIXTURE.parents[0] / "ccda",
            out=tmp_path / "out",
            source="ccda",
            pack="generic_soap",
            pack_dirs=None,
            force=False,
            section=None,
            qa=False,
            include=["growth-charts"],
        )

    assert caught.value.kind == "bad_selection"
    assert "Known: (none)" in str(caught.value)


@pytest.mark.parametrize("bad", [[""], ["   "], ["growth-charts", ""]])
def test_a_blank_rule_name_is_refused_rather_than_read_as_including_nothing(
    bad: list[str],
) -> None:
    with pytest.raises(PipelineError) as caught:
        parse_selection_includes(bad)

    assert caught.value.kind == "bad_selection"
    assert caught.value.exit_code == 2


def test_the_parser_is_indifferent_to_order_and_repetition() -> None:
    """A set, like the section flags are a mapping: saying it twice is saying it."""
    assert parse_selection_includes(["growth-charts", "empty-soap", "growth-charts"]) == frozenset(
        {"empty-soap", "growth-charts"}
    )
    assert parse_selection_includes(None) == frozenset()


def test_an_adapter_that_declares_rules_but_cannot_take_them_fails_loudly() -> None:
    """The two halves of the capability are a pair. Half of it is a half-built
    adapter, and it must say so rather than quietly applying every rule while
    the report claims one was switched off."""
    from anastomosis.sources import SelectionRule, with_selection

    class _HalfBuilt:
        name = "half-built"
        display = "Half built"
        description = "declares a rule it cannot be configured with"
        selection_rules = (SelectionRule(name="a-rule", reason="a_reason", label="A rule"),)

        def detect(self, path: Path) -> bool:
            return False

        def load(self, path: Path) -> Any:
            return iter(())

    adapter = _HalfBuilt()
    # Nothing switched off asks nothing of the adapter, and gets the adapter.
    assert with_selection(adapter, []) is adapter
    with pytest.raises(TypeError, match="half-built"):
        with_selection(adapter, ["a-rule"])


# --- the registry keeps its own adapter ----------------------------------------


def test_a_run_that_drops_a_rule_leaves_the_registered_adapter_alone(
    rendered: None, tmp_path: Path
) -> None:
    """The registry hands every caller the same adapter object. A run that
    configured THAT object would be choosing for the next run too — including a
    GUI session's next run, in the same process, with nothing on screen to say
    so."""
    from anastomosis.sources import get_source
    from anastomosis.sources.pf_tebra.mapper import DEFAULT_SELECTION

    registered = get_source("pf-tebra")
    _run(tmp_path / "out", include=["growth-charts"])

    assert registered.selection == DEFAULT_SELECTION  # type: ignore[attr-defined]
    assert get_source("pf-tebra") is registered
    after = _run(tmp_path / "after")
    assert GROWTH_CHART not in _rendered_ids(after)


# --- re-running into the same folder -------------------------------------------


def test_rerunning_the_same_folder_under_different_rules_is_refused(
    rendered: None, tmp_path: Path
) -> None:
    """The same guard the section flags get, for the same reason: the charts
    already there answer a different question from the one now being asked, and
    a silent no-op would report success over output the operator was trying to
    change."""
    out = tmp_path / "out"
    _run(out)

    with pytest.raises(PipelineError) as caught:
        _run(out, include=["growth-charts"])

    assert caught.value.kind == "settings_changed"
    assert "growth-charts" in str(caught.value)


# --- the CLI flag --------------------------------------------------------------


def test_the_cli_flag_reaches_the_run(rendered: None, tmp_path: Path) -> None:
    out = tmp_path / "charts"
    result = runner.invoke(
        app,
        [
            "pipeline",
            "run",
            str(FIXTURE),
            "--out",
            str(out),
            "--include",
            "growth-charts",
            "--no-qa",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "7 rendered" in result.output
    assert len(list(out.glob("*.pdf"))) == 7


def test_anast_info_names_each_rule_and_the_word_that_switches_it_off() -> None:
    """``--include`` accepts these names and nothing else, so a flag whose
    vocabulary is written down nowhere is a flag nobody can type."""
    result = runner.invoke(app, ["info"])

    assert result.exit_code == 0, result.output
    flattened = " ".join(result.output.split())
    assert "--include growth-charts" in flattened
    assert "--include empty-soap" in flattened
    assert "Growth-chart visits for patients who were adults at the visit" in flattened


def test_the_cli_refuses_an_unknown_rule_with_exit_2(rendered: None, tmp_path: Path) -> None:
    out = tmp_path / "charts"
    result = runner.invoke(
        app, ["pipeline", "run", str(FIXTURE), "--out", str(out), "--include", "nope", "--no-qa"]
    )

    assert result.exit_code == 2, result.output
    assert "Unknown --include nope" in " ".join(result.output.split())
    assert not list(out.glob("*.pdf"))
