"""Tests for the shortest-path delivery router — the preference matrix.

Pure logic, no I/O. Routes are chosen vendor_api > ccda_import > browser;
``unverified`` never routes; ``render()`` is deterministic and complete.
"""

from __future__ import annotations

from datetime import date

import pytest

from anastomosis.deliver.router import RouteKind, plan_route
from anastomosis.destinations.registry import (
    Capability,
    DestinationEntry,
    DestinationRegistry,
    Evidence,
)


def _ev(verified: date = date(2026, 5, 1)) -> Evidence:
    return Evidence(source_url="https://example.com/doc", verified=verified)


def _registry(
    *,
    doc_write_api: Capability,
    ccda_import: Capability,
    browser: Capability,
    name: str = "acme",
) -> DestinationRegistry:
    entry = DestinationEntry(
        name=name,
        display="Acme",
        doc_write_api=doc_write_api,
        ccda_import=ccda_import,
        browser=browser,
    )
    return DestinationRegistry(entries={name: entry})


# --- preference matrix ------------------------------------------------------


def test_all_viable_chooses_vendor_api() -> None:
    reg = _registry(
        doc_write_api=Capability(kind="fhir_documentreference", evidence=_ev()),
        ccda_import=Capability(kind="api", evidence=_ev()),
        browser=Capability(kind="pack", detail="destinations/acme"),
    )
    tm = plan_route("acme", reg)
    assert tm.chosen is not None
    assert tm.chosen.kind is RouteKind.VENDOR_API


def test_api_unverified_falls_through_to_ccda() -> None:
    reg = _registry(
        doc_write_api=Capability(kind="unverified"),
        ccda_import=Capability(kind="in_product", evidence=_ev()),
        browser=Capability(kind="pack", detail="destinations/acme"),
    )
    tm = plan_route("acme", reg)
    assert tm.chosen is not None
    assert tm.chosen.kind is RouteKind.CCDA_IMPORT


def test_only_browser_chooses_browser() -> None:
    reg = _registry(
        doc_write_api=Capability(kind="none"),
        ccda_import=Capability(kind="unverified"),
        browser=Capability(kind="pack", detail="destinations/acme"),
    )
    tm = plan_route("acme", reg)
    assert tm.chosen is not None
    assert tm.chosen.kind is RouteKind.BROWSER
    # The pack name rides in requires for the wizard PR to resolve.
    assert any("pack: destinations/acme" in r for r in tm.chosen.requires)


def test_nothing_viable_chooses_none() -> None:
    reg = _registry(
        doc_write_api=Capability(kind="none"),
        ccda_import=Capability(kind="none"),
        browser=Capability(kind="none"),
    )
    tm = plan_route("acme", reg)
    assert tm.chosen is None


def test_unverified_never_viable() -> None:
    reg = _registry(
        doc_write_api=Capability(kind="unverified"),
        ccda_import=Capability(kind="unverified"),
        browser=Capability(kind="none"),
    )
    tm = plan_route("acme", reg)
    assert all(not opt.viable for opt in tm.options)
    assert tm.chosen is None
    # unverified options say so, not a generic "not viable".
    why = next(o.why for o in tm.options if o.kind is RouteKind.VENDOR_API)
    assert "unverified" in why


def test_vendor_rest_is_viable() -> None:
    reg = _registry(
        doc_write_api=Capability(kind="vendor_rest", evidence=_ev()),
        ccda_import=Capability(kind="none"),
        browser=Capability(kind="none"),
    )
    tm = plan_route("acme", reg)
    assert tm.chosen is not None
    assert tm.chosen.kind is RouteKind.VENDOR_API


# --- render() ---------------------------------------------------------------


def test_render_is_deterministic_and_complete() -> None:
    reg = _registry(
        doc_write_api=Capability(kind="fhir_documentreference", evidence=_ev(date(2026, 5, 1))),
        ccda_import=Capability(kind="none"),
        browser=Capability(kind="pack", detail="destinations/acme"),
    )
    tm = plan_route("acme", reg)
    first = tm.render()
    assert tm.render() == first  # deterministic, no timestamps
    # All three options present, in preference order.
    assert first.index("vendor_api") < first.index("ccda_import") < first.index("browser")
    assert "chosen: vendor_api" in first


def test_render_no_timestamp_shapes() -> None:
    # The transit map carries the evidence verified date but no clock time.
    reg = _registry(
        doc_write_api=Capability(kind="fhir_documentreference", evidence=_ev(date(2026, 5, 1))),
        ccda_import=Capability(kind="none"),
        browser=Capability(kind="none"),
    )
    rendered = plan_route("acme", reg).render()
    assert "2026-05-01" in rendered  # the verified date IS carried
    assert "T" not in rendered.split("verified", 1)[1].split("\n", 1)[0]  # no ISO clock time


# --- viable api route why carries the verified date -------------------------


def test_viable_api_why_carries_verified_date() -> None:
    reg = _registry(
        doc_write_api=Capability(kind="fhir_documentreference", evidence=_ev(date(2026, 5, 1))),
        ccda_import=Capability(kind="none"),
        browser=Capability(kind="none"),
    )
    tm = plan_route("acme", reg)
    assert tm.chosen is not None
    assert "2026-05-01" in tm.chosen.why
    # The why carries the date, not the full source URL.
    assert "https://" not in tm.chosen.why


# --- unknown destination is loud --------------------------------------------


def test_unknown_destination_raises_keyerror() -> None:
    reg = _registry(
        doc_write_api=Capability(kind="none"),
        ccda_import=Capability(kind="none"),
        browser=Capability(kind="none"),
    )
    with pytest.raises(KeyError):
        plan_route("ghost", reg)
