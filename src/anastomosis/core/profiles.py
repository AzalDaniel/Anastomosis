"""Immutable, hash-addressed profiles: the exact inputs a run was prepared under.

A migration has three inputs that decide what its artifacts mean, and all three
can change under the operator between one command and the next:

* the **source** — a built-in adapter, or a learned ``mapping.json`` somebody
  taught and can hand-edit afterwards;
* the **destination** — a registry entry whose declared product, version and
  capabilities decide the route the artifacts were shaped for;
* the **layout** — the template pack whose ``context.py`` and ``template.html``
  rendered the chart pages.

Each becomes a frozen profile here, addressed by a SHA-256 over its own
canonical serialization. Nothing new is hashed that already had a hash: a
learned mapping's digest is the one ``save_mapping`` records in
``source_trust.json`` (:func:`~anastomosis.sources.learned.spec.mapping_content_hash`),
and a pack's is :func:`~anastomosis.reconstruct.packtrust.pack_content_hash`.
The profile hash is a hash *of those identities*, so a profile changes exactly
when the thing it names changes.

:class:`RunBinding` is the three together plus the digest over them. What it
buys is stated in one sentence: a run manifest
(:mod:`anastomosis.core.runmanifest`) names the exact profile hashes a run was
prepared under, and a later step over the same output folder recaptures them and
REFUSES when any one differs — naming which. There is no fallback path and no
"probably fine": a chart rendered by one layout and filed against another
destination's expectations is a misattribution, and this is the mechanical stop.

Canonical serialization is the whole contract, so it is pinned rather than
implied: ``json.dumps(payload, sort_keys=True, separators=(",", ":"),
ensure_ascii=True)`` over a JSON-only payload, domain-separated by schema
version and profile kind. No clock, no host, no ordering churn — two captures of
the same inputs on the same pinned environment produce the same hex.

PHI rule: a profile carries adapter names, destination/vendor identifiers,
version strings, capability kinds, pack names, and hex digests. Nothing
patient-derived reaches it, and nothing ever may.
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
    """A profile could not be captured — loud, PHI-safe, never a silent default.

    Raised only where guessing would be worse than stopping: an unknown source
    adapter, an unknown destination. A pack or mapping whose bytes cannot be
    read is a DIFFERENT case — that is recorded as a ``None`` content hash on
    the profile, which is itself a value the binding compares, so a file that
    becomes readable (or unreadable) later still shows up as drift.
    """


def _digest(kind: str, payload: Mapping[str, Any]) -> str:
    """SHA-256 hex over ``payload``'s canonical JSON, domain-separated by ``kind``.

    The prefix pins three things into the digest that the payload itself does
    not carry: that this is an Anastomosis profile hash, which schema version
    produced it, and which profile kind it describes. Without the last one, two
    different profiles that happened to serialize identically would collide.
    """
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256()
    digest.update(f"anastomosis.profile\0{PROFILE_SCHEMA_VERSION}\0{kind}\0".encode())
    digest.update(body.encode("utf-8"))
    return digest.hexdigest()


@dataclass(frozen=True)
class SourceProfile:
    """WHICH source adapter, and — for a learned one — exactly which mapping.

    A built-in adapter has no content hash: its behavior is the installed
    package's, which the run manifest pins separately as the pipeline version.
    A learned adapter's identity is its ``mapping.json`` digest, so editing a
    reviewed mapping changes this profile and every binding that named it.

    :attr:`taught_for_destination` is the destination the mapping was taught
    against (``None`` for a mapping taught before any destination was chosen,
    and for every built-in). It is carried HERE rather than compared elsewhere
    because it is a property of the mapping, and the refusal it powers — a
    mapping learned for one destination silently run at another — has to name
    both ends.
    """

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
    """One declared capability class of a destination: what it is, and how.

    ``slot`` is the registry field (``doc_write_api``/``ccda_import``/
    ``browser``), ``kind`` its closed-enum value, ``detail`` the vendor-facing
    specifics (an endpoint description, a destination-pack name).
    """

    slot: str
    kind: str
    detail: str

    def payload(self) -> dict[str, Any]:
        return {"slot": self.slot, "kind": self.kind, "detail": self.detail}


@dataclass(frozen=True)
class DestinationProfile:
    """WHICH destination product, at WHICH declared version, able to receive what.

    The version is the registry entry's ``version:`` — see
    :data:`anastomosis.destinations.registry.UNVERSIONED` for the explicit
    string an entry that declares none records.

    What is deliberately NOT hashed: each capability's ``evidence`` block. The
    quarterly re-verification ritual bumps a ``verified`` date without changing
    a single thing about what the destination can receive, and a binding that
    broke every time somebody re-read a vendor doc page would train operators to
    ignore it. A changed ``kind`` or ``detail`` IS a changed routing fact and
    does break the binding.
    """

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
    """WHICH representation rendered the chart pages, at WHICH content hash.

    ``render_mode`` is the operator's choice (``neutral``, ``ccda-standard``, or
    a pack name); ``pack`` is the template pack it resolved to. The
    ``ccda-standard`` view renders through HL7's own stylesheet and no Jinja
    pack at all, so ``pack``/``origin``/``content_hash`` are all ``None`` there
    — a truthful "no pack was involved", not a lost field.

    ``content_hash`` is :func:`~anastomosis.reconstruct.packtrust.pack_content_hash`
    verbatim: the same digest over the same ``context.py`` + ``template.html`` +
    ``pack.yaml`` bytes the trust store gates execution on. A pack that could not
    be discovered records ``None``, which is a value the binding compares like
    any other — a pack that appears or disappears between runs is drift. What
    that digest does NOT cover is what ``packtrust`` does not cover: auxiliary
    assets beside those three files are unpinned there and unpinned here.

    ``root`` is recorded and deliberately NOT hashed. A later step does not
    carry the ``--pack-dir`` list the migration was given, so re-running
    discovery there would fail to find an external pack, record no hash, and
    report drift for a pack nobody touched — a refusal people learn to ignore,
    which is worse than no refusal. Recording where the render actually read
    from lets the later step ask the honest question (do those bytes still hash
    the same?) at the right place. It stays out of the hash for the same reason
    the destination's ``evidence`` does: a pack moved to another absolute path
    is not a pack whose content changed, and a machine-specific path inside the
    digest would make every binding unportable.
    """

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

    :attr:`hashes` is the comparison surface — a plain ``{profile name: hex}``
    map, so a drift check names WHICH profile moved instead of reporting one
    opaque mismatch. :attr:`binding_hash` folds the three into one value for the
    manifest's own integrity line.
    """

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
    """Profile the registered source adapter ``name``.

    A learned adapter's mapping digest is read from its saved ``mapping.json``
    when one is on disk, and recomputed from the in-memory spec otherwise (a
    mapping saved outside the discoverable directory, or registered for this
    session only) — both through
    :func:`~anastomosis.sources.learned.spec.mapping_content_hash`, so the two
    routes cannot disagree about what a mapping's digest is.

    Raises :class:`ProfileError` for an unknown adapter: a run cannot be bound
    to a source that does not exist, and defaulting would bind it to nothing.
    """
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

    Raises :class:`ProfileError` (naming the known destinations, as the registry
    does) for an unknown name — the same loud refusal ``plan_route`` makes, so a
    binding can never be taken against a destination nobody declared.
    """
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
    """Profile the template pack ``pack`` as it exists on this machine right now.

    ``pack is None`` (the ``ccda-standard`` view) profiles the render mode alone.
    Discovery uses the SAME defensive walk the renderer uses, so the pack this
    profiles is the pack that would render — a shadowing user pack included. An
    undiscoverable or broken pack records a ``None`` content hash and says so in
    a PHI-free log line rather than raising: refusing here would block a run for
    a fault the renderer will report far better a moment later.
    """
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
    """Re-read the content of the layout a manifest already named.

    The manifest supplies the identity AND the place; this asks only whether
    those same bytes still hash the same. Discovery is not re-run, because the
    step asking is typically an upload or a delivery, which never received the
    ``--pack-dir`` list the migration was given: rediscovering would report a
    vanished pack for one that is exactly where it was, and send the operator
    to restore inputs nobody changed.

    A manifest written before ``root`` was recorded, or one naming no pack at
    all, has nothing to re-read here and falls back to discovery — the old
    behaviour, for the old shape.
    """
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
    """Capture all three profiles for one run — the binding a manifest names.

    ``layout`` is passed by a caller that already knows WHERE the layout is —
    :func:`~anastomosis.core.runmanifest.recapture_binding` re-reads it at the
    root the manifest recorded, because discovery from a ``--pack-dir`` list a
    later step never received would report a vanished pack for one that never
    moved.
    """
    return RunBinding(
        source=capture_source_profile(source, sources_dir=sources_dir),
        destination=capture_destination_profile(destination, registry),
        layout=layout
        if layout is not None
        else capture_layout_profile(
            render_mode, pack, pack_dirs=pack_dirs, allow_external=allow_external, trust=trust
        ),
    )
