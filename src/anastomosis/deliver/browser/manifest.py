"""Build the upload manifest from rendered documents, and the operator skiplist.

The manifest is the bridge from reconstruction (:class:`RenderedDoc`) to the
upload engine (:class:`UploadItem`): for each rendered chart it computes the
content hash and size that anchor the item's stable identity and its
preflight integrity check. Hashing is streamed in fixed chunks so an
arbitrarily large PDF never has to fit in memory.

Loud by design (the losslessness/loud-failure invariant): a manifest built
over a missing render is a *defect*, not something to skip — so a missing
file raises :class:`FileNotFoundError` rather than dropping the row. Likewise
a skiplist path that does not exist is operator error and raises; only blank
lines and ``#`` comments inside an existing skiplist are ignored.

PHI rule: nothing here logs or returns a patient-derived value. ``item_key``
embeds an encounter id and a hash prefix; the skiplist is matched on those
opaque keys only.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from anastomosis.core.hashutil import hash_and_size
from anastomosis.destinations.base import UploadItem
from anastomosis.reconstruct.engine import RenderedDoc

__all__ = ["build_manifest", "is_skiplisted", "load_skiplist"]


def build_manifest(documents: Iterable[RenderedDoc]) -> list[UploadItem]:
    """Turn rendered documents into upload items, one per document.

    For each :class:`RenderedDoc` the file's streaming sha256 and size are
    computed (via the shared :func:`anastomosis.core.hashutil.hash_and_size`, so
    the manifest measures a file exactly the way preflight and L0 re-measure
    it); ``item_key`` is ``f"{encounter_id}:{sha256[:12]}"`` (the resumability
    anchor) and ``fingerprint`` defaults via :class:`UploadItem`. A missing
    render file raises :class:`FileNotFoundError` — a defect the manifest must
    surface, never silently skip.
    """
    items: list[UploadItem] = []
    for doc in documents:
        sha256, size_bytes = hash_and_size(doc.path)
        items.append(
            UploadItem(
                item_key=f"{doc.encounter_id}:{sha256[:12]}",
                encounter_id=doc.encounter_id,
                patient_id=doc.patient_id,
                file_path=doc.path,
                sha256=sha256,
                size_bytes=size_bytes,
            )
        )
    return items


def load_skiplist(path: Path) -> frozenset[str]:
    """Read an operator skiplist: one ``item_key`` OR ``encounter_id`` per line.

    Blank lines and ``#`` comments are ignored; surrounding whitespace is
    stripped. A path that does not exist raises :class:`FileNotFoundError` —
    an explicitly supplied skiplist that is missing is operator error, not an
    empty skiplist.
    """
    entries: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        entries.add(line)
    return frozenset(entries)


def is_skiplisted(item: UploadItem, skiplist: frozenset[str]) -> bool:
    """Whether ``item`` is excluded — matched by ``item_key`` or ``encounter_id``."""
    return item.item_key in skiplist or item.encounter_id in skiplist
