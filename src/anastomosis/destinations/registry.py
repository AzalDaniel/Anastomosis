"""Data-driven destination capability registry (the no-hallucination rule).

What a destination can *receive* is a fact about the world that decays: a
vendor ships a FHIR ``DocumentReference`` endpoint one quarter and deprecates
it the next. So capabilities are **data, not code** — a YAML file
(``registry.yaml``) carrying, for every destination, the three delivery
classes the router cares about (vendor write API, C-CDA import, browser pack)
and, crucially, the **evidence** behind each non-trivial claim: a source URL
and the date it was verified.

The headline invariant, enforced in the model rather than left to reviewer
discipline: **any capability that is not ``none``/``unverified`` REQUIRES
evidence.** You cannot assert that Acme EHR accepts FHIR documents without
citing where you read that and when. A claim without a citation fails
validation loudly — this is the no-hallucination rule made mechanical.

Loading is **strict** (unlike the defensive template-pack registry): this YAML
is security-relevant routing data. A half-loaded registry could silently route
PHI to a destination that cannot actually receive it, so malformed YAML or a
schema violation raises rather than degrading. ``DestinationRegistry.get``
raises ``KeyError`` listing the known names (names only — a destination name is
a vendor identifier, never PHI).

PHI rule: this layer never touches patient data. It carries vendor names,
capability kinds, source URLs, and verification dates — nothing patient-derived
ever flows through it, and it must stay that way.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "BrowserKind",
    "Capability",
    "CcdaImportKind",
    "DestinationEntry",
    "DestinationRegistry",
    "DocWriteKind",
    "Evidence",
]

# The packaged registry ships alongside this module.
_PACKAGED_REGISTRY = "registry.yaml"


class DocWriteKind(StrEnum):
    """How a destination accepts a written clinical document by API."""

    FHIR_DOCUMENTREFERENCE = "fhir_documentreference"
    VENDOR_REST = "vendor_rest"
    NONE = "none"
    UNVERIFIED = "unverified"


class CcdaImportKind(StrEnum):
    """How a destination ingests a C-CDA document."""

    API = "api"
    IN_PRODUCT = "in_product"
    NONE = "none"
    UNVERIFIED = "unverified"


class BrowserKind(StrEnum):
    """Whether a browser-automation destination pack drives this destination.

    ``pack`` carries the destination-pack name in ``Capability.detail``. A
    browser capability needs NO evidence URL: its evidence is the pack's own
    canary fixtures (selectors re-validated against the live UI in preflight),
    not a citable web page — so the evidence-required rule below exempts it.
    """

    PACK = "pack"
    NONE = "none"


# Kinds that assert nothing about the world and therefore need no evidence.
_NO_EVIDENCE_KINDS = frozenset({"none", "unverified", BrowserKind.PACK.value})


class Evidence(BaseModel):
    """The citation behind a capability claim: where it was read, and when.

    ``source_url`` must be an http(s) URL (a vendor doc page, a developer
    portal). ``verified`` is the date a human last confirmed the claim against
    that source — the anchor of the quarterly re-verification ritual.
    """

    model_config = ConfigDict(extra="forbid")

    source_url: str
    verified: date
    note: str = ""

    @model_validator(mode="after")
    def _check_url_scheme(self) -> Self:
        if not (self.source_url.startswith(("http://", "https://"))):
            raise ValueError("source_url must start with http:// or https://")
        return self


class Capability(BaseModel):
    """One delivery capability of a destination, with its evidence.

    ``kind`` is drawn from a closed enum per capability class (see
    :class:`DocWriteKind`, :class:`CcdaImportKind`, :class:`BrowserKind`).

    The no-hallucination rule (enforced here, not in review): any ``kind`` that
    is not ``none``/``unverified`` REQUIRES ``evidence`` — except a browser
    ``pack``, whose evidence is the pack's canary fixtures rather than a URL.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str
    detail: str = ""
    evidence: Evidence | None = None

    @model_validator(mode="after")
    def _require_evidence(self) -> Self:
        if self.kind not in _NO_EVIDENCE_KINDS and self.evidence is None:
            raise ValueError(
                f"capability kind {self.kind!r} asserts a verifiable claim and "
                "requires evidence (source_url + verified date) — the "
                "no-hallucination rule"
            )
        return self


class DestinationEntry(BaseModel):
    """One destination's full capability declaration."""

    model_config = ConfigDict(extra="forbid")

    name: str
    display: str
    doc_write_api: Capability
    ccda_import: Capability
    browser: Capability


class DestinationRegistry(BaseModel):
    """The whole registry: destination name -> capability declaration.

    Load the packaged data with :meth:`load`; layer a user's own re-verified
    file on top with :meth:`merged`. Both raise on malformed input — a broken
    registry must never half-load (it routes PHI).
    """

    model_config = ConfigDict(extra="forbid")

    # default_factory so an empty/comment-only overlay file is a registry
    # with no entries (a harmless no-op overlay), not a ValidationError.
    entries: dict[str, DestinationEntry] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _keys_match_names(self) -> Self:
        # The mapping key is the lookup identity; a body `name:` that
        # disagrees would make `list` show one name and `route` answer to
        # another — a silent-disagreement class routing data cannot carry.
        mismatched = sorted(k for k, e in self.entries.items() if k != e.name)
        if mismatched:
            raise ValueError(
                f"registry key/name mismatch for: {', '.join(mismatched)} — "
                "the mapping key must equal the entry's `name` field"
            )
        return self

    @classmethod
    def _from_yaml(cls, text: str) -> DestinationRegistry:
        # ``or {}`` so an empty/comment-only file is a registry with no
        # entries rather than ``None`` blowing up validation.
        data = yaml.safe_load(text) or {}
        return cls.model_validate(data)

    @classmethod
    def load(cls, path: Path | None = None) -> DestinationRegistry:
        """Load the registry.

        With no ``path``, loads the packaged ``destinations/registry.yaml`` via
        ``importlib.resources`` (works from a wheel). An explicit ``path``
        loads that file instead — a user's own overlay used standalone.

        Malformed YAML or a schema violation raises (loud): the registry is
        routing data, and a half-loaded one is a patient-safety hazard.
        """
        if path is None:
            text = files(__package__).joinpath(_PACKAGED_REGISTRY).read_text(encoding="utf-8")
        else:
            text = path.read_text(encoding="utf-8")
        return cls._from_yaml(text)

    @classmethod
    def merged(cls, overlay: Path) -> DestinationRegistry:
        """Load the packaged registry, then overlay a user's file on top.

        Overlay entries **replace** same-named packaged entries wholesale (a
        practice keeping its own re-verified registry shadows the shipped data
        for those destinations); names only in the overlay are added. The
        overlay is validated exactly like the packaged file — it raises on
        malformed input.
        """
        base = cls.load()
        extra = cls.load(overlay)
        entries = dict(base.entries)
        entries.update(extra.entries)  # overlay wins on name collision
        return cls(entries=entries)

    def get(self, name: str) -> DestinationEntry:
        """Return one destination, or raise ``KeyError`` listing known names.

        The message carries destination names only — vendor identifiers, never
        anything patient-derived.
        """
        try:
            return self.entries[name]
        except KeyError:
            known = ", ".join(sorted(self.entries)) or "(none)"
            raise KeyError(f"unknown destination {name!r}; known: {known}") from None
