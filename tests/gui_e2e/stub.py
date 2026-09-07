"""The pywebview bridge stub the GUI lane injects, and its canned returns.

Rebuilds the ``window.pywebview.api``/``window.anastEvent`` seam headless.
:func:`api_surface` reads the method list off the REAL ``GuiApi`` class;
:func:`canned_returns` supplies one value per method (real controller
where cheap and offline, hand-written otherwise); :func:`init_script`
bakes both into the init-script payload. PHI: every hand-written value is
synthetic (``feedface-`` ids, fake names, ISO dates, opaque keys, counts,
exception TYPE names).
"""

from __future__ import annotations

import json
from typing import Any

from anastomosis.core.model_paths import canonical_target_paths
from anastomosis.gui.controller import GuiApi, GuiController
from anastomosis.gui.shared import _group_states

__all__ = [
    "BRIDGE_LATE",
    "BRIDGE_NONE",
    "BRIDGE_READY",
    "CANNED_DESTINATION",
    "CANNED_PATIENTS",
    "CANNED_SOURCE",
    "CANNED_SOURCE_SUGGESTIONS",
    "api_surface",
    "canned_returns",
    "init_script",
]

# The bridge lifecycles a test can ask for (see conftest's ``gui`` fixture).
#: The api is installed before the document loads (the ordinary case).
BRIDGE_READY = "ready"
#: No api at load; the test installs it later to replay pywebview's LATE attach.
BRIDGE_LATE = "late"
#: No api at all, ever — the plain-browser preview the pages must degrade to.
BRIDGE_NONE = "none"

#: The destination whose real transit map + browser-pack readiness back the
#: wizard's canned ``destination_status``. Chosen because it is the one registry
#: entry with a viable browser route, so the readiness chip has live data.
CANNED_DESTINATION = "tebra"

#: The source format the canned ``detect`` claims to have sniffed.
CANNED_SOURCE = "pf-tebra"

#: The per-patient roll-up ``last_run_summary`` hands back for local display.
#: Synthetic: feedface- ids, a placeholder name, an ISO date.
CANNED_PATIENTS: tuple[dict[str, object], ...] = (
    {
        "patient_id": "feedface-0000-4000-8000-000000000001",
        "display_name": "Testpatient Alpha",
        "birth_date": "1970-01-01",
        "encounters": 2,
        "documents": 3,
    },
    {
        "patient_id": "feedface-0000-4000-8000-000000000002",
        "display_name": "Testpatient Beta",
        "birth_date": "1968-05-04",
        "encounters": 1,
        "documents": 1,
    },
)

#: Per-state item counts for the canned ledger view. Grouped through the SAME
#: ``_group_states`` the controller uses, so the console's counter tiles are
#: checked against the real bucketing rather than a hand-typed copy.
CANNED_LEDGER_COUNTS: dict[str, int] = {"pending": 4, "uploading": 1, "completed": 2, "failed": 1}

#: The learn-a-source proposal the Teach lane opens on — and it is WRONG on
#: purpose, in the way the deterministic scorer really is wrong: ``VisitId`` is
#: a visit IDENTIFIER, and column-name similarity has aimed it at the visit
#: DATE and set it to be read as a date. Correcting that in place, and proving
#: what the correction puts on the wire, is what the Teach lane drives.
#:
#: Synthetic by construction, and by the same rule the real proposal follows:
#: column names, the profiler's inferred type labels, and its letter-for-letter
#: digit-for-digit masks. No cell value appears here, because none may.
CANNED_SOURCE_SUGGESTIONS: tuple[dict[str, object], ...] = (
    {
        "source": "MRN",
        "target": None,
        "transform": "strip",
        "confidence": 0.0,
        "inferred_type": "text",
        "sample_shape": "NNNNNN",
    },
    {
        "source": "VisitId",
        "target": "encounter.date_of_service",
        "transform": "parse_date",
        "confidence": 0.44,
        "inferred_type": "text",
        "sample_shape": "NN-NNN",
    },
    {
        "source": "VisitDate",
        "target": None,
        "transform": "strip",
        "confidence": 0.31,
        "inferred_type": "date",
        "sample_shape": "NN/NN/NNNN",
    },
    {
        "source": "Complaint",
        "target": None,
        "transform": "strip",
        "confidence": 0.22,
        "inferred_type": "text",
        "sample_shape": "Aaaaa aaaa",
    },
)

