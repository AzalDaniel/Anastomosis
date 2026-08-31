"""Template-pack contract and defensive discovery.

A template pack is a directory:

    my_pack/
      pack.yaml      — manifest (this module's schema)
      template.html  — Jinja2 page template
      context.py     — build_context(encounter, record, cfg) -> dict
      partials/…     — optional includes, assets

Discovery order (first definition of a name wins, so a user can shadow a
built-in): explicit ``--pack-dir`` directories → the per-user pack directory
(:func:`user_packs_dir`) → ``anastomosis.packs`` built-ins shipped under
``anastomosis/packs/``.

The per-user directory is where a layout taught from samples (``anast pack
init`` / the GUI's Teach view) lands, so a learned layout is discoverable from
any working directory and in any later process — the same per-user-home
convention as :func:`anastomosis.reconstruct.packtrust.user_pack_trust_path`
and :func:`anastomosis.sources.learned.user_sources_dir`.

Loading is **defensive** — the brain-like modularity invariant. A pack
with a broken manifest, missing template, or crashing ``context.py`` is
returned as unavailable *with a diagnosis*; it never raises out of
discovery and never takes the other packs down. A vendor template rotting
is a one-pack event.

Trust model: packs from ``--pack-dir`` execute Python
(``context.py``), so external packs load only when the caller passes
``allow_external=True`` (the CLI flag is explicit consent); built-ins are
implicitly trusted. On top of that, an optional content-hash pin
(``trust=``/``trust_new=``, see :mod:`anastomosis.reconstruct.packtrust`)
gates external code on a trust-on-first-use basis: an external pack whose
code changed since it was trusted is returned unavailable and is NOT
exec'd. Enforcement is opt-in — ``trust=None`` preserves the consent-only
behavior for bare programmatic callers.

Per-user packs sit deliberately between the two. They need no
``allow_external`` — the operator put them in their own home directory, and
requiring a second flag for one's own learned layout is what made a taught
layout unselectable. They are NOT implicitly trusted either: their
``context.py`` is executable Python, so it runs only when a ``trust`` store
maps the pack root to its current content hash. Without a store, or with a
hash that no longer matches, the pack is returned unavailable with a diagnosis
and nothing is exec'd. The consent is granted where it is meaningful: writing
the pack (the confirmed Teach) records the hash of the bytes it just wrote.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from anastomosis.core.model import CHARTABLE_KINDS
from anastomosis.reconstruct.packtrust import PackSnapshot, PackTrust, read_pack_snapshot

__all__ = [
    "ORIGIN_BUILTIN",
    "ORIGIN_PACK_DIR",
    "ORIGIN_USER",
    "LoadedPack",
    "PackCoverage",
    "PackManifest",
    "PackStatus",
    "SectionFlag",
    "discover_packs",
    "user_packs_dir",
]

_BUILTIN_DIR = Path(__file__).resolve().parent.parent / "packs"

#: Where a pack came from, as reported on :class:`PackStatus` and the info
#: surface. Named because three call sites now branch on the values and a
#: mistyped literal would silently downgrade a pack's trust handling.
ORIGIN_BUILTIN = "builtin"
ORIGIN_PACK_DIR = "pack-dir"
ORIGIN_USER = "user"


def user_packs_dir() -> Path:
    """The per-user directory learned template packs live in.

    A plain ``~/.anastomosis/packs`` (NOT ``platformdirs`` — no new dependency),
    matching :func:`anastomosis.reconstruct.packtrust.user_pack_trust_path`,
    :func:`anastomosis.core.migrate.user_migrations_path` and
    :func:`anastomosis.sources.learned.user_sources_dir` so all Anastomosis user
    state lives under one root. Each pack is ``<here>/<pack_name>/pack.yaml``.

    Stable across working directories and processes, which is the whole point:
    a layout taught in one session has to be selectable in the next one,
    wherever the app happens to be launched from.
    """
    return Path.home() / ".anastomosis" / "packs"


ContextBuilder = Callable[..., dict[str, Any]]


class SectionFlag(BaseModel):
    """One user-togglable section — a row in the GUI's checkbox matrix."""

    model_config = ConfigDict(extra="forbid")

    label: str
    default: bool = True
    description: str = ""


class PageGeometry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    size: str = "Letter"
    margin_top: str = "0.5in"
    margin_right: str = "0.5in"
    margin_bottom: str = "0.5in"
    margin_left: str = "0.5in"


class FilenameRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Render-time format string; fields come from the pack's build_context.
    pattern: str = "{family}_{given}_{dos}.pdf"
    # What to do when two documents resolve to the same name. Only
    # "guid_suffix" (append a short unique source-id suffix — the
    # same-day-visit defense) is implemented: reconstruct.engine's
    # _allocate_target hardcodes that one behavior, so any other value is
    # refused here rather than silently ignored.
    collision: str = "guid_suffix"

    @field_validator("collision")
    @classmethod
    def _collision_is_implemented(cls, value: str) -> str:
        if value != "guid_suffix":
            raise ValueError(
                f"filename.collision: only 'guid_suffix' is implemented (got {value!r})"
            )
        return value


class PackCoverage(BaseModel):
    """What this layout carries out of the record, and what it leaves out.

    QA holds both the record and the rendered page, so it can see when a
    populated collection reached neither. What it cannot see on its own is
    whether that is a defect or the layout: a SOAP visit note has no problem
    list by design, and a forensic chart replica very much does. Without a
    statement from the pack, the same evidence has to be read two ways, so the
    suite read it the generous way and every omission graded clean.

    Both halves are required of a shipped pack. ``carries`` names the kinds
    whose absence from a page is a failure; ``omits`` maps each remaining kind
    to the reason its layout has no place for it, and QA reports those as
    counted-but-expected rather than green. A kind in neither is not excused —
    it is undeclared, and the guard test in ``tests/unit/test_packs.py`` says
    so, because an exemption you get by forgetting is the kind that outlives
    the reason for it.

    A pack that declares nothing at all (an external pack written before this
    field existed) is treated as conservatively as it can be: every kind is
    verified, and a total absence warns rather than fails, with the finding
    naming this field as the fix.
    """

    model_config = ConfigDict(extra="forbid")

    carries: list[str] = Field(default_factory=list)
    omits: dict[str, str] = Field(default_factory=dict)

    @field_validator("carries")
    @classmethod
    def _carries_are_known_kinds(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - set(CHARTABLE_KINDS))
        if unknown:
            raise ValueError(f"coverage.carries: unknown kind(s) {unknown}")
        return value

    @field_validator("omits")
    @classmethod
    def _omits_are_known_kinds_with_reasons(cls, value: dict[str, str]) -> dict[str, str]:
        unknown = sorted(set(value) - set(CHARTABLE_KINDS))
        if unknown:
            raise ValueError(f"coverage.omits: unknown kind(s) {unknown}")
        blank = sorted(kind for kind, reason in value.items() if not reason.strip())
        if blank:
            raise ValueError(f"coverage.omits: {blank} need a reason, not an empty string")
        # Collapse whitespace: these reasons are written as folded YAML scalars
        # and come back with the block's line breaks and trailing newline still
        # in them, which then land mid-sentence in a QA finding.
        return {kind: " ".join(reason.split()) for kind, reason in value.items()}

    @model_validator(mode="after")
    def _no_kind_is_both(self) -> PackCoverage:
        both = sorted(set(self.carries) & set(self.omits))
        if both:
            raise ValueError(f"coverage: {both} listed as both carried and omitted")
        return self

    @property
    def declared(self) -> bool:
        return bool(self.carries or self.omits)


class PackManifest(BaseModel):
    """Schema of ``pack.yaml``."""

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    #: What a person should read instead of ``name``. Optional, so every
    #: pack.yaml written before this field existed stays valid under
    #: ``extra="forbid"``; empty means "derive one from the id", which is what
    #: every surface used to do because there was nowhere to put a real one.
    display: str = ""
    description: str = ""
    locale: str = "en_US"
    timezone: str = "America/New_York"
    page: PageGeometry = Field(default_factory=PageGeometry)
    filename: FilenameRules = Field(default_factory=FilenameRules)
    sections: dict[str, SectionFlag] = Field(default_factory=dict)
    # Design tokens the QA visual checks assert on (colors, fonts, spacing).
    tokens: dict[str, str] = Field(default_factory=dict)
    # Header fields the L3 delivery verification reads back off the PDF.
    verify_header_fields: list[str] = Field(default_factory=list)
    #: How many places this layout stamps the RENDER DAY on purpose.
    #:
    #: ``DateStalenessCheck`` treats today's date on an old chart as the
    #: signature of a template that called now() by mistake, which is usually
    #: exactly what it is. The Practice Fusion replica stamps it deliberately,
    #: once, in the medication list's "as of" heading — a forensic rule from
    #: the gold standard, not a bug — so that pack warned on 100% of its
    #: documents forever, and a signal that is always on is not a signal.
    #:
    #: A COUNT rather than a boolean, so the check stays alive on a pack that
    #: declares one: more render-day dates on the page than the layout admits
    #: to is still the accidental-now() defect, and still warns. Exempting the
    #: pack outright would have traded a useless warning for a blind check.
    render_day_stamps: int = Field(default=0, ge=0)
    # Which record collections this layout renders, and why it skips the rest.
    # Optional so a pack written before the field stays loadable; QA treats an
    # undeclared pack conservatively rather than as fully covered.
    coverage: PackCoverage = Field(default_factory=PackCoverage)


