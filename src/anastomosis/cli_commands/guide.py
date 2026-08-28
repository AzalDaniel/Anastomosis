"""``anast`` with no arguments — the guided session.

A physician who has never opened a manual types ``anast``, presses Enter, and is
walked to a finished run: a short branded header, five numbered choices, then one
prompt at a time, each stating what it will do. Every flow ends by running the
SAME command ``anast`` would have run from a typed command line — this module
gathers arguments and speaks plain English; it decides nothing a command decides
and reimplements nothing a command does.

Three rules hold this together:

* **A script never sees a prompt.** The guide runs only when a person is at both
  ends of the session (:func:`is_interactive_terminal`); every other caller gets
  the help page ``anast`` has always printed. CI cannot hang here.
* **One code path.** :func:`_dispatch` hands the assembled argument list to the
  real Typer app, so every gate, every printed line and every exit code belongs
  to the command, not to the guide. It is also the seam the tests patch.
* **One register.** The copy obeys ``docs/design/DESIGN_LANGUAGE.md`` §10 and
  ``docs/design/COPY_MAP.md``: no engineering vocabulary, no emoji, no
  exclamation marks, sentence case, and every prompt says what happens next.
  ``tests/unit/test_guide.py`` lints this file's prose for the banned words.

Imports stay inside the functions that need them (the CLI's lazy cold-start
rule); this module itself is imported only when ``anast`` is typed bare.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from rich.text import Text

from anastomosis.core.presentation import BRAND_PALETTE, terminal_glyphs

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from rich.console import Console

#: The exit status a shell expects from a session a person interrupted (128 +
#: SIGINT). Scripts branch on it, so it is named rather than typed inline.
INTERRUPTED_EXIT_CODE = 130

#: The suggested browser connection for the filing flow — the loopback debug
#: port the upload command validates. Pre-filled so nobody has to know it.
DEFAULT_BROWSER_CONNECTION = "http://127.0.0.1:9222"

#: The chart layout every rebuild uses unless the person names another one.
DEFAULT_CHART_LAYOUT = "generic_soap"


@dataclass(frozen=True)
class Plan:
    """One gathered flow: what will run, and how the guide talks about it.

    ``argv`` is handed to the real ``anast`` app verbatim. The three sentences
    around it are the guide's whole contribution: what is about to happen, what
    happened, and what a person would sensibly do next.
    """

    argv: tuple[str, ...]
    confirmation: str
    finished: str
    next_step: str = ""
    #: Filled only where a flow wants to say something specific about a refusal;
    #: otherwise the shared framing sentence carries it alone.
    notes: tuple[str, ...] = ()


def _attached_to_a_terminal(stream: Any) -> bool:
    """Whether ``stream`` is really a terminal — asked of the stream itself.

    Never of anything that reports colour capability. ``FORCE_COLOR`` and
    ``TTY_COMPATIBLE`` make Rich answer ``is_terminal`` True for a plain file
    (``typer/rich_utils.py`` sets them up), and both are exported by ordinary
    tooling — CI images, ``just``/``make`` wrappers, terminal multiplexers. With
    one of them set, ``anast > log`` from a shell used to open the guided
    session, write the menu into the log file, and then block on a question
    nobody could see.

    Whether output can carry colour and whether a person is reading it are
    different questions with different answers; letting the first stand in for
    the second is what made an unattended run hang.
    """
    try:
        return bool(stream is not None and stream.isatty())
    except (AttributeError, OSError, ValueError):
        # A closed or exotic stream is neither a keyboard nor a screen.
        return False


def is_interactive_terminal(console: Console) -> bool:
    """True only when a person is at BOTH ends of this session.

    The guided session asks questions, so it may start only when the answers can
    come from a keyboard and the questions can be seen: a real terminal on stdin
    AND on the console's own output stream. A pipe, a redirect, a cron job or a
    CI runner fails this test and is given the help page instead — which is why
    no script can ever hang waiting for a prompt that nobody will answer.

    Both ends are asked directly (see :func:`_attached_to_a_terminal`). Dropping
    the output end would not do: stdin stays a keyboard when only stdout is
    redirected, so ``anast > log`` would start the session and put its questions
    somewhere the person answering cannot read them.
    """
    import sys

    return _attached_to_a_terminal(getattr(sys, "stdin", None)) and _attached_to_a_terminal(
        getattr(console, "file", None)
    )


def run_guide(console: Console) -> int:
    """Run the guided session on ``console`` and return the exit status.

    Prints the header, takes one choice, gathers that flow's answers, confirms
    in one sentence, then runs the real command and says what happened. An
    interrupt before the command starts is honest about it and stops.
    """
    _print_header(console)
    try:
        plan = _choose_plan(console)
        if plan is None:
            return 0
        # The last thing anybody sees before work begins: one sentence naming
        # exactly what is about to run, and the fact that nothing has yet.
        keep_going = _ask_yes_no(
            console,
            plan.confirmation,
            "Nothing has been changed yet.",
            default=True,
            label="Continue",
        )
        if not keep_going:
            _say(console, "Nothing was changed.")
            return 0
    except (KeyboardInterrupt, EOFError):
        # Ctrl-C (or a closed stdin) while still asking questions: nothing has
        # run yet, so say exactly that. Once the command below starts, its own
        # interrupt handling owns the outcome and this branch is never reached.
        console.print()
        _say(console, "Nothing was changed.")
        return INTERRUPTED_EXIT_CODE

    console.print()
    exit_code = _dispatch(plan.argv)
    _report(console, plan, exit_code)
    return exit_code


# --- the execution seam ------------------------------------------------------


def _dispatch(argv: Sequence[str]) -> int:
    """Run one real ``anast`` command in this process; return its exit status.

    The guide never shells out and never reimplements a command: it hands the
    assembled arguments to the SAME Typer app the ``anast`` entry point runs, so
    the event lines stream exactly as they always have and the exit code is the
    command's own. Typer ends a command by raising ``SystemExit``; catching it
    is what turns "the command ran" back into a value the guide can talk about.
    """
    from anastomosis.cli import app
    from anastomosis.core.outcome import take_declined

    take_declined()  # clear any outcome from an earlier command in this session
    try:
        app(args=list(argv))
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 0
    return 0


# --- the header and the menu -------------------------------------------------


def _print_header(console: Console) -> None:
    """The one identity moment: the mark, the name, the version, the purpose."""
    import anastomosis

    bar = terminal_glyphs(console.file).bar
    title = Text(f"{bar} ", style=BRAND_PALETTE.brand_bright)
    title.append("Anastomosis ", style=BRAND_PALETTE.ink)
    title.append(anastomosis.__version__, style=BRAND_PALETTE.ink_muted)
    console.print()
    console.print(title)
    console.print(
        Text(
            "  Turn an EHR export into complete, verified charts you can keep or move.",
            style=BRAND_PALETTE.ink_muted,
        )
    )


def _choose_plan(console: Console) -> Plan | None:
    """Show the menu and gather the chosen flow. ``None`` means the person left."""
    choices = (
        ("1", "Rebuild charts from an EHR export"),
        ("2", "Move charts into another system"),
        ("3", "Watch charts being filed, or start and stop the work"),
        ("4", "Teach a new export format or document layout"),
        ("5", "Check this installation"),
        ("q", "Quit"),
    )
    _print_question(console, "What would you like to do?", "Type a number and press Enter.")
    for key, label in choices:
        console.print(Text(f"  {key}  {label}", style=BRAND_PALETTE.ink))
    keys = [key for key, _label in choices]
    while True:
        answer = _read(console, "Choice", default="1").lower()
        if answer == "q":
            return None
        if answer in keys:
            return _FLOWS[answer](console)
        _retry(console, "Type one of the numbers listed above, or q to leave.")


# --- the five flows ----------------------------------------------------------


def _flow_rebuild(console: Console) -> Plan:
    """1 — rebuild charts from an EHR export (``anast pipeline run``)."""
    export_dir = _ask_export_dir(console)
    out_dir = _ask_output_dir(console, "Where should the finished charts go?")
    source = _ask_source_format(console)
    layout = _ask_text(
        console,
        "How should the finished chart pages be laid out?",
        "Leave this as it is unless you have been given another layout name.",
        default=DEFAULT_CHART_LAYOUT,
    )
    double_check = _ask_yes_no(
        console,
        "Double-check every finished chart?",
        "Re-reads each chart and confirms names, dates, and values landed on the "
        "right patient. This takes longer.",
        default=True,
    )
    prepare = _ask_yes_no(
        console,
        "Also prepare these charts for filing into another system?",
        "Writes the files the filing step needs. Nothing is sent anywhere.",
        default=False,
    )

    argv = ["pipeline", "run", str(export_dir), "--out", str(out_dir)]
    if source is not None:
        argv += ["--source", source]
    argv += ["--pack", layout]
    if not double_check:
        argv += ["--no-qa"]
    if prepare:
        argv += ["--upload-manifest"]

    clauses = [f"Rebuild every chart from {export_dir} into {out_dir}"]
    clauses.append("working out the export format" if source is None else f"reading it as {source}")
    clauses.append(f"laying the pages out with {layout}")
    clauses.append(
        "double-checking each finished chart" if double_check else "with no double-check"
    )
    if prepare:
        clauses.append("and preparing them for filing")
    next_step = (
        "Run anast again and choose 3 to file these charts into another system."
        if prepare
        else "Run anast again and choose 1 to prepare these charts for filing."
    )
    return Plan(
        argv=tuple(argv),
        confirmation=", ".join(clauses) + ".",
        finished=f"The charts are in {out_dir}.",
        next_step=next_step,
    )


def _flow_move(console: Console) -> Plan:
    """2 — move charts into another system (``anast migrate``)."""
    export_dir = _ask_export_dir(console)
    out_dir = _ask_output_dir(console, "Where should the results go?")
    source = _ask_choice(
        console,
        "Which system did this export come from?",
        "A move needs the format named, so there is no detect option here.",
        _source_options(),
    )
    destination = _ask_choice(
        console,
        "Which system are the charts moving into?",
        "This decides the route the charts take. Nothing is sent during this step.",
        _destination_options(),
    )
    render = _ask_choice(
        console,
        "How should the chart pages look?",
        "Both options also write the transfer document the other system imports.",
        (
            ("neutral", "Pages laid out for reading (recommended)"),
            ("ccda-standard", "The standard C-CDA view"),
        ),
    )
    argv = (
        "migrate",
        str(export_dir),
        "--out",
        str(out_dir),
        "--from",
        source,
        "--to",
        destination,
        "--render",
        render,
    )
    return Plan(
        argv=argv,
        confirmation=(
            f"Prepare a move of every chart in {export_dir} from {source} to {destination}, "
            f"writing the charts and the transfer document into {out_dir}."
        ),
        finished=(
            f"The charts and the transfer document are in {out_dir}. Nothing has been sent yet."
        ),
        next_step="Run anast again and choose 3 to file these charts into the other system.",
    )


def _flow_file(console: Console) -> Plan:
    """3 — watch charts being filed, or start and stop the work (``anast upload``)."""
    charts_dir = _ask_existing_dir(
        console,
        "Which folder holds the charts to file?",
        "The results folder from a rebuild or a move. Charts already filed are left alone.",
    )
    route = _ask_choice(
        console,
        "How should the charts be filed?",
        "Anastomosis never stores your sign-in details for the other system.",
        (
            ("browser", "Through a browser window you are already signed in to"),
            ("api", "Straight to the other system's FHIR interface"),
        ),
    )
    if route == "browser":
        destination = _ask_choice(
            console,
            "Which system are the charts being filed into?",
            "The filing assistant for this system must already be set up on this computer.",
            _destination_options(),
        )
        connection = _ask_text(
            console,
            "Which browser connection should be used?",
            "Anastomosis files charts through a browser window it controls on this "
            "computer. Leave this as suggested unless support asks you to change it.",
            default=DEFAULT_BROWSER_CONNECTION,
        )
        route_argv = ["--to", destination, "--cdp", connection]
        where = f"into {destination} through the browser at {connection}"
    else:
        address = _ask_text(
            console,
            "What is the address of the other system's FHIR interface?",
            "It must be an https address, or a loopback address on this computer. "
            "The sign-in token is read from the ANAST_FHIR_TOKEN environment variable, "
            "never typed here.",
        )
        route_argv = ["--fhir", address]
        where = f"to {address}"

    double_check = _ask_yes_no(
        console,
        "Double-check each chart after filing?",
        "Confirms the right chart landed on the right patient before moving on.",
        default=True,
    )
    argv = ["upload", str(charts_dir), *route_argv]
    if not double_check:
        argv += ["--no-verify"]
    tail = ", double-checking each one after filing" if double_check else ", with no double-check"
    return Plan(
        argv=tuple(argv),
        confirmation=f"File the charts in {charts_dir} {where}{tail}.",
        finished="Filing has finished.",
        next_step="What was filed, and anything that needs attention, is listed above.",
    )


def _flow_teach(console: Console) -> Plan:
    """4 — teach an export format or a document layout (``source``/``pack init``)."""
    what = _ask_choice(
        console,
        "What would you like to teach it?",
        "Both are learned from your own files, and nothing leaves this computer.",
        (
            ("format", "An export format, from one example file"),
            ("layout", "A document layout, from sample chart pages"),
        ),
    )
    if what == "format":
        example = _ask_existing_path(
            console,
            "Where is the example file?",
            "One .csv, .tsv or .json file, or the folder holding it. It is read, never changed.",
        )
        name = _ask_name(
            console,
            "What should this format be called?",
            "A short label you will recognize later, for example acme_clinic.",
        )
        return Plan(
            argv=("source", "init", str(example), "--name", name),
            confirmation=(
                f"Read the columns of {example}, propose how each one is understood, and ask "
                f"before saving it as {name}."
            ),
            finished=f"Anastomosis now recognizes {name}.",
            next_step="A rebuild finds this format on its own from now on.",
        )
    samples = _ask_existing_dir(
        console,
        "Where are the sample chart pages?",
        "A folder of PDF pages from the same document, from DIFFERENT patients. "
        "They are read, never changed.",
    )
    name = _ask_name(
        console,
        "What should this layout be called?",
        "A short label you will recognize later, for example acme_soap.",
    )
    return Plan(
        argv=("pack", "init", "--from-samples", str(samples), "--name", name),
        confirmation=(
            f"Study the sample pages in {samples}, show what was found with no patient data, "
            f"and ask before writing a draft layout called {name}."
        ),
        finished=f"The draft layout {name} is written.",
        next_step="Review it against one of your own pages before relying on it.",
    )


def _flow_check(console: Console) -> Plan:
    """5 — check this installation (``anast doctor``)."""
    return Plan(
        argv=("doctor",),
        confirmation=(
            "Check that every part of this installation is present and readable. "
            "Nothing is changed, and no patient data is read."
        ),
        finished="This installation is complete.",
        next_step="Run anast again and choose 1 to rebuild charts from an export.",
        notes=("The parts listed above are missing or unreadable. Reinstall to replace them.",),
    )


#: The menu key each flow answers to. Kept beside the flows so a new entry
#: cannot be added to one and forgotten in the other.
_FLOWS = {
    "1": _flow_rebuild,
    "2": _flow_move,
    "3": _flow_file,
    "4": _flow_teach,
    "5": _flow_check,
}


# --- the choice lists a flow offers ------------------------------------------


def _source_options() -> tuple[tuple[str, str], ...]:
    """Every export format this installation can read, as (value, label) pairs.

    Read from the SAME registry the commands read, so a format taught through
    choice 4 shows up here the moment it is saved.
    """
    from anastomosis.sources import available_sources

    return tuple((adapter.name, adapter.description) for adapter in available_sources())


def _destination_options() -> tuple[tuple[str, str], ...]:
    """Every system charts can be moved into, as (value, label) pairs."""
    from anastomosis.destinations.registry import DestinationRegistry

    entries = DestinationRegistry.load().entries
    return tuple((name, entries[name].display) for name in sorted(entries))


def _ask_source_format(console: Console) -> str | None:
    """Ask which system an export came from; ``None`` means work it out."""
    detect = "detect"
    options = ((detect, "Work it out from the export (recommended)"), *_source_options())
    chosen = _ask_choice(
        console,
        "Which system did this export come from?",
        "Working it out is reliable for every format Anastomosis knows.",
        options,
    )
    return None if chosen == detect else chosen


# --- prompts -----------------------------------------------------------------


def _print_question(console: Console, question: str, note: str) -> None:
    """One question and the one line saying what answering it will do."""
    console.print()
    console.print(Text(question, style=BRAND_PALETTE.ink))
    if note:
        for line in _wrapped(note):
            console.print(Text(f"  {line}", style=BRAND_PALETTE.ink_muted))


def _wrapped(note: str, width: int = 76) -> list[str]:
    """Wrap a consequence line by words, so a narrow console stays readable."""
    import textwrap

    return textwrap.wrap(note, width=width) or [note]


def _read(console: Console, label: str, *, default: str = "") -> str:
    """Read one answer, showing the pre-filled default an empty Enter accepts.

    The prompt is a :class:`~rich.text.Text`, never markup, so a pasted Windows
    path or a default containing brackets is shown literally rather than being
    eaten as a style tag.
    """
    suffix = f" ({default})" if default else ""
    answer = console.input(Text(f"{label}{suffix}: ", style=BRAND_PALETTE.brand_bright))
    return answer.strip() or default


def _retry(console: Console, reason: str) -> None:
    """Say why the last answer cannot be used, and let the person type again."""
    console.print(Text(f"  {reason}", style=BRAND_PALETTE.attention))


def _say(console: Console, sentence: str, *, style: str = BRAND_PALETTE.ink) -> None:
    console.print(Text(sentence, style=style))


def _ask_text(console: Console, question: str, note: str, *, default: str = "") -> str:
    _print_question(console, question, note)
    while True:
        answer = _read(console, "Answer", default=default)
        if answer:
            return answer
        _retry(console, "An answer is needed here.")


def _ask_name(console: Console, question: str, note: str) -> str:
    """Ask for a short label, refusing here what the command would refuse later."""
    _print_question(console, question, note)
    while True:
        answer = _read(console, "Name")
        if answer and answer[0].isalpha() and answer.replace("_", "").isalnum():
            return answer.lower()
        _retry(console, "Use letters, digits and underscores, starting with a letter.")


def _ask_yes_no(
    console: Console, question: str, note: str, *, default: bool, label: str = "Answer"
) -> bool:
    _print_question(console, question, note)
    while True:
        answer = _read(console, label, default="yes" if default else "no").lower()
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        _retry(console, "Type yes or no.")


def _ask_choice(
    console: Console,
    question: str,
    note: str,
    options: Sequence[tuple[str, str]],
) -> str:
    """Ask a numbered question. The first option is the pre-filled default."""
    _print_question(console, question, note)
    for index, (_value, label) in enumerate(options, start=1):
        console.print(Text(f"  {index}  {label}", style=BRAND_PALETTE.ink))
    while True:
        answer = _read(console, "Number", default="1")
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1][0]
        _retry(console, f"Type a number between 1 and {len(options)}.")


def _ask_export_dir(console: Console) -> Path:
    """Ask for the EHR's export folder.

    Two flows begin with this exact question and this exact reassurance, and the
    reassurance is a promise about what the tool does to the operator's files.
    One place to say it, so the two flows cannot come to say it differently.
    """
    return _ask_existing_dir(
        console,
        "Where is the export folder?",
        "The folder your EHR gave you when you exported your records. "
        "Nothing inside it is changed.",
    )


def _ask_existing_dir(console: Console, question: str, note: str) -> Path:
    """Ask for a folder that must already be there and readable."""
    _print_question(console, question, note)
    while True:
        path = _typed_path(console, "Folder")
        if path is None:
            continue
        if not path.exists():
            _retry(console, "There is nothing at that path.")
        elif not path.is_dir():
            _retry(console, "That is a file. This step needs the folder holding it.")
        elif not os.access(path, os.R_OK):
            _retry(console, "That folder cannot be read by this account.")
        else:
            return path


def _ask_existing_path(console: Console, question: str, note: str) -> Path:
    """Ask for a file or folder that must already be there and readable."""
    _print_question(console, question, note)
    while True:
        path = _typed_path(console, "File")
        if path is None:
            continue
        if not path.exists():
            _retry(console, "There is nothing at that path.")
        elif not os.access(path, os.R_OK):
            _retry(console, "That cannot be read by this account.")
        else:
            return path


def _ask_output_dir(console: Console, question: str) -> Path:
    """Ask where results should go — the one path that may not exist yet.

    Checked as far as it can be checked here: a folder that is really a file, or
    one whose parent is missing, is caught now rather than after a long run.

    The question varies by flow ("the finished charts", "the results"); the
    reassurance underneath it does not, and it is the half that has to stay the
    same everywhere — an operator who reads "readable only by you" once should
    not have to wonder whether it still holds on the next screen.
    """
    _print_question(
        console,
        question,
        "This folder is created if it is not there yet, and stays readable only by you.",
    )
    while True:
        path = _typed_path(console, "Folder")
        if path is None:
            continue
        if path.exists() and not path.is_dir():
            _retry(console, "There is a file at that path already.")
        elif not path.exists() and not path.parent.is_dir():
            _retry(console, "The folder above that one does not exist yet.")
        else:
            return path


def _typed_path(console: Console, label: str) -> Path | None:
    """Read one path answer. ``None`` means it was empty and was asked again."""
    from pathlib import Path

    answer = _read(console, label)
    if not answer:
        _retry(console, "A path is needed here.")
        return None
    return Path(answer).expanduser()


# --- what happened -----------------------------------------------------------


def _report(console: Console, plan: Plan, exit_code: int) -> None:
    """Close the session: one plain sentence about the run, then what is next.

    A refusal keeps every word the command printed; all this adds is one
    sentence framing it, so nobody has to guess whether anything was written.

    Exit 0 alone cannot say which happened. A command that ends because the
    operator answered "no" to its own confirmation has not failed, so it exits
    0 too — and this used to read that as done and print "Filing has finished."
    in the success style, immediately after the operator's own refusal. The
    command records the refusal itself (`core.outcome`); anything else here
    would be the guide guessing.
    """
    from anastomosis.core.outcome import take_declined

    console.print()
    refusal = take_declined()
    if refusal is not None:
        _say(console, refusal, style=BRAND_PALETTE.attention)
        _say(
            console,
            "Nothing was changed. Re-run when you are ready.",
            style=BRAND_PALETTE.ink_muted,
        )
        return
    if exit_code == 0:
        _say(console, plan.finished, style=BRAND_PALETTE.ok)
        if plan.next_step:
            _say(console, plan.next_step, style=BRAND_PALETTE.ink_muted)
        return
    _say(console, "Anastomosis stopped without finishing.", style=BRAND_PALETTE.stop)
    for note in plan.notes:
        _say(console, note, style=BRAND_PALETTE.ink_muted)
    _say(console, "The reason is printed above.", style=BRAND_PALETTE.ink_muted)
