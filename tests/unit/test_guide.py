"""The guided session bare ``anast`` opens on a terminal.

Four things are pinned here, in the order they can hurt someone:

* **A script never sees a prompt.** Non-interactive stdin gets exactly the help
  page (and exit code) ``anast`` has always printed — CI cannot hang.
* **The gathered answers reach the real command.** Each flow is driven with
  scripted keystrokes and the execution seam is inspected, so a renamed option
  or a dropped argument fails here rather than in a clinic.
* **Refusals stay honest.** A bad path is refused and asked again; Ctrl-C before
  anything runs says nothing was changed and exits 130.
* **The register holds.** The guide's own copy is linted for the engineering
  vocabulary ``docs/design/COPY_MAP.md`` bans, and for exclamation marks.
"""

from __future__ import annotations

import ast
import io
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from rich.console import Console
from typer.testing import CliRunner

from anastomosis.cli import app
from anastomosis.cli_commands import guide

if TYPE_CHECKING:
    from collections.abc import Sequence

runner = CliRunner()

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"


class _Session:
    """One scripted run of the guide: answers in, output and argv out."""

    def __init__(self) -> None:
        # Wide enough that no asserted sentence ever soft-wraps: CI temp paths
        # (Windows especially) are long, and a wrap splits an asserted path
        # mid-word across a newline.
        self.console = Console(file=io.StringIO(), width=400)
        self.calls: list[list[str]] = []
        self.exit_code = 0

    @property
    def output(self) -> str:
        assert isinstance(self.console.file, io.StringIO)
        return self.console.file.getvalue()


def _drive(
    monkeypatch: pytest.MonkeyPatch,
    answers: Sequence[str],
    *,
    dispatch_code: int = 0,
    interrupt_at: int | None = None,
) -> _Session:
    """Run the guide with ``answers`` typed at its prompts, dispatch stubbed.

    ``interrupt_at`` raises :class:`KeyboardInterrupt` from that (0-based)
    prompt instead of answering it — the Ctrl-C a person actually presses.
    Exhausting the answers raises :class:`EOFError`, so a flow that asks more
    questions than the script expects fails loudly instead of blocking.
    """
    session = _Session()
    queue = list(answers)
    asked = 0

    def _fake_input(prompt: str = "") -> str:
        nonlocal asked
        if interrupt_at is not None and asked == interrupt_at:
            raise KeyboardInterrupt
        asked += 1
        if not queue:
            raise EOFError
        return queue.pop(0)

    def _fake_dispatch(argv: Sequence[str]) -> int:
        session.calls.append(list(argv))
        return dispatch_code

    monkeypatch.setattr("builtins.input", _fake_input)
    monkeypatch.setattr(guide, "_dispatch", _fake_dispatch)
    session.exit_code = guide.run_guide(session.console)
    return session


# --- the gate: who gets the guide, and who gets the help page ----------------