@dataclass(frozen=True)
class LoadedPack:
    manifest: PackManifest
    root: Path
    template_path: Path
    build_context: ContextBuilder


@dataclass(frozen=True)
class PackStatus:
    """Discovery result for one pack name: available or diagnosed-broken."""

    name: str
    pack: LoadedPack | None
    diagnosis: str | None = None
    origin: str = ORIGIN_BUILTIN  # ORIGIN_PACK_DIR | ORIGIN_USER | ORIGIN_BUILTIN
    #: The directory this status is about, whether or not it loaded. An
    #: unavailable pack needs it most: "untrusted" is only actionable once the
    #: operator knows which directory has to be reviewed.
    root: Path | None = None

    @property
    def available(self) -> bool:
        return self.pack is not None


def _load_context_builder(path: Path) -> ContextBuilder:
    # Unique module name: two packs both shipping context.py must not collide
    # in sys.modules.
    module_name = f"anastomosis._pack_context_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    builder = getattr(module, "build_context", None)
    if not callable(builder):
        raise AttributeError("context.py defines no callable build_context")
    return cast(ContextBuilder, builder)


def _load_context_builder_from_source(source: bytes, path: Path) -> ContextBuilder:
    """Compile and exec pinned ``context.py`` bytes into a fresh module.

    ``source`` is the exact snapshot bytes the trust hash covered, so the code
    that runs is provably the code that was hashed — no writer can swap the file
    between the check and ``exec`` (the TOCTOU the snapshot closes). ``__file__``
    is set to the real on-disk ``path`` so ``build_context``'s pack-relative asset
    resolution (``Path(__file__).parent / …``) keeps working; only the CODE is
    pinned, not those auxiliary assets. Mirrors :func:`_load_context_builder`: a
    unique ``sys.modules`` name (two packs' ``context.py`` must not collide),
    popped on failure.
    """
    module_name = f"anastomosis._pack_context_{uuid4().hex}"
    code = compile(source, str(path), "exec")
    module = types.ModuleType(module_name)
    module.__file__ = str(path)
    sys.modules[module_name] = module
    try:
        exec(code, module.__dict__)  # noqa: S102 — pinned, hash-gated pack code
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    builder = getattr(module, "build_context", None)
    if not callable(builder):
        raise AttributeError("context.py defines no callable build_context")
    return cast(ContextBuilder, builder)


_BuildResult = tuple[PackManifest, Path, ContextBuilder]


def _finish_load(
    name_cell: list[str], origin: str, root: Path, build: Callable[[], _BuildResult]
) -> PackStatus:
    """Run ``build()`` and turn it into a :class:`PackStatus`, diagnosing defensively.

    Shared tail for :func:`_load_pack_dir` and :func:`_load_pack_snapshot`: only
    how ``build`` reads the manifest/template/context (disk vs. pinned snapshot
    bytes) differs between the two; the diagnosis shape does not. ``name_cell``
    is a one-item mutable cell so ``build`` can update the reported name to
    ``manifest.name`` as soon as the manifest parses — including on a LATER
    failure (missing template, crashing ``context.py``).
    """
    try:
        manifest, template_path, builder = build()
    except (ValidationError, OSError, ImportError, AttributeError, yaml.YAMLError) as exc:
        # Diagnosis carries the exception type and pack-relative detail only —
        # safe to log, enough to start the re-discovery wizard.
        return PackStatus(
            name=name_cell[0],
            pack=None,
            diagnosis=f"{type(exc).__name__}: {exc}",
            origin=origin,
            root=root,
        )
    except Exception as exc:  # context.py crashed at import: arbitrary errors
        return PackStatus(
            name=name_cell[0],
            pack=None,
            diagnosis=f"context.py failed at import ({type(exc).__name__})",
            origin=origin,
            root=root,
        )
    return PackStatus(
        name=name_cell[0],
        pack=LoadedPack(
            manifest=manifest, root=root, template_path=template_path, build_context=builder
        ),
        origin=origin,
        root=root,
    )


