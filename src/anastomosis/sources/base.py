"""The source-adapter contract and registry.

An adapter declares what it can read and proves it can read a given folder
*before* the pipeline commits to it:

* ``detect(path)`` — cheap structural sniff ("does this look like my
  format?"), used by ``anast pipeline run`` auto-detection and by the GUI's
  source picker. Never raises; unknown is just ``False``.
* ``load(path)`` — yields one fully-joined :class:`PatientRecord` per
  patient. Loud on malformed data (the lossless guarantee forbids silent
  skips); per-row tolerance decisions live inside each adapter where the
  format knowledge is.

The registry is deliberately boring: explicit ``register`` calls at import
time, no metaclass magic, defensive lookups with diagnoses.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Protocol, runtime_checkable

from anastomosis.core.model import PatientRecord

__all__ = ["SourceAdapter", "available_sources", "detect_source", "get_source", "register"]


@runtime_checkable
class SourceAdapter(Protocol):
    """What every source adapter provides."""

    #: CLI/GUI identifier, e.g. ``"pf-tebra"``.
    name: str
    #: Human description shown in pickers and ``anast info``.
    description: str

    def detect(self, path: Path) -> bool:
        """Cheap check: does ``path`` look like this adapter's export format?"""
        ...

    def load(self, path: Path) -> Iterator[PatientRecord]:
        """Parse the export at ``path`` into canonical patient records."""
        ...


_REGISTRY: dict[str, SourceAdapter] = {}


def register(adapter: SourceAdapter) -> SourceAdapter:
    """Add an adapter to the registry (idempotent re-registration is an error)."""
    if adapter.name in _REGISTRY:
        raise ValueError(f"source adapter {adapter.name!r} is already registered")
    _REGISTRY[adapter.name] = adapter
    return adapter


def get_source(name: str) -> SourceAdapter:
    """Look up an adapter by name, with a diagnosis listing what exists."""
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "none registered"
        raise KeyError(f"unknown source {name!r} (available: {known})") from None


def available_sources() -> list[SourceAdapter]:
    return [_REGISTRY[name] for name in sorted(_REGISTRY)]


def detect_source(path: Path) -> SourceAdapter | None:
    """Return the unique adapter whose ``detect`` matches, else ``None``.

    Ambiguity (two adapters claiming one folder) returns ``None`` rather
    than guessing — the caller asks the user instead.
    """
    matches = [adapter for adapter in available_sources() if adapter.detect(path)]
    return matches[0] if len(matches) == 1 else None