def test_bare_anast_without_a_terminal_prints_the_unchanged_help_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole safety property: no TTY, no guide, no prompt, no hang."""

    def _never(console: object) -> int:
        raise AssertionError("the guide must not run without an interactive terminal")

    monkeypatch.setattr(guide, "run_guide", _never)
    bare = runner.invoke(app, [])
    explicit = runner.invoke(app, ["--help"])
    assert bare.exit_code == 2, bare.output
    # Typer's rich help interleaves ANSI styling through the text in some
    # environments (CI enables color where a local capture does not), so the
    # substring is asserted on the UNSTYLED text; the identical-output check
    # compares raw bytes captured under the same environment, so it stays raw.
    plain = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", bare.output)
    assert "Usage: anast" in plain, plain
    # Byte-identical to `--help` (which ends with one extra blank line) — the
    # page an operator and every script have always been shown here.
    assert bare.output.rstrip("\n") == explicit.output.rstrip("\n")
    assert "What would you like to do?" not in plain


def test_bare_anast_on_a_terminal_runs_the_guide(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guide, "is_interactive_terminal", lambda console: True)
    monkeypatch.setattr(guide, "run_guide", lambda console: 7)
    result = runner.invoke(app, [])
    assert result.exit_code == 7, result.output


def test_a_named_command_is_untouched_by_the_callback() -> None:
    result = runner.invoke(app, ["info"])
    assert result.exit_code == 0, result.output
    assert "anastomosis" in result.output


@pytest.mark.parametrize("stdin_is_tty", [True, False])
def test_is_interactive_terminal_needs_both_ends(
    monkeypatch: pytest.MonkeyPatch, *, stdin_is_tty: bool
) -> None:
    class _Stdin:
        def isatty(self) -> bool:
            return stdin_is_tty

    monkeypatch.setattr("sys.stdin", _Stdin())
    on_terminal = Console(file=io.StringIO(), force_terminal=True)
    off_terminal = Console(file=io.StringIO())
    assert guide.is_interactive_terminal(on_terminal) is stdin_is_tty
    assert guide.is_interactive_terminal(off_terminal) is False


def test_is_interactive_terminal_survives_a_closed_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    closed = io.StringIO()
    closed.close()
    monkeypatch.setattr("sys.stdin", closed)
    assert guide.is_interactive_terminal(Console(file=io.StringIO(), force_terminal=True)) is False


# --- the menu ----------------------------------------------------------------


def test_the_menu_lists_every_choice_in_the_shared_vocabulary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _drive(monkeypatch, ["q"])
    assert session.exit_code == 0
    assert session.calls == []
    for label in (
        "1  Rebuild charts from an EHR export",
        "2  Move charts into another system",
        "3  Watch charts being filed",
        "4  Teach a new export format or document layout",
        "5  Check this installation",
        "q  Quit",
    ):
        assert label in session.output
    assert "Anastomosis" in session.output  # the header's one identity moment


def test_an_unknown_menu_key_is_asked_again(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _drive(monkeypatch, ["7", "5", ""])
    assert "Type one of the numbers listed above" in session.output
    assert session.calls == [["doctor"]]


# --- the flows reach the real commands ---------------------------------------


def test_flow_1_rebuilds_charts_with_the_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "charts"
    session = _drive(
        monkeypatch,
        [
            "1",  # rebuild charts
            str(FIXTURE),  # export folder
            str(out),  # results folder
            "",  # export format: work it out
            "",  # chart layout: the default
            "",  # double-check: yes
            "",  # prepare for filing: no
            "",  # continue: yes
        ],
    )
    assert session.exit_code == 0, session.output
    assert session.calls == [
        ["pipeline", "run", str(FIXTURE), "--out", str(out), "--pack", "generic_soap"]
    ]
    assert f"The charts are in {out}." in session.output


def test_flow_1_carries_every_answer_into_the_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "charts"
    names = [value for value, _label in guide._source_options()]
    # The format list is "work it out" plus every adapter, in that order.
    chosen = "pf-tebra"
    session = _drive(
        monkeypatch,
        [
            "1",
            str(FIXTURE),
            str(out),
            str(names.index(chosen) + 2),
            "acme_soap",
            "no",  # no double-check
            "yes",  # prepare for filing
            "",
        ],
    )
    assert session.calls == [
        [
            "pipeline",
            "run",
            str(FIXTURE),
            "--out",
            str(out),
            "--source",
            chosen,
            "--pack",
            "acme_soap",
            "--no-qa",
            "--upload-manifest",
        ]
    ]
    assert "choose 3 to file these charts" in session.output


def test_flow_2_prepares_a_move(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    out = tmp_path / "move"
    sources = [value for value, _label in guide._source_options()]
    destinations = [value for value, _label in guide._destination_options()]
    session = _drive(
        monkeypatch,
        [
            "2",
            str(FIXTURE),
            str(out),
            str(sources.index("pf-tebra") + 1),
            str(destinations.index("tebra") + 1),
            "",  # chart pages: laid out for reading
            "",
        ],
    )
    assert session.calls == [
        [
            "migrate",
            str(FIXTURE),
            "--out",
            str(out),
            "--from",
            "pf-tebra",
            "--to",
            "tebra",
            "--render",
            "neutral",
        ]
    ]
    assert "Nothing has been sent yet." in session.output


def test_flow_3_files_through_a_browser(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    charts = tmp_path / "charts"
    charts.mkdir()
    destinations = [value for value, _label in guide._destination_options()]
    session = _drive(
        monkeypatch,
        [
            "3",
            str(charts),
            "",  # through a browser
            str(destinations.index("tebra") + 1),
            "",  # the suggested browser connection
            "",  # double-check after filing
            "",
        ],
    )
    assert session.calls == [
        [
            "upload",
            str(charts),
            "--to",
            "tebra",
            "--cdp",
            guide.DEFAULT_BROWSER_CONNECTION,
        ]
    ]


def test_flow_3_files_over_the_api_route(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    charts = tmp_path / "charts"
    charts.mkdir()
    session = _drive(
        monkeypatch,
        ["3", str(charts), "2", "https://ehr.example.com/fhir", "no", ""],
    )
    assert session.calls == [
        ["upload", str(charts), "--fhir", "https://ehr.example.com/fhir", "--no-verify"]
    ]


def test_flow_4_teaches_an_export_format(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    example = tmp_path / "example.csv"
    example.write_text("patient_id,dos\n", encoding="utf-8")
    session = _drive(monkeypatch, ["4", "", str(example), "Acme_Clinic", ""])
    assert session.calls == [["source", "init", str(example), "--name", "acme_clinic"]]


def test_flow_4_teaches_a_document_layout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    samples = tmp_path / "samples"
    samples.mkdir()
    session = _drive(monkeypatch, ["4", "2", str(samples), "acme_soap", ""])
    assert session.calls == [
        ["pack", "init", "--from-samples", str(samples), "--name", "acme_soap"]
    ]


def test_flow_5_checks_the_installation(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _drive(monkeypatch, ["5", ""])
    assert session.calls == [["doctor"]]
    assert "This installation is complete." in session.output


def test_the_dispatch_seam_runs_the_real_command(capsys: pytest.CaptureFixture[str]) -> None:
    """The seam every other test stubs is the genuine article, not a shim.

    ``info`` is the cheapest command that touches no path an operator owns; what
    matters is that the guide's argument list really reaches a command and comes
    back with that command's own exit status.
    """
    assert guide._dispatch(["info"]) == 0
    assert "anastomosis" in capsys.readouterr().out


# --- refusing bad answers ----------------------------------------------------


def test_a_missing_folder_is_refused_and_asked_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = _drive(
        monkeypatch,
        [
            "1",
            str(tmp_path / "nowhere"),
            "",
            str(FIXTURE),
            str(tmp_path / "out"),
            "",
            "",
            "",
            "",
            "",
        ],
    )
    assert "There is nothing at that path." in session.output
    assert "A path is needed here." in session.output
    assert session.calls[0][2] == str(FIXTURE)


def test_a_file_where_a_folder_belongs_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a_file = tmp_path / "notes.txt"
    a_file.write_text("x", encoding="utf-8")
    session = _drive(
        monkeypatch,
        ["1", str(a_file), str(FIXTURE), str(a_file), str(tmp_path / "out"), "", "", "", "", ""],
    )
    assert "This step needs the folder holding it." in session.output
    assert "There is a file at that path already." in session.output
    assert session.calls[0][4] == str(tmp_path / "out")


def test_an_output_folder_under_a_missing_parent_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Caught now rather than after a long run has already read the export."""
    nowhere = tmp_path / "missing" / "charts"
    out = tmp_path / "charts"
    session = _drive(
        monkeypatch,
        ["1", str(FIXTURE), str(nowhere), str(out), "", "", "", "", ""],
    )
    assert "The folder above that one does not exist yet." in session.output
    assert session.calls[0][4] == str(out)


