"""Tests for the destination capability registry — the no-hallucination rule.

The registry is security-relevant routing data: a claim without evidence must
fail validation (the headline test), a broken file must raise rather than
half-load, and overlays replace packaged entries wholesale.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from anastomosis.destinations.registry import (
    BrowserKind,
    Capability,
    CcdaImportKind,
    DestinationEntry,
    DestinationRegistry,
    DocWriteKind,
    Evidence,
)

GOOD_EVIDENCE = {
    "source_url": "https://example.com/acme/fhir/documentreference",
    "verified": "2026-06-11",
}


def _entry_yaml(name: str = "acme") -> str:
    """A registry YAML with one fully-evidenced entry."""
    return f"""\
entries:
  {name}:
    name: {name}
    display: Acme EHR
    doc_write_api:
      kind: fhir_documentreference
      evidence:
        source_url: https://example.com/acme/fhir
        verified: 2026-03-01
    ccda_import:
      kind: api
      evidence:
        source_url: https://example.com/acme/ccda
        verified: 2026-04-15
    browser: {{kind: none}}
"""


# --- packaged registry ------------------------------------------------------


def test_packaged_registry_loads_clean() -> None:
    """The shipped registry honors its own contract: it loads, carries the
    six researched vendors, and every positive capability claim has evidence
    (the schema enforces this, but pin the DATA too — a future edit that
    swaps a kind to dodge validation should fail here)."""
    reg = DestinationRegistry.load()
    assert set(reg.entries) == {
        "epic",
        "athenahealth",
        "drchrono",
        "canvas",
        "tebra",
        "practice_fusion",
    }
    for entry in reg.entries.values():
        for cap in (entry.doc_write_api, entry.ccda_import):
            if cap.kind not in ("none", "unverified"):
                assert cap.evidence is not None, f"{entry.name}: uncited claim"
    # The verified negatives (no write API) carry citations too.
    assert reg.get("tebra").doc_write_api.kind == DocWriteKind.NONE
    assert reg.get("tebra").doc_write_api.evidence is not None
    assert reg.get("practice_fusion").doc_write_api.kind == DocWriteKind.NONE
    # No browser packs are declared until the packs actually exist.
    assert all(e.browser.kind == BrowserKind.NONE for e in reg.entries.values())
    # The positive headline claims.
    assert reg.get("epic").doc_write_api.kind == DocWriteKind.FHIR_DOCUMENTREFERENCE
    assert reg.get("athenahealth").ccda_import.kind == CcdaImportKind.API


# --- the no-hallucination enforcement (headline) ----------------------------


@pytest.mark.parametrize(
    "kind",
    [
        DocWriteKind.FHIR_DOCUMENTREFERENCE.value,
        DocWriteKind.VENDOR_REST.value,
        CcdaImportKind.API.value,
        CcdaImportKind.IN_PRODUCT.value,
    ],
)
def test_non_none_capability_requires_evidence(kind: str) -> None:
    with pytest.raises(ValidationError, match="no-hallucination"):
        Capability(kind=kind)


@pytest.mark.parametrize("kind", ["none", "unverified"])
def test_none_and_unverified_need_no_evidence(kind: str) -> None:
    cap = Capability(kind=kind)
    assert cap.evidence is None


def test_browser_pack_needs_no_evidence_url() -> None:
    # A browser pack's evidence is its canary fixtures, not a URL.
    cap = Capability(kind=BrowserKind.PACK.value, detail="destinations/tebra")
    assert cap.evidence is None
    assert cap.detail == "destinations/tebra"


def test_evidenced_capability_accepts() -> None:
    cap = Capability(
        kind=DocWriteKind.FHIR_DOCUMENTREFERENCE.value,
        evidence=Evidence.model_validate(GOOD_EVIDENCE),
    )
    assert cap.evidence is not None
    assert cap.evidence.verified == date(2026, 6, 11)


# --- strictness: extra keys and bad URLs ------------------------------------


def test_extra_forbid_rejects_unknown_capability_key() -> None:
    with pytest.raises(ValidationError):
        Capability.model_validate({"kind": "none", "bogus": 1})


def test_extra_forbid_rejects_unknown_entry_key() -> None:
    with pytest.raises(ValidationError):
        DestinationEntry.model_validate(
            {
                "name": "x",
                "display": "X",
                "doc_write_api": {"kind": "none"},
                "ccda_import": {"kind": "none"},
                "browser": {"kind": "none"},
                "surprise": True,
            }
        )


@pytest.mark.parametrize("bad_url", ["ftp://example.com", "example.com", "/local/path", ""])
def test_bad_url_scheme_rejected(bad_url: str) -> None:
    with pytest.raises(ValidationError, match="http"):
        Evidence(source_url=bad_url, verified=date(2026, 6, 11))


@pytest.mark.parametrize("good_url", ["http://example.com/x", "https://example.com/x"])
def test_http_and_https_accepted(good_url: str) -> None:
    ev = Evidence(source_url=good_url, verified=date(2026, 6, 11))
    assert ev.source_url == good_url


# --- get(): loud KeyError listing known names -------------------------------


def test_get_unknown_lists_known_names() -> None:
    reg = DestinationRegistry.load()
    with pytest.raises(KeyError) as excinfo:
        reg.get("nope")
    message = excinfo.value.args[0]
    assert "nope" in message
    assert "tebra" in message  # known names listed


# --- loud failure on malformed input ----------------------------------------


def test_malformed_schema_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("entries:\n  x:\n    name: x\n")  # missing required fields
    with pytest.raises(ValidationError):
        DestinationRegistry.load(bad)


def test_explicit_path_loads(tmp_path: Path) -> None:
    path = tmp_path / "reg.yaml"
    path.write_text(_entry_yaml())
    reg = DestinationRegistry.load(path)
    assert reg.get("acme").doc_write_api.kind == DocWriteKind.FHIR_DOCUMENTREFERENCE


# --- overlay replace semantics ----------------------------------------------


def test_merged_overlay_replaces_same_name(tmp_path: Path) -> None:
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        """\
entries:
  tebra:
    name: tebra
    display: Tebra (my re-verified copy)
    doc_write_api:
      kind: vendor_rest
      evidence:
        source_url: https://example.com/tebra/api
        verified: 2026-06-01
    ccda_import: {kind: none}
    browser: {kind: none}
