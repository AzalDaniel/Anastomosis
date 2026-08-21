"""Defensive browser-pack discovery (mirrors :mod:`anastomosis.reconstruct.packs`).

A browser destination pack is a directory shipped in the wheel:

    destinations/<name>/
      __init__.py
      pack.yaml      — display name, config knobs, and selector slots

The selector slots ship at the ``DISCOVER`` placeholder (the no-hallucination
rule: Anastomosis never invents a vendor's DOM). An operator fills them with
``anast destination init <name>``, which writes a ``selectors.yaml`` into their
*user directory* — the built-in ``pack.yaml`` stays pristine, and the user file
OVERLAYS its selectors. So a pack is "ready" only once a discovered overlay
exists.

Discovery order (first hit wins), mirroring the template-pack loader:

1. explicit ``--pack-dir`` directories (a pack dir, or a parent containing them);
2. the user directory ``~/.anastomosis/destinations/<name>/`` (where the wizard
   writes ``selectors.yaml`` — NO new dependency: a plain ``Path.home()`` join,
   not ``platformdirs``, documented here);
3. the built-in scaffolds shipped under ``anastomosis/destinations/``.

Loading is defensive: a broken pack file is returned as
:class:`LoadedBrowserPack` with the selectors unresolved and a *diagnosis that
names the offending file*, never a crash that hides which file. An undiscovered
(but otherwise valid) pack loads fine; its :attr:`LoadedBrowserPack.ready` is
``False`` and resolving its selectors raises the actionable
:class:`~anastomosis.destinations.browserpack.PackNotReadyError`.

PHI rule: this layer carries pack names, file paths to PACK config (never PHI),
and selector strings (the vendor's DOM — not patient data). Nothing
patient-derived flows through it.
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

    Deliberately a plain ``~/.anastomosis/destinations`` (NOT ``platformdirs`` —
    no new dependency). Documented so the wizard and the loader agree on one
    location: the wizard writes ``<here>/<name>/selectors.yaml`` and the loader
    reads it back as the selector overlay.
    """
    return Path.home() / ".anastomosis" / "destinations"


@dataclass(frozen=True)
class LoadedBrowserPack:
    """One discovered browser pack: its config, its (maybe-undiscovered) selectors.

    ``selectors`` is ``None`` when the pack's slots are still undiscovered (the
    shipped scaffold before the wizard ran) — :attr:`ready` is ``False`` and
    :meth:`require_selectors` raises the actionable
    :class:`~anastomosis.destinations.browserpack.PackNotReadyError`.

    ``source`` is the directory the manifest was read from; ``selectors_source``
    is where the resolved selectors came from (the overlay file, or the built-in
    ``pack.yaml`` itself); ``builtin`` flags a pack that shipped in the wheel.
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

        Raises the :class:`~anastomosis.destinations.browserpack.PackNotReadyError`
        captured at load time — it names the undiscovered slots and the wizard
        command — rather than a generic "selectors is None".
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

    Precedence ``--pack-dir`` > user dir > built-in. A ``--pack-dir`` may BE the
    pack directory (its name matches) or CONTAIN a ``<name>/`` child — both are
    honored, mirroring the template loader.
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

    The wizard writes discovered selectors into the USER directory's
    ``selectors.yaml`` so the built-in ``pack.yaml`` stays pristine. The overlay
    rule is precedence-aware so an explicit ``--pack-dir`` is authoritative:

    * a BUILT-IN or USER-dir pack is overlaid by the user-dir ``selectors.yaml``
      (the wizard's output for that pack name);
    * a ``--pack-dir`` pack uses ONLY a ``selectors.yaml`` sitting beside it —
      the operator who pointed at that directory meant it, so the user-dir
      wizard overlay must not silently override their explicit choice.

    Returns ``(selectors, None, source)`` when ready, or ``(None, error, None)``
    when slots remain undiscovered (the error is the actionable
    :class:`PackNotReadyError`).
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

    Discovery order: ``--pack-dir`` directories, then the user directory, then
    the built-in scaffold. The first directory with a ``pack.yaml`` wins; its
    selectors are overlaid by a user ``selectors.yaml`` (the wizard's output).

    Raises :class:`BrowserPackError` (naming the file) when NO pack of that name
    is found or a pack file is malformed. An undiscovered-but-valid pack loads
    successfully with :attr:`LoadedBrowserPack.ready` ``False``.
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
