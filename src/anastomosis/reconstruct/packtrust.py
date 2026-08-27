"""Content-hash pinning + explicit trust for external template packs.

External packs (``--pack-dir``, entry points) execute arbitrary Python — their
``context.py`` is executed at load time. ``--pack-dir`` consent alone gates
*whether* external code runs, not *which* code: a trusted pack's ``context.py``
can be edited underneath the operator and run unnoticed.

This module adds trust-on-first-use over a content hash:

* :func:`read_pack_snapshot` captures a pack's executable + structural content in
  a *single read* — the ordered ``{relpath: bytes}`` the hash covers.
* :func:`pack_content_hash` is a stable SHA-256 over that snapshot (``context.py``
  then ``template.html`` then ``pack.yaml``). Hashing the snapshot bytes — the
  same bytes the loader executes — is what closes the swap-between-hash-and-exec
  window (:mod:`anastomosis.reconstruct.packs`).
* :class:`PackTrust` is a tiny JSON store mapping a pack's resolved absolute
  root to the hash the operator trusted. A pack is trusted only when the store
  maps its root to *exactly* its current hash — so changing any of the three
  files (in particular the code) un-trusts it until re-confirmed.

Enforcement is OPT-IN at :func:`~anastomosis.reconstruct.packs.discover_packs`
(``trust=`` / ``trust_new=``); the gate runs BEFORE the code is executed, and
the executed bytes come from the hashed snapshot, so untrusted code is never run.

PHI: this layer carries pack file paths (config, never patient data) and hex
digests only — nothing patient-derived flows through it. The trust store lives
beside the other ``~/.anastomosis`` state and is owner-only on POSIX.
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

    ``files`` maps each present-and-readable name in :data:`_HASHED_FILES` to its
    bytes; a missing or unreadable file is simply absent from the mapping (it
    contributes only its separator to the hash, exactly as before). The snapshot
    is what makes hashing and execution atomic with respect to disk: the loader
    executes ``files["context.py"]`` rather than re-reading the file, so a writer
    that swaps ``context.py`` after the snapshot cannot run un-hashed code.

    Boundary: the snapshot — and therefore the trust hash — pins the *code*
    (``context.py``), the *manifest* (``pack.yaml``), and ``template.html``.
    Auxiliary assets under the pack root (partials, images, fonts) are NOT
    pinned; a builder that reads them at render time reads whatever is on disk.
    """

    root: Path
    files: Mapping[str, bytes]

    @property
    def content_hash(self) -> str:
        """SHA-256 hex over this snapshot's bytes — see :func:`pack_content_hash`."""
        return _hash_snapshot_files(self.files)


def read_pack_snapshot(root: Path) -> PackSnapshot:
    """Read a pack's hashed files once into a :class:`PackSnapshot`.

    Each of :data:`_HASHED_FILES` is read exactly once, in order; a missing or
    unreadable file (``OSError``) is omitted (it contributes only its separator to
    the digest, mirroring the pre-snapshot behavior). This is the single point of
    I/O that both the trust hash and the loader consume, so the bytes that get
    hashed are provably the bytes that get executed.
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

    Each name emits ``b"\\0<name>\\0"`` then its bytes (nothing but the separator
    when the file is absent from the mapping). Kept byte-identical to the historic
    read-per-file digest: same order, same separators, same update sequence.
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
    """The per-user pack-trust store path.

    A plain ``~/.anastomosis/pack_trust.json`` (NOT ``platformdirs`` — no new
    dependency), matching
    :func:`anastomosis.destinations.loader.user_destinations_dir`'s convention so
    all Anastomosis user state lives under one root.
    """
    return Path.home() / ".anastomosis" / "pack_trust.json"


def _read_store(path: Path) -> dict[str, str]:
    """Load the trust store from ``path``, tolerating absence and garbage.

    A missing or unparseable store yields an empty mapping (loud failures belong
    to the discovery layer; an unreadable trust file simply trusts nothing). Only
    ``str -> str`` entries survive, so a hand-corrupted value can't masquerade as
    a hash.
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

    The store is ``{"<resolved-abs-pack-root>": "<sha256>"}``. A pack is trusted
    only when its current content hash equals the recorded one, so editing any
    hashed file (notably ``context.py``) un-trusts it until re-recorded.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._store: dict[str, str] = _read_store(path)

    def is_trusted(self, root: Path, content_hash: str) -> bool:
        """True iff the store maps ``root`` to *exactly* ``content_hash``."""
        return self._store.get(str(root.resolve())) == content_hash

    def record(self, root: Path, content_hash: str) -> None:
        """Trust ``root`` at ``content_hash`` and persist the store.

        Concurrency-safe against other recorders sharing the store: under an
        advisory lock on the store's directory, the on-disk store is *re-read*
        (tolerantly, as the ctor does), the one new entry is merged in, and the
        merged mapping is written to a same-dir temp file then ``os.replace``\\ d
        atomically. Re-reading is what prevents a lost update — a recorder whose
        ctor snapshot predates another's write still sees that write and keeps it.
        The atomic replace means a reader never observes a torn file. The parent
        directory is created owner-only (``0o600``) on POSIX, mirroring the
        ``~/.anastomosis`` state hygiene used elsewhere.
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
