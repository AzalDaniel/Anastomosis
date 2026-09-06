"""The shortest-path delivery router (pure logic, no I/O).

Given a destination's declared capabilities, picks the cheapest viable
route. Preference is fixed cheapest-first: vendor API > C-CDA import >
browser automation. Returns all three routes in that order (viable or
not) plus the first viable one, so the wizard can show the whole map.

No network calls (RULES.md 37); every ``why`` string is capability
kinds, pack names, and evidence dates only, never patient-derived
(RULES.md 3)."""

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


#: Display labels for the Migrate screen and `destination list`; the enum
#: values stay the registry's own names.
ROUTE_NAMES = {
    RouteKind.VENDOR_API: "Send directly",
    RouteKind.CCDA_IMPORT: "Import a transfer document",
    RouteKind.BROWSER: "Through a browser",
}


@dataclass(frozen=True)
class RouteOption:
    """One candidate route, viable or not.

    ``why`` is PHI-free (capability kinds, pack names, dates). ``requires``
    lists what taking this route needs, e.g. ``("credentials: vendor API",)``."""

    kind: RouteKind
    viable: bool
    why: str
    requires: tuple[str, ...] = ()


@dataclass(frozen=True)
class TransitMap:
    """All routes for one destination, viable or not, and the chosen one.

    ``chosen`` is the first viable option in ``options``, or ``None``."""

    destination: str
    options: tuple[RouteOption, ...]
    chosen: RouteOption | None

    def render(self, glyphs: Glyphs = UNICODE_GLYPHS) -> str:
        """The routes into this system, as the CLI prints them.

        Deterministic: same registry renders identical output every time.
        ``glyphs`` picks ASCII markers so a non-UTF-8 stream never raises
        :class:`UnicodeEncodeError`."""
        lines = [f"Ways to file charts into {self.destination}:"]
        for opt in self.options:
            mark = glyphs.ok if opt.viable else glyphs.fail
            lines.append(f"  {mark} {ROUTE_NAMES[opt.kind]:<28} {opt.why}")
            for req in opt.requires:
                lines.append(f"         needs {req}")
        if self.chosen is not None:
            lines.append(f"Anastomosis would use: {ROUTE_NAMES[self.chosen.kind]}")
        else:
            lines.append("No route into this system is available yet.")
        return "\n".join(lines)


def _capability_option(
    kind: RouteKind,
    field: str,
    entry: str,
    viable_values: tuple[str, ...],
    verified_label: str,
    requires: tuple[str, ...],
) -> RouteOption:
    """Shared viability check for the vendor-API and C-CDA routes; they
    differ only in field name, viable values, and what taking the route
    ``requires``. ``"unverified"`` (RULES.md 69) gets its own why, pointing
    at re-verification, distinct from an ordinary unviable value."""
    if entry in viable_values:
        return RouteOption(
            kind=kind,
            viable=True,
            why=f"available (confirmed working {verified_label})",
            requires=requires,
        )
    if entry == "unverified":
        return RouteOption(
            kind=kind,
            viable=False,
            why="not confirmed yet — this opens once it has been checked",
        )
    return RouteOption(kind=kind, viable=False, why="not available")


def _vendor_api_option(entry_doc_write_kind: str, verified_label: str) -> RouteOption:
    return _capability_option(
        RouteKind.VENDOR_API,
        "doc_write_api",
        entry_doc_write_kind,
        (DocWriteKind.FHIR_DOCUMENTREFERENCE.value, DocWriteKind.VENDOR_REST.value),
        verified_label,
        ("sign-in details for that system",),
    )


def _ccda_option(entry_ccda_kind: str, verified_label: str) -> RouteOption:
    return _capability_option(
        RouteKind.CCDA_IMPORT,
        "ccda_import",
        entry_ccda_kind,
        (CcdaImportKind.API.value, CcdaImportKind.IN_PRODUCT.value),
        verified_label,
        ("a C-CDA transfer document",),
    )


def _browser_option(entry_browser_kind: str, pack_name: str) -> RouteOption:
    # Declaration check only — never imports or executes the pack; the pack
    # name rides in ``requires`` for the wizard to resolve later.
    if entry_browser_kind == BrowserKind.PACK.value:
        pack_ref = pack_name or "(unnamed pack)"
        return RouteOption(
            kind=RouteKind.BROWSER,
            viable=True,
            why=f"available, using the {pack_ref} filing assistant",
            requires=("the browser parts installed", f"the {pack_ref} filing assistant"),
        )
    return RouteOption(
        kind=RouteKind.BROWSER,
        viable=False,
        why="not available",
    )


def plan_route(destination: str, registry: DestinationRegistry) -> TransitMap:
    """Select the shortest viable delivery route for ``destination``.

    Raises ``KeyError`` (from :meth:`DestinationRegistry.get`) when the
    destination is unknown — loud, never a silent empty map."""
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
