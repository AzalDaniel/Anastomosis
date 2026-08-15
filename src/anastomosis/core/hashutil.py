"""The one streaming file hasher every integrity check shares.

Three places prove a rendered chart's bytes: the upload manifest measures the
file (:mod:`anastomosis.deliver.browser.manifest`), the engine re-hashes it in
preflight before sending anything
(:mod:`anastomosis.deliver.browser.engine`), and verification level L0
re-hashes it again inside the ladder
(:mod:`anastomosis.deliver.verify.levels`). Those re-reads are deliberate — L0
exists to re-prove integrity independently, so the ladder is correct when run
without the engine — but the *hashing* must be one definition: a digest that
disagrees with the manifest because one site chunked differently would be an
integrity failure invented by the tooling.

Hashing is streamed in fixed chunks so an arbitrarily large PDF never has to be
resident all at once.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

__all__ = ["HASH_CHUNK_BYTES", "hash_and_size"]

# 1 MiB: large enough to amortize read syscalls, small enough that a huge PDF
# never has to be resident all at once.
HASH_CHUNK_BYTES = 1024 * 1024


def hash_and_size(path: Path) -> tuple[str, int]:
    """Stream ``path`` to a ``(sha256 hex digest, byte count)`` pair.

    Raises whatever opening/reading the file raises (``FileNotFoundError`` and
    friends are all :class:`OSError`): a caller that can tolerate an unreadable
    file catches it and decides what that means — a missing render is a defect
    the manifest must surface, while the engine's preflight and L0 turn it into
    a refusal to upload. Nothing here logs; the caller owns the PHI-safe
    message.
    """
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(HASH_CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size
