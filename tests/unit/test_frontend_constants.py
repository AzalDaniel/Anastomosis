# AI-assisted: written with Claude agents under the author's direction and review; see DESIGN.md.
"""Pin frontend/backend constant parity.

The browser UI used to hand-mirror backend constants;
``GuiController.gui_config()`` is the Python-canonical source the JS refreshes
from on load. The JS keeps same-valued FALLBACKS for the api-less browser
preview — these tests pin (a) each fallback to its Python constant, so
neither side can drift alone, and (b) the ``gui_config()`` payload itself
to the canonical values and complete coverage.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from anastomosis.core.upload_command import DEFAULT_MAX_ATTEMPTS
from anastomosis.deliver.browser.states import UploadState
from anastomosis.gui.controller import _STAGE_MAP, _STAGE_RAIL, _STATE_GROUPS, GuiController

_WEB = Path(__file__).resolve().parents[2] / "src" / "anastomosis" / "gui" / "web"
_CONSOLE_JS = _WEB / "console.js"
_APP_JS = _WEB / "app.js"


class _NullSink:
    def emit(self, event: dict[str, object]) -> None:  # pragma: no cover - unused
        pass


def test_frontend_backend_retry_constants_do_not_drift() -> None:
    """The JS console's fallback retry budget must equal the Python
    ``DEFAULT_MAX_ATTEMPTS`` — the value the upload engine actually enforces.
    (The live value is refreshed from ``gui_config()``; the fallback covers
    the api-less preview and must not drift either.)
    """
    source = _CONSOLE_JS.read_text(encoding="utf-8")
    match = re.search(r"^let DEFAULT_MAX_ATTEMPTS\s*=\s*(\d+)\s*;", source, re.MULTILINE)
    assert match is not None, (
        f"could not find 'let DEFAULT_MAX_ATTEMPTS = <int>;' in {_CONSOLE_JS} — "
        "if the constant moved, update this test to the new source of truth."
    )
    assert int(match.group(1)) == DEFAULT_MAX_ATTEMPTS, (
        f"frontend/backend retry-budget drift: console.js fallback has "
        f"{match.group(1)}, core.upload_command.DEFAULT_MAX_ATTEMPTS is "
        f"{DEFAULT_MAX_ATTEMPTS}. Change both together."
    )
    # And the console actually refreshes from the canonical endpoint.
    assert "gui_config" in source, "console.js no longer refreshes from gui_config()"


def test_frontend_stage_rail_fallback_does_not_drift() -> None:
    """app.js's fallback RAIL must equal the Python-canonical _STAGE_RAIL."""
    source = _APP_JS.read_text(encoding="utf-8")
    match = re.search(r"^let RAIL\s*=\s*(\[[^\]]*\])\s*;", source, re.MULTILINE)
    assert match is not None, f"could not find 'let RAIL = [...];' in {_APP_JS}"
    js_rail = json.loads(match.group(1))
    assert js_rail == list(_STAGE_RAIL), (
        f"stage-rail drift: app.js fallback {js_rail} != controller._STAGE_RAIL "
        f"{list(_STAGE_RAIL)}. Change both together."
    )
    assert "gui_config" in source, "app.js no longer refreshes from gui_config()"


def test_gui_config_serves_the_canonical_values() -> None:
    cfg = GuiController(_NullSink()).gui_config()
    assert cfg["ok"] is True
    assert cfg["max_attempts"] == DEFAULT_MAX_ATTEMPTS
    assert cfg["stage_rail"] == list(_STAGE_RAIL)
    assert cfg["state_groups"] == {g: list(s) for g, s in _STATE_GROUPS.items()}
    # JSON-safe (the pywebview bridge serializes the return value).
    json.dumps(cfg)


def test_gui_config_state_groups_cover_every_upload_state_exactly_once() -> None:
    """The pending/active/terminal buckets must partition the FULL UploadState
    enum — a new state added to the engine without a bucket would silently
    vanish from the console's counters.
    """
    bucketed = [state for states in _STATE_GROUPS.values() for state in states]
    assert sorted(bucketed) == sorted(s.value for s in UploadState), (
        "state-group drift vs the UploadState enum: "
        f"bucketed={sorted(bucketed)} enum={sorted(s.value for s in UploadState)}"
    )
    assert len(bucketed) == len(set(bucketed)), "a state appears in two buckets"


def test_stage_map_values_are_members_of_the_rail() -> None:
    """Every pipeline-stage -> rail mapping must land on a rail the dashboard
    actually draws."""
    assert set(_STAGE_MAP.values()) <= set(_STAGE_RAIL)


def test_gui_api_exposes_gui_config() -> None:
    from anastomosis.gui.controller import GuiApi

    api = GuiApi(GuiController(_NullSink()))
    cfg = api.gui_config()
    assert cfg["ok"] is True and cfg["max_attempts"] == DEFAULT_MAX_ATTEMPTS