def test_a_yes_or_no_question_takes_only_yes_or_no(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _drive(monkeypatch, ["5", "maybe", ""])
    assert "Type yes or no." in session.output
    assert session.calls == [["doctor"]]


def test_a_name_must_be_a_label_the_command_will_accept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    samples = tmp_path / "samples"
    samples.mkdir()
    session = _drive(monkeypatch, ["4", "2", str(samples), "9 lives", "acme_soap", ""])
    assert "Use letters, digits and underscores, starting with a letter." in session.output
    assert session.calls[0][-1] == "acme_soap"


# --- leaving, and being interrupted ------------------------------------------


def test_ctrl_c_before_anything_runs_says_so_and_exits_130(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _drive(monkeypatch, [], interrupt_at=0)
    assert session.exit_code == guide.INTERRUPTED_EXIT_CODE == 130
    assert session.calls == []
    assert "Nothing was changed." in session.output


def test_ctrl_c_mid_flow_still_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _drive(monkeypatch, ["1"], interrupt_at=1)
    assert session.exit_code == 130
    assert session.calls == []


def test_declining_the_confirmation_runs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _drive(monkeypatch, ["5", "no"])
    assert session.exit_code == 0
    assert session.calls == []
    assert "Nothing was changed." in session.output


def test_a_refused_command_is_framed_but_never_softened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _drive(monkeypatch, ["5", ""], dispatch_code=1)
    assert session.exit_code == 1
    assert "Anastomosis stopped without finishing." in session.output
    assert "The reason is printed above." in session.output


# --- the register ------------------------------------------------------------

#: Vocabulary a clinician never has to learn (DESIGN_LANGUAGE §10.2 plus the
#: cross-cutting bans in COPY_MAP). Matched whole-word, case-insensitively.
BANNED_WORDS = (
    "pipeline",
    "manifest",
    "ledger",
    "CDP",
    "selectors",
    "item-key",
    "extra",
    "viable",
    "ritual",
    "milestone",
    "pack",
    "payload",
    "operator",
    "surface",
    "drive",
    "driven",
    "round-trip",
    "PHI",
)

#: The literals that are ARGUMENTS to a command, not words anybody reads: the
#: guide types these at the CLI on the person's behalf. They are exempt from the
#: sweep below, and the sweep proves each one is still really used.
COMMAND_TOKENS = frozenset({"pipeline", "pack", "--pack", "--cdp", "--upload-manifest"})


def _guide_copy() -> list[str]:
    """Every string literal in guide.py that a person can end up reading.

    Docstrings are excluded: they are written for whoever maintains this file,
    not for the clinician at the prompt, and they name the commands and options
    the flows drive. Everything else in the module is copy.
    """
    source = Path(guide.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_the_guide_speaks_no_engineering_vocabulary() -> None:
    offenders: list[str] = []
    for literal in _guide_copy():
        if literal in COMMAND_TOKENS:
            continue
        for word in BANNED_WORDS:
            if re.search(rf"\b{re.escape(word)}\b", literal, flags=re.IGNORECASE):
                offenders.append(f"{word!r} in {literal!r}")
    assert not offenders, "the guide must not use engineering vocabulary:\n  " + "\n  ".join(
        offenders
    )


def test_the_guide_never_raises_its_voice() -> None:
    shouting = [literal for literal in _guide_copy() if "!" in literal]
    assert not shouting, f"no exclamation marks in this product's copy: {shouting}"


def test_the_command_token_exemption_carries_no_dead_entries() -> None:
    """The exemption above may only cover literals the guide really types."""
    literals = set(_guide_copy())
    assert COMMAND_TOKENS <= literals, sorted(COMMAND_TOKENS - literals)
