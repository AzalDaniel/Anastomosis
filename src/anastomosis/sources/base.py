"""The source-adapter contract and registry.

``detect(path)`` proves an adapter can read a folder before the pipeline
commits to it; it never raises, unknown is ``False``. ``load(path)`` yields
one joined :class:`PatientRecord` per patient and is loud on malformed data.

An adapter may declare RENDER-SELECTION rules (:class:`SelectionRule`) that
park an encounter in ``extensions`` instead of rendering it; a run switches
any of them off (:func:`with_selection`). The registry is explicit
``register()`` calls at import time — no metaclass magic."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from anastomosis.core.model import PatientRecord

__all__ = [
    "QuarantinedRows",
    "SelectionRule",
    "SourceAdapter",
    "SourceDataError",
    "available_sources",
    "detect_source",
    "get_source",
    "register",
    "selection_rules",
    "with_selection",
]


class SourceDataError(Exception):
    """An adapter's fail-closed refusal, safe to print verbatim: schema/table
    names and counts only, never a cell value or id (rule 2). The pipeline
    passes its message straight to the CLI/GUI; an arbitrary adapter
    exception may embed the input that caused it, so only its TYPE may be
    shown."""


@dataclass(frozen=True)
class QuarantinedRows:
    """Rows an adapter could not attribute to a patient — held, never merged
    into a neighbour (rule 66, #247). ``table`` names the source table,
    ``reason`` is schema-only (never a row value), ``rows`` are verbatim. An
    adapter exposes ``quarantine`` (a list of these) via ``getattr`` — one
    with nothing to hold back needs no attribute at all."""

    table: str
    reason: str
    rows: tuple[dict[str, str | None], ...]


@dataclass(frozen=True)
class SelectionRule:
    """Contract: one render-selection rule, schema only — never data. ``name``
    is the CLI/GUI toggle key; ``reason`` is the exact string the adapter
    stamps on an excluded encounter (fixed by the ``:skipped_encounters``
    extension and the selection report, not free text); ``label`` is the
    sentence a picker shows."""

    name: str
    reason: str
    label: str


@runtime_checkable
class SourceAdapter(Protocol):
    """What every source adapter provides."""

    #: CLI/GUI identifier, e.g. ``"pf-tebra"``.
    name: str
    #: What a person should read instead of ``name`` — "Practice Fusion /
    #: Tebra", not "pf-tebra"; a re-cased id can't produce "C-CDA" from "ccda".
    display: str
    #: Human description shown in pickers and ``anast info``.
    description: str

    def detect(self, path: Path) -> bool:
        """Cheap check: does ``path`` look like this adapter's export format?"""
        ...

    def load(self, path: Path) -> Iterator[PatientRecord]:
        """Parse the export at ``path`` into canonical patient records."""
        ...


def selection_rules(adapter: SourceAdapter) -> tuple[SelectionRule, ...]:
    """The render-selection rules ``adapter`` applies, empty for one with none.
    Read through ``getattr``, same as ``quarantine``: optional, so an adapter
    that excludes nothing needs no attribute at all."""
    return tuple(getattr(adapter, "selection_rules", ()))


def with_selection(adapter: SourceAdapter, include: Iterable[str]) -> SourceAdapter:
    """The adapter this run loads through, given the rules told to drop.
    ``include`` names rules switched OFF; empty returns ``adapter`` unchanged.
    Raises ``TypeError`` if the adapter declares rules but has no
    ``including`` method — a programming error: names are pre-validated by
    the caller against :func:`selection_rules`."""
    dropped = frozenset(include)
    if not dropped:
        return adapter
    including = getattr(adapter, "including", None)
    if including is None:
        raise TypeError(
            f"source adapter {adapter.name!r} declares selection rules but cannot "
            "be configured with them (no `including` method)"
        )
    configured: SourceAdapter = including(dropped)
    return configured


_REGISTRY: dict[str, SourceAdapter] = {}

# Module paths, not eager imports: each pulls in core.model + destinations +
# yaml, and CLI startup (--help, doctor, gui) must not pay for an unused registry.
_builtins_loaded = False


def _ensure_builtin_adapters() -> None:
    """Import each built-in adapter once; idempotent. Literal imports, never
    importlib over strings — the frozen Windows build ships only
    statically-reachable modules, and these are each adapter's only
    remaining reference. ``sources.learned`` self-registers separately via
    ``register_learned_sources()``."""
    global _builtins_loaded
    if _builtins_loaded:
        return
    import anastomosis.sources.ccda
    import anastomosis.sources.fhir_r4
    import anastomosis.sources.oracle_ehi
    import anastomosis.sources.pf_tebra  # noqa: F401

    _builtins_loaded = True


def register(adapter: SourceAdapter) -> SourceAdapter:
    """Add an adapter to the registry (idempotent re-registration is an error)."""
    if adapter.name in _REGISTRY:
        raise ValueError(f"source adapter {adapter.name!r} is already registered")
    _REGISTRY[adapter.name] = adapter
    return adapter


def get_source(name: str) -> SourceAdapter:
    """Look up an adapter by name, with a diagnosis listing what exists."""
    _ensure_builtin_adapters()
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "none registered"
        raise KeyError(f"unknown source {name!r} (available: {known})") from None


def available_sources() -> list[SourceAdapter]:
    _ensure_builtin_adapters()
    return [_REGISTRY[name] for name in sorted(_REGISTRY)]


def detect_source(path: Path) -> SourceAdapter | None:
    """The unique adapter whose ``detect`` matches, else ``None`` — two
    adapters claiming one folder returns ``None`` rather than guessing; the
    caller asks the user instead."""
    _ensure_builtin_adapters()
    matches = [adapter for adapter in available_sources() if adapter.detect(path)]
    return matches[0] if len(matches) == 1 else None
