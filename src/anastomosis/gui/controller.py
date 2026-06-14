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
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from anastomosis.core.logutil import exc_tag
from anastomosis.gui.events import done_event, error_event, progress_event, stage_event

if TYPE_CHECKING:
    from anastomosis.core.commands import DeliveryOutcome
    from anastomosis.core.packinit import PackInitResult
    from anastomosis.deliver.browser.tracking import TrackingDB
    from anastomosis.deliver.router import TransitMap
    from anastomosis.destinations.registry import DestinationEntry
    from anastomosis.pipeline import StageEvent

__all__ = ["EventSink", "GuiController"]


logger = logging.getLogger(__name__)

# A pack name must be a lowercase manifest identifier (mirrors the CLI's
# _PACK_NAME_RE — it is the pack name AND the directory name). The same rule
# governs a learned-source mapping id.
_PACK_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Structured-export file types the source-learning wizard can read.
_LEARNABLE_SUFFIXES = (".csv", ".tsv", ".json", ".ndjson", ".jsonl")

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
        # The most recent run's per-patient detail (display name, DOB, note
        # counts), held for the dashboard to fetch via last_run_summary() once
        # the count-only `done` event lands. PHI: local display only, never
        # logged or emitted. Empty until the first successful run.
        self._last_patients: list[dict[str, object]] = []
        # The most recent pack_init_async run's result dict, held for the wizard
        # to fetch via last_pack_result() once the packgen `done` event lands.
        # PHI-safe (static template text, counts, pack config); empty until the
        # first async pack-init run.
        self._last_pack: dict[str, object] = {}

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

    def last_run_summary(self) -> dict[str, object]:
        """The most recent run's per-patient detail, for LOCAL dashboard display.

        The async run path returns immediately with ``{"started": True}`` and
        streams PHI-safe COUNTS back as events; the per-patient roll-up (display
        name, DOB, #encounters, #notes) is held here for the dashboard to fetch
        once the ``done`` event lands. These values are PHI by design — they are
        returned for direct on-screen display on the operator's own machine and
        are NEVER emitted as events or written to any log. Returns
        ``{"ok": True, "patients": [...]}``; the list is empty before the first
        successful run and after a failed run. Never raises.
        """
        return {"ok": True, "patients": list(self._last_patients)}

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

        A thin adapter over the shared
        :func:`anastomosis.core.packinit.run_pack_init` — the SAME analyze →
        confirm → emit flow the CLI's ``anast pack init`` runs. Validate the
        pack name, collect the sample PDFs, harvest + analyze them, render the
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
            from anastomosis.core.packinit import PackInitCommand, run_pack_init

            result = run_pack_init(
                PackInitCommand(
                    samples=[samples_dir],
                    name=name,
                    display=display,
                    out_dir=Path(out_dir) if out_dir is not None else Path("packs"),
                    confirmed=confirmed_distinct_patients,
                )
            )
            return self._pack_init_result_dict(result)
        except Exception as exc:
            return self._fail("pack_init", exc)

    def _pack_init_result_dict(
        self, result: PackInitResult, *, emit_failure: bool = True
    ) -> dict[str, object]:
        """Map a :class:`PackInitResult` to the wizard's JSON-safe dict.

        Shared by the sync :meth:`pack_init` and the async worker so both
        present an identical surface. The PHI-free validation errors
        (``InvalidPackName`` / ``NoSamplesFound``) return the bare code; the
        refusal carries the summary + caveat so the operator can confirm;
        success carries the pack path + ``DRAFT.md``. An analyze/emit failure
        (``error`` is an :func:`exc_tag` type name) ALSO emits a ``pack_init``
        ``error`` event (the sync controller's loud-failure contract) — but only
        when ``emit_failure`` is set. The async worker passes
        ``emit_failure=False`` because IT emits the single ``packgen`` error
        event on its own channel; the gate prevents a double, stage-mismatched
        error event on the async path.
        """
        if result.error in {"InvalidPackName", "NoSamplesFound"}:
            return {"ok": False, "error": result.error}
        if result.error == "ConfirmationRequired":
            return {
                "ok": False,
                "error": "ConfirmationRequired",
                "caveat": result.caveat,
                "summary": result.summary,
                "sample_count": result.sample_count,
                "low_confidence": result.low_confidence,
            }
        if result.ok:
            return {
                "ok": True,
                "pack_dir": str(result.pack_dir),
                "draft_md": result.draft_md,
                "summary": result.summary,
                "sample_count": result.sample_count,
                "low_confidence": result.low_confidence,
            }
        # An analyze/emit failure: a type-name diagnosis. Mirror _fail — emit an
        # error event AND return the no-traceback error dict (sync path only;
        # the async worker emits its own packgen error event).
        error = result.error or "PackInitError"
        if emit_failure:
            self._emit(error_event("pack_init", error))
        return {"ok": False, "error": error}

    def pack_init_async(
        self,
        samples_dir: str,
        name: str,
        display: str | None = None,
        confirmed_distinct_patients: bool = False,
        out_dir: str | None = None,
    ) -> dict[str, object]:
        """Run :meth:`pack_init` on a daemon thread (the GUI stays responsive).

        Mirrors :meth:`run_pipeline_async`: acquires the busy flag SYNCHRONOUSLY
        before returning, emits a ``packgen`` ``start`` stage event, and runs the
        shared :func:`anastomosis.core.packinit.run_pack_init` flow on a daemon
        worker. Returns ``{"ok": True, "started": True}`` immediately, or
        ``{"ok": False, "error": "Busy"}`` if a run is already in flight. The
        result dict is stashed for :meth:`last_pack_result` and a terminal event
        lands: a ``packgen`` ``done`` stage event for a written draft OR for a
        ``ConfirmationRequired`` refusal (the expected analyze checkpoint, which
        carries the summary the wizard renders), and a ``packgen`` ``error``
        event only for a genuine failure (bad name, no samples, an analyze/emit
        crash). The JS fetches :meth:`last_pack_result` on ``done`` to route the
        result, so ConfirmationRequired must be ``done``, not ``error``.

        The same-patient semantics match the sync path: ``confirmed=False`` runs
        analyze-only and stashes the ``ConfirmationRequired`` dict (with the
        summary + caveat), so the JS can use the async path for BOTH wizard
        steps. Never raises.
        """
        if not self._acquire():
            return {"ok": False, "error": "Busy"}

        self._emit(stage_event("packgen", "start"))

        def _worker() -> None:
            try:
                from anastomosis.core.packinit import PackInitCommand, run_pack_init

                result_dict = self._pack_init_result_dict(
                    run_pack_init(
                        PackInitCommand(
                            samples=[samples_dir],
                            name=name,
                            display=display,
                            out_dir=Path(out_dir) if out_dir is not None else Path("packs"),
                            confirmed=confirmed_distinct_patients,
                        )
                    ),
                    emit_failure=False,  # the worker emits the single packgen error below
                )
                self._last_pack = result_dict
                # A written draft OR the expected ConfirmationRequired refusal are
                # both `done` (the wizard fetches last_pack_result and routes the
                # summary vs the draft). Only a genuine failure is an `error`.
                if result_dict.get("ok") or result_dict.get("error") == "ConfirmationRequired":
                    self._emit(stage_event("packgen", "done"))
                else:
                    self._emit(error_event("packgen", str(result_dict.get("error"))))
            except Exception as exc:  # never-raise: stash + emit, swallow nothing else
                tag = exc_tag(exc)
                self._last_pack = {"ok": False, "error": tag}
                self._emit(error_event("packgen", tag))
            finally:
                self._release()

        threading.Thread(target=_worker, name="anast-packgen", daemon=True).start()
        return {"ok": True, "started": True}

    def last_pack_result(self) -> dict[str, object]:
        """The most recent :meth:`pack_init_async` result, for the wizard to fetch.

        The async path returns immediately with ``{"started": True}`` and streams
        PHI-safe ``packgen`` stage/error events back; the full result dict (the
        summary + caveat for ``ConfirmationRequired``, or the pack path +
        ``DRAFT.md`` for a written draft) is held here for the wizard to fetch
        once the ``done``/``error`` event lands. PHI-safe (static template text,
        counts, pack config). Returns ``{"ok": False, "error": "NoResult"}``
        before the first async run. Never raises.
        """
        return deepcopy(self._last_pack) if self._last_pack else {"ok": False, "error": "NoResult"}

    def source_init(
        self,
        example_path: str,
        name: str,
        display: str | None = None,
        confirmed: bool = False,
        out_dir: str | None = None,
    ) -> dict[str, object]:
        """Learn a new structured-export format from one example (wizard backend).

        Mirrors the CLI ``anast source init`` headlessly: resolve the example to
        a single structured file, analyze it LOCALLY, and propose a mapping to the
        canonical model. Without ``confirmed`` this REFUSES
        (``error: ConfirmationRequired``) and writes nothing — returning the
        proposed mapping so the operator sees what they are confirming (the same
        two-step shape as :meth:`pack_init`). With ``confirmed`` it builds the
        mapping, round-trips it against the example to PROVE no column is dropped,
        and only then saves it (owner-only), returning the mapping directory and
        ``MAPPING.md``; a mapping that would drop a column refuses with
        ``error: WouldDropColumns`` and the offending column names.

        PHI rule: ``summary``/``suggestions`` carry column names, inferred type
        labels, counts, and digit/letter-masked shapes only — never a cell value;
        the example path the operator typed is not echoed back. Returns JSON-safe
        data; never raises.
        """
        try:
            from anastomosis.core.sourcelearn import (
                analyze_source,
                build_mapping,
                round_trip,
                save_mapping,
            )
            from anastomosis.sources.learned import user_sources_dir
            from anastomosis.sources.learned.spec import MappingError

            if not isinstance(name, str) or not _PACK_NAME_RE.match(name):
                return {"ok": False, "error": "InvalidSourceName"}

            resolved, resolve_error = self._resolve_example(Path(example_path))
            if resolved is None:
                return {"ok": False, "error": resolve_error}

            try:
                analysis = analyze_source(resolved)
            except MappingError:
                # An unreadable / header-less / column-less example. Surface an
                # enumerated code (not a raw type name); the underlying message
                # may embed the example path, so it is not echoed.
                return {"ok": False, "error": "CannotAnalyze"}
            proposal: dict[str, object] = {
                "format": analysis.fmt.type,
                "columns": len(analysis.fmt.columns),
                "patient_key": analysis.patient_key,
                "encounter_key": analysis.encounter_key,
                "row_scope": analysis.row_scope,
                "summary": list(analysis.summary_lines()),
                "suggestions": [
                    {
                        "source": s.source_path,
                        "target": s.target_path,
                        "transform": s.transform,
                        "confidence": round(s.confidence, 2),
                    }
                    for s in analysis.suggestions
                ],
                "mapped": sum(1 for s in analysis.suggestions if s.target_path is not None),
            }

            if not confirmed:
                return {"ok": False, "error": "ConfirmationRequired", **proposal}

            try:
                spec = build_mapping(analysis, mapping_id=name, display=display or name)
            except MappingError:
                return {"ok": False, "error": "CannotBuildMapping", **proposal}

            report = round_trip(spec, resolved)
            if not report.ok:
                # Mirror the CLI: a LOAD failure (a mapped column's transform
                # choked) is a fixable mapping mistake, distinct from a column
                # that would be dropped. report.error names columns/targets only
                # (no cell value), so it is safe to surface.
                if report.error is not None:
                    return {
                        "ok": False,
                        "error": "MappingLoadFailed",
                        "detail": report.error,
                        **proposal,
                    }
                return {
                    "ok": False,
                    "error": "WouldDropColumns",
                    "dropped": report.dropped_columns,
                    **proposal,
                }

            try:
                base = Path(out_dir) if out_dir is not None else user_sources_dir()
                mapping_dir = save_mapping(spec, base)
            except (MappingError, OSError):
                return {"ok": False, "error": "SaveFailed", **proposal}
            return {
                "ok": True,
                "mapping_dir": str(mapping_dir),
                "mapping_md": (mapping_dir / "MAPPING.md").read_text(encoding="utf-8"),
                "record_count": report.record_count,
                "unmapped": len(spec.unmapped_source_fields),
                **proposal,
            }
        except Exception as exc:
            return self._fail("source_init", exc)

    def _resolve_example(self, example: Path) -> tuple[Path | None, str]:
        """Resolve an example path to one structured file (mirrors the CLI helper).

        Returns ``(file, "")`` on success, else ``(None, code)`` where code is
        ``NoExampleFile`` (nothing of a learnable type) or ``AmbiguousExample`` (a
        directory holding more than one) — never raises.
        """
        if example.is_file():
            return example, ""
        if not example.is_dir():
            return None, "NoExampleFile"
        candidates = sorted(
            p for p in example.iterdir() if p.is_file() and p.suffix.lower() in _LEARNABLE_SUFFIXES
        )
        if len(candidates) == 1:
            return candidates[0], ""
        return (None, "NoExampleFile") if not candidates else (None, "AmbiguousExample")

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
        force: bool = False,
        pack_dirs: list[str] | None = None,
        trust_new: bool = False,
    ) -> dict[str, object]:
        """Drive the shared pipeline core, emitting stage/progress events.

        Returns the final roll-up dict (also emitted as a ``done`` event), with
        a ``patients`` key carrying the per-patient detail for local display
        (names/DOB/note counts — never emitted as events; see
        :meth:`last_run_summary`). Any failure becomes ``{"ok": False, "error":
        <type-or-diagnosis>}`` plus an ``error`` event. The ``busy`` guard
        rejects a second concurrent run.

        ``force`` re-renders documents that already exist; ``pack_dirs`` makes
        extra pack directories available and ``trust_new`` records (trusts)
        their current code hash on first use — the same backend levers the CLI
        exposes, no longer hard-coded off. Deliverer flags
        (``archive``/``bundle``/``ccda``) write into sibling subdirectories of
        ``out_dir`` since the GUI has one output-dir field.
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
                force=force,
                pack_dirs=pack_dirs,
                trust_new=trust_new,
            )
        finally:
            self._release()

    def run_pipeline_async(
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
        force: bool = False,
        pack_dirs: list[str] | None = None,
        trust_new: bool = False,
    ) -> dict[str, object]:
        """Run the pipeline on a daemon thread (the GUI stays responsive).

        Acquires the busy flag SYNCHRONOUSLY before returning, so two quick
        clicks can't both get ``{"started": True}`` (the worker then runs the
        locked body and releases in ``finally``). Returns ``{"ok": True,
        "started": True}`` on success or ``{"ok": False, "error": "Busy"}`` if a
        run is already in flight. The result arrives as
        ``stage``/``progress``/``done``/``error`` events; the per-patient detail
        is fetched after ``done`` via :meth:`last_run_summary` (the events stay
        count-only).
        """
        if not self._acquire():
            return {"ok": False, "error": "Busy"}

        def _worker() -> None:
            try:
                self._run_pipeline_locked(
                    export_dir=export_dir,
                    out_dir=out_dir,
                    pack=pack,
                    source=source,
                    sections=sections or {},
                    qa=qa,
                    archive=archive,
                    bundle=bundle,
                    ccda=ccda,
                    force=force,
                    pack_dirs=pack_dirs,
                    trust_new=trust_new,
                )
            finally:
                self._release()

        threading.Thread(target=_worker, name="anast-pipeline", daemon=True).start()
        return {"ok": True, "started": True}

    # --- the migration run (EHR-to-EHR; PF→Tebra is one instance) -----------

    def run_migration(
        self,
        export_dir: str,
        out_dir: str,
        source: str,
        destination: str,
        render: str = "neutral",
        sections: dict[str, bool] | None = None,
        qa: bool = True,
        force: bool = False,
        pack_dirs: list[str] | None = None,
        trust_new: bool = False,
    ) -> dict[str, object]:
        """Drive the shared migration core, emitting stage/progress events.

        Mirrors :meth:`run_pipeline` exactly for the contract: never raises (a
        failure is ``{"ok": False, "error": <type-or-diagnosis>}`` plus an
        ``error`` event), busy-guarded, PHI-safe events only, and the per-patient
        roll-up stored for :meth:`last_run_summary`. The resolved transit map
        rides the return value (``route``) so the wizard can draw the chosen
        route the migration would take. Returns ``{"ok": True, **rollup,
        "route": {...}, "patients": [...]}``.
        """
        if not self._acquire():
            return {"ok": False, "error": "Busy"}
        try:
            return self._run_migration_locked(
                export_dir=export_dir,
                out_dir=out_dir,
                source=source,
                destination=destination,
                render=render,
                sections=sections or {},
                qa=qa,
                force=force,
                pack_dirs=pack_dirs,
                trust_new=trust_new,
            )
        finally:
            self._release()

    def run_migration_async(
        self,
        export_dir: str,
        out_dir: str,
        source: str,
        destination: str,
        render: str = "neutral",
        sections: dict[str, bool] | None = None,
        qa: bool = True,
        force: bool = False,
        pack_dirs: list[str] | None = None,
        trust_new: bool = False,
    ) -> dict[str, object]:
        """Run the migration on a daemon thread (the GUI stays responsive).

        Mirrors :meth:`run_pipeline_async`: acquires the busy flag SYNCHRONOUSLY
        so two quick clicks can't both start, returns ``{"ok": True, "started":
        True}`` (or ``{"ok": False, "error": "Busy"}``), and streams the result
        as ``stage``/``progress``/``done``/``error`` events. The per-patient
        detail and the route are fetched after ``done`` via
        :meth:`last_run_summary` (the route also rides the synchronous return of
        :meth:`run_migration`; the async path's done event carries counts only).
        """
        if not self._acquire():
            return {"ok": False, "error": "Busy"}

        def _worker() -> None:
            try:
                self._run_migration_locked(
                    export_dir=export_dir,
                    out_dir=out_dir,
                    source=source,
                    destination=destination,
                    render=render,
                    sections=sections or {},
                    qa=qa,
                    force=force,
                    pack_dirs=pack_dirs,
                    trust_new=trust_new,
                )
            finally:
                self._release()

        threading.Thread(target=_worker, name="anast-migration", daemon=True).start()
        return {"ok": True, "started": True}

    # --- internals ----------------------------------------------------------

    def _run_migration_locked(
        self,
        *,
        export_dir: str,
        out_dir: str,
        source: str,
        destination: str,
        render: str,
        sections: dict[str, bool],
        qa: bool,
        force: bool,
        pack_dirs: list[str] | None,
        trust_new: bool,
    ) -> dict[str, object]:
        from anastomosis.core.commands import summarize_patients
        from anastomosis.core.migrate import (
            RENDER_CCDA_STANDARD,
            MigrationCommand,
            run_migration,
        )
        from anastomosis.pipeline import PipelineError

        rollup: dict[str, int] = {}
        # Clear stale detail up front so a failed run never leaves the previous
        # run's patients fetchable.
        self._last_patients = []

        def _on_event(event: StageEvent) -> None:
            stage = _STAGE_MAP.get(event.stage)
            if stage is None:
                return  # the detect stage has no rail of its own
            self._emit(stage_event(stage, "start"))
            self._emit(progress_event(stage, **event.counts))
            self._emit(stage_event(stage, "done"))
            rollup.update(event.counts)

        try:
            result = run_migration(
                MigrationCommand(
                    export_dir=Path(export_dir),
                    out_dir=Path(out_dir),
                    source=source,
                    destination=destination,
                    render=render,
                    pack_dirs=tuple(Path(p) for p in pack_dirs or []),
                    trust_new=trust_new,
                    force=force,
                    sections=sections,
                    qa=qa,
                ),
                on_event=_on_event,
            )
        except PipelineError as exc:
            self._emit(error_event(_failed_stage(str(exc)), str(exc)))
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # any non-migration crash: type name only, no PHI
            return self._fail("run_migration", exc)

        # The structured C-CDA payload count rides the roll-up (the headline of a
        # migration: how many patients' charts moved as importable C-CDA).
        rollup["ccda_patients"] = result.ccda_export.counts["patients"]

        # Per-patient detail rides the RETURN value (and last_run_summary), never
        # an event. In neutral/pack mode the pipeline result yields it; in
        # ccda-standard mode (no pipeline) it is derived from the loaded records
        # and the per-patient view (one document per patient).
        if result.render_mode == RENDER_CCDA_STANDARD:
            patients = self._ccda_standard_patients(result)
        else:
            assert result.pipeline is not None  # pack mode always carries a pipeline
            patients = [
                {
                    "patient_id": s.patient_id,
                    "display_name": s.display_name,
                    "birth_date": s.birth_date,
                    "encounters": s.encounters,
                    "documents": s.documents,
                }
                for s in summarize_patients(result.pipeline)
            ]
        self._last_patients = patients
        route = _transit_to_dict(result.transit)
        self._emit(done_event(**rollup))
        return {"ok": True, **rollup, "route": route, "patients": patients}

    @staticmethod
    def _ccda_standard_patients(result: object) -> list[dict[str, object]]:
        """Per-patient roll-up for ccda-standard mode (no pipeline result).

        The standard-view render has no Jinja pack and thus no
        :class:`PipelineResult` to feed :func:`summarize_patients`, but the
        migration retains the canonical records, so the same per-patient detail
        is available here as in pack mode: display name, DOB, encounter count,
        and one C-CDA-view document per patient (this mode renders exactly one
        whole-patient PDF each). PHI: LOCAL display only — these values ride the
        return value / :meth:`last_run_summary`, never an event or a log.
        """
        from anastomosis.core.migrate import MigrationResult

        assert isinstance(result, MigrationResult)
        return [
            {
                "patient_id": record.patient.id,
                "display_name": record.patient.display_name,
                "birth_date": (
                    record.patient.birth_date.isoformat() if record.patient.birth_date else None
                ),
                "encounters": len(record.encounters),
                "documents": 1,  # ccda-standard renders one whole-patient PDF
            }
            for record in result.records
        ]

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
        force: bool = False,
        pack_dirs: list[str] | None = None,
        trust_new: bool = False,
    ) -> dict[str, object]:
        from anastomosis.core.commands import (
            DeliveryCommand,
            PipelineCommand,
            run_pipeline_command,
            summarize_patients,
        )
        from anastomosis.pipeline import PipelineError

        out = Path(out_dir)
        rollup: dict[str, int] = {}
        # Clear stale detail up front so a failed run never leaves the previous
        # run's patients fetchable.
        self._last_patients = []

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
                    pack_dirs=tuple(Path(p) for p in pack_dirs or []),
                    force=force,
                    trust_new=trust_new,
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

        # Per-patient detail rides the RETURN value (and last_run_summary), never
        # an event: names/DOB are local display only, the event stream is counts.
        # Store before emitting `done` so the dashboard's done handler can fetch
        # it immediately.
        patients: list[dict[str, object]] = [
            {
                "patient_id": s.patient_id,
                "display_name": s.display_name,
                "birth_date": s.birth_date,
                "encounters": s.encounters,
                "documents": s.documents,
            }
            for s in summarize_patients(result.pipeline)
        ]
        self._last_patients = patients
        self._emit(done_event(**rollup))
        return {"ok": True, **rollup, "patients": patients}

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

    def _pack_readiness(self, transit: TransitMap) -> dict[str, object] | None:
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

        name = transit.destination
        browser = next(
            (opt for opt in transit.options if opt.kind == RouteKind.BROWSER),
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
    def _latest_run(tracking: TrackingDB) -> dict[str, object] | None:
        """The most-recent run row (by started_at), as a JSON-safe dict, or None.

        Reuses :meth:`TrackingDB.latest_run_id` + :meth:`TrackingDB.run_info`
        (the upload console shows one current run). All values are log-safe: a
        run id, a destination name, ISO timestamps, and an abort TYPE name —
        never a patient value.
        """
        run_id = tracking.latest_run_id()
        if run_id is None:
            return None
        return {"run_id": run_id, **tracking.run_info(run_id)}

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


def _transit_to_dict(transit: TransitMap) -> dict[str, object]:
    """Serialize a :class:`TransitMap` to a JSON-safe dict for the GUI."""
    options = [
        {
            "kind": opt.kind.value,
            "viable": opt.viable,
            "why": opt.why,
            "requires": list(opt.requires),
        }
        for opt in transit.options
    ]
    chosen = transit.chosen
    return {
        "destination": transit.destination,
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
