"""The shortest-path delivery router (pure logic, no I/O).

Given a destination's declared capabilities (the
:mod:`anastomosis.destinations.registry` data), pick the cheapest viable way to
get a chart there. Route preference, cheapest first:

    vendor API  >  C-CDA import  >  browser automation

A vendor write API is one HTTP call; a C-CDA import is a file the destination
ingests; browser automation drives the UI a human would and is the route of
last resort. The router returns ALL three options in preference order (viable
or not) plus the chosen one (the first viable) so the discovery wizard can show
the operator the full transit map, not just the winner.

The ``unverified`` capability is deliberately **not viable** — an uncited claim
must never silently route PHI. ``none`` is not viable. This module is the
mechanical half of the no-hallucination rule: the registry refuses to *store* a
claim without evidence; the router refuses to *act* on one that is unverified.

PHI rule: this layer is pure capability logic. Every ``why`` string carries
only capability kinds, pack names, and evidence dates — never anything
patient-derived. There is no I/O here at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from anastomosis.destinations.registry import (
    BrowserKind,
    CcdaImportKind,
    DestinationRegistry,
    DocWriteKind,
)

__all__ = ["RouteKind", "RouteOption", "TransitMap", "plan_route"]


class RouteKind(StrEnum):
    """A delivery route, ordered cheapest-first by declaration order."""

    VENDOR_API = "vendor_api"
    CCDA_IMPORT = "ccda_import"
    BROWSER = "browser"


@dataclass(frozen=True)
class RouteOption:
    """One candidate route and whether it is usable for this destination.

    ``why`` is a PHI-free explanation (capability kinds, pack names, evidence
    dates). ``requires`` lists what an operator must supply or install to take
    this route, e.g. ``("extra: deliver-browser", "pack: destinations/tebra")``
    or ``("credentials: vendor API",)``.
    """

    kind: RouteKind
    viable: bool
    why: str
    requires: tuple[str, ...] = ()


@dataclass(frozen=True)
class TransitMap:
    """The full set of routes for one destination, with the chosen one.

    ``options`` always holds all three :class:`RouteKind` values in preference
    order (viable or not — the wizard shows the whole map). ``chosen`` is the
    first viable option, or ``None`` when nothing is viable.
    """

    destination: str
    options: tuple[RouteOption, ...]
    chosen: RouteOption | None

    def render(self) -> str:
        """A small fixed-width text transit map for the CLI.

        Deterministic: no timestamps, no ordering churn — the same registry
        renders byte-identical output every time.
        """
        lines = [f"delivery routes for {self.destination}:"]
        for opt in self.options:
            mark = "✓" if opt.viable else "✗"  # ✓ / ✗ (dingbats, not emoji)
            lines.append(f"  {mark} {opt.kind.value:<12} {opt.why}")
            for req in opt.requires:
                lines.append(f"         requires {req}")
        if self.chosen is not None:
            lines.append(f"chosen: {self.chosen.kind.value}")
        else:
            lines.append("chosen: none (no viable route)")
        return "\n".join(lines)


def _vendor_api_option(entry_doc_write_kind: str, verified_label: str) -> RouteOption:
    if entry_doc_write_kind in (
        DocWriteKind.FHIR_DOCUMENTREFERENCE.value,
        DocWriteKind.VENDOR_REST.value,
    ):
        return RouteOption(
            kind=RouteKind.VENDOR_API,
            viable=True,
            why=f"doc_write_api={entry_doc_write_kind} (verified {verified_label})",
            requires=("credentials: vendor API",),
        )
    if entry_doc_write_kind == DocWriteKind.UNVERIFIED.value:
        return RouteOption(
            kind=RouteKind.VENDOR_API,
            viable=False,
            why="doc_write_api unverified — run re-verification or contribute evidence",
        )
    return RouteOption(
        kind=RouteKind.VENDOR_API,
        viable=False,
        why=f"doc_write_api={entry_doc_write_kind}",
    )


def _ccda_option(entry_ccda_kind: str, verified_label: str) -> RouteOption:
    if entry_ccda_kind in (CcdaImportKind.API.value, CcdaImportKind.IN_PRODUCT.value):
        return RouteOption(
            kind=RouteKind.CCDA_IMPORT,
            viable=True,
            why=f"ccda_import={entry_ccda_kind} (verified {verified_label})",
            requires=("export: C-CDA document",),
        )
    if entry_ccda_kind == CcdaImportKind.UNVERIFIED.value:
        return RouteOption(
            kind=RouteKind.CCDA_IMPORT,
            viable=False,
            why="ccda_import unverified — run re-verification or contribute evidence",
        )
    return RouteOption(
        kind=RouteKind.CCDA_IMPORT,
        viable=False,
        why=f"ccda_import={entry_ccda_kind}",
    )


def _browser_option(entry_browser_kind: str, pack_name: str) -> RouteOption:
    # For THIS PR, browser viability == (kind == pack). Whether the named pack
    # is actually importable/registered is checked by the discovery wizard PR;
    # here a declared pack is treated as viable and the pack name rides in
    # ``requires`` for the wizard to resolve.
    if entry_browser_kind == BrowserKind.PACK.value:
        pack_ref = pack_name or "(unnamed pack)"
        return RouteOption(
            kind=RouteKind.BROWSER,
            viable=True,
            why=f"browser pack {pack_ref}",
            requires=("extra: deliver-browser", f"pack: {pack_ref}"),
        )
    return RouteOption(
        kind=RouteKind.BROWSER,
        viable=False,
        why=f"browser={entry_browser_kind}",
    )


def plan_route(destination: str, registry: DestinationRegistry) -> TransitMap:
    """Select the shortest viable delivery route for ``destination``.

    Raises ``KeyError`` (from :meth:`DestinationRegistry.get`) when the
    destination is unknown — loud, never a silent empty map.
    """
    entry = registry.get(destination)  # KeyError lists known names

    doc = entry.doc_write_api
    ccda = entry.ccda_import
    browser = entry.browser

    options = (
        _vendor_api_option(doc.kind, doc.evidence.verified.isoformat() if doc.evidence else "n/a"),
        _ccda_option(ccda.kind, ccda.evidence.verified.isoformat() if ccda.evidence else "n/a"),
        _browser_option(browser.kind, browser.detail),
    )
    chosen = next((opt for opt in options if opt.viable), None)
    return TransitMap(destination=destination, options=options, chosen=chosen)
