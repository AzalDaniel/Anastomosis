"""The reader and the writer share their vocabulary; they do not copy
it. `sources/ccda/parser.py` and `deliver/ccda_export/builder.py` are
two halves of one round trip, and the builder must emit exactly what
the parser traverses — a value that must agree across a boundary
should not be a hand-mirrored comment a reader keeps current.

This reads syntax, not behaviour, because a drifted section code does
not crash: the parser simply stops recognising it and captures it as
foreign narrative instead, quietly lossy, every test still passing.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from anastomosis.core import ccda_codes

_SRC = Path(__file__).resolve().parents[2] / "src" / "anastomosis"
_HALVES = {
    "parser": _SRC / "sources" / "ccda" / "parser.py",
    "builder": _SRC / "deliver" / "ccda_export" / "builder.py",
}

#: One-sided on purpose: the writer stamps a version the parser does not read.
_ONE_SIDED = {"LOSS_NARRATIVE_TEMPLATE_VERSION"}


def _module_constants(path: Path) -> dict[str, object]:
    """Module-level `NAME = <literal>` assignments."""
    return {
        node.targets[0].id: node.value.value
        for node in ast.parse(path.read_text(encoding="utf-8")).body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant)
    }


@pytest.mark.parametrize("half", sorted(_HALVES))
def test_neither_half_redefines_a_shared_constant(half: str) -> None:
    own = _module_constants(_HALVES[half])
    shadowed = sorted(set(own) & set(ccda_codes.__all__))
    assert not shadowed, (
        f"{half} redefines what core.ccda_codes already owns: {shadowed} — "
        "a constant that must agree across the round trip is defined once"
    )


def test_the_two_halves_share_no_constant_by_copy() -> None:
    """Nothing is defined identically in both files any more.

    The set difference is the point: a value in both, with the same name and
    the same literal, is a mirror somebody has to maintain by hand.
    """
    parser = _module_constants(_HALVES["parser"])
    builder = _module_constants(_HALVES["builder"])
    copied = sorted(
        name
        for name, value in builder.items()
        if name in parser and parser[name] == value and name not in _ONE_SIDED
    )
    assert not copied, f"defined identically in both halves: {copied}"


def test_every_shared_constant_is_read_by_both_halves() -> None:
    """A "shared" constant only one half imports belongs to that half.

    Moving a one-sided value here would be indirection dressed as a contract —
    and it would tell the next reader that the other half depends on it.
    """
    used: dict[str, set[str]] = {name: set() for name in ccda_codes.__all__}
    for half, path in _HALVES.items():
        source = path.read_text(encoding="utf-8")
        body = source.split(")\n", 1)[1] if ")\n" in source else source
        for name in used:
            if name in body:
                used[name].add(half)
    lonely = sorted(name for name, halves in used.items() if len(halves) < 2)
    assert not lonely, f"shared constants only one half actually reads: {lonely}"


#: An HL7 OID or template id: dotted digits, long enough not to be a version.
_OID_RE = re.compile(r"^\d+(?:\.\d+){4,}$")


def _string_literals(path: Path) -> set[str]:
    """Every string literal in a module, wherever it sits."""
    return {
        node.value
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def test_no_oid_is_shared_between_the_halves_as_a_bare_literal() -> None:
    """The named-constant check has a blind spot: a value spelled out
    inline in both halves is a shared value whether or not it has a
    name, and its drift is silent by nature — a check that only
    compares `NAME = literal` assignments cannot see a bare literal
    typed into an `elif` on the reader's side."""
    shared = _string_literals(_HALVES["parser"]) & _string_literals(_HALVES["builder"])
    owned = {value for value in vars(ccda_codes).values() if isinstance(value, str)}
    stray = sorted(oid for oid in shared if _OID_RE.match(oid) and oid not in owned)
    assert not stray, (
        f"OIDs spelled out in both halves instead of shared via core.ccda_codes: {stray}"
    )
