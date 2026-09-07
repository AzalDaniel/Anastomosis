"""Template-pack contract and defensive discovery (RULES.md 21-22).

A pack is a directory: ``pack.yaml`` (manifest), ``template.html`` (Jinja2),
``context.py`` (``build_context(encounter, record, cfg) -> dict``), optional
``partials/``. A broken pack returns unavailable with a diagnosis; it never
raises out of discovery or takes another pack down.

What admitted code is then HANDED (restricted globals, import allowlist) is
:mod:`anastomosis.reconstruct.packexec`'s decision, not this module's.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from collections.abc import Callable
from dataclasses import dataclass, replace
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
from anastomosis.reconstruct.packexec import restrict_module
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
    "builtin_pack_names",
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


def builtin_pack_names() -> frozenset[str]:
    """The shipped layouts' names, read off the package directory without
    loading anything — the set a Teach may not claim (see ``run_pack_init``)."""
    if not _BUILTIN_DIR.is_dir():
        return frozenset()
    return frozenset(child.name for child in _BUILTIN_DIR.iterdir() if child.is_dir())


def user_packs_dir() -> Path:
    """``~/.anastomosis/packs`` — matches
    :func:`anastomosis.reconstruct.packtrust.user_pack_trust_path` and sibling
    user-state paths, so a layout taught in one session stays selectable in
    the next, wherever the app launches from.
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
    """Contract: what this layout carries from the record, what it omits, why.

    ``carries`` names kinds whose absence from a page is a QA failure;
    ``omits`` maps each remaining kind to a reason, reported as
    counted-but-expected rather than a defect. A kind in neither is
    undeclared (guarded in ``tests/unit/test_packs.py``), and an undeclared
    pack is graded conservatively: every kind verified, absence warns.
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
    #: What a person should read instead of ``name``; empty derives one from
    #: the id. Optional so pre-existing pack.yaml stays valid under
    #: ``extra="forbid"``.
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
    #: How many places this layout stamps the RENDER DAY on purpose — a COUNT,
    #: not a bool, so a pack that declares one still warns if MORE render-day
    #: dates appear than it admits to. The PF replica stamps it once,
    #: deliberately, in the medication list's "as of" heading.
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


def _load_context_builder(path: Path, *, restricted: bool) -> ContextBuilder:
    """Import ``context.py`` off disk and return its ``build_context``.

    ``restricted`` installs :func:`anastomosis.reconstruct.packexec.restrict_module`
    before the body runs, for every origin but built-ins.
    """
    # Unique module name: two packs both shipping context.py must not collide
    # in sys.modules.
    module_name = f"anastomosis._pack_context_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    if restricted:
        restrict_module(module.__dict__)
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


def _load_context_builder_from_source(
    source: bytes, path: Path, *, restricted: bool
) -> ContextBuilder:
    """Compile and exec pinned ``context.py`` bytes into a fresh module.

    Contract: ``source`` is the exact snapshot bytes the trust hash covered —
    no writer can swap the file between the check and ``exec`` (TOCTOU).
    ``__file__`` is set to the real on-disk ``path`` for tracebacks; only the
    CODE is pinned, never a file beside it (an external layout embeds assets
    in ``pack.yaml`` instead). Mirrors :func:`_load_context_builder`: a unique
    ``sys.modules`` name, popped on failure.
    """
    module_name = f"anastomosis._pack_context_{uuid4().hex}"
    code = compile(source, str(path), "exec")
    module = types.ModuleType(module_name)
    module.__file__ = str(path)
    if restricted:
        restrict_module(module.__dict__)
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

    Shared tail for :func:`_load_pack_dir` and :func:`_load_pack_snapshot`.
    ``name_cell`` is a one-item mutable cell so ``build`` can update the
    reported name once the manifest parses, including on a later failure.
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
        return (
            manifest,
            template_path,
            _load_context_builder(context_path, restricted=origin != ORIGIN_BUILTIN),
        )

    return _finish_load(name_cell, origin, root, build)


def _load_pack_snapshot(snapshot: PackSnapshot, origin: str) -> PackStatus:
    """Load a pack from its hashed :class:`PackSnapshot` — the trusted-external path.

    Contract: parses and execs from the snapshot's PINNED bytes, never
    re-read, so what runs is exactly what the trust hash covered.
    ``context.py`` is pinned to execution; ``template.html``'s presence is
    checked here but read from disk at render time; other assets are outside
    the hash. Diagnoses defensively like :func:`_load_pack_dir`.
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
        # Every caller of this path is a non-built-in origin, so pack code is
        # always restricted here; the flag is passed rather than assumed so the
        # two loaders read the same way.
        builder = _load_context_builder_from_source(
            context_bytes, root / "context.py", restricted=origin != ORIGIN_BUILTIN
        )
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
    """Discover every reachable pack, loading each defensively (RULES.md 21-22).

    Hash pinning is opt-in via ``trust``: ``None`` keeps consent-only behavior;
    given a :class:`~anastomosis.reconstruct.packtrust.PackTrust`, an external
    or per-user candidate is gated on its content hash before ``context.py``
    execs — trusted loads, ``trust_new`` records-then-loads, else unavailable.
    Built-ins are never hash-checked. ``include_user=False`` skips the
    per-user directory, for the install self-check only.
    """
    results: dict[str, PackStatus] = {}
    for root, origin in _iter_candidate_dirs(pack_dirs or [], include_user=include_user):
        status = _discover_one(root, origin, allow_external, trust, trust_new)
        seen = results.get(status.name)
        if seen is None:
            results[status.name] = status  # first definition wins
        elif origin == ORIGIN_BUILTIN and seen.pack is None and status.pack is not None:
            # Refusal stands (falling back to the built-in would defeat the
            # operator's own layout), but the diagnosis must name what it is
            # standing in front of, or "untrusted pack" hides that a built-in
            # of the same name was shadowed.
            results[status.name] = replace(
                seen,
                diagnosis=(
                    f"{seen.diagnosis}; this layout is standing in front of the "
                    f"built-in of the same name"
                ),
            )
    return results


