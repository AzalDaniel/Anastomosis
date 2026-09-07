"""Content-hash pinning + explicit trust for external template packs (RULES.md 22).

:func:`read_pack_snapshot` captures ``context.py``/``template.html``/``pack.yaml``
in one read; :func:`pack_content_hash` is a stable SHA-256 over those same
bytes, closing the swap-between-hash-and-exec window
(:mod:`anastomosis.reconstruct.packs`). :class:`PackTrust` maps a pack's
resolved root to the hash last trusted; changing any hashed file un-trusts it.

PHI: carries pack file paths and hex digests only, never patient data.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from anastomosis.core.atomic import atomic_write_text
from anastomosis.core.locking import output_lock

__all__ = [
    "PackSnapshot",
    "PackTrust",
    "default_pack_trust",
    "pack_content_hash",
    "read_pack_snapshot",
    "user_pack_trust_path",
]

# The pack files that contribute to the content hash, in a fixed order. Each is
# prefixed by an unambiguous separator so concatenation can't be confused (a byte
# moving across a file boundary changes the digest).
_HASHED_FILES: tuple[str, ...] = ("context.py", "template.html", "pack.yaml")


@dataclass(frozen=True)
class PackSnapshot:
    """A single-read capture of the pack files the content hash covers.

    ``files`` maps a present-and-readable :data:`_HASHED_FILES` name to its
    bytes; the loader execs ``files["context.py"]`` rather than re-reading,
    so a writer cannot swap it after the snapshot. Auxiliary assets
    (partials, images) are NOT pinned.
    """

    root: Path
    files: Mapping[str, bytes]

    @property
    def content_hash(self) -> str:
        """SHA-256 hex over this snapshot's bytes — see :func:`pack_content_hash`."""
        return _hash_snapshot_files(self.files)


def read_pack_snapshot(root: Path) -> PackSnapshot:
    """Read a pack's hashed files once into a :class:`PackSnapshot`.

    Each of :data:`_HASHED_FILES` is read once, in order; a missing or
    unreadable file is omitted (contributes only its separator to the
    digest). The single I/O point both the trust hash and loader consume.
    """
    files: dict[str, bytes] = {}
    for name in _HASHED_FILES:
        try:
            files[name] = (root / name).read_bytes()
        except OSError:
            # Missing/unreadable file contributes nothing beyond its separator.
            continue
    return PackSnapshot(root=root, files=files)


def _hash_snapshot_files(files: Mapping[str, bytes]) -> str:
    """SHA-256 hex over ``files`` in the fixed :data:`_HASHED_FILES` order.

    Each name emits ``b"\\0<name>\\0"`` then its bytes (nothing but the
    separator when the file is absent from the mapping).
    """
    digest = hashlib.sha256()
    for name in _HASHED_FILES:
        digest.update(b"\0" + name.encode("utf-8") + b"\0")
        data = files.get(name)
        if data is not None:
            digest.update(data)
    return digest.hexdigest()


def pack_content_hash(root: Path) -> str:
    """SHA-256 hex over a pack's ``context.py`` + ``template.html`` + ``pack.yaml``.

    Thin wrapper over :func:`read_pack_snapshot` — see its docstring and
    :func:`_hash_snapshot_files` for the exact byte layout.
    """
    return read_pack_snapshot(root).content_hash


def user_pack_trust_path() -> Path:
    """``~/.anastomosis/pack_trust.json`` — matches
    :func:`anastomosis.destinations.loader.user_destinations_dir`'s convention
    so all Anastomosis user state lives under one root.
    """
    return Path.home() / ".anastomosis" / "pack_trust.json"


def _read_store(path: Path) -> dict[str, str]:
    """Load the trust store from ``path``, tolerating absence and garbage.

    A missing or unparseable store yields an empty mapping — an unreadable
    trust file simply trusts nothing. Only ``str -> str`` entries survive.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if isinstance(data, dict):
        return {str(key): value for key, value in data.items() if isinstance(value, str)}
    return {}


class PackTrust:
    """A JSON store mapping a pack's resolved absolute root to its trusted hash.

    ``{"<resolved-abs-pack-root>": "<sha256>"}``; editing any hashed file
    (notably ``context.py``) un-trusts the pack until re-recorded.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._store: dict[str, str] = _read_store(path)

    def is_trusted(self, root: Path, content_hash: str) -> bool:
        """True iff the store maps ``root`` to *exactly* ``content_hash``."""
        return self._store.get(str(root.resolve())) == content_hash

    def record(self, root: Path, content_hash: str) -> None:
        """Trust ``root`` at ``content_hash`` and persist the store.

        Contract: concurrency-safe — under an advisory lock the on-disk
        store is re-read, the entry merged in, then written atomically via a
        same-dir temp file and ``os.replace``, so a lost update or a torn
        read never happens. The parent directory is owner-only on POSIX.
        """
        key = str(root.resolve())
        with output_lock(self._path.parent):
            merged = _read_store(self._path)
            merged[key] = content_hash
            payload = json.dumps(merged, indent=2, sort_keys=True) + "\n"
            atomic_write_text(self._path, payload, mode=0o600)
            if os.name == "posix":
                self._path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0o600 — owner only
            self._store = merged


def default_pack_trust() -> PackTrust:
    """The :class:`PackTrust` backed by :func:`user_pack_trust_path`."""
    return PackTrust(user_pack_trust_path())
