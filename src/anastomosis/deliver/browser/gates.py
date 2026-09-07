"""The route a bundle was prepared for, the gates it passed, and the refusal.

:class:`RoutePlan` and :class:`RunGates` ride in the manifest from schema v3
(``docs/UPLOAD_MANIFEST.md``); :func:`assert_deliverable` refuses a bundle
whose recorded gates failed or never ran, whose route found no viable way
in, or whose bytes fail to match what was reviewed (46). A pre-v3 manifest
warns instead of refusing. PHI: counts, gate names, a destination name and
a route kind only — never an item key, a path, or a patient value (3).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from anastomosis.core.hashutil import hash_and_size
from anastomosis.core.logutil import exc_tag

#: Manifest version that introduced the route plan and gate outcomes; lives
#: here (not in `persist`, which imports this module) to avoid a cycle.
GATE_VERSION = 3

if TYPE_CHECKING:
    from anastomosis.deliver.router import TransitMap
    from anastomosis.destinations.base import UploadItem

    from .persist import UploadManifest

__all__ = [
    "CONSERVATION_BALANCED",
    "GATE_FAIL",
    "GATE_NOT_RUN",
    "GATE_PASS",
    "GATE_VERSION",
    "DeliveryRefused",
    "RoutePlan",
    "RunGates",
    "assert_deliverable",
    "route_plan_of",
]

logger = logging.getLogger(__name__)

#: The three things a gate can say. ``not_run`` differs semantically from
#: ``fail``, but an executor treats both as not a pass.
GATE_PASS = "pass"  # noqa: S105 — a gate verdict label, not a password
GATE_FAIL = "fail"
GATE_NOT_RUN = "not_run"

#: What the canonical -> rendered seam says when every offered encounter ended
#: in exactly one column (:class:`anastomosis.core.conservation.Conservation`).
CONSERVATION_BALANCED = "balanced"


class DeliveryRefused(Exception):
    """The bundle read fine but should not be delivered; distinct from
    :class:`~anastomosis.deliver.browser.persist.ManifestError` (unreadable).
    """


@dataclass(frozen=True)
class RoutePlan:
    """The destination route a run resolved. ``kind`` is ``None`` when the
    planner found no automated route — a capability gap the artifacts
    survive (the C-CDA stays importable by hand).
    """

    destination: str
    kind: str | None

    @property
    def viable(self) -> bool:
        return self.kind is not None

    def as_json(self) -> dict[str, Any]:
        return {"destination": self.destination, "kind": self.kind}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> RoutePlan:
        kind = data.get("kind")
        return cls(destination=str(data["destination"]), kind=None if kind is None else str(kind))


def route_plan_of(transit: TransitMap) -> RoutePlan:
    """A :class:`~anastomosis.deliver.router.TransitMap` as the recordable
    plan. ``transit`` is imported for typing only, so this module keeps
    loading without the router pulling the destination registry in.
    """
    chosen = transit.chosen
    return RoutePlan(
        destination=str(transit.destination), kind=None if chosen is None else str(chosen.kind)
    )


@dataclass(frozen=True)
class RunGates:
    """What the run checked: ``qa`` (documents verified), ``conservation``
    (every encounter landed in one column), ``layout_hash`` (which layout
    produced these pages — ``None`` for the HL7-stylesheet view is a real
    answer, not a gap).
    """

    qa: str
    conservation: str
    layout_hash: str | None

    @classmethod
    def from_run(cls, *, qa_ok: bool | None, layout_hash: str | None) -> RunGates:
        """``qa_ok`` is ``None`` when QA did not run (``--no-qa`` or no
        PyMuPDF). Conservation is always recorded balanced: an unbalanced
        batch raises upstream before a manifest is ever written.
        """
        return cls(
            qa=GATE_NOT_RUN if qa_ok is None else (GATE_PASS if qa_ok else GATE_FAIL),
            conservation=CONSERVATION_BALANCED,
            layout_hash=layout_hash,
        )

    def failures(self) -> list[str]:
        """Why this bundle is not deliverable, one PHI-free clause each."""
        reasons: list[str] = []
        if self.qa == GATE_NOT_RUN:
            reasons.append(
                "the charts were never verified (QA did not run) — re-render with QA on, "
                "or install the render extra it needs"
            )
        elif self.qa != GATE_PASS:
            reasons.append("QA failed on this bundle")
        if self.conservation != CONSERVATION_BALANCED:
            reasons.append(f"the render seam did not balance ({self.conservation})")
        return reasons

    @property
    def passed(self) -> bool:
        return not self.failures()

    def as_json(self) -> dict[str, Any]:
        return {"qa": self.qa, "conservation": self.conservation, "layout_hash": self.layout_hash}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> RunGates:
        layout = data.get("layout_hash")
        return cls(
            qa=str(data["qa"]),
            conservation=str(data["conservation"]),
            layout_hash=None if layout is None else str(layout),
        )


def _changed_items(items: list[UploadItem]) -> int:
    """How many items fail to hash to what the manifest recorded, counting
    an unreadable file as changed too (cannot show it is the reviewed one).
    """
    changed = 0
    for item in items:
        try:
            digest, size = hash_and_size(item.file_path)
        except OSError as exc:
            logger.warning("manifest item unreadable at delivery time (%s)", exc_tag(exc))
            changed += 1
            continue
        if digest != item.sha256 or size != item.size_bytes:
            changed += 1
    return changed


def assert_deliverable(manifest: UploadManifest) -> None:
    """Contract: returns ``None`` or raises :class:`DeliveryRefused`.
    Checked cost-order — recorded verdicts, then route, then a re-hash of
    every rendered file — so a doomed bundle never pays for the file walk.
    """
    gates = manifest.gates
    if gates is None and manifest.version >= GATE_VERSION:
        # Not the grandfather clause: every writer at this version records
        # gates, so declaring none here is incomplete or edited data (46).
        raise DeliveryRefused(
            "refusing to deliver this bundle: its upload manifest declares version "
            f"{manifest.version} and records no gate outcomes, so nothing says whether these "
            "charts were verified, whether the render seam balanced, or which layout produced "
            "them. Re-render rather than delivering past a record that is not there."
        )
    if gates is None:
        # Grandfather clause: a manifest old enough to predate the record (46).
        logger.warning(
            "this bundle's upload manifest (v%d) records no gate outcomes: delivery cannot "
            "tell whether these charts were verified, whether the render seam balanced, or "
            "which layout produced them. Re-render to record them.",
            manifest.version,
        )
    elif not gates.passed:
        raise DeliveryRefused(
            "refusing to deliver this bundle: " + "; ".join(gates.failures()) + "."
        )
    route = manifest.route
    if route is not None and not route.viable:
        raise DeliveryRefused(
            f"refusing to deliver this bundle: the run found no viable automated route to "
            f"{route.destination!r}, so there is no reviewed route to execute. Import the "
            f"C-CDA by hand, or teach a browser route with 'anast destination init'."
        )
    changed = _changed_items(manifest.items)
    if changed:
        raise DeliveryRefused(
            f"refusing to deliver this bundle: {changed} of {len(manifest.items)} chart(s) "
            f"no longer match the bytes recorded when this manifest was written, so what "
            f"would be filed is not what was reviewed. Re-render, or restore the folder."
        )
