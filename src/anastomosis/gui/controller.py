"""The GUI controller: the JS-API bridge, with no webview import anywhere.

This is the headless half of the GUI. pywebview exposes an object's methods to
the browser as ``window.pywebview.api.<method>`` and JSON-serializes their
return values; this controller IS that object, but it imports nothing from
pywebview, so the whole surface is unit-testable against a recording fake sink
(see ``tests/unit/test_gui_controller.py``). The shell
(:mod:`anastomosis.gui.shell`) is the only place webview is touched: it
constructs the controller, wires a sink that marshals events into
``window.evaluate_js("anastEvent(...)")``, and opens the window.

Architecture: this class is a thin FACADE. The async-job choreography lives in
:class:`~anastomosis.gui.jobs.GuiJobRunner`; the operator surfaces live in the
:mod:`anastomosis.gui.consoles` package (upload, packgen, source, and the
pipeline/migration run flows sharing a
:class:`~anastomosis.gui.consoles.runs.SummaryStore`); the pure shared constants
and serializers live in the leaf :mod:`anastomosis.gui.shared`. The controller
constructs one of each, keeps the light read-only queries, and delegates every
moved method one-to-one. Its public method surface is unchanged.

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
import threading
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from anastomosis.core.logutil import exc_tag
from anastomosis.core.upload_command import DEFAULT_MAX_ATTEMPTS, LEDGER_NAME
from anastomosis.gui.consoles import (
    MigrationConsole,
    PackgenConsole,
    PipelineConsole,
    SourceConsole,
    SummaryStore,
    UploadConsole,
)
from anastomosis.gui.jobs import GuiJobRunner

# _STAGE_RAIL/_STATE_GROUPS feed gui_config(); _transit_to_dict() feeds
# routes()/destination_status(); fail_result() backs the query-flow _fail below.
from anastomosis.gui.shared import _STAGE_RAIL, _STATE_GROUPS, _transit_to_dict, fail_result

if TYPE_CHECKING:
    from anastomosis.deliver.router import TransitMap
    from anastomosis.destinations.registry import DestinationEntry

__all__ = ["EventSink", "GuiController"]


logger = logging.getLogger(__name__)

# Local destination selectors older than this (relative to the registry's
# freshest evidence date) are flagged stale — the quarterly re-verification
# window the registry documents. Surfaced as a dismissible dashboard toast.
_STALE_DAYS = 90


def _attach_destination(cdp_url: str, loaded: object) -> object:
    """Build the live browser destination for an upload run (the Playwright seam).

    Delegates to :func:`anastomosis.deliver.browser.attach.attach_destination`
    — the ONE place a CDP attach over Playwright happens — WITHOUT depending
    on ``anastomosis.cli``. The caller has already validated the loopback
    gate. This is kept a SEPARATE module-level function so the GUI tests can
    ``monkeypatch.setattr(controller, "_attach_destination",
    lambda cdp, loaded: FakeDestination(...))`` and drive the whole upload
    flow with no browser. The import is lazy so the controller loads
    cleanly without the ``deliver-browser`` extra.
    """
    from anastomosis.deliver.browser.attach import attach_destination

    return attach_destination(cdp_url, loaded)


class EventSink(Protocol):
    """Where the controller posts events; the shell adapts this to the window.

    The single method takes a JSON-safe event dict (see
    :mod:`anastomosis.gui.events`). Tests pass a recording fake; the shell
    passes an adapter that calls ``window.evaluate_js("anastEvent(...)")``.
    """

    def emit(self, event: dict[str, object]) -> None: ...


class GuiController:
    """The plain-Python brain behind the GUI window (a facade over the consoles)."""

    # The controller's own read-only queries (info/doctor/detect/routes/
    # destination_status/pack_freshness) are page-agnostic request/response
    # calls: their authoritative failure delivery is the SYNCHRONOUS return dict
    # the caller awaits, not an event. They are not one of the five run flows a
    # page owns, so their defensive error events carry this distinct "query"
    # flow — no page's flow guard renders it (the return value already did),
    # which is exactly right: a read-query error never gets mistaken by a page
    # for one of its own run's terminal events (the per-flow guard).
    _QUERY_FLOW = "query"

    def __init__(self, sink: EventSink) -> None:
        self._sink = sink
        # The async-job choreography (busy guard + spawn/release/error) lives
        # in GuiJobRunner — one owner instead of five hand-rolled copies.
        # Sync busy-guarded paths contend on the SAME guard via
        # _acquire/_release below.
        self._jobs = GuiJobRunner(self._emit)
        # The five operator surfaces: each takes the controller's emit + the
        # shared job runner, so every entry (sync or async) contends on the
        # SAME busy guard. The two run flows share one SummaryStore for the
        # keyed per-patient roll-up.
        self._summary_store = SummaryStore()
        self._upload = UploadConsole(self._emit, self._jobs)
        self._packgen = PackgenConsole(self._emit, self._jobs)
        self._source = SourceConsole(self._emit, self._jobs)
        self._pipeline = PipelineConsole(self._emit, self._jobs, self._summary_store)
        self._migration = MigrationConsole(self._emit, self._jobs, self._summary_store)

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

    def gui_config(self) -> dict[str, object]:
        """The Python-canonical constants the browser UI mirrors.

        The frontends used to hand-mirror these (``console.js`` hard-coded
        ``DEFAULT_MAX_ATTEMPTS``; ``app.js`` hard-coded the stage rail), risking
        the two copies drifting apart. The JS keeps same-valued fallbacks
        for the api-less browser preview and refreshes from this endpoint on
        load; ``tests/unit/test_frontend_constants.py`` pins the fallbacks to
        these values so neither side can drift alone. PHI-free by
        construction (retry budget, the record's filename, stage names, state-group names). Never
        raises.
        """
        return {
            "ok": True,
            "max_attempts": DEFAULT_MAX_ATTEMPTS,
            "ledger_name": LEDGER_NAME,
            "stage_rail": list(_STAGE_RAIL),
            "state_groups": {group: list(states) for group, states in _STATE_GROUPS.items()},
        }

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
                "sources": [
                    {"name": name, "display": display, "description": desc}
                    for name, display, desc in toolkit.sources
                ],
                "packs": [
                    {
                        "name": pack.name,
                        "display": pack.display,
                        "available": pack.available,
                        "origin": pack.origin,
                        # The exact directory a run naming this layout will bind
                        # to. Three origins can answer to one name, and after a
                        # Teach the operator is entitled to see which one they
                        # are about to select.
                        "root": pack.root,
                        "sections": pack.sections,
                    }
                    for pack in toolkit.packs
                ],
            }
        except Exception as exc:  # defensive: info() must never raise into JS
            return self._fail("info", exc)

    def doctor(self) -> dict[str, object]:
        """Bundled-asset self-check for the dashboard (install health).

        Wraps the SAME shared core
        (:func:`anastomosis.core.selfcheck.check_bundled_assets`) the CLI's
        ``anast doctor`` runs, so the two frontends report identical install
        health. On success returns ``{"ok": bool, "checks": [{name, ok, detail},
        ...]}``; on the never-raise failure path returns ``{"ok": False, "error":
        <type>}`` (no ``checks`` key), so a caller must branch on ``ok`` before
        reading ``checks``. PHI-free (asset names + counts / type-name details
        only). Never raises.
        """
        try:
            from anastomosis.core.selfcheck import check_bundled_assets

            result = check_bundled_assets()
            return {
                "ok": result.ok,
                "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in result.checks],
            }
        except Exception as exc:
            return self._fail("doctor", exc)

    def last_run_summary(self, summary_id: str | None = None) -> dict[str, object]:
        """A run's per-patient detail, for LOCAL dashboard display, by summary id.

        The async run path returns immediately with ``{"started": True}`` and
        streams PHI-safe COUNTS back as events; the per-patient roll-up (display
        name, DOB, #encounters, #notes) is held in the SummaryStore, keyed by the
        ``summary_id`` the ``done`` event carries, for the dashboard/wizard to
        fetch by that id. Keying by id (not a single slot) means a rapid second
        run cannot erase or replace the first run's detail before its UI reads it.
        These values are PHI by design — returned for direct on-screen display on
        the operator's own machine, NEVER emitted as events or written to any log.
        Returns ``{"ok": True, "patients": [...]}``; the list is empty for an
        unknown or missing id. Never raises.
        """
        return {"ok": True, "patients": self._summary_store.get(summary_id)}

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

    # --- upload console (delegated to UploadConsole) ------------------------

    def upload_status(self, db_path: str) -> dict[str, object]:
        return self._upload.upload_status(db_path)

    def upload_item_keys(self, db_path: str, limit: int = 200) -> dict[str, object]:
        return self._upload.upload_item_keys(db_path, limit)

    def upload_manifest_preview(self, out_dir: str) -> dict[str, object]:
        return self._upload.upload_manifest_preview(out_dir)

    def upload_safety_notice(self) -> dict[str, object]:
        return self._upload.upload_safety_notice()

    def upload_start(self, *args: Any, **kwargs: Any) -> dict[str, object]:
        return self._upload.upload_start(*args, **kwargs)

    def upload_stop(self) -> dict[str, object]:
        return self._upload.upload_stop()

    @property
    def _upload_stop(self) -> threading.Event | None:
        """The in-flight upload's cooperative stop flag, or None (read-only view).

        A read-only window onto the UploadConsole's stop-event state (a
        pre-existing test asserts this is ``None`` after a spawn failure). The
        console owns the flag; upload_stop() drives it.
        """
        return self._upload._upload_stop

    # --- pack-from-samples wizard (delegated to PackgenConsole) -------------

    def pack_init(self, *args: Any, **kwargs: Any) -> dict[str, object]:
        return self._packgen.pack_init(*args, **kwargs)

    def pack_init_async(self, *args: Any, **kwargs: Any) -> dict[str, object]:
        return self._packgen.pack_init_async(*args, **kwargs)

    def last_pack_result(self) -> dict[str, object]:
        return self._packgen.last_pack_result()

    # --- learn-a-source wizard (delegated to SourceConsole) -----------------

    def source_init(self, *args: Any, **kwargs: Any) -> dict[str, object]:
        return self._source.source_init(*args, **kwargs)

    def source_init_async(self, *args: Any, **kwargs: Any) -> dict[str, object]:
        return self._source.source_init_async(*args, **kwargs)

    def last_source_result(self) -> dict[str, object]:
        return self._source.last_source_result()

    # --- the pipeline run (delegated to PipelineConsole) --------------------

    def run_pipeline(self, *args: Any, **kwargs: Any) -> dict[str, object]:
        return self._pipeline.run_pipeline(*args, **kwargs)

    def run_pipeline_async(self, *args: Any, **kwargs: Any) -> dict[str, object]:
        return self._pipeline.run_pipeline_async(*args, **kwargs)

    # --- the migration run (delegated to MigrationConsole) ------------------

    def run_migration(self, *args: Any, **kwargs: Any) -> dict[str, object]:
        return self._migration.run_migration(*args, **kwargs)

    def run_migration_async(self, *args: Any, **kwargs: Any) -> dict[str, object]:
        return self._migration.run_migration_async(*args, **kwargs)

    # --- internals ----------------------------------------------------------

    def _pack_readiness(self, transit: TransitMap) -> dict[str, object] | None:
        """Resolve a transit map's browser-pack readiness (used by destination_status).

        Delegates to the MigrationConsole, which owns the readiness helper (the
        browser-route domain). Kept callable here because ``destination_status``
        uses it and a test drives it directly.
        """
        return self._migration._pack_readiness(transit)

    def _fail(self, stage: str, exc: BaseException) -> dict[str, object]:
        """Convert a caught exception to the no-traceback error contract."""
        return fail_result(self._emit, self._QUERY_FLOW, stage, exc)

    def _acquire(self) -> bool:
        return self._jobs.acquire()

    def _release(self) -> None:
        self._jobs.release()

    # --- window-close barrier surface (read by the shell) -------------------

    @property
    def busy(self) -> bool:
        """True while a long-running job holds the busy guard.

        The shell's ``closing`` handler reads this to veto a window close while
        a run is in flight, so a mid-run close can't interrupt an in-flight
        PDF/ledger write. Delegates to the one busy guard the job runner owns.
        """
        return self._jobs.busy

    def join_active_job(self, timeout: float | None = None) -> bool:
        """Wait up to ``timeout`` seconds for the active job's worker to finish.

        Returns ``True`` if no job is active or it finished in time, ``False``
        if a worker is still running. The shell's close barrier uses the veto
        (see ``busy``); this is the join surface the barrier's fallback path
        (and its tests) rely on. Delegates to the job runner.
        """
        return self._jobs.join(timeout)


def _freshest_evidence(entry: DestinationEntry) -> date | None:
    """The newest ``verified`` date across an entry's cited capabilities, or None.

    A destination's evidence ages at the rate of its freshest citation: re-
    verifying any one capability resets the clock. Browser ``pack`` capabilities
    carry no evidence (their proof is canary fixtures), so they do not count.
    """
    dates: list[date] = []
    for cap in (
        entry.doc_write_api,
        entry.ccda_import,
        entry.browser,
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


class GuiApi:
    """The pywebview ``js_api`` surface: a thin facade over a :class:`GuiController`.

    It exposes ONLY the async/busy-guarded run methods and the light read-only
    queries the front end actually calls (the exact set ``gui/web/*.js`` uses).
    The controller ALSO has synchronous heavy methods — ``run_pipeline``,
    ``run_migration``, ``pack_init``, ``source_init`` — and ``doctor`` (which can
    start Playwright in the bundled-asset check). Those are kept for tests and
    internal use, but they would block the single pywebview bridge thread and
    freeze the UI if a page invoked them. Binding THIS facade (not the raw
    controller) as ``js_api`` makes them un-callable from JS by construction.
    """

    def __init__(self, controller: GuiController) -> None:
        self._c = controller

    # --- light read-only queries ---
    def gui_config(self) -> dict[str, object]:
        return self._c.gui_config()

    def info(self) -> dict[str, object]:
        return self._c.info()

    def detect(self, export_dir: str) -> dict[str, object]:
        return self._c.detect(export_dir)

    def routes(self, destination: str | None = None) -> dict[str, object]:
        return self._c.routes(destination)

    def destination_status(self, name: str) -> dict[str, object]:
        return self._c.destination_status(name)

    def pack_freshness(self) -> dict[str, object]:
        return self._c.pack_freshness()

    def last_run_summary(self, summary_id: str | None = None) -> dict[str, object]:
        return self._c.last_run_summary(summary_id)

    def last_pack_result(self) -> dict[str, object]:
        return self._c.last_pack_result()

    def last_source_result(self) -> dict[str, object]:
        return self._c.last_source_result()

    def upload_status(self, db_path: str) -> dict[str, object]:
        return self._c.upload_status(db_path)

    def upload_item_keys(self, db_path: str, limit: int = 200) -> dict[str, object]:
        return self._c.upload_item_keys(db_path, limit)

    def upload_manifest_preview(self, out_dir: str) -> dict[str, object]:
        return self._c.upload_manifest_preview(out_dir)

    def upload_safety_notice(self) -> dict[str, object]:
        return self._c.upload_safety_notice()

    # --- async runs + upload driving (return immediately / busy-guarded) ---
    def run_pipeline_async(self, *args: Any, **kwargs: Any) -> dict[str, object]:
        return self._c.run_pipeline_async(*args, **kwargs)

    def run_migration_async(self, *args: Any, **kwargs: Any) -> dict[str, object]:
        return self._c.run_migration_async(*args, **kwargs)

    def pack_init_async(self, *args: Any, **kwargs: Any) -> dict[str, object]:
        return self._c.pack_init_async(*args, **kwargs)

    def source_init_async(self, *args: Any, **kwargs: Any) -> dict[str, object]:
        return self._c.source_init_async(*args, **kwargs)

    def upload_start(self, *args: Any, **kwargs: Any) -> dict[str, object]:
        return self._c.upload_start(*args, **kwargs)

    def upload_stop(self) -> dict[str, object]:
        return self._c.upload_stop()
