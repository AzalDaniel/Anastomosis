"""Immutable, hash-addressed profiles for a migration's three inputs
(source, destination, layout), each a SHA-256 over its own canonical JSON
serialization (``sort_keys``, no clock, domain-separated by schema version
and kind). :class:`RunBinding` is the three plus the digest over them, so
a later step over the same output folder recaptures them and refuses when
any one differs, naming which (53). PHI: adapter/destination/pack names,
version strings, capability kinds, hex digests — never a patient value.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from anastomosis.destinations.registry import DestinationRegistry
    from anastomosis.reconstruct.packtrust import PackTrust

__all__ = [
    "PROFILE_SCHEMA_VERSION",
    "SOURCE_BUILTIN",
    "SOURCE_LEARNED",
    "DestinationCapability",
    "DestinationProfile",
    "LayoutProfile",
    "ProfileError",
    "RunBinding",
    "SourceProfile",
    "capture_binding",
    "capture_destination_profile",
    "capture_layout_profile",
    "capture_source_profile",
]

logger = logging.getLogger(__name__)

#: Bumped when the canonical payload of ANY profile changes shape. It is mixed
#: into every digest, so an old manifest's hashes can never accidentally compare
#: equal to a new capture's — a schema change invalidates bindings loudly rather
#: than silently comparing two different questions.
PROFILE_SCHEMA_VERSION = 1

#: What kind of thing a :class:`SourceProfile` names.
SOURCE_BUILTIN = "builtin"
SOURCE_LEARNED = "learned"


class ProfileError(Exception):
    """A profile could not be captured — loud, PHI-safe, never a silent
    default. Raised only for an unknown source adapter or destination; an
    unreadable pack/mapping is different and records a ``None`` content
    hash instead, itself a value the binding compares as drift."""


def _digest(kind: str, payload: Mapping[str, Any]) -> str:
    """SHA-256 hex over ``payload``'s canonical JSON, domain-separated by
    ``kind`` and schema version: without the kind prefix, two different
    profiles that serialize identically would collide."""
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256()
    digest.update(f"anastomosis.profile\0{PROFILE_SCHEMA_VERSION}\0{kind}\0".encode())
    digest.update(body.encode("utf-8"))
    return digest.hexdigest()


@dataclass(frozen=True)
class SourceProfile:
    """WHICH source adapter and, for a learned one, exactly which mapping:
    a built-in has no content hash, a learned adapter's is its
    ``mapping.json`` digest. :attr:`taught_for_destination` is ``None``
    for an unbound teach or a built-in (32)."""

    name: str
    kind: str
    mapping_id: str | None = None
    mapping_sha256: str | None = None
    spec_version: int | None = None
    taught_for_destination: str | None = None
    taught_for_destination_hash: str | None = None

    def payload(self) -> dict[str, Any]:
        """The canonical, JSON-only body this profile's hash is taken over."""
        return {
            "name": self.name,
            "kind": self.kind,
            "mapping_id": self.mapping_id,
            "mapping_sha256": self.mapping_sha256,
            "spec_version": self.spec_version,
            "taught_for_destination": self.taught_for_destination,
            "taught_for_destination_hash": self.taught_for_destination_hash,
        }

    @property
    def profile_hash(self) -> str:
        """SHA-256 hex over :meth:`payload` — this profile's address."""
        return _digest("source", self.payload())

    def to_json(self) -> dict[str, Any]:
        """The payload plus its own hash, as it lands in a run manifest."""
        return {**self.payload(), "profile_hash": self.profile_hash}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> SourceProfile:
        """Rebuild a profile from a run manifest's recorded fields."""
        return cls(
            name=str(data["name"]),
            kind=str(data["kind"]),
            mapping_id=_opt_str(data.get("mapping_id")),
            mapping_sha256=_opt_str(data.get("mapping_sha256")),
            spec_version=_opt_int(data.get("spec_version")),
            taught_for_destination=_opt_str(data.get("taught_for_destination")),
            taught_for_destination_hash=_opt_str(data.get("taught_for_destination_hash")),
        )


@dataclass(frozen=True)
class DestinationCapability:
    """One declared capability class of a destination. ``slot`` is the
    registry field, ``kind`` its closed-enum value, ``detail`` the
    vendor-facing specifics."""

    slot: str
    kind: str
    detail: str

    def payload(self) -> dict[str, Any]:
        return {"slot": self.slot, "kind": self.kind, "detail": self.detail}