#: The canned ledger's latest run row (ids, a destination name, ISO stamps).
CANNED_RUN: dict[str, object] = {
    "run_id": "run-feedface01",
    "destination": CANNED_DESTINATION,
    "started_at": "2026-08-03T10:00:00+00:00",
    "finished_at": None,
    "aborted_reason": None,
}


class _NullSink:
    """The controller needs a sink; the canned queries never emit."""

    def emit(self, event: dict[str, object]) -> None:  # pragma: no cover - unused
        pass


def api_surface() -> list[str]:
    """The public ``GuiApi`` method names, sorted — the stub's whole
    surface. Read off the class rather than hand-listed, so a renamed or
    removed method changes this list (and fails the drift test) instead
    of leaving a page calling into nothing."""
    return sorted(name for name in dir(GuiApi) if not name.startswith("_"))


def canned_returns() -> dict[str, object]:
    """One JSON-safe return value per ``GuiApi`` method: the five cheap
    read-only queries are answered by the REAL controller so the fixture
    cannot drift from the shape pages parse; the rest are minimal payloads
    matching the documented contract. Raises loudly on a real query
    failure."""
    controller = GuiController(_NullSink())
    live = {
        "gui_config": controller.gui_config(),
        "info": controller.info(),
        "routes": controller.routes(),
        "destination_status": controller.destination_status(CANNED_DESTINATION),
        "upload_safety_notice": controller.upload_safety_notice(),
    }
    for name, payload in live.items():
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise RuntimeError(
                f"the GUI lane's canned {name}() came back not-ok ({payload!r}). "
                "The fixture is derived from the real controller on purpose — fix the "
                "controller (or the registry it reads) rather than hand-writing a payload."
            )

    canned: dict[str, object] = dict(live)
    canned.update(
        {
            # detect(): the picker hint for step 1 of the wizard.
            "detect": {"ok": True, "source": CANNED_SOURCE},
            # pack_freshness(): one stale destination so the dashboard toast renders.
            "pack_freshness": {
                "ok": True,
                "stale": [
                    {
                        "destination": CANNED_DESTINATION,
                        "selectors_date": "2026-01-01",
                        "evidence_date": "2026-06-11",
                        "gap_days": 161,
                        "advice": f"anast destination init {CANNED_DESTINATION} --validate",
                    }
                ],
                "checked": 1,
                "stale_after_days": 90,
            },
            # last_run_summary(): PHI-by-design rows, rendered locally only.
            "last_run_summary": {"ok": True, "patients": [dict(p) for p in CANNED_PATIENTS]},
            # The two learn-a-thing wizards' stashed results. Both open on the
            # ConfirmationRequired branch — the EXPECTED outcome of the analyze
            # step, and the one that drives the confirm-then-write gate.
            "last_pack_result": {
                "ok": False,
                "error": "ConfirmationRequired",
                "summary": [
                    "3 samples analyzed",
                    "9 recurring headings kept as static template text",
                ],
                "caveat": "static text recurring across all 3 samples was kept",
            },
            "last_source_result": {
                "ok": False,
                "error": "ConfirmationRequired",
                "format": "csv",
                "columns": len(CANNED_SOURCE_SUGGESTIONS),
                "patient_key": "MRN",
                "encounter_key": None,
                "row_scope": "encounter",
                "mapped": 1,
                "suggestions": [dict(row) for row in CANNED_SOURCE_SUGGESTIONS],
                "summary": ["4 columns analyzed", "3 unmapped columns ride extensions"],
                # The closed set every correction chooser is filled from, read
                # off the real canonical model rather than hand-listed: a field
                # renamed there changes what the page offers, here too.
                "targets": sorted(canonical_target_paths()),
            },
            # The upload console's read-only ledger views.
            "upload_status": {
                "ok": True,
                "counts": dict(CANNED_LEDGER_COUNTS),
                "groups": _group_states(CANNED_LEDGER_COUNTS),
                "total": sum(CANNED_LEDGER_COUNTS.values()),
                "run": dict(CANNED_RUN),
                "attempts_histogram": {"1": 6, "2": 2},
                "error_type_histogram": {"PlaywrightTimeoutError": 1},
            },
            "upload_item_keys": {
                "ok": True,
                "item_keys": ["enc-0001:0f1e2d3c4b5a", "enc-0002:9a8b7c6d5e4f"],
                "count": 2,
                "total": 2,
            },
            "upload_manifest_preview": {"ok": True, "renderable": 7, "total_bytes": 123456},
            # The busy-guarded drives: every one returns immediately.
            "upload_start": {"ok": True, "started": True},
            "upload_stop": {"ok": True, "stopping": True},
            "run_pipeline_async": {"ok": True, "started": True},
            "run_migration_async": {"ok": True, "started": True},
            "pack_init_async": {"ok": True, "started": True},
            "source_init_async": {"ok": True, "started": True},
        }
    )
    return canned


