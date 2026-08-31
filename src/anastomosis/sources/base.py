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

An adapter may also apply RENDER-SELECTION rules — reasons an encounter it
read is kept out of the render and parked in ``extensions`` instead. Those are
one practice's judgement, not a property of the format, so they are per-run
options: the adapter declares them (:class:`SelectionRule`) and a run may
switch any of them off (:func:`with_selection`). Both halves are optional and
read defensively, the way ``quarantine`` is — an adapter that selects nothing
declares nothing and every default run takes the path it always took.

The registry is deliberately boring: explicit ``register`` calls at import
time, no metaclass magic, defensive lookups with diagnoses.
"""

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
    """An adapter's fail-closed refusal whose MESSAGE the operator must see.

    A loud failure is only half the contract: the operator also needs to know
    *what* to repair. These refusals carry the diagnosis — the offending table
    or resource-type names and their row/resource COUNTS — and are written to be
    PHI-safe, schema names and integers only, never a cell value, an id, or any
    patient-derived string. That is what makes them safe to print verbatim.

    The pipeline distinguishes them from an arbitrary adapter exception (whose
    message may embed the input that caused it, so only its TYPE may be shown)
    and passes the message straight through to the CLI/GUI. Every adapter
    refusal that names schema + counts should subclass this; one that could
    embed a value must not.
    """


@dataclass(frozen=True)
class QuarantinedRows:
    """Rows an adapter could not place on any patient — held, never dropped.

    A real export dangles: a few hundred rows out of hundreds of thousands
    whose patient key is blank or names nobody the export contains. Refusing
    the whole run over them blocks every attributable row behind the broken
    few; attaching them to a guessed patient is the one sin this product
    promises never to commit. So they are quarantined: the rows verbatim,
    the table they came from, and one PHI-free ``reason`` naming the exact
    way attribution failed (schema words only — a column that is blank, a
    key that matches nothing — never a value from the row).

    An adapter that quarantines exposes ``quarantine`` (a list of these,
    reset on every ``load``) once a load has been fully consumed; the
    pipeline reads it with ``getattr`` so adapters with nothing to hold
    back need no attribute at all. The pipeline persists the held rows to
    ``quarantine.json`` in the output directory — which already holds the
    rendered charts, so the rows travel no further than the charts do —
    and everything else (events, logs, CLI) carries counts only.
    """

    table: str
    reason: str
    rows: tuple[dict[str, str | None], ...]


@dataclass(frozen=True)
class SelectionRule:
    """One render-selection rule an adapter applies unless a run turns it off.

    ``name`` is the word an operator types (``anast pipeline run --include
    growth-charts``) and the key a GUI toggle carries. ``reason`` is what the
    rule stamps on an encounter it excludes — the string that already travels
    in the adapter's ``:skipped_encounters`` extension and in the run's
    selection report, so it is fixed by that contract rather than free text.
    ``label`` is the sentence a picker or a report shows a person.

    All three are schema, not data: they name a rule, never anything the rule
    read. That is what lets the whole set be written into the selection report
    beside the charts and printed in a picker.
    """

    name: str
    reason: str
    label: str


@runtime_checkable
class SourceAdapter(Protocol):
    """What every source adapter provides."""

    #: CLI/GUI identifier, e.g. ``"pf-tebra"``.
    name: str
    #: What a person should read instead of ``name`` — "Practice Fusion /
    #: Tebra", not "pf-tebra". Every picker used to re-case the id, which
    #: cannot produce "C-CDA" from "ccda"; that one was hard-coded in the
    #: front end for exactly this reason.
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

    Read through ``getattr`` for the same reason ``quarantine`` is: the
    capability is optional, and an adapter that keeps nothing out of the render
    needs no attribute at all.
    """
    return tuple(getattr(adapter, "selection_rules", ()))


def with_selection(adapter: SourceAdapter, include: Iterable[str]) -> SourceAdapter:
    """The adapter this run loads through, given the rules it was told to drop.

    ``include`` names the rules the operator switched OFF, so the encounters
    they would have excluded are rendered instead. An empty ``include`` returns
    the registered adapter itself — the default run is not merely equivalent to
    the one before this seam existed, it is the same object taking the same
    path.

    Loud, and a programming error rather than an operator one: names are
    validated against :func:`selection_rules` by the caller before they get
    here, so an adapter that declares rules and cannot be configured with them
    is a half-built adapter, not a bad flag.
    """
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

# The built-in adapters, as module paths rather than eager imports: each one
# pulls in core.model + destinations + yaml (a real chunk of startup cost), so
# nothing outside this module imports them directly anymore — CLI startup
# (--help, doctor, gui) must not pay for a registry no one has asked for yet.
_builtins_loaded = False


def _ensure_builtin_adapters() -> None:
    """Import each built-in adapter module once (each ``register()``\\ s itself).

    Idempotent and cheap after the first call, so every registry entry point
    below can call it unconditionally — no caller can observe an unpopulated
    registry just because it happened to be first.

    These MUST be literal ``import`` statements, never importlib over strings:
    the frozen Windows build includes only statically-reachable modules
    (packaging/build_windows.py ships package DATA wholesale, code
    selectively), and these calls are the adapters' only remaining reference —
    a string-based import here compiles fine and then strips every adapter
    out of the shipped app. ``sources.learned`` is deliberately absent: it
    self-registers only through ``register_learned_sources()``.
    """
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
    """Return the unique adapter whose ``detect`` matches, else ``None``.

    Ambiguity (two adapters claiming one folder) returns ``None`` rather
    than guessing — the caller asks the user instead.
    """
    _ensure_builtin_adapters()
    matches = [adapter for adapter in available_sources() if adapter.detect(path)]
    return matches[0] if len(matches) == 1 else None
