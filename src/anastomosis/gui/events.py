# AI-assisted: written with Claude agents under the author's direction and review; see DESIGN.md.
"""Typed constructors for the GUI's JSON-safe event dicts (one schema, one place).

Every event the controller emits to the front end is a flat, JSON-safe dict
with a ``type`` discriminator the browser's ``anastEvent`` dispatcher switches
on:

* ``{"type": "stage",    "flow": str, "stage": str, "state": "start"|"done"}`` —
  a pipeline stage opened or closed (the stage rail lights up).
* ``{"type": "progress", "flow": str, "stage": str, **fields}`` — live counters
  for a stage (records, rendered/skipped/failed, pass/warn/fail, per-deliverer);
  ``fields`` are integers plus PHI-free string labels (e.g. ``deliverer="archive"``).
* ``{"type": "done",     "flow": str, **counts}`` — the run finished successfully;
  carries the final roll-up counts.
* ``{"type": "error",    "flow": str, "stage": str, "error": str}`` — a failure;
  ``error`` is an exception TYPE name or a PHI-free diagnosis, never a traceback.

Flow invariant (P2-5): every event carries a ``flow`` naming the operation
family a page owns — one of ``"pipeline"``, ``"migration"``, ``"source_init"``,
``"pack_init"``, ``"upload"``. Two pages emit the SAME event kinds (dashboard
pipeline and wizard migration both raise stage/progress/done/error), so a page
that navigated mid-run could otherwise consume the other page's terminal event
(the wizard announcing "migration prepared" for a dashboard pipeline run). Each
page's dispatcher early-returns on events whose ``flow`` it does not own, so an
event only ever reaches the page that raised it. ``flow`` is a fixed operation
label, never patient-derived.

PHI rule (enforced by a test): event *values* are integers, stage names, ids,
flow labels, and exception type names only — never patient field values and
never rendered filenames (counts of them, yes; the names, no).
"""

from __future__ import annotations

__all__ = ["done_event", "error_event", "progress_event", "stage_event"]


def stage_event(flow: str, stage: str, state: str) -> dict[str, object]:
    """A stage opened (``state="start"``) or closed (``state="done"``).

    ``flow`` names the owning operation family (see the module flow invariant);
    the page that does not own ``flow`` early-returns instead of lighting a rail.
    """
    return {"type": "stage", "flow": flow, "stage": stage, "state": state}


def progress_event(flow: str, stage: str, **fields: int | str) -> dict[str, object]:
    """Live counters for ``stage`` in ``flow`` (no patient-derived values).

    Values are integers (counts) plus PHI-free string labels — e.g. the
    deliver stage tags each event with ``deliverer="archive"`` alongside its
    ``patients`` count. Never carries patient field values. ``flow`` scopes the
    event to its owning page (see the module flow invariant).
    """
    return {"type": "progress", "flow": flow, "stage": stage, **fields}


def done_event(flow: str, summary_id: str | None = None, **counts: int) -> dict[str, object]:
    """The run finished successfully, with its final roll-up counts (integers).

    ``flow`` scopes the terminal event to its owning page (see the module flow
    invariant): with two pages emitting identical ``done`` kinds, the flow guard
    is what stops the wizard consuming a pipeline ``done`` (and vice versa).

    ``summary_id`` (when given) is a non-PHI opaque key the front end passes back
    to ``last_run_summary(summary_id)`` to fetch THIS run's per-patient detail —
    so a rapid second run cannot overwrite the slot the first run's UI then reads
    (the summary race). It is a random hex id, never patient-derived.
    """
    event: dict[str, object] = {"type": "done", "flow": flow, **counts}
    if summary_id is not None:
        event["summary_id"] = summary_id
    return event


def error_event(flow: str, stage: str, error: str) -> dict[str, object]:
    """A failure at ``stage`` in ``flow``; ``error`` is a PHI-free type name / diagnosis.

    ``flow`` scopes the event to its owning page (see the module flow invariant).
    """
    return {"type": "error", "flow": flow, "stage": stage, "error": error}
