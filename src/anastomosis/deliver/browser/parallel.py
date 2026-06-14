"""Patient-partitioned parallel upload runner (M2 item 10).

Throughput is the *secondary* goal here; patient safety is the primary one.
The partitioning rule exists so that all the work for one patient is driven by
exactly one worker — a single patient's chart is never navigated by two
browser sessions at once, which would race the wrong-patient banner defense
the whole subsystem is built around.

How a run is shaped:

* The coordinator opens ONE :class:`TrackingDB` on ``db_path`` and stamps a
  single ``begin_run``/``finish_run`` for the whole run — the ledger's WAL
  mode plus per-thread connections let many workers share the file safely, but
  only the coordinator writes the run row, so several workers can't race the
  abort stamp.
* Items are partitioned by patient (see :func:`_partition`): distinct
  ``patient_id`` values are bucketed greedily onto the least-loaded worker by
  cumulative item count, ties broken by ``patient_id`` order, so the split is
  deterministic and the per-worker item counts stay balanced.
* Each worker thread builds its OWN :class:`TrackingDB` (its own connection on
  the same file), its OWN :class:`Destination` via the factory (so each gets
  its own session — :class:`ManagedDestination` is single-threaded by
  contract), and its OWN :class:`UploadEngine`, then drives only its partition
  with ``manage_run=False`` and a shared ``stop`` event.
* A wrong-patient abort in any worker sets the shared ``stop`` event; the other
  workers see it at their next item boundary and stop promptly, leaving their
  remaining items PENDING (resumable). The coordinator records the single
  ``finish_run`` with the abort reason after every worker has joined.
* A worker dying with a ``BaseException`` (a real kill, e.g.
  :class:`FakeCrash`) must not deadlock the coordinator: all workers are
  joined, then the first ``BaseException`` is re-raised — preserving
  kill-and-resume semantics at the parallel level (the ledger is left
  consistent and a later run resumes it).

The merged result counts come from a single :meth:`TrackingDB.counts` at the
end: the ledger is the one source of truth, never a sum of per-worker tallies.

PHI rule: logs counts, worker indices, and ``exc_tag`` type names only.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from anastomosis.core.logutil import exc_tag
from anastomosis.core.model import Patient
from anastomosis.destinations.base import Destination, UploadItem

from .engine import UploadEngine
from .tracking import TrackingDB
from .verify import Verifier

__all__ = ["ParallelResult", "run_parallel"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParallelResult:
    """The outcome of one :func:`run_parallel` call.

    ``counts`` is the ledger's final per-state tally (the single source of
    truth, read once at the end). ``aborted_reason`` is the patient-safety
    abort type name if any worker hit one, else ``None``.
    ``processed_per_worker`` is how many items each worker actually drove (in
    worker order); ``partitions`` is how many items each worker was *assigned*
    (its queue depth), for the report. ``run_id`` identifies the run in the
    ledger so callers can read the run row back or write a run report.
    """

    counts: Mapping[str, int]
    aborted_reason: str | None
    processed_per_worker: tuple[int, ...]
    partitions: tuple[int, ...]
    run_id: str


def _partition(items: Sequence[UploadItem], workers: int) -> list[list[UploadItem]]:
    """Split ``items`` into ``workers`` buckets, keeping each patient whole.

    Greedy least-loaded by cumulative item count: distinct ``patient_id``
    values are processed in sorted order and each is assigned to the bucket
    with the fewest items so far (ties broken by the lowest bucket index, which
    — because patients are walked in ``patient_id`` order — makes the split
    fully deterministic). Every item of one patient lands in one bucket, so no
    patient is ever split across workers.
    """
    by_patient: dict[str, list[UploadItem]] = defaultdict(list)
    for item in items:
        by_patient[item.patient_id].append(item)

    buckets: list[list[UploadItem]] = [[] for _ in range(workers)]
    for patient_id in sorted(by_patient):
        # Least-loaded bucket; min() returns the first (lowest-index) on a tie.
        target = min(range(workers), key=lambda i: len(buckets[i]))
        buckets[target].extend(by_patient[patient_id])
    return buckets


def _worker(
    *,
    index: int,
    destination_factory: Callable[[], Destination],
    prebuilt: Destination | None,
    db_path: Path,
    partition: Sequence[UploadItem],
    patients: Mapping[str, Patient],
    run_id: str,
    stop: threading.Event,
    verifier_factory: Callable[[], Verifier] | None,
    max_attempts: int,
    backoff_base_s: float,
    sleeper: Callable[[float], None],
    processed: list[int],
    aborts: list[str],
    crashes: list[BaseException],
) -> None:
    """One worker thread: own ledger handle, own destination, own engine.

    Drives only its partition, with ``manage_run=False`` (the coordinator owns
    the run row) and the shared ``stop`` event (a sibling abort stops it). A
    wrong-patient abort records its reason; a ``BaseException`` (a kill) is
    captured for the coordinator to re-raise after all joins. Either way the
    shared ``stop`` is set so siblings wind down promptly.

    ``prebuilt`` is the destination the coordinator already built (to read its
    name for the run row); the first worker reuses it instead of building a
    second one — a real browser destination must not be spun up twice. Every
    other worker builds its own via ``destination_factory`` so each gets its
    own session (the single-session-per-worker contract).
    """
    tracking = TrackingDB(db_path)
    try:
        destination = prebuilt if prebuilt is not None else destination_factory()
        verifier = verifier_factory() if verifier_factory is not None else None
        engine = UploadEngine(
            destination,
            tracking,
            verifier=verifier,
            max_attempts=max_attempts,
            backoff_base_s=backoff_base_s,
            sleeper=sleeper,
        )
        result = engine.run(
            partition,
            patients,
            run_id,
            stop=stop,
            manage_run=False,
            restrict_to_items=True,
        )
        processed[index] = result.processed
        if result.aborted_reason is not None:
            aborts.append(result.aborted_reason)
    except BaseException as exc:  # a worker kill must not deadlock the run.
        # A real process-death-shaped failure (FakeCrash) or any other
        # BaseException: stop the siblings, capture it, and let the coordinator
        # re-raise after joins so nothing is lost and the ledger stays usable.
        logger.warning("worker %d died (%s); stopping run", index, exc_tag(exc))
        stop.set()
        crashes.append(exc)
    finally:
        tracking.close()


def run_parallel(
    destination_factory: Callable[[], Destination],
    db_path: Path,
    items: Sequence[UploadItem],
    patients: Mapping[str, Patient],
    *,
    workers: int = 2,
    verifier_factory: Callable[[], Verifier] | None = None,
    max_attempts: int = 3,
    backoff_base_s: float = 2.0,
    sleeper: Callable[[float], None] = time.sleep,
) -> ParallelResult:
    """Run an upload across ``workers`` threads, partitioned by patient.

    The coordinator owns the single run row (one ``begin_run``/``finish_run``);
    each worker constructs its own ledger handle, destination, and engine and
    drives only its patient-partitioned slice. A wrong-patient abort or a
    worker kill stops the rest promptly via a shared event. Returns a
    :class:`ParallelResult` whose counts come from one final ledger read.

    A worker that died with a ``BaseException`` (e.g. :class:`FakeCrash`) is
    re-raised after every worker has joined and after the run is finished on
    the ledger — so the run is left consistent and resumable.
    """
    if workers < 1:
        raise ValueError("workers must be >= 1")

    # Build one destination up front to read its name for the run row, then
    # hand it to the first worker so the factory is not called an extra time
    # (a real browser destination must never be spun up twice). If the factory
    # cannot build, the run is still recorded under a neutral placeholder.
    first_dest, run_name = _first_destination(destination_factory)

    coordinator = TrackingDB(db_path)
    run_id = coordinator.begin_run(run_name)
    buckets = _partition(items, workers)

    stop = threading.Event()
    processed = [0] * workers
    aborts: list[str] = []
    crashes: list[BaseException] = []

    threads = [
        threading.Thread(
            target=_worker,
            kwargs={
                "index": i,
                "destination_factory": destination_factory,
                "prebuilt": first_dest if i == 0 else None,
                "db_path": db_path,
                "partition": buckets[i],
                "patients": patients,
                "run_id": run_id,
                "stop": stop,
                "verifier_factory": verifier_factory,
                "max_attempts": max_attempts,
                "backoff_base_s": backoff_base_s,
                "sleeper": sleeper,
                "processed": processed,
                "aborts": aborts,
                "crashes": crashes,
            },
            name=f"anast-upload-{i}",
        )
        for i in range(workers)
    ]
    for thread in threads:
        thread.start()
    # Always join every worker before touching the ledger again — a worker
    # crash must never deadlock the coordinator.
    for thread in threads:
        thread.join()

    aborted_reason = aborts[0] if aborts else None
    coordinator.finish_run(run_id, aborted_reason=aborted_reason)
    counts = coordinator.counts()
    coordinator.close()

    result = ParallelResult(
        counts=counts,
        aborted_reason=aborted_reason,
        processed_per_worker=tuple(processed),
        partitions=tuple(len(bucket) for bucket in buckets),
        run_id=run_id,
    )

    # Re-raise the first worker kill AFTER the run row is finished and the
    # ledger handle is closed — the ledger is consistent and resumable.
    if crashes:
        raise crashes[0]

    return result


def _first_destination(
    destination_factory: Callable[[], Destination],
) -> tuple[Destination | None, str]:
    """Build the first worker's destination and read its name for the run row.

    Returns ``(destination, name)``. The destination is reused by the first
    worker so the factory is not called twice for it. If the factory cannot
    build (e.g. a browser is unavailable), returns ``(None, "unknown")`` so the
    run is still recorded and the first worker falls back to building its own.
    """
    try:
        dest = destination_factory()
    except Exception as exc:  # pragma: no cover - factory-dependent.
        logger.warning("could not build destination for run name (%s)", exc_tag(exc))
        return None, "unknown"
    return dest, dest.name
