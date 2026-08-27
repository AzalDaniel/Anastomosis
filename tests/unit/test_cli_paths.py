"""A CLI option that names a path refuses a blank one, on every command.

`--out ""` used to mean "here": Typer types the option as `Path`, so `Path("")`
became `Path(".")` before the command body existed, `validate_output_target`
saw a directory that exists, and the run wrote patient-named PDFs into whatever
directory the operator launched from — hardening that directory to 0700
underneath them and reporting success. The fix for that (#123) had reached the
GUI door only.

These tests hold two lines. The first is that every option which can name an
output still refuses a blank value; the parametrised list is the audit's own
inventory, so an option added later without a parser shows up as a gap here
rather than in someone's working directory.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from anastomosis.cli import app
from anastomosis.cli_commands._paths import out_dir

CLI_COMMANDS = Path(__file__).resolve().parents[2] / "src" / "anastomosis" / "cli_commands"

runner = CliRunner()

#: CI sets GITHUB_ACTIONS, which makes Typer's rich integration force a terminal
#: (typer/rich_utils.py) — so an option name comes back with escape codes
#: threaded through it and a plain `"--out" in output` misses. Strip first.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    return _ANSI.sub("", text)


#: (argv, the option that must refuse). Each is a real invocation shape.
BLANKABLE = [
    (["pipeline", "run", "EXPORT", "--out", ""], "--out"),
    (["pipeline", "run", "EXPORT", "--out", "OUT", "--archive", ""], "--archive"),
    (["pipeline", "run", "EXPORT", "--out", "OUT", "--bundle", ""], "--bundle"),
    (["pipeline", "run", "EXPORT", "--out", "OUT", "--ccda", ""], "--ccda"),
    (["migrate", "EXPORT", "--out", "", "--from", "pf-tebra", "--to", "tebra"], "--out"),
]


@pytest.mark.parametrize(("argv", "option"), BLANKABLE, ids=[o for _, o in BLANKABLE])
def test_a_blank_output_is_refused_not_read_as_here(
    argv: list[str], option: str, tmp_path: Path
) -> None:
    export = tmp_path / "export"
    export.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    filled = [str(export) if a == "EXPORT" else str(out) if a == "OUT" else a for a in argv]

    before = sorted(p.name for p in tmp_path.iterdir())
    result = runner.invoke(app, filled)

    plain = _plain(result.output)
    assert result.exit_code != 0, f"{option} accepted a blank value:\n{plain}"
    assert option in plain, plain
    assert "no output" in plain.lower(), plain
    # And nothing was written on the way to refusing.
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_a_pasted_windows_path_works_on_the_cli_too(tmp_path: Path) -> None:
    """Explorer's "Copy as path" quotes; the CLI never unquoted it."""
    assert out_dir(f'"{tmp_path}"') == tmp_path
    assert out_dir(f"  {tmp_path}  ") == tmp_path
    assert out_dir(str(tmp_path)) == tmp_path


def _options_missing_a_parser(source: str) -> list[str]:
    """Option flags declared with a `Path` type and no `parser=`.

    A `Path`-typed option is the exact shape that swallows a blank value, so
    every one of them has to name a parser. Read off the syntax tree rather
    than a grep, because these declarations wrap across lines.
    """
    missing: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Subscript):
            continue
        # Annotated[Path | None, typer.Option(...)]
        base = node.value
        if not (isinstance(base, ast.Name) and base.id == "Annotated"):
            continue
        elts = node.slice.elts if isinstance(node.slice, ast.Tuple) else []
        if len(elts) < 2:
            continue
        annotation = ast.unparse(elts[0])
        if "Path" not in annotation:
            continue
        call = elts[1]
        if not (isinstance(call, ast.Call) and ast.unparse(call.func) == "typer.Option"):
            continue
        if any(kw.arg == "parser" for kw in call.keywords):
            continue
        flags = [a.value for a in call.args if isinstance(a, ast.Constant)]
        if flags:
            missing.append(flags[0])
    return missing


@pytest.mark.parametrize("module", sorted(p.name for p in CLI_COMMANDS.glob("*.py")))
def test_every_path_option_names_a_parser(module: str) -> None:
    missing = _options_missing_a_parser((CLI_COMMANDS / module).read_text(encoding="utf-8"))
    # `--pack-dir` and friends name folders that must ALREADY exist, so Typer's
    # own `exists=True` refuses a blank one for us; only options that name
    # something to be created need the parser.
    missing = [flag for flag in missing if flag not in {"--pack-dir"}]
    assert not missing, (
        f"{module} declares {missing} as Path options with no parser — a blank "
        "value there becomes Path('.') before the command body runs. Give them "
        "parser=out_dir (see cli_commands/_paths.py)."
    )


def test_the_parser_check_catches_the_shape_it_forbids() -> None:
    bad = (
        'def f(\n    out: Annotated[Path, typer.Option("--out", "-o", help="x")],\n) -> None: ...\n'
    )
    assert _options_missing_a_parser(bad) == ["--out"]

    good = (
        "def f(\n"
        '    out: Annotated[Path, typer.Option("--out", "-o", parser=out_dir)],\n'
        '    name: Annotated[str, typer.Option("--name")],\n'
        ") -> None: ...\n"
    )
    assert _options_missing_a_parser(good) == []