# The injected stub. Installed with ``add_init_script`` so it exists before the
# page's own scripts parse — the position pywebview's own injection occupies.
# Every call is recorded on ``window.__anastCalls`` (method + arguments) so a
# test can assert WHAT the page asked the controller for, and every return value
# is round-tripped through JSON exactly as the real bridge serializes it.
_INIT_TEMPLATE = """
(() => {
  "use strict";
  const METHODS = __METHODS__;
  const CANNED = __CANNED__;
  const MODE = __MODE__;

  window.__anastCalls = [];

  function makeApi() {
    const api = {};
    for (const name of METHODS) {
      api[name] = function () {
        const args = Array.prototype.slice.call(arguments);
        window.__anastCalls.push({ method: name, args: args });
        const value = Object.prototype.hasOwnProperty.call(CANNED, name) ? CANNED[name] : null;
        // The real bridge JSON-serializes every return value; do the same so a
        // page can never lean on object identity the live bridge would not give.
        return Promise.resolve(JSON.parse(JSON.stringify(value)));
      };
    }
    return api;
  }

  // pywebview's LATE attach, on demand: install the api object and announce it
  // with the `pywebviewready` window event, exactly as the real bridge does.
  window.__installAnastBridge = function () {
    window.pywebview = { api: makeApi() };
    window.dispatchEvent(new Event("pywebviewready"));
  };

  if (MODE === "ready") {
    window.pywebview = { api: makeApi() };
    // The announcement still lands after the document is up — the pages must
    // tolerate BOTH orderings, which is the whole point of the ready event.
    window.addEventListener("load", function () {
      window.dispatchEvent(new Event("pywebviewready"));
    });
  }
})();
"""


def init_script(mode: str = BRIDGE_READY, canned: dict[str, Any] | None = None) -> str:
    """The init script installing the bridge stub for ``mode``. ``canned``
    merges over the default return values, so a test can ask for the one
    payload it cares about without rebuilding the whole surface."""
    if mode not in (BRIDGE_READY, BRIDGE_LATE, BRIDGE_NONE):
        raise ValueError(f"unknown bridge mode {mode!r}")
    payload = canned_returns()
    if canned:
        payload.update(canned)
    return (
        _INIT_TEMPLATE.replace("__METHODS__", json.dumps(api_surface()))
        .replace("__CANNED__", json.dumps(payload))
        .replace("__MODE__", json.dumps(mode))
    )