@dataclass(frozen=True)
class DestinationProfile:
    """WHICH destination product, at WHICH declared version, able to
    receive what. Deliberately NOT hashed: each capability's ``evidence``
    block — the quarterly re-verification date changes with no routing
    fact changing, and a binding that broke on that would train operators
    to ignore it. A changed ``kind`` or ``detail`` does break it."""

    name: str
    display: str
    version: str
    capabilities: tuple[DestinationCapability, ...]

    def payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display": self.display,
            "version": self.version,
            "capabilities": [cap.payload() for cap in self.capabilities],
        }

    @property
    def profile_hash(self) -> str:
        return _digest("destination", self.payload())

    def to_json(self) -> dict[str, Any]:
        return {**self.payload(), "profile_hash": self.profile_hash}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> DestinationProfile:
        raw = data.get("capabilities") or []
        caps = tuple(
            DestinationCapability(
                slot=str(item["slot"]), kind=str(item["kind"]), detail=str(item["detail"])
            )
            for item in raw
        )
        return cls(
            name=str(data["name"]),
            display=str(data["display"]),
            version=str(data["version"]),
            capabilities=caps,
        )


@dataclass(frozen=True)
class LayoutProfile:
    """WHICH representation rendered the chart pages, at WHICH content
    hash — ``ccda-standard`` uses no Jinja pack, so those fields are
    truthfully ``None``. ``root`` is recorded but NOT hashed: a moved pack
    is not a changed one, and :func:`reprofile_layout` needs it to ask the
    honest question without a ``--pack-dir`` list it never received."""

    render_mode: str
    pack: str | None = None
    origin: str | None = None
    content_hash: str | None = None
    root: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "render_mode": self.render_mode,
            "pack": self.pack,
            "origin": self.origin,
            "content_hash": self.content_hash,
        }

    @property
    def profile_hash(self) -> str:
        return _digest("layout", self.payload())

    def to_json(self) -> dict[str, Any]:
        return {**self.payload(), "root": self.root, "profile_hash": self.profile_hash}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> LayoutProfile:
        return cls(
            render_mode=str(data["render_mode"]),
            pack=_opt_str(data.get("pack")),
            origin=_opt_str(data.get("origin")),
            content_hash=_opt_str(data.get("content_hash")),
            root=_opt_str(data.get("root")),
        )


