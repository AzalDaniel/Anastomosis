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

from anastomosis.core.presentation import UNICODE_GLYPHS, Glyphs
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

    def render(self, glyphs: Glyphs = UNICODE_GLYPHS) -> str:
        """A small fixed-width text transit map for the CLI.

        Deterministic: no timestamps, no ordering churn — the same registry
        renders byte-identical output every time. ``glyphs`` selects the
        viable/unviable markers; the CLI passes a stream-appropriate set so a
        non-UTF-8 console gets ASCII rather than a :class:`UnicodeEncodeError`.
        """
        lines = [f"delivery routes for {self.destination}:"]
        for opt in self.options:
            mark = glyphs.ok if opt.viable else glyphs.fail
            lines.append(f"  {mark} {opt.kind.value:<12} {opt.why}")
            for req in opt.requires:
                lines.append(f"         requires {req}")
        if self.chosen is not None:
            lines.append(f"chosen: {self.chosen.kind.value}")
        else:
            lines.append("chosen: none (no viable route)")
        return "\n".join(lines)


def _capability_option(
    kind: RouteKind,
    field: str,
    entry: str,
    viable_values: tuple[str, ...],
    verified_label: str,
    requires: tuple[str, ...],
) -> RouteOption:
    """One capability's viability — shared by the vendor-API and C-CDA checks
    below, which differ only in the field name, the values that count as
    viable, and what taking the route ``requires``. ``"unverified"`` is the
    cross-capability sentinel (see destinations.registry._NO_EVIDENCE_KINDS):
    deliberately not viable, distinct from an ordinary unviable value so its
    ``why`` can point the operator at re-verification instead of just naming
    the entry.
    """
    if entry in viable_values:
        return RouteOption(
            kind=kind,
            viable=True,
            why=f"{field}={entry} (verified {verified_label})",
            requires=requires,
        )
    if entry == "unverified":
        return RouteOption(
            kind=kind,
            viable=False,
            why=f"{field} unverified — run re-verification or contribute evidence",
        )
    return RouteOption(kind=kind, viable=False, why=f"{field}={entry}")


def _vendor_api_option(entry_doc_write_kind: str, verified_label: str) -> RouteOption:
    return _capability_option(
        RouteKind.VENDOR_API,
        "doc_write_api",
        entry_doc_write_kind,
        (DocWriteKind.FHIR_DOCUMENTREFERENCE.value, DocWriteKind.VENDOR_REST.value),
        verified_label,
        ("credentials: vendor API",),
    )


def _ccda_option(entry_ccda_kind: str, verified_label: str) -> RouteOption:
    return _capability_option(
        RouteKind.CCDA_IMPORT,
        "ccda_import",
        entry_ccda_kind,
        (CcdaImportKind.API.value, CcdaImportKind.IN_PRODUCT.value),
        verified_label,
        ("export: C-CDA document",),
    )


def _browser_option(entry_browser_kind: str, pack_name: str) -> RouteOption:
    # Browser viability is a DECLARATION check, never an import: a destination
    # declaring ``browser: {kind: pack}`` is routable, and the pack name rides
    # in ``requires`` so the discovery wizard resolves and validates it there.
    # Route planning must stay side-effect-free — it never executes pack code.
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
