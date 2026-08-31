"""The route a bundle was prepared for, the gates it passed, and the refusal.

``anast migrate`` PREPARES: it writes the artifacts, resolves the destination's
transit map, and stops (:mod:`anastomosis.core.migration_status` holds that
verdict). The route it chose was a fact about the run that lived only in the
run's own output — the operator saw it once, on the terminal — while the upload
manifest, which is the thing an executor actually reads hours later on another
machine, carried no trace of it and no trace of whether the run's gates had
passed. So the artifacts and the plan for them were reviewed in one place and
executed from another, with nothing tying the two together.

This module is that tie. :class:`RoutePlan` and :class:`RunGates` ride in the
manifest from schema v3 (see ``docs/UPLOAD_MANIFEST.md``), and
:func:`assert_deliverable` is the check every executor runs before it moves a
single chart:

* a gate the run RECORDED as failed, or as never run, refuses. Verification is
  not optional decoration on a bundle that is about to be filed into somebody's
  chart — an unverified upload is exactly the wrong-patient risk the ladder
  exists for;
* a recorded route that found no viable way in refuses. Executing a route the
  run's own planner rejected means running something nobody reviewed;
* an item whose bytes no longer hash to what the manifest recorded refuses the
  whole run, not just that item. A file that changed after review is either a
  re-render nobody re-reviewed or a corruption, and neither is a per-item
  problem: the bundle is no longer the bundle that was checked.

The one thing it does NOT refuse is a bundle with no gate record at all — a
manifest from before v3. Operators have rendered trees on disk; stranding them
would be a worse failure than the one being fixed, and it is the same posture
the reader already takes with a v1 manifest. It warns, loudly, naming what could
not be checked, and never silently.

PHI: refusals carry counts, gate names, a destination name and a route kind.
Never an item key, never a path, never a patient value — an executor's refusal
is printed to a terminal and written to a log, and both are outside the hardened
output directory.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from anastomosis.core.hashutil import hash_and_size
from anastomosis.core.logutil import exc_tag

if TYPE_CHECKING:
    from anastomosis.deliver.router import TransitMap
    from anastomosis.destinations.base import UploadItem

    from .persist import UploadManifest

__all__ = [
    "CONSERVATION_BALANCED",
    "GATE_FAIL",
    "GATE_NOT_RUN",
    "GATE_PASS",
    "DeliveryRefused",
    "RoutePlan",
    "RunGates",
    "assert_deliverable",
    "route_plan_of",
]

logger = logging.getLogger(__name__)

#: The three things a gate can say. ``not_run`` is deliberately distinct from
#: ``fail``: they mean different things to a reader and they came about
#: differently, but for an executor they land the same way — neither is a pass.
GATE_PASS = "pass"  # noqa: S105 — a gate verdict label, not a password
GATE_FAIL = "fail"
GATE_NOT_RUN = "not_run"

#: What the canonical -> rendered seam says when every offered encounter ended
#: in exactly one column (:class:`anastomosis.core.conservation.Conservation`).
CONSERVATION_BALANCED = "balanced"


class DeliveryRefused(Exception):
    """An executor will not deliver this bundle, and the message says why.

    Distinct from :class:`~anastomosis.deliver.browser.persist.ManifestError`:
    that one means the file could not be read. This one means it read
    perfectly and describes a bundle nobody should act on.
    """


@dataclass(frozen=True)
class RoutePlan:
    """The destination route a run resolved, as reviewed.

    ``kind`` is a :class:`~anastomosis.deliver.router.RouteKind` value, or
    ``None`` when the planner found no viable automated route at all — which is
    a capability gap the artifacts survive (the C-CDA is still importable by
    hand) and an executor must not paper over.
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
    """A :class:`~anastomosis.deliver.router.TransitMap` as the recordable plan.

    The router is imported for typing only: this module is imported by the
    manifest writer, which must keep loading on a machine with no browser extra,
    and the router pulls the destination registry in behind it. The two fields
    read here are the two the record carries.
    """
    chosen = transit.chosen
    return RoutePlan(
        destination=str(transit.destination), kind=None if chosen is None else str(chosen.kind)
    )


@dataclass(frozen=True)
class RunGates:
    """What the render run checked before it wrote this bundle.

    Three gates, because three different things can be wrong with a bundle and
    an executor needs to be able to say which:

    * :attr:`qa` — the semantic gate. Did every rendered document verify against
      the record it came from?
    * :attr:`conservation` — the accounting gate. Did every encounter the seam
      was offered end in exactly one column, so the survivors are not being
      reported as the whole set?
    * :attr:`layout_hash` — the provenance gate. Which layout bytes produced
      these pages (:mod:`anastomosis.reconstruct.provenance`)? ``None`` where no
      Jinja layout was involved at all — the whole-patient standard C-CDA view
      renders through HL7's own stylesheet — which is a real answer, not a gap.
    """

    qa: str
    conservation: str
    layout_hash: str | None

    @classmethod
    def from_run(cls, *, qa_ok: bool | None, layout_hash: str | None) -> RunGates:
        """The gates of a run that has reached the point of writing a manifest.

        ``qa_ok`` is ``None`` when QA did not run at all (``--no-qa``, or the
        optional PyMuPDF dependency the checks read PDFs with is not installed).

        Conservation is recorded as balanced because reaching here IS the
        evidence: the render seam's
        :meth:`~anastomosis.core.conservation.Conservation.check` raises on an
        unbalanced batch, and a raised run never writes a manifest. The field
        exists so a future stage that can settle it differently has somewhere to
        say so, and so a reader is not left inferring it from silence.
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
    """How many items no longer hash to what the manifest recorded.

    Counts unreadable files too: a chart that cannot be re-measured cannot be
    shown to be the chart that was reviewed, and "cannot show" and "does not
    match" reach an executor as the same refusal.
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
    """Refuse a bundle no executor should act on. Returns ``None`` or raises.

    Takes the loaded
    :class:`~anastomosis.deliver.browser.persist.UploadManifest`, imported for
    typing only — that module imports this one to (de)serialize the two records
    below, so a runtime import back would close the circle.

    Checked in cost order: the recorded verdicts first, then the route, then the
    re-hash of every rendered file — so a bundle that was never going to be
    delivered does not pay for a walk over its charts first.
    """
    gates = manifest.gates
    if gates is None:
        # The grandfather clause. Loud, and never silent — see the module
        # docstring for why this is a warning and not a refusal.
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
