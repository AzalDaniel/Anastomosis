"""The one file/byte digest implementation every integrity check shares (16)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Final, TypeVar, overload

__all__ = ["HASH_CHUNK_BYTES", "file_sha256", "hash_and_size", "sha256_hex"]

# 1 MiB: large enough to amortize read syscalls, small enough that a huge PDF
# never has to be resident all at once.
HASH_CHUNK_BYTES = 1024 * 1024

_T = TypeVar("_T")


class _Raise:
    """The type of :data:`_RAISE`, so ``unreadable=None`` is still a value."""


#: What ``unreadable=`` means when the caller names nothing: let the OSError out.
_RAISE: Final = _Raise()


def sha256_hex(*parts: bytes) -> str:
    """Contract: sha256 hex over ``parts`` concatenated in the order given.
    A caller wanting domain separation passes its prefix as the first part."""
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part)
    return digest.hexdigest()


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


@overload
def file_sha256(path: Path) -> str: ...


@overload
def file_sha256(path: Path, *, unreadable: _T) -> str | _T: ...


def file_sha256(path: Path, *, unreadable: Any = _RAISE) -> Any:
    """Contract: :func:`hash_and_size`'s digest alone, for a caller with no
    use for the size. ``unreadable=`` names the value an ``OSError`` answers
    with; named nothing, the ``OSError`` is re-raised."""
    try:
        return hash_and_size(path)[0]
    except OSError:
        if isinstance(unreadable, _Raise):
            raise
        return unreadable
