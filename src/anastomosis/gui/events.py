"""Typed constructors for the GUI's JSON-safe event dicts (one schema, one place).

Every event the controller emits to the front end is a flat, JSON-safe dict
with a ``type`` discriminator the browser's ``anastEvent`` dispatcher switches
on:

* ``{"type": "stage",    "stage": str, "state": "start"|"done"}`` —
  a pipeline stage opened or closed (the stage rail lights up).
* ``{"type": "progress", "stage": str, **fields}`` — live counters for a stage
  (records, rendered/skipped/failed, pass/warn/fail, per-deliverer); ``fields``
  are integers plus PHI-free string labels (e.g. ``deliverer="archive"``).
* ``{"type": "done",     **counts}`` — the run finished successfully; carries
  the final roll-up counts.
* ``{"type": "error",    "stage": str, "error": str}`` — a failure; ``error``
  is an exception TYPE name or a PHI-free diagnosis, never a traceback.

PHI rule (enforced by a test): event *values* are integers, stage names, ids,
and exception type names only — never patient field values and never rendered
filenames (counts of them, yes; the names, no).
"""

from __future__ import annotations

__all__ = ["done_event", "error_event", "progress_event", "stage_event"]


def stage_event(stage: str, state: str) -> dict[str, object]:
    """A stage opened (``state="start"``) or closed (``state="done"``)."""
    return {"type": "stage", "stage": stage, "state": state}


def progress_event(stage: str, **fields: int | str) -> dict[str, object]:
    """Live counters for ``stage`` (no patient-derived values).

    Values are integers (counts) plus PHI-free string labels — e.g. the
    deliver stage tags each event with ``deliverer="archive"`` alongside its
    ``patients`` count. Never carries patient field values.
    """
    return {"type": "progress", "stage": stage, **fields}


def done_event(**counts: int) -> dict[str, object]:
    """The run finished successfully, with its final roll-up counts (integers)."""
    return {"type": "done", **counts}


def error_event(stage: str, error: str) -> dict[str, object]:
    """A failure at ``stage``; ``error`` is a PHI-free type name / diagnosis."""
    return {"type": "error", "stage": stage, "error": error}