"""
    )
    reg = DestinationRegistry.merged(overlay)
    tebra = reg.get("tebra")
    # The overlay wholesale-replaced the packaged (unverified) entry.
    assert tebra.display == "Tebra (my re-verified copy)"
    assert tebra.doc_write_api.kind == DocWriteKind.VENDOR_REST


def test_merged_overlay_adds_new_name(tmp_path: Path) -> None:
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(_entry_yaml(name="acme"))
    reg = DestinationRegistry.merged(overlay)
    assert "tebra" in reg.entries  # packaged kept
    assert "acme" in reg.entries  # overlay added


def test_empty_or_comment_only_file_is_noop_registry(tmp_path: Path) -> None:
    # An empty (or comment-only) overlay is a registry with zero entries —
    # a harmless no-op — not a ValidationError.
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text("# nothing re-verified yet\n")
    reg = DestinationRegistry.load(overlay)
    assert reg.entries == {}
    merged = DestinationRegistry.merged(overlay)
    assert "tebra" in merged.entries  # packaged entries intact


def test_key_name_mismatch_raises_loudly(tmp_path: Path) -> None:
    # A mapping key that disagrees with the body's `name:` would make
    # `list` and `route` answer to different identities — must refuse.
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        """\
entries:
  alpha:
    name: beta
    display: Disagrees
    doc_write_api: {kind: unverified}
    ccda_import: {kind: unverified}
    browser: {kind: none}
"""
    )
    with pytest.raises(ValidationError, match=r"key/name mismatch.*alpha"):
        DestinationRegistry.load(overlay)
