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
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from anastomosis.core.logutil import exc_tag
from anastomosis.gui.events import done_event, error_event, progress_event, stage_event

if TYPE_CHECKING:
    from anastomosis.pipeline import StageEvent

__all__ = ["EventSink", "GuiController"]


logger = logging.getLogger(__name__)


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

        Reuses :func:`available_sources` and :func:`discover_packs` (the same
        data ``anast info`` shows) plus an extras-installed probe identical to
        the CLI's. PHI-free by construction — versions, names, booleans.
        """
        try:
            import anastomosis
            import anastomosis.pipeline  # registers built-in source adapters at import
            from anastomosis.reconstruct import discover_packs
            from anastomosis.sources import available_sources

            extras = {
                extra: _module_available(module)
                for extra, module in (
                    ("render", "playwright"),
                    ("render-qa", "fitz"),
                    ("fhir", "fhir.resources"),
                    ("gui", "webview"),
                )
            }
            sources = [{"name": a.name, "description": a.description} for a in available_sources()]
            packs = [
                {
                    "name": status.name,
                    "available": status.available,
                    "origin": status.origin,
                    "sections": _pack_sections(status),
                }
                for status in discover_packs().values()
            ]
            return {
                "ok": True,
                "version": anastomosis.__version__,
                "extras": extras,
                "sources": sources,
                "packs": packs,
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
        from anastomosis.pipeline import PipelineError, run_pipeline

        out = Path(out_dir)
        # The pipeline core consumes ``--section`` strings; translate the GUI's
        # ``{name: bool}`` matrix into that exact form so both frontends agree.
        section = [f"{name}={'on' if value else 'off'}" for name, value in sections.items()]

        rollup: dict[str, int] = {}

        def _on_event(event: StageEvent) -> None:
            stage = _STAGE_MAP.get(event.stage)
            if stage is None:
                return  # the detect stage has no rail of its own
            self._emit(stage_event(stage, "start"))
            self._emit(progress_event(stage, **event.counts))
            self._emit(stage_event(stage, "done"))
            rollup.update(event.counts)

        try:
            result = run_pipeline(
                export_dir=Path(export_dir),
                out=out,
                source=source,
                pack=pack,
                pack_dirs=None,
                force=False,
                section=section,
                qa=qa,
                on_event=_on_event,
            )
        except PipelineError as exc:
            self._emit(error_event(_failed_stage(str(exc)), str(exc)))
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # any non-pipeline crash: type name only, no PHI
            return self._fail("run_pipeline", exc)

        if archive or bundle or ccda:
            self._deliver(result, out, archive=archive, bundle=bundle, ccda=ccda, rollup=rollup)

        self._emit(done_event(**rollup))
        return {"ok": True, **rollup}

    def _deliver(
        self,
        result: object,
        out: Path,
        *,
        archive: bool,
        bundle: bool,
        ccda: bool,
        rollup: dict[str, int],
    ) -> None:
        """Run the requested deliverers, emitting one progress event each.

        PHI rule: each event carries a COUNT of artifacts written, never the
        rendered filenames. The output subdirectory the operator's choice
        implies is not echoed into the event log (only counts are).
        """
        from anastomosis.pipeline import PipelineResult

        assert isinstance(result, PipelineResult)
        self._emit(stage_event("deliver", "start"))
        if archive:
            from anastomosis.deliver.archive import ArchiveDeliverer

            arc = ArchiveDeliverer().deliver(
                result.records, out, out / "archive", qa_report=result.qa_report
            )
            count = arc.patient_count
            rollup["archive_patients"] = count
            self._emit(progress_event("deliver", deliverer="archive", patients=count))
        if bundle:
            from anastomosis.deliver.bundle import BundleDeliverer

            deliverer = BundleDeliverer()
            pdfs = sorted(out.glob("*.pdf")) if out.is_dir() else []
            written = 0
            for record in result.records:
                deliverer.deliver(record, pdfs, out / "bundles", qa_report=result.qa_report)
                written += 1
            rollup["bundle_patients"] = written
            self._emit(progress_event("deliver", deliverer="bundle", patients=written))
        if ccda:
            from anastomosis.deliver.ccda_export import deliver_ccda

            paths = deliver_ccda(result.records, out / "ccda")
            rollup["ccda_patients"] = len(paths)
            self._emit(progress_event("deliver", deliverer="ccda", patients=len(paths)))
        self._emit(stage_event("deliver", "done"))

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


def _module_available(module: str) -> bool:
    try:
        __import__(module)
    except ImportError:
        return False
    return True


def _pack_sections(status: object) -> dict[str, dict[str, object]]:
    """The section-flag matrix for a pack (label + default), or empty if broken.

    Items 18/19 (the section-selection matrix UI) consume this; the dashboard
    surfaces only which packs exist. Pure data, no PHI.
    """
    pack = getattr(status, "pack", None)
    if pack is None:
        return {}
    return {
        key: {"label": flag.label, "default": flag.default}
        for key, flag in pack.manifest.sections.items()
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
