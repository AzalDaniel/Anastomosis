"""not_carried has to reach a human, not just qa_report.json (#297).

``settle_qa`` puts ``not_carried`` on the QA :class:`StageEvent`'s counts dict
whenever it is nonzero — #271's "never green-with-nothing-said" rule — but
neither frontend used to say what the number meant. The CLI's QA line stopped
at pass/warn/fail, and the GUI's generic key-dump rendered the bare key as
"not carried N", which told an operator nothing about what N counted. Both
surfaces now read the count in the same words qa_report.json already knew: N
facts the record holds landed nowhere but the record summary, not the visit
charts.

Mirrors #225's ``test_delivery_shortfall.py`` on purpose: same shape of bug (a
count a stage already had, invisible past its own report), same fix (print it
only when nonzero, on both surfaces), same test structure (a CLI-line recorder
plus a GUI-event capture, run through the real formatting code — never a
reimplementation of it).

Counts only, as everywhere: no fact name crosses into an event, a log, or a
line here or anywhere upstream of qa_report.json.
"""

from __future__ import annotations

from pathlib import Path

from anastomosis.pipeline import STAGE_QA, StageEvent

# --- the CLI's QA line --------------------------------------------------------


def _qa_lines(counts: dict[str, int]) -> list[str]:
    """What ``_make_event_printer`` puts on screen for one QA StageEvent."""
    from rich.console import Console

    import anastomosis.cli as cli

    recorder = Console(record=True, width=200, no_color=True)
    original, cli.console = cli.console, recorder
    try:
        printer = cli._make_event_printer(source="ccda", charts_dir=Path("charts"))
        printer(StageEvent(STAGE_QA, counts=counts))
    finally:
        cli.console = original
    return [line.strip() for line in recorder.export_text().splitlines() if line.strip()]


def test_the_cli_explains_a_nonzero_not_carried_count() -> None:
    """The issue's own example, byte for byte."""
    lines = _qa_lines({"pass": 2, "warn": 0, "fail": 0, "not_carried": 13})
    assert len(lines) == 1, lines
    assert lines[0] == (
        "QA: 2 pass, 0 warn, 0 fail — 13 fact(s) carried by the record summary, "
        "not the visit charts → qa_report.json"
    )


def test_the_cli_stays_silent_when_nothing_was_left_out() -> None:
    """A run that abbreviates nothing gets the line it always had — settle_qa
    never puts the key on the counts dict at all, so this is the ordinary
    shape, not a zero the printer has to notice and suppress."""
    lines = _qa_lines({"pass": 2, "warn": 0, "fail": 0})
    assert len(lines) == 1, lines
    assert lines[0] == "QA: 2 pass, 0 warn, 0 fail → qa_report.json"
    assert "fact(s)" not in lines[0]


def test_the_cli_stays_silent_on_an_explicit_zero_too() -> None:
    """Belt and braces: were settle_qa ever to send the key at 0, the printer
    itself must not turn that into a clause — it checks truthiness, not
    presence."""
    lines = _qa_lines({"pass": 2, "warn": 0, "fail": 0, "not_carried": 0})
    assert lines[0] == "QA: 2 pass, 0 warn, 0 fail → qa_report.json"


# --- the GUI's rail event -----------------------------------------------------


def _qa_events(counts: dict[str, int]) -> list[dict[str, object]]:
    """The events the GUI's QA rail receives for one QA StageEvent."""
    from anastomosis.gui.consoles.runs import PipelineConsole, SummaryStore

    class _Jobs:
        def acquire(self) -> bool:
            return True

        def release(self) -> None:
            pass

    emitted: list[dict[str, object]] = []
    on_event = PipelineConsole(emitted.append, _Jobs(), SummaryStore())._stage_emitter({})
    on_event(StageEvent(STAGE_QA, counts=counts))
    return [e for e in emitted if e.get("type") == "progress"]


def test_the_gui_qa_rail_carries_the_not_carried_count() -> None:
    """The count reaches the rail's own data — confirmed at the wire, not
    guessed: this is the same ``**event.counts`` unpack every other QA count
    rides, so a chart the layout abbreviates was never a special case to wire
    up, only one the front end had not been told to read."""
    (event,) = _qa_events({"pass": 2, "warn": 0, "fail": 0, "not_carried": 13})
    assert event["not_carried"] == 13


def test_the_gui_qa_rail_stays_short_when_nothing_was_left_out() -> None:
    (event,) = _qa_events({"pass": 2, "warn": 0, "fail": 0})
    assert "not_carried" not in event
