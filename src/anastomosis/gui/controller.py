"""The GUI controller: the JS-API bridge, with no webview import anywhere.

This is the headless half of the GUI. pywebview exposes an object's methods to
the browser as ``window.pywebview.api.<method>`` and JSON-serializes their
return values; this controller IS that object, but it imports nothing from
pywebview, so the whole surface is unit-testable against a recording fake sink
(see ``tests/unit/test_gui_controller.py``). The shell
(:mod:`anastomosis.gui.shell`) is the only place webview is touched: it
constructs the controller, wires a sink that marshals events into
``window.evaluate_js("anastEvent(...)")``, and opens the window.

Contract for every public method:

* return a **JSON-safe dict** (the browser receives it directly);
* **never raise** — every exception is caught and converted to
  ``{"ok": False, "error": exc_tag(exc)}`` plus an ``error`` event, because the
  GUI must never see a Python traceback;
* emit only PHI-safe events (counts, stage names, ids, exception type names) —
  output paths the operator chose are echoed back to them, but rendered
  filenames never are (count summaries only).

Long-running work (``run_pipeline``) runs synchronously in
:meth:`GuiController.run_pipeline` and is also offered as a fire-and-forget
daemon thread via :meth:`GuiController.run_pipeline_async`, guarded by a
``busy`` flag so a second concurrent run is rejected rather than racing the
first. pywebview's ``evaluate_js`` is thread-safe, so the sink adapter (owned by
the shell) is free to be called from the worker thread; the controller just
emits.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from anastomosis.core.logutil import exc_tag
from anastomosis.gui.events import done_event, error_event, progress_event, stage_event

if TYPE_CHECKING:
    from anastomosis.core.commands import DeliveryOutcome
    from anastomosis.pipeline import StageEvent

__all__ = ["EventSink", "GuiController"]


logger = logging.getLogger(__name__)

# A pack name must be a lowercase manifest identifier (mirrors the CLI's
# _PACK_NAME_RE — it is the pack name AND the directory name).
_PACK_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Local destination selectors older than this (relative to the registry's
# freshest evidence date) are flagged stale — the quarterly re-verification
# window the registry documents. Surfaced as a dismissible dashboard toast.
_STALE_DAYS = 90


class EventSink(Protocol):
    """Where the controller posts events; the shell adapts this to the window.

    The single method takes a JSON-safe event dict (see
    :mod:`anastomosis.gui.events`). Tests pass a recording fake; the shell
    passes an adapter that calls ``window.evaluate_js("anastEvent(...)")``.
    """

    def emit(self, event: dict[str, object]) -> None: ...


class GuiController:
    """The plain-Python brain behind the GUI window."""

    def __init__(self, sink: EventSink) -> None:
        self._sink = sink
        self._lock = threading.Lock()
        self._busy = False

    def _emit(self, event: dict[str, object]) -> None:
        """Emit through the sink, swallowing sink failures.

        The controller's contract is never-raise: a broken evaluate_js (a
        closed window, a JS error) must not kill the pipeline thread or
        escape to the caller. Sink failures are logged as type names only.
        """
        try:
            self._sink.emit(event)
        except Exception as exc:
            logger.warning("event sink failed (%s)", exc_tag(exc))

    # --- read-only queries --------------------------------------------------

    def info(self) -> dict[str, object]:
        """Toolkit status for the dashboard header and the run form.

        Wraps the shared :func:`anastomosis.core.commands.get_toolkit_info` (the
        same data ``anast info`` renders). PHI-free by construction — versions,
        names, booleans.
        """
        try:
            from anastomosis.core.commands import get_toolkit_info

            toolkit = get_toolkit_info()
            return {
                "ok": True,
                "version": toolkit.version,
                "extras": dict(toolkit.extras),
                "sources": [{"name": name, "description": desc} for name, desc in toolkit.sources],
                "packs": [
                    {
                        "name": pack.name,
                        "available": pack.available,
                        "origin": pack.origin,
                        "sections": pack.sections,
                    }
                    for pack in toolkit.packs
                ],
            }
        except Exception as exc:  # defensive: info() must never raise into JS
            return self._fail("info", exc)

    def detect(self, export_dir: str) -> dict[str, object]:
        """Sniff ``export_dir`` for a known source format (the picker hint)."""
        try:
            import anastomosis.pipeline  # noqa: F401  registers built-in source adapters
            from anastomosis.sources import detect_source

            adapter = detect_source(Path(export_dir))
            return {"ok": True, "source": adapter.name if adapter else None}
        except Exception as exc:
            return self._fail("detect", exc)

    def routes(self, destination: str | None = None) -> dict[str, object]:
        """The transit-map data for every registry entry, or just one.

        Mirrors the CLI's ``destination route`` data path
        (:func:`plan_route` over the packaged registry) but returns structured
        JSON for the GUI to draw, not a fixed-width text map. An unknown
        ``destination`` is a clean ``{"ok": False, ...}``, never a traceback.
        """
        try:
            from anastomosis.deliver.router import plan_route
            from anastomosis.destinations.registry import DestinationRegistry

            registry = DestinationRegistry.load()
            names = [destination] if destination is not None else sorted(registry.entries)
            maps = [_transit_to_dict(plan_route(name, registry)) for name in names]
            return {"ok": True, "routes": maps}
        except KeyError as exc:
            # plan_route raises KeyError listing known names (names only, no PHI).
            return {"ok": False, "error": str(exc.args[0] if exc.args else exc)}
        except Exception as exc:
            return self._fail("routes", exc)

    def destination_status(self, name: str) -> dict[str, object]:
        """The wizard's per-destination view: transit map + browser-pack readiness.

        Combines the router's transit map (:func:`plan_route`) with the browser
        pack's discovery status (:func:`load_destination_pack`) so the wizard can
        tell a browser-route operator whether the pack is ``ready`` (selectors
        discovered) or still ``needs-discovery`` (run ``anast destination init``).
        ``pack`` is ``None`` for destinations with no browser pack at all (the
        common case — most route by API or C-CDA). An unknown destination is a
        clean ``{"ok": False, ...}``, never a traceback.

        PHI rule: returns destination names, capability kinds, evidence dates,
        pack names, and booleans only — nothing patient-derived.
        """
        try:
            from anastomosis.deliver.router import plan_route
            from anastomosis.destinations.registry import DestinationRegistry

            registry = DestinationRegistry.load()
            transit = plan_route(name, registry)  # KeyError lists known names
            return {
                "ok": True,
                "transit": _transit_to_dict(transit),
                "pack": self._pack_readiness(transit),
            }
        except KeyError as exc:
            return {"ok": False, "error": str(exc.args[0] if exc.args else exc)}
        except Exception as exc:
            return self._fail("destination_status", exc)

    def pack_freshness(self) -> dict[str, object]:
        """Vendor-change detection: which destinations' local selectors are stale.

        For every registry destination that has a discovered browser pack (a
        user ``selectors.yaml`` exists), compare that file's modification date
        against the registry entry's freshest evidence date. When the local
        selectors predate the evidence by more than :data:`_STALE_DAYS` days,
        they were validated against a now-superseded understanding of the
        vendor's UI — the dashboard raises a dismissible toast advising
        ``anast destination init --validate``.

        Returns ``{"ok": True, "stale": [...], "checked": N}`` where each stale
        entry carries the destination name, the selectors date, the evidence
        date, and the gap in days — counts/dates/names only, never PHI. A
        destination with no discovered pack is simply not checked (nothing to
        compare); it never appears in either list.
        """
        try:
            from anastomosis.destinations.loader import (
                BrowserPackError,
                load_destination_pack,
            )
            from anastomosis.destinations.registry import DestinationRegistry

            registry = DestinationRegistry.load()
            stale: list[dict[str, object]] = []
            checked = 0
            for dest_name in sorted(registry.entries):
                evidence_date = _freshest_evidence(registry.entries[dest_name])
                if evidence_date is None:
                    continue
                try:
                    loaded = load_destination_pack(dest_name)
                except BrowserPackError:
                    continue  # no browser pack for this destination — nothing to age
                selectors_date = _selectors_mtime_date(loaded)
                if selectors_date is None:
                    continue  # selectors undiscovered (built-in scaffold) — not aged
                checked += 1
                # Stale when the local selectors were generated more than the
                # window BEFORE the latest verified evidence: a vendor change
                # the evidence may already reflect but the local pack predates.
                gap = (evidence_date - selectors_date).days
                if gap > _STALE_DAYS:
                    stale.append(
                        {
                            "destination": dest_name,
                            "selectors_date": selectors_date.isoformat(),
                            "evidence_date": evidence_date.isoformat(),
                            "gap_days": gap,
                            "advice": f"anast destination init {dest_name} --validate",
                        }
                    )
            return {"ok": True, "stale": stale, "checked": checked, "stale_after_days": _STALE_DAYS}
        except Exception as exc:
            return self._fail("pack_freshness", exc)

    # --- upload console (browser-delivery operator surface) -----------------

    def upload_status(self, db_path: str) -> dict[str, object]:
        """The upload console's read-only view of a tracking ledger.

        Opens the WAL SQLite ledger at ``db_path`` read-only-in-spirit (no
        writes, never resumed here — live driving is M6) and returns the
        state-machine counters grouped into pending/active/terminal, the latest
        run's info, and the attempts + error-TYPE histograms (from the same
        :mod:`reports` accessors the run report uses). Every value is a count, a
        state name, a destination/run id, an ISO timestamp, or an exception TYPE
        name — never an item key, a path, or any patient-derived value.

        A missing/garbage ledger file is a clean ``{"ok": False, ...}`` (the DB
        is opened defensively); never a traceback.
        """
        tracking = None
        try:
            from anastomosis.deliver.browser.tracking import TrackingDB

            path = Path(db_path)
            if not path.is_file():
                return {"ok": False, "error": "FileNotFoundError"}
            tracking = TrackingDB(path)
            counts = tracking.counts()
            run = self._latest_run(tracking)
            return {
                "ok": True,
                "counts": dict(counts),
                "groups": _group_states(counts),
                "total": sum(counts.values()),
                "run": run,
                "attempts_histogram": {str(k): v for k, v in tracking.attempts_histogram().items()},
                "error_type_histogram": (
                    dict(tracking.error_type_histogram(str(run["run_id"])))
                    if run is not None
                    else {}
                ),
            }
        except Exception as exc:
            return self._fail("upload_status", exc)
        finally:
            if tracking is not None:
                tracking.close()

    def upload_item_keys(self, db_path: str, limit: int = 200) -> dict[str, object]:
        """The patient command sheet's payload: pending item KEYS only.

        Returns the opaque ``item_key`` values (``encounter_id:sha256[:12]``) of
        items still owing work, for the Cmd+K palette. These are ids by
        construction — never a patient name, never a file path. The full
        live-driving console (start/pause real uploads) is M6; this is the STUB
        that lists what *would* be driven. Capped at ``limit`` so a huge ledger
        cannot flood the palette.
        """
        tracking = None
        try:
            from anastomosis.deliver.browser.tracking import TrackingDB

            path = Path(db_path)
            if not path.is_file():
                return {"ok": False, "error": "FileNotFoundError"}
            tracking = TrackingDB(path)
            keys = [item.item_key for item in tracking.pending_items(limit=limit)]
            return {"ok": True, "item_keys": keys, "count": len(keys)}
        except Exception as exc:
            return self._fail("upload_item_keys", exc)
        finally:
            if tracking is not None:
                tracking.close()

    def upload_manifest_preview(self, out_dir: str) -> dict[str, object]:
        """Count the renderable PDFs an upload run would carry, from ``out_dir``.

        A thin, read-only preview over the reconstruction output directory: the
        number of ``*.pdf`` files (the unit of upload work) and their total
        bytes. No manifest is built and no hashing happens — that needs the
        per-encounter ids the upload engine carries, not on-disk files — so this
        is a count-and-size sketch only, by design. Counts and a byte total
        only; never a filename. A missing directory is a clean error.
        """
        try:
            path = Path(out_dir)
            if not path.is_dir():
                return {"ok": False, "error": "NotADirectoryError"}
            pdfs = sorted(path.glob("*.pdf"))
            total_bytes = sum(p.stat().st_size for p in pdfs)
            return {"ok": True, "renderable": len(pdfs), "total_bytes": total_bytes}
        except Exception as exc:
            return self._fail("upload_manifest_preview", exc)

    # --- the pack-from-samples wizard ---------------------------------------

    def pack_init(
        self,
        samples_dir: str,
        name: str,
        display: str | None = None,
        confirmed_distinct_patients: bool = False,
        out_dir: str | None = None,
    ) -> dict[str, object]:
        """Learn a DRAFT template pack from sample PDFs (the wizard's backend).

        Mirrors the CLI ``anast pack init`` flow headlessly: validate the pack
        name, collect the sample PDFs, harvest + analyze them, render the
        PHI-safe :meth:`PackAnalysis.summary_lines` digest, and — only with
        ``confirmed_distinct_patients`` checked (the CLI's interactive
        same-patient guard, ported as a required checkbox) — emit the draft and
        return its path plus the ``DRAFT.md`` text for display.

        Without the confirmation this REFUSES (``ok: False``, ``error:
        ConfirmationRequired``) and writes nothing — the same guard the CLI
        enforces with ``typer.confirm``. The single-sample text-suppression
        behavior is inherited from ``summary_lines`` (the draft never echoes
        per-patient text).

        PHI rule: ``summary`` carries only static template text (recurring
        across distinct samples) and counts; sample paths are never echoed (the
        count is). Returns JSON-safe data; never raises.
        """
        try:
            from anastomosis.packgen import analyze, extract_samples
            from anastomosis.packgen.emit import SAME_PATIENT_CAVEAT, emit_draft_pack

            if not _PACK_NAME_RE.match(name):
                return {"ok": False, "error": "InvalidPackName"}

            pdfs = sorted(Path(samples_dir).glob("*.pdf")) if Path(samples_dir).is_dir() else []
            if not pdfs:
                return {"ok": False, "error": "NoSamplesFound"}

            analysis = analyze(extract_samples(pdfs))
            summary = list(analysis.summary_lines())

            # The same-patient guard: ported from the CLI's typer.confirm. An
            # unchecked confirmation refuses and writes nothing — but still
            # returns the PHI-safe summary so the operator sees what they are
            # being asked to confirm.
            if not confirmed_distinct_patients:
                return {
                    "ok": False,
                    "error": "ConfirmationRequired",
                    "caveat": SAME_PATIENT_CAVEAT,
                    "summary": summary,
                    "sample_count": analysis.sample_count,
                    "low_confidence": analysis.low_confidence,
                }

            target = Path(out_dir) if out_dir is not None else Path("packs")
            pack_dir = emit_draft_pack(analysis, name=name, display=display or name, out_dir=target)
            draft_md = (pack_dir / "DRAFT.md").read_text(encoding="utf-8")
            return {
                "ok": True,
                "pack_dir": str(pack_dir),
                "draft_md": draft_md,
                "summary": summary,
                "sample_count": analysis.sample_count,
                "low_confidence": analysis.low_confidence,
            }
        except Exception as exc:
            return self._fail("pack_init", exc)

    # --- the pipeline run ---------------------------------------------------

    def run_pipeline(
        self,
        export_dir: str,
        out_dir: str,
        pack: str = "generic_soap",
        source: str | None = None,
        sections: dict[str, bool] | None = None,
        qa: bool = True,
        archive: bool = False,
        bundle: bool = False,
        ccda: bool = False,
    ) -> dict[str, object]:
        """Drive the shared pipeline core, emitting stage/progress events.

        Returns the final roll-up dict (also emitted as a ``done`` event). Any
        failure becomes ``{"ok": False, "error": <type-or-diagnosis>}`` plus an
        ``error`` event. The ``busy`` guard rejects a second concurrent run.

        Deliverer flags (``archive``/``bundle``/``ccda``) write into
        sibling subdirectories of ``out_dir`` (``out_dir/archive`` etc.) since
        the GUI has one output-dir field; each emits a per-deliverer count.
        """
        if not self._acquire():
            return {"ok": False, "error": "Busy"}
        try:
            return self._run_pipeline_locked(
                export_dir=export_dir,
                out_dir=out_dir,
                pack=pack,
                source=source,
                sections=sections or {},
                qa=qa,
                archive=archive,
                bundle=bundle,
                ccda=ccda,
            )
        finally:
            self._release()

    def run_pipeline_async(self, *args: object, **kwargs: object) -> dict[str, object]:
        """Run :meth:`run_pipeline` on a daemon thread (the GUI stays responsive).

        Returns immediately with ``{"ok": True, "started": True}`` (or
        ``{"ok": False, "error": "Busy"}`` if a run is already in flight, so the
        rejection is synchronous — the button stays disabled). The actual
        result arrives as ``stage``/``progress``/``done``/``error`` events.
        """
        if self._busy:
            return {"ok": False, "error": "Busy"}

        def _worker() -> None:
            self.run_pipeline(*args, **kwargs)  # type: ignore[arg-type]

        threading.Thread(target=_worker, name="anast-pipeline", daemon=True).start()
        return {"ok": True, "started": True}

    # --- internals ----------------------------------------------------------

    def _run_pipeline_locked(
        self,
        *,
        export_dir: str,
        out_dir: str,
        pack: str,
        source: str | None,
        sections: dict[str, bool],
        qa: bool,
        archive: bool,
        bundle: bool,
        ccda: bool,
    ) -> dict[str, object]:
        from anastomosis.core.commands import (
            DeliveryCommand,
            PipelineCommand,
            run_pipeline_command,
        )
        from anastomosis.pipeline import PipelineError

        out = Path(out_dir)
        rollup: dict[str, int] = {}

        def _on_event(event: StageEvent) -> None:
            stage = _STAGE_MAP.get(event.stage)
            if stage is None:
                return  # the detect stage has no rail of its own
            self._emit(stage_event(stage, "start"))
            self._emit(progress_event(stage, **event.counts))
            self._emit(stage_event(stage, "done"))
            rollup.update(event.counts)

        # GUI deliveries land in sibling subdirectories of the output dir (the
        # GUI has one output-dir field), through the same command path the CLI
        # uses with operator-chosen paths.
        deliveries: list[DeliveryCommand] = []
        if archive:
            deliveries.append(DeliveryCommand("archive", out / "archive"))
        if bundle:
            deliveries.append(DeliveryCommand("bundle", out / "bundles"))
        if ccda:
            deliveries.append(DeliveryCommand("ccda", out / "ccda"))

        try:
            result = run_pipeline_command(
                PipelineCommand(
                    export_dir=Path(export_dir),
                    charts_dir=out,
                    source=source,
                    pack=pack,
                    sections=sections,
                    qa=qa,
                    deliveries=tuple(deliveries),
                ),
                on_event=_on_event,
            )
        except PipelineError as exc:
            self._emit(error_event(_failed_stage(str(exc)), str(exc)))
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # any non-pipeline crash: type name only, no PHI
            return self._fail("run_pipeline", exc)

        if result.deliveries:
            self._present_deliveries(result.deliveries, rollup)

        self._emit(done_event(**rollup))
        return {"ok": True, **rollup}

    def _present_deliveries(
        self, deliveries: dict[str, DeliveryOutcome], rollup: dict[str, int]
    ) -> None:
        """Emit the deliver-rail events from the completed delivery outcomes.

        The deliverers themselves ran inside the shared command core; this only
        presents the counts. PHI rule: each event carries a COUNT of artifacts
        written, never the rendered filenames or the operator's chosen paths.
        """
        self._emit(stage_event("deliver", "start"))
        for kind in ("archive", "bundle", "ccda"):
            outcome = deliveries.get(kind)
            if outcome is None:
                continue
            patients = outcome.counts["patients"]
            rollup[f"{kind}_patients"] = patients
            self._emit(progress_event("deliver", deliverer=kind, patients=patients))
        self._emit(stage_event("deliver", "done"))

    def _pack_readiness(self, transit: object) -> dict[str, object] | None:
        """Resolve the browser pack for a transit map, if it has one.

        A destination whose browser route is viable names a pack in the
        BROWSER option's ``requires``; we load it defensively to report
        ``ready`` (selectors discovered) vs ``needs-discovery``. Destinations
        with no browser pack return ``None`` — the wizard simply omits the
        readiness chip. Loud failures from the loader are swallowed into a
        diagnosis (type name), never raised.
        """
        from anastomosis.deliver.router import RouteKind
        from anastomosis.destinations.loader import BrowserPackError, load_destination_pack

        name = transit.destination  # type: ignore[attr-defined]
        browser = next(
            (opt for opt in transit.options if opt.kind == RouteKind.BROWSER),  # type: ignore[attr-defined]
            None,
        )
        if browser is None or not browser.viable:
            return None
        try:
            loaded = load_destination_pack(name)
        except BrowserPackError as exc:
            return {"name": name, "ready": False, "diagnosis": exc_tag(exc)}
        return {
            "name": loaded.name,
            "ready": loaded.ready,
            "builtin": loaded.builtin,
        }

    @staticmethod
    def _latest_run(tracking: object) -> dict[str, object] | None:
        """The most-recent run row (by started_at), as a JSON-safe dict, or None.

        Reuses :meth:`TrackingDB.run_info` for the field shape but resolves the
        latest ``run_id`` itself (the upload console shows one current run). All
        values are log-safe: a run id, a destination name, ISO timestamps, and
        an abort TYPE name — never a patient value.
        """
        conn = tracking._conn()  # type: ignore[attr-defined]
        row = conn.execute(
            "SELECT run_id FROM runs ORDER BY started_at DESC, run_id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        run_id = row["run_id"]
        info = tracking.run_info(run_id)  # type: ignore[attr-defined]
        return {"run_id": run_id, **info}

    def _fail(self, stage: str, exc: BaseException) -> dict[str, object]:
        """Convert a caught exception to the no-traceback error contract."""
        tag = exc_tag(exc)
        self._emit(error_event(stage, tag))
        return {"ok": False, "error": tag}

    def _acquire(self) -> bool:
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            return True

    def _release(self) -> None:
        with self._lock:
            self._busy = False


# Pipeline-core stage names -> dashboard rail names (detect has no rail).
_STAGE_MAP = {
    "ingest": "ingest",
    "reconstruct": "reconstruct",
    "qa": "qa",
}


def _transit_to_dict(transit: object) -> dict[str, object]:
    """Serialize a :class:`TransitMap` to a JSON-safe dict for the GUI."""
    options = [
        {
            "kind": opt.kind.value,
            "viable": opt.viable,
            "why": opt.why,
            "requires": list(opt.requires),
        }
        for opt in transit.options  # type: ignore[attr-defined]
    ]
    chosen = transit.chosen  # type: ignore[attr-defined]
    return {
        "destination": transit.destination,  # type: ignore[attr-defined]
        "options": options,
        "chosen": chosen.kind.value if chosen is not None else None,
    }


# State groupings for the upload console's glass cards (the 15 states bucketed
# pending/active/terminal). PENDING is its own "pending" bucket; mid-flight work
# is "active"; everything else is "terminal" (no work owed). Pure presentation
# data — counts only flow through it.
_STATE_GROUPS: dict[str, tuple[str, ...]] = {
    "pending": ("pending",),
    "active": (
        "resolving_patient",
        "verifying_pre",
        "uploading",
        "upload_interrupted",
        "retry_wait",
        "verifying_post",
    ),
    "terminal": (
        "skipped_skiplist",
        "preflight_failed",
        "patient_not_found",
        "duplicate_at_destination",
        "pre_verify_failed",
        "failed",
        "post_verify_failed",
        "completed",
    ),
}


def _group_states(counts: dict[str, int]) -> dict[str, int]:
    """Bucket per-state item counts into pending/active/terminal totals."""
    return {
        group: sum(counts.get(state, 0) for state in states)
        for group, states in _STATE_GROUPS.items()
    }


def _freshest_evidence(entry: object) -> date | None:
    """The newest ``verified`` date across an entry's cited capabilities, or None.

    A destination's evidence ages at the rate of its freshest citation: re-
    verifying any one capability resets the clock. Browser ``pack`` capabilities
    carry no evidence (their proof is canary fixtures), so they do not count.
    """
    dates: list[date] = []
    for cap in (
        entry.doc_write_api,  # type: ignore[attr-defined]
        entry.ccda_import,  # type: ignore[attr-defined]
        entry.browser,  # type: ignore[attr-defined]
    ):
        evidence = getattr(cap, "evidence", None)
        if evidence is not None:
            dates.append(evidence.verified)
    return max(dates) if dates else None


def _selectors_mtime_date(loaded: object) -> date | None:
    """The UTC modification date of a discovered ``selectors.yaml``, or None.

    A ready pack's selectors came from a discovered overlay file
    (``selectors_source``); a built-in scaffold with no overlay has no aged
    artifact (its slots are still the DISCOVER placeholder), so it returns None
    and is not freshness-checked.
    """
    if not getattr(loaded, "ready", False):
        return None
    source = getattr(loaded, "selectors_source", None)
    if source is None:
        return None
    source_path = Path(source)
    # The wizard writes selectors into a file named selectors.yaml; the built-in
    # pack.yaml is not an aged selectors artifact even when it resolves.
    if source_path.name != "selectors.yaml" or not source_path.is_file():
        return None
    from datetime import UTC, datetime

    return datetime.fromtimestamp(source_path.stat().st_mtime, tz=UTC).date()


def _failed_stage(message: str) -> str:
    """Best-effort: which rail stage a PipelineError belongs to (for the event).

    Maps the loud failure messages the pipeline core raises onto a rail name so
    the error banner can highlight the right card. Falls back to ``ingest`` (the
    earliest stage) for source/pack-resolution failures.
    """
    if message.startswith("QA failed"):
        return "qa"
    if "failed to render" in message:
        return "reconstruct"
    return "ingest"
