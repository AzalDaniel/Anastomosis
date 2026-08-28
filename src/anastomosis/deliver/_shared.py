"""Mechanics shared by the file-writing deliverers (archive, bundle, C-CDA).

Module-private to :mod:`anastomosis.deliver`: the personas keep their own
layouts — the archive writes ONE cross-patient tree (index + ``unattributed/``
routing), the bundle writes one self-contained directory PER patient (QA slice
+ its own README), the C-CDA export writes one XML per patient — and nothing
here merges them. What lives here is only the mechanics that are byte-for-byte
identical across them today:

* :func:`write_fhir_bundle` — the machine-readable rendition every persona
  emits, same filename, same JSON serialization, same PHI-BY-DESIGN guarantee;
* :func:`copy_delivered_file` — the copy-never-move step, returning the
  PHI-safe exception TYPE name so each caller logs the artifact it was copying
  with its own module logger and its own message;
* :func:`budgeted_copy_name` — the destination name for a copied chart, cut
  to fit the path budget of the tree it is being copied INTO;
* :func:`claim_delivered_name` — the per-run ledger that makes a name
  collision between two different source ids a loud failure instead of a
  silent merge of two patients' output;
* :func:`copy_claimed_chart` — the budget→claim→copy sequence for one chart,
  built on the three above, that every attributed-, unattributed-, and
  bundle-copy site runs identically.

Deliberately NOT shared: PDF *attribution* (the archive routes unclaimed PDFs
to ``unattributed/`` and maps encounter→filename for its per-encounter pages;
the bundle has no unattributed slot because it is per-patient by definition)
and the README texts (three distinct operator-facing documents:
``core.output``'s directory warning, the archive's, and the bundle's).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from anastomosis.core.atomic import atomic_copy, atomic_write_text
from anastomosis.core.fhir import to_bundle
from anastomosis.core.logutil import exc_tag, safe_log_id
from anastomosis.core.model import PatientRecord
from anastomosis.core.textutil import budgeted_name

__all__ = [
    "DeliveredNameCollision",
    "budgeted_copy_name",
    "claim_delivered_name",
    "copy_claimed_chart",
    "copy_delivered_file",
    "write_fhir_bundle",
]

#: The per-patient FHIR rendition's filename, identical across deliverers.
BUNDLE_FILENAME = "bundle.json"


class DeliveredNameCollision(Exception):
    """Two DIFFERENT source ids resolved to the same delivered name.

    The deliverers write with ``mkdir(exist_ok=True)`` / ``write_bytes`` /
    ``write_text``, so a collision does not fail — it MERGES, filing one
    patient's chart into another patient's slot. That is the wrong-patient
    failure the whole toolkit exists to prevent, so it is raised, never warned:
    a partial delivery the operator must look at beats a complete-looking one
    that quietly lost a chart.

    Two sanitizations can produce one name: a value cut by
    :func:`~anastomosis.core.textutil.safe_name` whose hash tags happen to
    agree (astronomically unlikely at 64 bits, and never assumed away), and —
    the reachable one — two ids that differ only in characters the sanitizer
    collapses (``MRN 1234`` and ``MRN/1234`` both become ``MRN_1234``).
    """


def budgeted_copy_name(target_dir: Path, source_name: str) -> str:
    """The delivered filename for a chart copied into ``target_dir``.

    ONE definition, shared by every deliverer that copies a rendered chart into
    the tree it hands the operator — and, within a deliverer, by both the
    copier (which creates the file) and whatever links to it. A second,
    differently budgeted derivation would produce a link pointing at nothing: a
    chart the operator cannot reach.

    Why it must be budgeted at all: the renderer names charts
    ``{family}_{given}_{dos}_{type}.pdf`` where each component is capped by
    ``safe_name`` at :data:`~anastomosis.core.textutil.MAX_NAME_CHARS` — up to
    ~617 characters before a copy even starts. Copying that into a deep
    delivered tree fails with an OSError that the copy callers log and continue
    past, which is a chart SILENTLY MISSING from a delivered tree — the
    losslessness violation the path budget exists to prevent. A name that fits
    is returned byte-identical (every real chart name); one that cannot be cut
    to a distinct name raises :class:`ValueError` from ``budgeted_name``, and
    the run stops loudly.
    """
    source = Path(source_name)
    suffix = source.suffix
    return budgeted_name(source.stem, "chart", parent=target_dir, suffix=suffix) + suffix


def _owner(source_id: str, content: str | None) -> str:
    """Who holds a delivered slot: a source id, or a source id AND its content.

    An id alone answers "is this the same record?" only while ids are unique.
    When the caller can supply what it is about to write, the digest of that
    goes in too, so two records the source gave one id are two owners.
    """
    if content is None:
        return source_id
    return f"{source_id}@{hashlib.sha256(content.encode('utf-8')).hexdigest()[:16]}"


def claim_delivered_name(
    claims: dict[str, str], name: str, source_id: str, *, kind: str, content: str | None = None
) -> None:
    """Record that ``source_id`` owns the delivered ``name``, or raise.

    ``claims`` is a per-run ledger the caller owns (one dict per delivery run),
    mirroring the render engine's per-run ``claimed`` set. Re-claiming a name
    with the same owner is a no-op — a record legitimately delivered twice in
    one run keeps its slot; a claim by a different owner raises
    :class:`DeliveredNameCollision`.

    Pass ``content`` — the artifact about to be written — wherever two records
    could arrive carrying one id. Without it the ledger reads a re-claim by the
    same id as the same record and lets the second write land on the first:
    one file holding the second visit, while the run reports two. A physician
    opens visit 1 and reads visit 2, with nothing saying so.

    Two records under one id is not a naming problem to route around. It means
    the SOURCE cannot say which visit is which — so writing both under invented
    names would be this tool guessing at a patient's chart, which is the one
    thing it must never do. It refuses instead, and says which kind collided.

    PHI: the message names the artifact ``kind`` and the ids as run-scoped
    :func:`~anastomosis.core.logutil.safe_log_id` surrogates — never the
    delivered name (built from a source id) and never a patient value.
    """
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


def write_fhir_bundle(record: PatientRecord, out_dir: Path) -> Path:
    """Write ``record`` as ``out_dir/bundle.json`` (FHIR R4) and return the path.

    ``indent=2, sort_keys=True`` makes the file diffable and byte-stable across
    runs — delivered archives and bundles are compared by operators, so the
    serialization is part of the contract, not an implementation detail.
    """
    target = out_dir / BUNDLE_FILENAME
    # PHI-BY-DESIGN: writing the patient's FHIR record to disk IS the product.
    # ``out_dir`` sits under a secure_output_dir-hardened tree (0o700 owner-only
    # on POSIX; on Windows NTFS, inheritance stripped and access limited to the
    # current user, SYSTEM, and Administrators) with a PHI-warning README — the
    # deliverers call :func:`anastomosis.core.output.secure_output_dir` before
    # they reach here. See SECURITY.md, "Code scanning & suppression policy
    # (auditable)".
    # codeql[py/clear-text-storage-sensitive-data]
    atomic_write_text(target, json.dumps(to_bundle(record), indent=2, sort_keys=True))
    return target


def copy_claimed_chart(
    target_dir: Path, claims: dict[str, str], source: Path, name: str, *, kind: str = "chart"
) -> tuple[str | None, str | None]:
    """Budget, claim, and copy one chart into ``target_dir``; ``(delivered, failure)``.

    The three deliverers (archive's own tree, archive's ``unattributed/``
    sweep, bundle) each budgeted a destination name, claimed it against the
    caller's per-run ledger, and copied — this is that ONE sequence. Exactly
    one of the pair returned is not ``None``: the delivered filename on
    success, or the PHI-safe failure TYPE name (:func:`copy_delivered_file`)
    on an ``OSError``. The caller logs its own message on failure and decides
    what "no result" means for its own bookkeeping — skip a mapping entry,
    skip a claimed-source add, skip a copied-list append — so a copy that
    fails is never counted as delivered by any of the three.
    """
    delivered = budgeted_copy_name(target_dir, name)
    claim_delivered_name(claims, delivered, name, kind=kind)
    failure = copy_delivered_file(source, target_dir / delivered)
    return (None, failure) if failure else (delivered, None)


def copy_delivered_file(source: Path, destination: Path) -> str | None:
    """Copy (never move) ``source`` to ``destination``; ``None`` when it landed.

    On an :class:`OSError` the PHI-safe exception TYPE name is returned instead
    of being logged here: each deliverer names the artifact it was copying in
    its own message, under its own module logger. Copying (rather than moving)
    leaves the caller's chart directory intact for a re-run.
    """
    try:
        atomic_copy(source, destination)
    except OSError as exc:
        return exc_tag(exc)
    return None
