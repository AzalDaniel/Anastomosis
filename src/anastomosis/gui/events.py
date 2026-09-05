"""Contract: JSON-safe event dicts the controller emits; the front end's
``anastEvent`` dispatcher switches on ``type``.

* ``stage``    — ``{flow, stage, state: "start"|"done"}``
* ``progress`` — ``{flow, stage, **fields}`` (ints + PHI-free labels)
* ``done``     — ``{flow, **counts}``, plus optional ``summary_id`` and,
  when the source kept a ledger, ``source_reading``
* ``error``    — ``{flow, stage, error}``; ``error`` is a type name or a
  PHI-free diagnosis, never a traceback

``flow`` (one of ``pipeline``/``migration``/``source_init``/``pack_init``/
``upload``) scopes an event to the page that raised it: two pages emit the
same event kinds, and each dispatcher early-returns on a ``flow`` it does not
own, so a page navigated mid-run cannot consume the other's terminal event.
Values throughout are ints, ids, labels, type names or fixed sentences —
never patient field values, never rendered filenames.
"""

from __future__ import annotations

__all__ = ["done_event", "error_event", "progress_event", "stage_event"]


def stage_event(flow: str, stage: str, state: str) -> dict[str, object]:
    """A stage opened (``state="start"``) or closed (``state="done"``).

    ``flow`` names the owning operation family (module docstring); the page
    that does not own it early-returns instead of lighting a rail.
    """
    return {"type": "stage", "flow": flow, "stage": stage, "state": state}


def progress_event(flow: str, stage: str, **fields: int | str) -> dict[str, object]:
    """Live counters for ``stage`` in ``flow`` (no patient-derived values).

    Ints plus PHI-free string labels — e.g. the deliver stage's
    ``deliverer="archive"`` alongside its ``patients`` count.
    """
    return {"type": "progress", "flow": flow, "stage": stage, **fields}


def done_event(flow: str, summary_id: str | None = None, **counts: int) -> dict[str, object]:
    """The run finished, with its final roll-up counts (integers) for ``flow``.

    ``summary_id``, when given, is a random non-PHI hex key the front end
    passes to ``last_run_summary`` to fetch this run's detail race-free.
    """
    event: dict[str, object] = {"type": "done", "flow": flow, **counts}
    if summary_id is not None:
        event["summary_id"] = summary_id
    return event


def error_event(flow: str, stage: str, error: str) -> dict[str, object]:
    """A failure at ``stage`` in ``flow``; ``error`` is a type name or a
    PHI-free diagnosis, never a traceback."""
    return {"type": "error", "flow": flow, "stage": stage, "error": error}
