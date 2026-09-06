"""Defensive browser-pack discovery (mirrors :mod:`anastomosis.reconstruct.packs`, RULES.md 21).

Selector slots ship at ``DISCOVER``; ``anast destination init <name>``
writes a ``selectors.yaml`` overlay into the user directory, leaving the
built-in ``pack.yaml`` pristine. A pack is "ready" only once that overlay
exists. Discovery order: ``--pack-dir`` → user dir → built-in scaffold.

Loading is defensive: a broken file returns a diagnosis naming it, never
a crash. PHI: pack names, config paths, and selector strings (vendor DOM).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from anastomosis.destinations.browserpack import (
    BrowserPackConfig,
    PackNotReadyError,
    SelectorMap,
)

__all__ = [
    "BrowserPackError",
    "LoadedBrowserPack",
    "load_destination_pack",
    "user_destinations_dir",
]

# Built-in scaffolds ship alongside this module (destinations/<name>/pack.yaml).
_BUILTIN_DIR = Path(__file__).resolve().parent

# The pack manifest and the wizard's selector overlay file names.
_PACK_FILE = "pack.yaml"
_SELECTORS_FILE = "selectors.yaml"


class BrowserPackError(Exception):
    """A destination pack could not be loaded — message names the file at fault."""


def user_destinations_dir() -> Path:
    """The per-user directory the discovery wizard writes packs into.

    ``~/.anastomosis/destinations`` (NOT ``platformdirs``); the wizard
    writes ``<here>/<name>/selectors.yaml`` and the loader reads it back.
    """
    return Path.home() / ".anastomosis" / "destinations"


@dataclass(frozen=True)
class LoadedBrowserPack:
    """One discovered browser pack: its config, its (maybe-undiscovered) selectors.

    ``selectors`` is ``None`` when slots are still undiscovered — ``ready``
    is ``False`` and :meth:`require_selectors` raises
    :class:`~anastomosis.destinations.browserpack.PackNotReadyError`.
    """

    name: str
    config: BrowserPackConfig
    selectors: SelectorMap | None
    not_ready: PackNotReadyError | None
    source: Path
    selectors_source: Path | None
    builtin: bool

    @property
    def ready(self) -> bool:
        """Whether every required selector slot is discovered (the pack can run)."""
        return self.selectors is not None

    def require_selectors(self) -> SelectorMap:
        """Return the selectors, or raise the actionable not-ready error.

        Raises the :class:`PackNotReadyError` captured at load time — named
        slots and the wizard command — rather than a generic ``None`` error.
        """
        if self.selectors is None:
            assert self.not_ready is not None  # ready==False implies a captured error
            raise self.not_ready
        return self.selectors


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    """Read a YAML file as a mapping, raising :class:`BrowserPackError` on trouble.

    The error names the file (path is pack config, never PHI) so a broken pack is
    diagnosed rather than crashing opaquely.
    """
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise BrowserPackError(
            f"cannot read pack file {path}: {type(exc).__name__}: {exc}"
        ) from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise BrowserPackError(
            f"pack file {path} must be a YAML mapping, got {type(data).__name__}"
        )
    return data


def _build_config(name: str, raw_config: Any, source: Path) -> BrowserPackConfig:
    if raw_config is None:
        raw_config = {}
    if not isinstance(raw_config, dict):
        raise BrowserPackError(f"pack {name!r} `config:` in {source} must be a mapping")
    # The pack name is authoritative; the config inherits it (so logs and the
    # registry overlay snippet agree on one identifier).
    try:
        return BrowserPackConfig(name=name, **raw_config)
    except TypeError as exc:
        # An unknown/mistyped config key: name the file, never crash opaquely.
        raise BrowserPackError(f"pack {name!r} `config:` in {source}: {exc}") from exc


# Origins, in the precedence order the loader walks them. A pack is taken from
# the FIRST origin that carries a pack.yaml.
_ORIGIN_PACK_DIR = "pack-dir"
_ORIGIN_USER = "user"
_ORIGIN_BUILTIN = "builtin"


def _candidate_dirs(name: str, pack_dirs: list[Path]) -> list[tuple[Path, str]]:
    """The directories to look in, in precedence order, as (dir, origin).

    ``--pack-dir`` > user dir > built-in; a ``--pack-dir`` may BE the pack
    directory or CONTAIN a ``<name>/`` child.
    """
    candidates: list[tuple[Path, str]] = []
    for parent in pack_dirs:
        if (parent / _PACK_FILE).is_file() and parent.name == name:
            candidates.append((parent, _ORIGIN_PACK_DIR))
        child = parent / name
        if (child / _PACK_FILE).is_file():
            candidates.append((child, _ORIGIN_PACK_DIR))
    candidates.append((user_destinations_dir() / name, _ORIGIN_USER))
    candidates.append((_BUILTIN_DIR / name, _ORIGIN_BUILTIN))
    return candidates


def _resolve_selectors(
    name: str,
    pack_selectors: dict[str, Any],
    pack_dir: Path,
    origin: str,
) -> tuple[SelectorMap | None, PackNotReadyError | None, Path | None]:
    """Resolve the selector map, overlaying a discovered ``selectors.yaml`` if present.

    Contract: a BUILT-IN or USER-dir pack is overlaid by the user-dir
    ``selectors.yaml`` (the wizard's output); a ``--pack-dir`` pack uses
    only a ``selectors.yaml`` beside it, so the wizard's overlay never
    silently overrides an explicit operator choice. Returns
    ``(selectors, None, source)`` when ready, or ``(None, error, None)``.
    """
    merged: dict[str, Any] = dict(pack_selectors)
    selectors_source: Path = pack_dir

    if origin == _ORIGIN_PACK_DIR:
        overlays = [pack_dir / _SELECTORS_FILE]
    else:
        overlays = [user_destinations_dir() / name / _SELECTORS_FILE]
    for overlay in overlays:
        if overlay.is_file():
            overlay_data = _read_yaml_mapping(overlay)
            overlay_selectors = overlay_data.get("selectors", overlay_data)
            if not isinstance(overlay_selectors, dict):
                raise BrowserPackError(
                    f"selectors overlay {overlay} must be a mapping (or a `selectors:` mapping)"
                )
            merged.update(overlay_selectors)
            selectors_source = overlay

    try:
        selectors = SelectorMap.from_yaml_dict(merged, pack_name=name)
    except PackNotReadyError as exc:
        return None, exc, None
    return selectors, None, selectors_source


def load_destination_pack(name: str, pack_dirs: list[Path] | None = None) -> LoadedBrowserPack:
    """Load one browser destination pack by name, defensively.

    Discovery order: ``--pack-dir`` → user directory → built-in scaffold;
    the first ``pack.yaml`` wins, overlaid by a user ``selectors.yaml``.
    Raises :class:`BrowserPackError` (naming the file) when none is found
    or malformed. An undiscovered-but-valid pack loads with ``ready=False``.
    """
    for pack_dir, origin in _candidate_dirs(name, list(pack_dirs or [])):
        manifest_path = pack_dir / _PACK_FILE
        if not manifest_path.is_file():
            continue
        data = _read_yaml_mapping(manifest_path)
        pack_name = data.get("name", name)
        if not isinstance(pack_name, str) or not pack_name:
            raise BrowserPackError(f"pack file {manifest_path} has a missing/invalid `name`")
        config = _build_config(pack_name, data.get("config"), manifest_path)
        raw_selectors = data.get("selectors")
        if raw_selectors is None:
            raw_selectors = {}
        if not isinstance(raw_selectors, dict):
            raise BrowserPackError(
                f"pack {pack_name!r} `selectors:` in {manifest_path} must be a mapping"
            )
        selectors, not_ready, selectors_source = _resolve_selectors(
            pack_name, raw_selectors, pack_dir, origin
        )
        return LoadedBrowserPack(
            name=pack_name,
            config=config,
            selectors=selectors,
            not_ready=not_ready,
            source=manifest_path,
            selectors_source=selectors_source,
            builtin=origin == _ORIGIN_BUILTIN,
        )
    raise BrowserPackError(
        f"no destination pack {name!r} found (looked in --pack-dir, "
        f"{user_destinations_dir()}, and built-ins under {_BUILTIN_DIR})"
    )
