"""The one file/byte digest implementation every integrity check shares (16)."""

from __future__ import annotations

import hashlib
from pathlib import Path

__all__ = ["HASH_CHUNK_BYTES", "hash_and_size"]

# 1 MiB: large enough to amortize read syscalls, small enough that a huge PDF
# never has to be resident all at once.
HASH_CHUNK_BYTES = 1024 * 1024


def hash_and_size(path: Path) -> tuple[str, int]:
    """Contract: streams ``path`` to a ``(sha256 hex digest, byte count)``
    pair. Raises whatever opening/reading raises (``OSError`` and its
    subclasses); the caller decides what that means and owns the PHI-safe
    message — nothing here logs."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(HASH_CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size
