"""Leaf constants and serializers shared across the GUI consoles.

Pure building blocks with no behavior of their own; imports nothing from
:mod:`anastomosis.gui.controller` or the consoles, so both can import it
without a cycle. The underscore-prefixed names stay public:
``tests/unit/test_frontend_constants.py`` imports them directly.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from anastomosis.core.logutil import exc_tag
from anastomosis.gui.events import error_event

if TYPE_CHECKING:
    from anastomosis.deliver.router import TransitMap

__all__ = [
    "_STAGE_MAP",
    "_STAGE_RAIL",
    "_STATE_GROUPS",
    "_group_states",
    "_transit_to_dict",
    "fail_result",
]


# Pipeline-core stage names -> dashboard rail names (detect has no rail).
_STAGE_MAP = {
    "ingest": "ingest",
    "reconstruct": "reconstruct",
    "qa": "qa",
}

# JS mirrors this via gui_config() (drift-tested against app.js's fallback);
# every _STAGE_MAP value must appear here; "deliver" comes from delivery events.
_STAGE_RAIL: tuple[str, ...] = ("ingest", "reconstruct", "qa", "deliver")


def _transit_to_dict(transit: TransitMap) -> dict[str, object]:
    """Serialize a :class:`TransitMap` to a JSON-safe dict for the GUI."""
    options = [
        {
            "kind": opt.kind.value,
            "viable": opt.viable,
            "why": opt.why,
            "requires": list(opt.requires),
        }
        for opt in transit.options
    ]
    chosen = transit.chosen
    return {
        "destination": transit.destination,
        "options": options,
        "chosen": chosen.kind.value if chosen is not None else None,
    }


# The 15 upload states bucketed for the console's glass cards; "terminal"
# means no work owed. Presentation only — counts flow through, no values.
_STATE_GROUPS: dict[str, tuple[str, ...]] = {
    "pending": ("pending",),
    "active": (
        "resolving_patient",
        "verifying_pre",
        "uploading",
        "upload_interrupted",
        "retry_wait",
        "verifying_post",
    ),
    "terminal": (
        "skipped_skiplist",
        "preflight_failed",
        "patient_not_found",
        "duplicate_at_destination",
        "pre_verify_failed",
        "failed",
        "post_verify_failed",
        "completed",
    ),
}


def _group_states(counts: dict[str, int]) -> dict[str, int]:
    """Bucket per-state item counts into pending/active/terminal totals."""
    return {
        group: sum(counts.get(state, 0) for state in states)
        for group, states in _STATE_GROUPS.items()
    }


def fail_result(
    emit: Callable[[dict[str, object]], None], flow: str, stage: str, exc: BaseException
) -> dict[str, object]:
    """Convert a caught exception to the no-traceback error contract.

    Emits :func:`~anastomosis.gui.events.error_event` with the exception's
    TYPE name only; every controller/console ``_fail`` method delegates here.
    """
    tag = exc_tag(exc)
    emit(error_event(flow, stage, tag))
    return {"ok": False, "error": tag}