def _discover_one(
    root: Path, origin: str, allow_external: bool, trust: PackTrust | None, trust_new: bool
) -> PackStatus:
    """Apply the consent + hash gates for one candidate, then load it.

    A built-in loads outright; a ``--pack-dir`` pack needs ``allow_external``;
    a per-user pack needs neither flag but still needs a trust store mapping
    its root to its current content hash.
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
        # A per-user pack has no consent-only path: only the hash proves the
        # code is what the operator confirmed.
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
    # trust_new is --trust-pack's consent for --pack-dir directories only; a
    # per-user layout is trusted by exactly one act (confirming a Teach), so
    # an edited one is never silently re-trusted because a vendor pack needed it.
    return _load_trusted_external(
        root, origin, trust, trust_new=trust_new and origin == ORIGIN_PACK_DIR
    )


def _load_trusted_external(
    root: Path, origin: str, trust: PackTrust, *, trust_new: bool
) -> PackStatus:
    """Gate one code-bearing candidate on its content hash, then load it if allowed.

    Contract: the pack is read ONCE into a :class:`PackSnapshot`; a trusted
    hash execs those SAME bytes via :func:`_load_pack_snapshot` — no
    swap-between-hash-and-exec window. ``trust_new`` records the hash
    (trust-on-first-use) and proceeds.
    """
    snapshot = read_pack_snapshot(root)
    content_hash = snapshot.content_hash
    if trust.is_trusted(root, content_hash):
        return _load_pack_snapshot(snapshot, origin)
    if trust_new:
        trust.record(root, content_hash)
        return _load_pack_snapshot(snapshot, origin)
    remedy = (
        "re-run with --trust-pack to trust it"
        if origin == ORIGIN_PACK_DIR
        else "re-confirm the Teach to trust it at its current code"
    )
    return PackStatus(
        name=root.name,
        pack=None,
        diagnosis=f"untrusted pack: its code is not trusted at its current hash; {remedy}",
        origin=origin,
        root=root,
    )