def _load_pack_dir(root: Path, origin: str) -> PackStatus:
    name_cell = [root.name]

    def build() -> _BuildResult:
        manifest_path = root / "pack.yaml"
        if not manifest_path.is_file():
            raise FileNotFoundError("pack.yaml not found")
        manifest = PackManifest.model_validate(
            yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        )
        name_cell[0] = manifest.name
        template_path = root / "template.html"
        if not template_path.is_file():
            raise FileNotFoundError("template.html not found")
        context_path = root / "context.py"
        if not context_path.is_file():
            raise FileNotFoundError("context.py not found")
        return manifest, template_path, _load_context_builder(context_path)

    return _finish_load(name_cell, origin, root, build)


def _load_pack_snapshot(snapshot: PackSnapshot, origin: str) -> PackStatus:
    """Load a pack from its hashed :class:`PackSnapshot` — the trusted-external path.

    Parses ``pack.yaml`` and executes ``context.py`` from the snapshot's pinned
    bytes rather than re-reading them, so the loaded/executed content is exactly
    what the trust hash covered (the TOCTOU close for arbitrary-code execution).
    Pinning boundary, precisely: ``context.py`` (executable Python) is pinned to
    execution; ``pack.yaml`` is parsed from pinned bytes; ``template.html``
    contributes to the hash and its presence is checked here, but the render
    engine reads it from disk at render time — a Jinja template is a bounded,
    non-importing surface, and execution-pinning it (render-from-snapshot) is
    tracked on the backlog. Auxiliary assets (partials, images) are outside the
    hash entirely. Diagnoses defensively, identically to :func:`_load_pack_dir`.
    """
    root = snapshot.root
    name_cell = [root.name]

    def build() -> _BuildResult:
        manifest_bytes = snapshot.files.get("pack.yaml")
        if manifest_bytes is None:
            raise FileNotFoundError("pack.yaml not found")
        manifest = PackManifest.model_validate(yaml.safe_load(manifest_bytes.decode("utf-8")))
        name_cell[0] = manifest.name
        if snapshot.files.get("template.html") is None:
            raise FileNotFoundError("template.html not found")
        context_bytes = snapshot.files.get("context.py")
        if context_bytes is None:
            raise FileNotFoundError("context.py not found")
        builder = _load_context_builder_from_source(context_bytes, root / "context.py")
        return manifest, root / "template.html", builder

    return _finish_load(name_cell, origin, root, build)


def _packs_under(parent: Path, origin: str) -> list[tuple[Path, str]]:
    """The pack candidates a parent directory offers, as ``(root, origin)``.

    A directory may BE a pack (it holds ``pack.yaml``) or CONTAIN packs. A
    directory that is neither — absent, a file, empty — contributes nothing;
    discovery stays defensive about what it is pointed at.
    """
    if not parent.is_dir():
        return []
    if (parent / "pack.yaml").is_file():
        return [(parent, origin)]
    return [
        (child, origin)
        for child in sorted(parent.iterdir())
        if child.is_dir() and (child / "pack.yaml").is_file()
    ]


def _iter_candidate_dirs(
    pack_dirs: list[Path], *, include_user: bool = True
) -> list[tuple[Path, str]]:
    """Every candidate pack root, in precedence order (first name wins).

    Explicit ``--pack-dir`` directories, then the per-user directory a taught
    layout is written to, then the shipped built-ins.
    """
    candidates: list[tuple[Path, str]] = []
    for parent in pack_dirs:
        candidates.extend(_packs_under(parent, ORIGIN_PACK_DIR))
    if include_user:
        candidates.extend(_packs_under(user_packs_dir(), ORIGIN_USER))
    if _BUILTIN_DIR.is_dir():
        candidates.extend(
            (child, ORIGIN_BUILTIN)
            for child in sorted(_BUILTIN_DIR.iterdir())
            if child.is_dir() and (child / "pack.yaml").is_file()
        )
    return candidates


