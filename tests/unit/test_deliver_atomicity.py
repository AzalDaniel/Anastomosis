"""A killed run never leaves a truncated chart where a complete one was.

`core/atomic.py` says it is "the one place [the write-then-replace shape]
lives now, so every site gets the unlink-on-failure safety net without having
to remember to write it". The `deliver/` package had not been converted: every
final-artifact write in it — including the copied chart PDFs — wrote straight
over its target, so a crash partway through left a half-written file where a
complete one had been. `reconstruct/engine.py` renders the same PDF through
`atomic_replace` and says why: "a crash mid-write (or a concurrent reader)
never sees a partial PDF." The deliverer that copied it did not hold that.

Two tests: one proves the property on the shared helpers, the other reads the
package's syntax tree so a new write site cannot quietly reintroduce the gap.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from anastomosis.core.atomic import atomic_copy, atomic_write_bytes, atomic_write_text

DELIVER = Path(__file__).resolve().parents[2] / "src" / "anastomosis" / "deliver"

#: Writes that go straight to the target, by the name of the call.
RAW_WRITES = frozenset({"write_text", "write_bytes", "copyfile"})

#: The atomic equivalents every deliver/ site must use instead.
ATOMIC = frozenset({"atomic_write_text", "atomic_write_bytes", "atomic_copy", "atomic_replace"})


def test_a_failed_copy_leaves_the_previous_chart_intact(tmp_path: Path) -> None:
    """The case that matters: a chart page already exists and the copy dies."""
    source = tmp_path / "new.pdf"
    source.write_bytes(b"%PDF-1.7 complete new chart")
    target = tmp_path / "filed.pdf"
    target.write_bytes(b"%PDF-1.7 the chart already filed")
    before = target.read_bytes()

    atomic_copy(source, target)
    assert target.read_bytes() == source.read_bytes()

    # Now make the copy fail partway and confirm the target is untouched.
    missing = tmp_path / "not-there.pdf"
    target.write_bytes(before)
    with pytest.raises(OSError):
        atomic_copy(missing, target)
    assert target.read_bytes() == before, "a failed copy truncated the filed chart"
    assert not list(tmp_path.glob(".*.tmp")), "a temp file was left behind"


def test_a_failed_write_leaves_the_previous_file_intact(tmp_path: Path) -> None:
    target = tmp_path / "index.json"
    target.write_text('{"complete": true}', encoding="utf-8")
    before = target.read_text(encoding="utf-8")

    class Unserializable:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    for write in (
        lambda: atomic_write_text(target, str(Unserializable())),
        lambda: atomic_write_bytes(target, str(Unserializable()).encode()),
    ):
        with pytest.raises(RuntimeError):
            write()
        assert target.read_text(encoding="utf-8") == before
        assert not list(tmp_path.glob(".*.tmp")), "a temp file was left behind"


def _raw_write_calls(source: str) -> list[tuple[int, str]]:
    """`x.write_text(...)`, `x.write_bytes(...)` and `shutil.copyfile(...)` calls."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in RAW_WRITES:
            found.append((node.lineno, func.attr))
    return found


@pytest.mark.parametrize(
    "module", sorted(str(p.relative_to(DELIVER)) for p in DELIVER.rglob("*.py"))
)
def test_deliver_writes_only_through_the_atomic_helpers(module: str) -> None:
    raw = _raw_write_calls((DELIVER / module).read_text(encoding="utf-8"))
    assert not raw, (
        f"deliver/{module} writes straight to its target at "
        + ", ".join(f"line {line} ({call})" for line, call in raw)
        + f" — use one of {sorted(ATOMIC)}. A crash partway through leaves a "
        "truncated file where a complete one was, and for a chart page that is "
        "a patient's record, half-written."
    )


def test_the_write_check_catches_the_shape_it_forbids() -> None:
    """The guard is only worth having if it fails on what it forbids."""
    assert _raw_write_calls("def f(p, html):\n    p.write_text(html)\n") == [(2, "write_text")]
    assert _raw_write_calls("def f(p, b):\n    p.write_bytes(b)\n") == [(2, "write_bytes")]
    assert _raw_write_calls("def f(a, b):\n    shutil.copyfile(a, b)\n") == [(2, "copyfile")]
    assert _raw_write_calls("def f(p, html):\n    atomic_write_text(p, html)\n") == []
