"""An option that two commands share is declared once.

Seven `Annotated[...]` blocks were written out in full in each of three command
modules — including a four-line ``--trust-pack`` help string repeated word for
word. Three copies of the same sentence is not a style problem, it is a drift
problem: change one and the other two go on telling the operator something
slightly different, and nothing anywhere fails.

The alias itself is the fix; this is what stops the copies coming back. It
reads the command modules' syntax rather than their behaviour, because the
failure it guards against — a fourth command that re-types `--pack-dir` instead
of importing it — produces perfectly working code.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from anastomosis.cli_commands import _options

_COMMANDS = Path(__file__).resolve().parents[2] / "src" / "anastomosis" / "cli_commands"

#: The flag each shared alias owns. A command module may not name one of these
#: in a `typer.Option(...)` of its own — with one carve-out, below.
_OWNED = {
    "--out": "OutDir",
    "--source": "Source",
    "--pack": "Pack",
    "--pack-dir": "PackDirs",
    "--trust-pack": "TrustPack",
    "--force": "Force",
    "--qa/--no-qa": "QaFlag",
}

#: A carve-out named here is a decision; a re-declaration not named here is a
#: copy. Each of these is the SAME FLAG meaning a DIFFERENT THING — which is
#: exactly when an alias would be wrong, because it would make two unrelated
#: options impossible to tell apart.
_ALLOWED = {
    # `archive`/`bundle` say something different about where their deliverable
    # lands, and that wording is the only thing separating the pair.
    ("delivery.py", "--out"),
    # `migrate` needs a THIRD state — unset, so a saved profile can supply it —
    # which the shared boolean cannot express.
    ("migrate.py", "--qa/--no-qa"),
    # Not template-pack directories: where to find a pack SCAFFOLD to write.
    ("destination.py", "--pack-dir"),
    # Not template-pack directories: where to find the DESTINATION pack.
    ("upload.py", "--pack-dir"),
}


def _option_flags(path: Path) -> set[str]:
    """Every flag string this module passes to `typer.Option(...)` directly."""
    flags: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name != "Option":
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                flags.add(arg.value)
    return flags


@pytest.mark.parametrize(
    "module", sorted(p.name for p in _COMMANDS.glob("*.py") if not p.name.startswith("_"))
)
def test_no_command_redeclares_a_shared_option(module: str) -> None:
    flags = _option_flags(_COMMANDS / module)
    repeated = sorted(
        f"{flag} (use {alias})"
        for flag, alias in _OWNED.items()
        if flag in flags and (module, flag) not in _ALLOWED
    )
    assert not repeated, f"{module} re-declares an option that already has an alias: {repeated}"


def test_every_alias_is_used_by_at_least_two_commands() -> None:
    """An alias only one command uses is indirection, not sharing.

    The point of the module is that two callers cannot drift apart. One caller
    cannot drift from anything, and putting its option somewhere else only
    makes it harder to read.
    """
    modules = [p for p in _COMMANDS.glob("*.py") if not p.name.startswith("_")]
    users: dict[str, int] = dict.fromkeys(_options.__all__, 0)
    for path in modules:
        source = path.read_text(encoding="utf-8")
        for alias in users:
            # The annotation position, not the import line.
            if f": {alias}" in source or f": {alias} =" in source:
                users[alias] += 1
    lonely = sorted(alias for alias, n in users.items() if n < 2)
    assert not lonely, f"aliases with fewer than two callers: {lonely}"


def test_the_aliases_are_what_the_module_defines() -> None:
    """`__all__` is the module's contract, so it may not drift from the file.

    Read off the source rather than the namespace: `Annotated` and `Path` are
    capitalised imports, and a namespace check counts those as aliases.
    """
    tree = ast.parse((_COMMANDS / "_options.py").read_text(encoding="utf-8"))
    defined = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id[:1].isupper()
    }
    assert set(_options.__all__) == defined
