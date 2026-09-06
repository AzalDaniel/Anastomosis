"""Data-driven destination capability registry (RULES.md 69-70).

Capabilities are DATA (``registry.yaml``), not code: a source URL and
verified date back every non-``none``/``unverified`` claim, enforced here.
Loading is STRICT (unlike the template-pack registry): malformed YAML or a
schema violation raises, since a half-loaded registry could route PHI to a
destination that cannot receive it.

PHI: vendor names, capability kinds, source URLs, verification dates only.
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
    "UNVERSIONED",
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

#: The version a destination entry declares when it declares none. An explicit
#: value rather than a missing key: "this product has no version we can read"
#: is a fact, and a run that binds to it should say so out loud rather than
#: record an absence somebody later reads as an oversight.
UNVERSIONED = "unversioned"


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

    ``pack`` carries the destination-pack name in ``Capability.detail`` and
    needs no evidence URL — its evidence is the pack's own canary fixtures.
    """

    PACK = "pack"
    NONE = "none"


# Kinds that assert nothing about the world and therefore need no evidence.
_NO_EVIDENCE_KINDS = frozenset({"none", "unverified", BrowserKind.PACK.value})


class Evidence(BaseModel):
    """The citation behind a capability claim: where it was read, and when.

    ``source_url`` must be http(s); ``verified`` anchors the quarterly
    re-verification ritual.
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

    ``kind`` is drawn from a closed enum per class (:class:`DocWriteKind`,
    :class:`CcdaImportKind`, :class:`BrowserKind`). Any ``kind`` that is
    not ``none``/``unverified`` REQUIRES ``evidence`` (RULES.md 69) — except
    a browser ``pack``, whose evidence is its canary fixtures.
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
    """One destination's full capability declaration, at one declared version.

    ``version`` defaults to :data:`UNVERSIONED` — an explicit value, not a
    missing field, for a vendor with no readable product version. An entry
    that DOES carry one refuses when it changes underneath
    (:mod:`anastomosis.core.profiles`).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    display: str
    #: The destination product version these capabilities describe. See the
    #: class docstring for why "unversioned" is a value rather than an absence.
    version: str = UNVERSIONED
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

        With no ``path``, loads the packaged registry via
        ``importlib.resources``; an explicit ``path`` loads a user overlay
        standalone. Malformed YAML or a schema violation raises — a
        half-loaded registry is a patient-safety hazard.
        """
        if path is None:
            text = files(__package__).joinpath(_PACKAGED_REGISTRY).read_text(encoding="utf-8")
        else:
            text = path.read_text(encoding="utf-8")
        return cls._from_yaml(text)

    @classmethod
    def merged(cls, overlay: Path) -> DestinationRegistry:
        """Load the packaged registry, then overlay a user's file on top.

        Overlay entries REPLACE same-named packaged entries wholesale;
        names only in the overlay are added. Validated exactly like the
        packaged file.
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
