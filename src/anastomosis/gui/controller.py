"""Contract: the GUI controller is the JS-API bridge, with no webview import
anywhere — pywebview exposes its methods as ``window.pywebview.api.<method>``;
:mod:`anastomosis.gui.shell` is the only place webview is touched.

A thin facade: async choreography lives in
:class:`~anastomosis.gui.jobs.GuiJobRunner`, the five operator surfaces in
:mod:`anastomosis.gui.consoles`, shared constants in
:mod:`anastomosis.gui.shared`.

Every public method returns a JSON-safe dict, never raises (an exception
becomes ``{"ok": False, "error": exc_tag(exc)}`` plus an error event), and
emits only PHI-safe events — operator-chosen paths are echoed back, rendered
filenames never are.
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

# Local selectors older than this vs. the registry's freshest evidence are
# flagged stale (the quarterly re-verification window); a dismissible toast.
_STALE_DAYS = 90


def _attach_destination(cdp_url: str, loaded: object) -> object:
    """Build the live browser destination for an upload run (the Playwright seam).

    Delegates to :func:`anastomosis.deliver.browser.attach.attach_destination`;
    module-level so tests monkeypatch it, lazily imported (no ``deliver-browser`` extra needed).
    """
    from anastomosis.deliver.browser.attach import attach_destination

    return attach_destination(cdp_url, loaded)


class EventSink(Protocol):
    """Where the controller posts events; the shell adapts this to the window.

    Takes a JSON-safe event dict (:mod:`anastomosis.gui.events`); tests pass
    a recording fake, the shell an ``evaluate_js`` adapter.
    """

    def emit(self, event: dict[str, object]) -> None: ...


class GuiController:
    """The plain-Python brain behind the GUI window (a facade over the consoles)."""

    # Read-only queries fail via their SYNCHRONOUS return, not an event; this
    # flow value keeps their defensive error events out of any page's flow guard.
    _QUERY_FLOW = "query"

    def __init__(self, sink: EventSink) -> None:
        self._sink = sink
        # Async-job choreography (busy guard + spawn/release/error) lives here, one
        # owner instead of five hand-rolled copies; sync paths share it via _acquire/_release.
        self._jobs = GuiJobRunner(self._emit)
        # Each surface shares the controller's emit + job runner, so every entry
        # contends on the SAME busy guard; the two run flows share one SummaryStore.
        self._summary_store = SummaryStore()
        self._upload = UploadConsole(self._emit, self._jobs)
        self._packgen = PackgenConsole(self._emit, self._jobs)
        self._source = SourceConsole(self._emit, self._jobs)
        self._pipeline = PipelineConsole(self._emit, self._jobs, self._summary_store)
        self._migration = MigrationConsole(self._emit, self._jobs, self._summary_store)

    def _emit(self, event: dict[str, object]) -> None:
        """Emit through the sink, swallowing sink failures.

        A broken ``evaluate_js`` must never kill the worker thread; failures
        are logged as type names only.
        """
        try:
            self._sink.emit(event)
        except Exception as exc:
            logger.warning("event sink failed (%s)", exc_tag(exc))

    # --- read-only queries --------------------------------------------------

    def gui_config(self) -> dict[str, object]:
        """The Python-canonical constants the browser UI mirrors.

        JS keeps same-valued fallbacks, refreshed here on load; drift-pinned
        by ``test_frontend_constants.py``. PHI-free. Never raises.
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

        Wraps :func:`anastomosis.core.commands.get_toolkit_info`; PHI-free
        (versions, names, booleans).
        """
        try:
            from anastomosis.core.commands import get_toolkit_info

            toolkit = get_toolkit_info()
            return {
                "ok": True,
                "version": toolkit.version,
                "extras": dict(toolkit.extras),
                "sources": [
                    {
                        "name": source.name,
                        "display": source.display,
                        "description": source.description,
                        "selection": source.selection,
                    }
                    for source in toolkit.sources
                ],
                "packs": [
                    {
                        "name": pack.name,
                        "display": pack.display,
                        "available": pack.available,
                        "origin": pack.origin,
                        # The exact directory a run naming this layout binds to;
                        # three origins can answer to one name; Teach shows which.
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

        Wraps :func:`anastomosis.core.selfcheck.check_bundled_assets`; on
        failure returns ``{"ok": False, "error": <type>}`` with no ``checks`` key. Never raises.
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
        """Contract: a run's per-patient detail for LOCAL display, by ``summary_id``.

        PHI by design (name, DOB, counts) — on-screen only, NEVER emitted
        or logged. Keyed in :class:`SummaryStore` by the ``done`` event's id.
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

        Mirrors the CLI's ``destination route`` path (:func:`plan_route`) as
        structured JSON; an unknown ``destination`` fails clean, never a traceback.
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

        Combines :func:`plan_route` with :func:`load_destination_pack`'s status;
        ``pack`` is ``None`` with no browser pack. PHI-free."""
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
        """Contract: which destinations' local browser-pack selectors are
        stale against the registry's freshest evidence (vendor-change detection).

        Stale when ``selectors.yaml`` predates the newest ``verified`` date
        by more than :data:`_STALE_DAYS` days. Names/dates/counts only."""
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
                # Stale when local selectors predate the evidence by more than
                # the window — a vendor change the evidence may reflect but the pack does not.
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

        A read-only window onto ``UploadConsole``'s state; the console owns
        the flag, ``upload_stop()`` drives it.
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
        """Resolve a transit map's browser-pack readiness.

        Delegates to ``MigrationConsole``, which owns the readiness helper;
        kept callable here for ``destination_status`` and its own test.
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

        The shell's ``closing`` handler reads this to veto a close mid-run.
        Delegates to the job runner's one guard.
        """
        return self._jobs.busy

    def join_active_job(self, timeout: float | None = None) -> bool:
        """Wait up to ``timeout`` seconds for the active job's worker to finish.

        Returns whether it finished. Delegates to the job runner; the
        shell's close-barrier fallback (and its tests) rely on this surface.
        """
        return self._jobs.join(timeout)


def _freshest_evidence(entry: DestinationEntry) -> date | None:
    """The newest ``verified`` date across an entry's cited capabilities, or None.

    Browser ``pack`` capabilities carry no evidence (proof is canary
    fixtures), so they never count toward this.
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

    A built-in scaffold with no discovered overlay (slots still DISCOVER)
    has no aged artifact, so this returns None and skips the freshness check.
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
    """Contract: the pywebview ``js_api`` facade — exposes only the
    async/busy-guarded run methods and read-only queries the front end calls.

    Excludes the controller's synchronous heavy methods, so a page can
    never freeze the bridge thread."""

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
