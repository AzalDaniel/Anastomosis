"""Where a pack may live and which origin it came from — rule 21's one order."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

__all__ = ["ORIGIN_BUILTIN", "ORIGIN_PACK_DIR", "ORIGIN_USER", "candidate_pack_dirs"]

ORIGIN_PACK_DIR = "pack-dir"  # named, not typed: call sites branch on these
ORIGIN_USER = "user"
ORIGIN_BUILTIN = "builtin"


def _children(parent: Path, name: str | None) -> list[Path]:
    if name is not None:
        return [parent / name]
    return sorted(parent.iterdir()) if parent.is_dir() else []


def _under(parent: Path, origin: str, manifest: str, name: str | None) -> list[tuple[Path, str]]:
    if (parent / manifest).is_file() and name in (None, parent.name):
        return [(parent, origin)]
    return [(c, origin) for c in _children(parent, name) if (c / manifest).is_file()]


def candidate_pack_dirs(
    pack_dirs: Sequence[Path],
    *,
    user_dir: Path | None,
    builtin_dir: Path,
    manifest: str,
    name: str | None = None,
) -> list[tuple[Path, str]]:
    """Every candidate root as ``(dir, origin)``, first definition first: a
    parent may BE a pack or CONTAIN packs; ``user_dir=None`` skips the
    per-user directory; ``name`` narrows the walk to that one pack."""
    found = [c for parent in pack_dirs for c in _under(parent, ORIGIN_PACK_DIR, manifest, name)]
    if user_dir is not None:
        found.extend(_under(user_dir, ORIGIN_USER, manifest, name))
    return found + _under(builtin_dir, ORIGIN_BUILTIN, manifest, name)
