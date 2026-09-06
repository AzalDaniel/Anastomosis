"""Mechanics shared by the file-writing deliverers (archive, bundle, C-CDA).

Module-private to :mod:`anastomosis.deliver`. Each persona keeps its own
layout (archive: one cross-patient tree; bundle: one directory per
patient; C-CDA: one XML per patient); this module holds only the parts
that are byte-for-byte identical across them — see each function's own
docstring.

Not shared: PDF attribution (archive-only) and the three personas'
distinct README texts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

from anastomosis.core.atomic import atomic_copy, atomic_write_text
from anastomosis.core.fhir import DeliveredAttachment, to_bundle
from anastomosis.core.hashutil import hash_and_size
from anastomosis.core.logutil import exc_tag, safe_log_id
from anastomosis.core.model import PatientRecord
from anastomosis.core.textutil import budgeted_name

__all__ = [
    "DeliveredNameCollision",
    "budgeted_copy_name",
    "claim_delivered_name",
    "copy_claimed_chart",
    "copy_delivered_file",
    "measure_delivered_attachment",
    "measured_attachment",
    "record_witness",
    "write_fhir_bundle",
]

#: The per-patient FHIR rendition's filename, identical across deliverers.
BUNDLE_FILENAME = "bundle.json"


class DeliveredNameCollision(Exception):
    """Two different source ids resolved to the same delivered name.

    Raised, never warned: a silent merge would file one patient's chart into
    another's. Collisions come from a shared hash-tag, or ids the sanitizer
    folds together (``MRN 1234`` / ``MRN/1234``)."""


def budgeted_copy_name(target_dir: Path, source_name: str) -> str:
    """The delivered filename for a chart copied into ``target_dir``.

    One definition shared by every copier and its linkers. Renderer names run
    past the Windows path budget; raises ``ValueError`` (via ``budgeted_name``)
    rather than silently dropping the chart."""
    source = Path(source_name)
    suffix = source.suffix
    return budgeted_name(source.stem, "chart", parent=target_dir, suffix=suffix) + suffix


def _owner(source_id: str, content: str | None) -> str:
    """Who holds a delivered slot: a source id, or id+content digest.

    An id alone assumes ids are unique; passing ``content`` (its digest)
    distinguishes two records the source gave one id."""
    if content is None:
        return source_id
    return f"{source_id}@{hashlib.sha256(content.encode('utf-8')).hexdigest()[:16]}"


def claim_delivered_name(
    claims: dict[str, str], name: str, source_id: str, *, kind: str, content: str | None = None
) -> None:
    """Record that ``source_id`` owns delivered ``name``, or raise.

    Re-claiming with the same owner is a no-op; a different owner raises
    :class:`DeliveredNameCollision`. Pass ``content`` when two records could
    share one source id, so a second write cannot land on the first."""
    owner = _owner(source_id, content)
    previous = claims.setdefault(name, owner)
    if previous == owner:
        return
    if previous == source_id or previous.startswith(f"{source_id}@"):
        raise DeliveredNameCollision(
            f"two different {kind}s carry the same source id ({safe_log_id(source_id)}) and "
            "would land in the same slot; refusing to write one over the other. The source "
            "gave both records one id, so nothing downstream can tell them apart"
        )
    raise DeliveredNameCollision(
        f"two different source ids resolve to the same delivered {kind} name "
        f"({safe_log_id(previous)} and {safe_log_id(source_id)}); refusing to "
        "merge two records into one slot"
    )


def record_witness(record: PatientRecord) -> str:
    """The ``content`` a per-patient claim writes its slot with.

    Stands in for bytes not written yet: two records that differ anywhere
    are two owners; identical are one delivered twice. ``default=str`` covers
    ``extensions`` values JSON cannot spell, so a preserved field never fails."""
    return json.dumps(record.model_dump(), sort_keys=True, default=str)


def write_fhir_bundle(
    record: PatientRecord,
    out_dir: Path,
    attachments: Mapping[str, DeliveredAttachment] | None = None,
) -> Path:
    """Write ``record`` as ``out_dir/bundle.json`` (FHIR R4); return the path.

    ``indent=2, sort_keys=True`` for diffable, byte-stable output. ``attachments``
    is what the caller measured off documents beside ``out_dir``; omitted, no
    attachment is carried at all (see :func:`measure_delivered_attachment`)."""
    target = out_dir / BUNDLE_FILENAME
    # PHI-BY-DESIGN: the patient's FHIR record is the product; the caller
    # has already hardened ``out_dir`` (RULES.md 18). See SECURITY.md.
    # codeql[py/clear-text-storage-sensitive-data]
    atomic_write_text(target, json.dumps(to_bundle(record, attachments), indent=2, sort_keys=True))
    return target


def measure_delivered_attachment(path: Path, url: str) -> DeliveredAttachment:
    """``DeliveredAttachment`` for the file this run just wrote at ``path``.

    Hashed off disk, never a record's own claim, so a short write is
    reported as what landed. ``url`` is the caller's relative spelling;
    this only measures, it does not name."""
    sha256, size = hash_and_size(path)
    return DeliveredAttachment(url=url, size=size, sha256=sha256)


def measured_attachment(
    landed: dict[str, DeliveredAttachment], path: Path, url: str
) -> DeliveredAttachment:
    """``DeliveredAttachment`` for ``path``: reused from ``landed``'s
    per-run cache if this delivered name was already measured this call,
    freshly measured (and cached) otherwise. Lets two artifacts naming
    ONE carried file share one measurement instead of re-hashing a copy
    :func:`copy_claimed_chart` already wrote."""
    measured = landed.get(path.name)
    if measured is None:
        measured = measure_delivered_attachment(path, url)
        landed[path.name] = measured
    return measured


def copy_claimed_chart(
    target_dir: Path, claims: dict[str, str], source: Path, name: str, *, kind: str = "chart"
) -> tuple[str | None, str | None]:
    """Budget, claim, and copy one chart into ``target_dir``.

    Returns ``(delivered, failure)``: exactly one is not ``None`` — the
    delivered filename on success, or the PHI-safe failure TYPE name on an
    ``OSError``. A failed copy is never counted as delivered by the caller."""
    delivered = budgeted_copy_name(target_dir, name)
    claim_delivered_name(claims, delivered, name, kind=kind)
    failure = copy_delivered_file(source, target_dir / delivered)
    return (None, failure) if failure else (delivered, None)


def copy_delivered_file(source: Path, destination: Path) -> str | None:
    """Copy (never move) ``source`` to ``destination``.

    Returns ``None`` on success, or the PHI-safe exception TYPE name on
    ``OSError`` — the caller logs its own message under its own logger.
    Copy, not move, leaves the source directory intact for a re-run."""
    try:
        atomic_copy(source, destination)
    except OSError as exc:
        return exc_tag(exc)
    return None
