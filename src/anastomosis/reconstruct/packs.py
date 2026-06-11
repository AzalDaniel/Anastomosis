"""Template-pack contract and defensive discovery.

A template pack is a directory:

    my_pack/
      pack.yaml      — manifest (this module's schema)
      template.html  — Jinja2 page template
      context.py     — build_context(encounter, record, cfg) -> dict
      partials/…     — optional includes, assets

Discovery order (first definition of a name wins, so a user can shadow a
built-in): explicit ``--pack-dir`` directories → ``anastomosis.packs``
entry points → built-ins shipped under ``anastomosis/packs/``.

Loading is **defensive** — the brain-like modularity invariant. A pack
with a broken manifest, missing template, or crashing ``context.py`` is
returned as unavailable *with a diagnosis*; it never raises out of
discovery and never takes the other packs down. A vendor template rotting
is a one-pack event.

Trust model (security backlog): packs from ``--pack-dir`` and entry points
execute Python (``context.py``), so external packs load only when the
caller passes ``allow_external=True`` (the CLI flag is explicit consent);
built-ins are implicitly trusted. Hash pinning lands in M2.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

__all__ = ["LoadedPack", "PackManifest", "PackStatus", "SectionFlag", "discover_packs"]

_BUILTIN_DIR = Path(__file__).resolve().parent.parent / "packs"

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
    # What to do when two documents resolve to the same name ("guid_suffix"
    # appends a short unique suffix — the same-day-visit defense).
    collision: str = "guid_suffix"


class PackManifest(BaseModel):
    """Schema of ``pack.yaml``."""

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
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
    origin: str = "builtin"  # "pack-dir" | "entry-point" | "builtin"

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
    return builder


def _load_pack_dir(root: Path, origin: str) -> PackStatus:
    name = root.name
    try:
        manifest_path = root / "pack.yaml"
        if not manifest_path.is_file():
            raise FileNotFoundError("pack.yaml not found")
        manifest = PackManifest.model_validate(
            yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        )
        name = manifest.name
        template_path = root / "template.html"
        if not template_path.is_file():
            raise FileNotFoundError("template.html not found")
        context_path = root / "context.py"
        if not context_path.is_file():
            raise FileNotFoundError("context.py not found")
        builder = _load_context_builder(context_path)
    except (ValidationError, OSError, ImportError, AttributeError, yaml.YAMLError) as exc:
        # Diagnosis carries the exception type and pack-relative detail only —
        # safe to log, enough to start the re-discovery wizard.
        return PackStatus(
            name=name, pack=None, diagnosis=f"{type(exc).__name__}: {exc}", origin=origin
        )
    except Exception as exc:  # context.py crashed at import: arbitrary errors
        return PackStatus(
            name=name,
            pack=None,
            diagnosis=f"context.py failed at import ({type(exc).__name__})",
            origin=origin,
        )
    return PackStatus(
        name=name,
        pack=LoadedPack(
            manifest=manifest, root=root, template_path=template_path, build_context=builder
        ),
        origin=origin,
    )


def _iter_candidate_dirs(pack_dirs: list[Path]) -> list[tuple[Path, str]]:
    candidates: list[tuple[Path, str]] = []
    for parent in pack_dirs:
        if not parent.is_dir():
            continue
        # A --pack-dir may BE a pack or CONTAIN packs.
        if (parent / "pack.yaml").is_file():
            candidates.append((parent, "pack-dir"))
        else:
            candidates.extend(
                (child, "pack-dir")
                for child in sorted(parent.iterdir())
                if child.is_dir() and (child / "pack.yaml").is_file()
            )
    for ep in entry_points(group="anastomosis.packs"):
        try:
            located = Path(str(ep.load()))
        except Exception as exc:  # a broken third-party plugin must not stop discovery
            logging.getLogger(__name__).warning(
                "skipping pack entry point %s (%s)", ep.name, type(exc).__name__
            )
            continue
        candidates.append((located, "entry-point"))
    if _BUILTIN_DIR.is_dir():
        candidates.extend(
            (child, "builtin")
            for child in sorted(_BUILTIN_DIR.iterdir())
            if child.is_dir() and (child / "pack.yaml").is_file()
        )
    return candidates


def discover_packs(
    pack_dirs: list[Path] | None = None, *, allow_external: bool = False
) -> dict[str, PackStatus]:
    """Discover every reachable pack, loading each defensively.

    External packs (``--pack-dir``, entry points) execute code at load time
    and are skipped with a diagnosis unless ``allow_external`` is set.
    """
    results: dict[str, PackStatus] = {}
    for root, origin in _iter_candidate_dirs(pack_dirs or []):
        if origin != "builtin" and not allow_external:
            status = PackStatus(
                name=root.name,
                pack=None,
                diagnosis="external pack not loaded (pass allow_external/--allow-external-packs)",
                origin=origin,
            )
        else:
            status = _load_pack_dir(root, origin)
        results.setdefault(status.name, status)  # first definition wins
    return results
