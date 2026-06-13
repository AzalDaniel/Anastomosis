"""Content-hash pinning + explicit trust for external template packs.

External packs (``--pack-dir``, entry points) execute arbitrary Python — their
``context.py`` is ``exec_module``'d at load time. ``--pack-dir`` consent alone
gates *whether* external code runs, not *which* code: a trusted pack's
``context.py`` can be edited underneath the operator and run unnoticed.

This module adds trust-on-first-use over a content hash:

* :func:`pack_content_hash` is a stable SHA-256 over a pack's executable +
  structural content (``context.py`` then ``template.html`` then ``pack.yaml``).
* :class:`PackTrust` is a tiny JSON store mapping a pack's resolved absolute
  root to the hash the operator trusted. A pack is trusted only when the store
  maps its root to *exactly* its current hash — so changing any of the three
  files (in particular the code) un-trusts it until re-confirmed.

Enforcement is OPT-IN at :func:`~anastomosis.reconstruct.packs.discover_packs`
(``trust=`` / ``trust_new=``); the gate runs BEFORE ``exec_module`` so untrusted
code is never executed.

PHI: this layer carries pack file paths (config, never patient data) and hex
digests only — nothing patient-derived flows through it. The trust store lives
beside the other ``~/.anastomosis`` state and is owner-only on POSIX.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

__all__ = [
    "PackTrust",
    "default_pack_trust",
    "pack_content_hash",
    "user_pack_trust_path",
]

# The pack files that contribute to the content hash, in a fixed order. Each is
# prefixed by an unambiguous separator so concatenation can't be confused (a byte
# moving across a file boundary changes the digest).
_HASHED_FILES: tuple[str, ...] = ("context.py", "template.html", "pack.yaml")


def pack_content_hash(root: Path) -> str:
    """SHA-256 hex over a pack's executable + structural content.

    Hashes ``context.py``, ``template.html``, ``pack.yaml`` in that fixed order,
    each prefixed by ``b"\\0<name>\\0"`` so the concatenation is unambiguous. A
    missing file contributes only its separator (a broken pack fails to load
    anyway, defensively, in :mod:`anastomosis.reconstruct.packs`). Pure: the only
    I/O is reading those three files.
    """
    digest = hashlib.sha256()
    for name in _HASHED_FILES:
        digest.update(b"\0" + name.encode("utf-8") + b"\0")
        path = root / name
        try:
            digest.update(path.read_bytes())
        except OSError:
            # Missing/unreadable file contributes nothing beyond its separator.
            continue
    return digest.hexdigest()


def user_pack_trust_path() -> Path:
    """The per-user pack-trust store path.

    A plain ``~/.anastomosis/pack_trust.json`` (NOT ``platformdirs`` — no new
    dependency), matching
    :func:`anastomosis.destinations.loader.user_destinations_dir`'s convention so
    all Anastomosis user state lives under one root.
    """
    return Path.home() / ".anastomosis" / "pack_trust.json"


class PackTrust:
    """A JSON store mapping a pack's resolved absolute root to its trusted hash.

    The store is ``{"<resolved-abs-pack-root>": "<sha256>"}``. A pack is trusted
    only when its current content hash equals the recorded one, so editing any
    hashed file (notably ``context.py``) un-trusts it until re-recorded.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._store: dict[str, str] = {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Missing or garbage store → start empty (loud failures belong to the
            # discovery layer; an unreadable trust file simply trusts nothing).
            return
        if isinstance(data, dict):
            self._store = {str(key): value for key, value in data.items() if isinstance(value, str)}

    def is_trusted(self, root: Path, content_hash: str) -> bool:
        """True iff the store maps ``root`` to *exactly* ``content_hash``."""
        return self._store.get(str(root.resolve())) == content_hash

    def record(self, root: Path, content_hash: str) -> None:
        """Trust ``root`` at ``content_hash`` and persist the store.

        Creates the parent directory and writes owner-only (``0o600``) on POSIX,
        mirroring the ``~/.anastomosis`` state hygiene used elsewhere.
        """
        self._store[str(root.resolve())] = content_hash
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._store, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if os.name == "posix":
            self._path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0o600 — owner only


def default_pack_trust() -> PackTrust:
    """The :class:`PackTrust` backed by :func:`user_pack_trust_path`."""
    return PackTrust(user_pack_trust_path())