@dataclass(frozen=True)
class RunBinding:
    """The three profiles a run is bound to, plus the digest over them.
    :attr:`hashes` names WHICH profile moved on drift instead of one
    opaque mismatch; :attr:`binding_hash` folds all three into the
    manifest's integrity line."""

    source: SourceProfile
    destination: DestinationProfile
    layout: LayoutProfile

    @property
    def hashes(self) -> dict[str, str]:
        return {
            "source": self.source.profile_hash,
            "destination": self.destination.profile_hash,
            "layout": self.layout.profile_hash,
        }

    @property
    def binding_hash(self) -> str:
        return _digest("binding", self.hashes)

    def to_json(self) -> dict[str, Any]:
        return {
            "source": self.source.to_json(),
            "destination": self.destination.to_json(),
            "layout": self.layout.to_json(),
            "binding_hash": self.binding_hash,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> RunBinding:
        return cls(
            source=SourceProfile.from_json(data["source"]),
            destination=DestinationProfile.from_json(data["destination"]),
            layout=LayoutProfile.from_json(data["layout"]),
        )


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _opt_int(value: Any) -> int | None:
    return None if value is None else int(value)


# --- capture -----------------------------------------------------------------
#
# Every capture reads the LIVE state of the machine: the registered adapter, the
# packaged registry, the discovered pack. That is the whole point — a profile
# captured now, compared against one recorded then, is what makes drift visible.
# The imports are lazy (the module discipline the rest of core keeps), so
# importing this module costs nothing.


def capture_source_profile(name: str, *, sources_dir: Path | None = None) -> SourceProfile:
    """Profile the registered source adapter ``name``. A learned adapter's
    mapping digest is read from disk when saved there, else recomputed via
    :func:`~anastomosis.sources.learned.spec.mapping_content_hash` so both
    routes agree. Raises :class:`ProfileError` for an unknown adapter."""
    from anastomosis.sources import get_source
    from anastomosis.sources.learned import LearnedSourceAdapter, user_sources_dir
    from anastomosis.sources.learned.spec import SPEC_FILENAME, mapping_content_hash

    try:
        adapter = get_source(name)
    except KeyError as exc:
        raise ProfileError(str(exc.args[0] if exc.args else exc)) from None
    if not isinstance(adapter, LearnedSourceAdapter):
        return SourceProfile(name=name, kind=SOURCE_BUILTIN)

    spec = adapter.spec
    base = sources_dir if sources_dir is not None else user_sources_dir()
    spec_path = base / spec.mapping_id / SPEC_FILENAME
    try:
        digest = hashlib.sha256(spec_path.read_bytes()).hexdigest()
    except OSError:
        # No file to read (custom --out-dir, or a session-only registration):
        # the spec in hand IS the mapping, and its canonical text is the same
        # text save_mapping would have written.
        digest = mapping_content_hash(spec)
    bound = spec.destination_binding
    return SourceProfile(
        name=name,
        kind=SOURCE_LEARNED,
        mapping_id=spec.mapping_id,
        mapping_sha256=digest,
        spec_version=spec.spec_version,
        taught_for_destination=None if bound is None else bound.destination,
        taught_for_destination_hash=None if bound is None else bound.profile_hash,
    )


def capture_destination_profile(
    name: str, registry: DestinationRegistry | None = None
) -> DestinationProfile:
    """Profile the destination ``name`` from the capability registry.
    Raises :class:`ProfileError` for an unknown name, naming the known
    destinations, the same refusal ``plan_route`` makes."""
    from anastomosis.destinations.registry import DestinationRegistry

    reg = registry if registry is not None else DestinationRegistry.load()
    try:
        entry = reg.get(name)
    except KeyError as exc:
        raise ProfileError(str(exc.args[0] if exc.args else exc)) from None
    caps = tuple(
        DestinationCapability(slot=slot, kind=cap.kind, detail=cap.detail)
        for slot, cap in (
            ("doc_write_api", entry.doc_write_api),
            ("ccda_import", entry.ccda_import),
            ("browser", entry.browser),
        )
    )
    return DestinationProfile(
        name=entry.name, display=entry.display, version=entry.version, capabilities=caps
    )


def capture_layout_profile(
    render_mode: str,
    pack: str | None,
    *,
    pack_dirs: Sequence[Path] = (),
    allow_external: bool = False,
    trust: PackTrust | None = None,
) -> LayoutProfile:
    """Profile ``pack`` as it exists on this machine now, via the SAME
    discovery walk the renderer uses, so this is the pack that would
    render. ``pack is None`` profiles the render mode alone; undiscoverable
    or broken records ``None`` and logs rather than raising, leaving the
    renderer to report the fault better a moment later."""
    if pack is None:
        return LayoutProfile(render_mode=render_mode)
    from anastomosis.reconstruct.packs import discover_packs
    from anastomosis.reconstruct.packtrust import pack_content_hash

    status = discover_packs(list(pack_dirs), allow_external=allow_external, trust=trust).get(pack)
    if status is None or status.root is None:
        logger.warning(
            "layout profile for pack %r has no content hash: the pack is not discoverable here",
            pack,
        )
        return LayoutProfile(render_mode=render_mode, pack=pack)
    return LayoutProfile(
        render_mode=render_mode,
        pack=pack,
        origin=status.origin,
        content_hash=pack_content_hash(status.root),
        root=str(status.root),
    )


def reprofile_layout(profile: LayoutProfile) -> LayoutProfile:
    """Re-read the content of the layout a manifest already named, asking
    only whether those bytes still hash the same — never re-running
    discovery, since the caller (upload/delivery) lacks the migration's
    ``--pack-dir`` list and would falsely report a vanished pack. Falls
    back to discovery only when ``root``/``pack`` is unrecorded."""
    from pathlib import Path

    from anastomosis.reconstruct.packtrust import pack_content_hash

    if profile.pack is None or profile.root is None:
        return capture_layout_profile(profile.render_mode, profile.pack)
    root = Path(profile.root)
    if not root.is_dir():
        # Gone since the render. Recorded as no hash, which the binding reads
        # as drift — correctly: a layout that is not there cannot be the layout
        # these charts were made from.
        logger.warning("the layout this run names is no longer at the place it rendered from")
        return LayoutProfile(
            render_mode=profile.render_mode,
            pack=profile.pack,
            origin=profile.origin,
            root=profile.root,
        )
    return LayoutProfile(
        render_mode=profile.render_mode,
        pack=profile.pack,
        origin=profile.origin,
        content_hash=pack_content_hash(root),
        root=profile.root,
    )


def capture_binding(
    *,
    source: str,
    destination: str,
    render_mode: str,
    pack: str | None,
    pack_dirs: Sequence[Path] = (),
    allow_external: bool = False,
    trust: PackTrust | None = None,
    registry: DestinationRegistry | None = None,
    sources_dir: Path | None = None,
    layout: LayoutProfile | None = None,
) -> RunBinding:
    """Capture all three profiles for one run. ``layout`` is passed by a
    caller that already knows WHERE it is
    (:func:`~anastomosis.core.runmanifest.recapture_binding` re-reads it
    at the recorded root) rather than re-discovering it and falsely
    reporting a vanished pack."""
    return RunBinding(
        source=capture_source_profile(source, sources_dir=sources_dir),
        destination=capture_destination_profile(destination, registry),
        layout=layout
        if layout is not None
        else capture_layout_profile(
            render_mode, pack, pack_dirs=pack_dirs, allow_external=allow_external, trust=trust
        ),
    )
