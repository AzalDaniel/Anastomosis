# AI-assisted: written with Claude agents under the author's direction and review; see DESIGN.md.
"""Leaf constants and serializers shared across the GUI consoles.

The pieces here have no behavior of their own — they are the small, pure
building blocks (the stage-rail names, the upload state groupings, the
transit-map serializer) that several GUI consoles and the controller need in
common. Kept in a dependency-free leaf module (it imports nothing from
:mod:`anastomosis.gui.controller` or the consoles) so both the controller
facade and every console can import it without a cycle.

The underscore-prefixed public names are preserved as-is to minimize churn:
``tests/unit/test_frontend_constants.py`` imports ``_STAGE_MAP``,
``_STAGE_RAIL`` and ``_STATE_GROUPS`` (via the controller's re-export), and the
controller still uses ``_transit_to_dict``/``_STAGE_RAIL``/``_STATE_GROUPS``
directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from anastomosis.deliver.router import TransitMap

__all__ = [
    "_STAGE_MAP",
    "_STAGE_RAIL",
    "_STATE_GROUPS",
    "_group_states",
    "_transit_to_dict",
]


# Pipeline-core stage names -> dashboard rail names (detect has no rail).
_STAGE_MAP = {
    "ingest": "ingest",
    "reconstruct": "reconstruct",
    "qa": "qa",
}

# The dashboard's stage rail, in display order — the Python-canonical list the
# JS consumes via gui_config() (app.js keeps a same-valued fallback for the
# api-less browser preview; the drift test pins the two together). Every
# _STAGE_MAP value must be a member; "deliver" is driven by delivery events.
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


# State groupings for the upload console's glass cards (the 15 states bucketed
# pending/active/terminal). PENDING is its own "pending" bucket; mid-flight work
# is "active"; everything else is "terminal" (no work owed). Pure presentation
# data — counts only flow through it.
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