def discover_packs(
    pack_dirs: list[Path] | None = None,
    *,
    allow_external: bool = False,
    trust: PackTrust | None = None,
    trust_new: bool = False,
    include_user: bool = True,
) -> dict[str, PackStatus]:
    """Discover every reachable pack, loading each defensively.

    External packs (``--pack-dir``) execute code at load time
    and are skipped with a diagnosis unless ``allow_external`` is set.

    Hash pinning is OPT-IN via ``trust``. When ``trust is None`` the behavior is
    unchanged (consent-only). When a :class:`~anastomosis.reconstruct.packtrust.PackTrust`
    is supplied, every external candidate that ``allow_external`` would otherwise
    load is gated on its content hash BEFORE its ``context.py`` is exec'd:

    * trusted at its current hash → load;
    * else if ``trust_new`` → record the hash, then load (trust-on-first-use);
    * else → returned unavailable with an untrusted diagnosis, never exec'd.

    Built-ins are never hash-checked (implicitly trusted). For ``--pack-dir``
    packs the ``allow_external`` refusal takes precedence — trust only matters
    once external packs are allowed.

    Per-user packs (:func:`user_packs_dir`) are discovered unconditionally —
    ``allow_external`` does not gate a layout the operator taught into their own
    home — but their ``context.py`` is executed only under the same hash gate:
    with no ``trust`` store they are returned unavailable, never exec'd.

    ``include_user=False`` leaves that directory out of the walk entirely. It
    exists for the one caller whose question is about the SHIPPED files (the
    install self-check): a user pack shadowing a built-in name is a legitimate
    thing for an operator to do, and it must not be able to make a healthy
    install report a missing asset.
    """
    results: dict[str, PackStatus] = {}
    for root, origin in _iter_candidate_dirs(pack_dirs or [], include_user=include_user):
        status = _discover_one(root, origin, allow_external, trust, trust_new)
        results.setdefault(status.name, status)  # first definition wins
    return results


def _discover_one(
    root: Path, origin: str, allow_external: bool, trust: PackTrust | None, trust_new: bool
) -> PackStatus:
    """Apply the consent + hash gates for one candidate, then load it.

    The three origins differ only here: a built-in loads outright, a
    ``--pack-dir`` pack needs the caller's ``allow_external`` consent first, and
    a per-user pack needs no such flag. Both non-built-in origins then need a
    trust store that maps the root to its current content hash before any of its
    code runs.
    """
    if origin == ORIGIN_BUILTIN:
        return _load_pack_dir(root, origin)
    if origin == ORIGIN_PACK_DIR and not allow_external:
        return PackStatus(
            name=root.name,
            pack=None,
            diagnosis="external pack not loaded (pass allow_external/--allow-external-packs)",
            origin=origin,
            root=root,
        )
    if trust is None:
        # Consent-only callers get the historic --pack-dir behavior; a per-user
        # pack has no consent-only path at all, because nothing but the hash
        # says the code is the code the operator confirmed.
        if origin == ORIGIN_PACK_DIR:
            return _load_pack_dir(root, origin)
        return PackStatus(
            name=root.name,
            pack=None,
            diagnosis=(
                "learned pack not loaded: this caller checks no trust store, so its code "
                "cannot be shown to be the code that was confirmed"
            ),
            origin=origin,
            root=root,
        )
    return _load_trusted_external(root, origin, trust, trust_new=trust_new)


def _load_trusted_external(
    root: Path, origin: str, trust: PackTrust, *, trust_new: bool
) -> PackStatus:
    """Gate one code-bearing candidate on its content hash, then load it if allowed.

    The pack is read ONCE into a :class:`PackSnapshot`; the hash is computed from
    the snapshot bytes and, when trusted, those SAME bytes are parsed/executed by
    :func:`_load_pack_snapshot`. So an untrusted pack's ``context.py`` is never
    run, and a trusted pack runs exactly the code that was hashed — there is no
    swap-between-hash-and-exec window. ``trust_new`` records the current hash
    (trust-on-first-use) and proceeds.
    """
    snapshot = read_pack_snapshot(root)
    content_hash = snapshot.content_hash
    if trust.is_trusted(root, content_hash):
        return _load_pack_snapshot(snapshot, origin)
    if trust_new:
        trust.record(root, content_hash)
        return _load_pack_snapshot(snapshot, origin)
    return PackStatus(
        name=root.name,
        pack=None,
        diagnosis=(
            "untrusted pack: its code is not trusted at its current hash; "
            "re-run with --trust-pack to trust it"
        ),
        origin=origin,
        root=root,
    )
