"""Tests for the source-adapter registry."""

import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

import anastomosis.sources.base as base
from anastomosis.core.model import Patient, PatientRecord
from anastomosis.sources.base import SourceAdapter, detect_source, get_source, register


class _FakeAdapter:
    def __init__(self, name: str, *, detects: bool) -> None:
        self.name = name
        self.display = f"Fake {name}"
        self.description = f"fake {name}"
        self._detects = detects

    def detect(self, path: Path) -> bool:
        return self._detects

    def load(self, path: Path) -> Iterator[PatientRecord]:
        yield PatientRecord(patient=Patient(given_name="Synthetic"))


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "_REGISTRY", {})
    # Also short-circuit the lazy built-in load: these tests probe the boring
    # registry mechanics with fakes only. Leaving the flag False here would
    # try to re-import the (possibly already-imported, in this same process)
    # built-in modules against a freshly emptied _REGISTRY — a module already
    # in sys.modules does not re-run its `register()` call on a second
    # import, so the registry would end up missing adapters other tests in
    # this file assume aren't there. The lazy-load path itself is covered
    # separately below, in a clean subprocess.
    monkeypatch.setattr(base, "_builtins_loaded", True)


def test_register_and_lookup() -> None:
    adapter = _FakeAdapter("fake", detects=True)
    register(adapter)
    assert get_source("fake") is adapter
    assert isinstance(adapter, SourceAdapter)


def test_double_registration_is_an_error() -> None:
    register(_FakeAdapter("fake", detects=True))
    with pytest.raises(ValueError, match="already registered"):
        register(_FakeAdapter("fake", detects=True))


def test_unknown_source_diagnosis_lists_available() -> None:
    register(_FakeAdapter("pf-tebra", detects=False))
    with pytest.raises(KeyError, match="pf-tebra"):
        get_source("epic-ehi")


def test_detect_source_unique_match(tmp_path: Path) -> None:
    winner = _FakeAdapter("a", detects=True)
    register(winner)
    register(_FakeAdapter("b", detects=False))
    assert detect_source(tmp_path) is winner


def test_detect_source_ambiguity_returns_none(tmp_path: Path) -> None:
    register(_FakeAdapter("a", detects=True))
    register(_FakeAdapter("b", detects=True))
    assert detect_source(tmp_path) is None


# --- lazy built-in loading ---------------------------------------------------


def test_builtin_adapter_modules_excludes_learned() -> None:
    """``sources.learned`` self-registers only through
    ``register_learned_sources()`` — it has no module-level ``register()``
    call, so it must not appear in the eager-load tuple."""
    import ast
    import inspect

    ensure_src = inspect.getsource(base._ensure_builtin_adapters)
    imported = [
        alias.name
        for node in ast.walk(ast.parse(ensure_src.replace("    def", "def", 1)))
        if isinstance(node, ast.Import)
        for alias in node.names
    ]
    assert imported, "the lazy loader must import the built-in adapters"
    assert all(name.startswith("anastomosis.sources.") for name in imported)
    assert not any("learned" in name for name in imported)


def test_ensure_builtin_adapters_is_idempotent() -> None:
    """A second call must not re-run the built-in modules' ``register()`` —
    that would raise (double registration), whether this is the first call in
    the process or the hundredth."""
    base._ensure_builtin_adapters()
    base._ensure_builtin_adapters()


def test_available_sources_lazily_registers_builtins_in_fresh_process() -> None:
    """The production entry point: a bare ``from anastomosis.sources import
    available_sources`` call, with no other module having imported an
    adapter first, must still resolve all four built-ins. Runs in a clean
    subprocess — this file's own registry-clearing fixture (and whatever
    other tests already ran in-process) would otherwise mask a regression
    where ``available_sources()`` stopped triggering the lazy load."""
    script = textwrap.dedent("""
        from anastomosis.sources import available_sources
        print(",".join(sorted(a.name for a in available_sources())))
    """)
    proc = subprocess.run(
        [sys.executable, "-c", script], check=True, capture_output=True, text=True
    )
    assert proc.stdout.strip() == "ccda,fhir-r4,oracle-ehi,pf-tebra"


def test_get_source_and_detect_source_also_lazily_register_in_fresh_process(
    tmp_path: Path,
) -> None:
    """``get_source`` and ``detect_source`` are independent entry points from
    ``available_sources`` — each must ensure the built-ins too, not rely on
    a caller having hit ``available_sources`` first."""
    script = textwrap.dedent(f"""
        from pathlib import Path
        from anastomosis.sources import detect_source, get_source
        assert get_source("pf-tebra").name == "pf-tebra"
        assert detect_source(Path({str(tmp_path)!r})) is None  # empty dir: no match, no crash
        print("ok")
    """)
    proc = subprocess.run(
        [sys.executable, "-c", script], check=True, capture_output=True, text=True
    )
    assert proc.stdout.strip() == "ok"
