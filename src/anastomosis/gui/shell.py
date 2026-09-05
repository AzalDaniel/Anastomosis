"""The pywebview shell: the ONLY module that touches webview (lazy-imported).

Everything testable lives in :mod:`anastomosis.gui.controller`; this file is
the thin adapter. :func:`launch` lazily imports ``webview`` (missing install
raises, naming the ``gui`` extra), builds a window over the bundled
``web/index.html``, and wires a sink marshaling events through
``window.evaluate_js`` (thread-safe, so the daemon worker may call it).
"""

from __future__ import annotations

import json
import logging
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

from anastomosis.core.logutil import exc_tag
from anastomosis.gui.controller import GuiApi, GuiController

__all__ = ["launch"]

logger = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).resolve().parent / "web"
_INDEX = _WEB_DIR / "index.html"
_WINDOW_TITLE = "Anastomosis"

# Read as a value we hand pywebview, never exported to the runtime — see
# _webview2_user_data_folder (RULES.md 73).
_WEBVIEW2_USER_DATA_ENV = "WEBVIEW2_USER_DATA_FOLDER"
_WEBVIEW2_USER_DATA_PARTS = ("Anastomosis", "WebView2")

# Diagnostics-only opt-in: the port WebView2's Chromium should open for remote
# debugging, and the pywebview setting that is the only route to it.
_REMOTE_DEBUG_PORT_ENV = "ANAST_GUI_REMOTE_DEBUGGING_PORT"
_REMOTE_DEBUG_SETTING = "REMOTE_DEBUGGING_PORT"

# Surfaced on whatever page is up when the operator tries to close mid-run
# (see launch); PHI-free by construction (a fixed advisory).
_JOB_RUNNING_NOTICE = "a job is still running — stop it before closing"


class _WindowSink:
    """An :class:`~anastomosis.gui.controller.EventSink` backed by a window.

    Attached after construction (until then ``emit`` is a no-op). Each event
    is marshalled as ``anastEvent(<json>)`` — ``json.dumps`` keeps it a
    literal the browser parses, never interpolated JS source.
    """

    def __init__(self) -> None:
        self._window: Any = None

    def attach(self, window: Any) -> None:
        self._window = window

    def emit(self, event: dict[str, object]) -> None:
        if self._window is None:
            return
        payload = json.dumps(event)
        self._window.evaluate_js(f"window.anastEvent({payload})")  # pragma: no cover - needs window


def _warn_job_running(window: Any) -> None:  # pragma: no cover - needs window
    """Best-effort: surface the close-barrier notice on whatever page is up.

    Routed through ``AnastShell.logEvent`` (safe no-op without a log strip),
    bypassing the flow-guarded ``anastEvent`` channel. The real guarantee is
    the ``closing`` handler's veto; a failure here is logged (type only) and swallowed.
    """
    notice = json.dumps(_JOB_RUNNING_NOTICE)  # a JS string literal, never interpolated source
    js = (
        "window.AnastShell && window.AnastShell.logEvent &&"
        f" window.AnastShell.logEvent({{kind: 'error', msg: {notice}}})"
    )
    try:
        window.evaluate_js(js)
    except Exception as exc:
        logger.warning("close-barrier notice failed (%s)", exc_tag(exc))


def _claim_windows_taskbar_identity() -> None:
    """Set the process AppUserModelID before any window exists (Windows only).

    Without it, Windows derives one from the host process path and the
    taskbar groups the app under a generic Python identity. Must match the
    id the installer stamps on the Start-menu shortcut. Cosmetic, never fatal.
    """
    import sys

    if sys.platform == "win32":  # pragma: no cover - windows-only branch
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("AzalDaniel.Anastomosis")
        except Exception as exc:
            logger.warning("taskbar identity not set (%s)", exc_tag(exc))


def _webview2_user_data_folder() -> str | None:
    """Contract: the WebView2 profile folder for ``webview.start``'s
    ``storage_path`` (Windows only). pywebview assigns it to
    ``CoreWebView2CreationProperties.UserDataFolder`` itself — WebView2's own
    ``WEBVIEW2_USER_DATA_FOLDER`` env var is never consulted (RULES.md 73), so
    this returns a value instead of exporting one.

    Returns the operator's override if set, else ``%LOCALAPPDATA%\\Anastomosis\\
    WebView2`` (created); ``None`` off Windows or on failure, so pywebview's
    own default stands. Failure is warned (type name only), never fatal.
    """
    import os
    import sys

    # Positive test, not `!= "win32"` early-return: mypy narrows `sys.platform`
    # statically, and an early return would make everything after unreachable.
    if sys.platform == "win32":
        try:
            override = os.environ.get(_WEBVIEW2_USER_DATA_ENV)
            folder = (
                Path(override)
                if override
                else Path(os.environ["LOCALAPPDATA"], *_WEBVIEW2_USER_DATA_PARTS)
            )
            # pywebview is handed a path, not a promise: create it here so a
            # failure surfaces as this warning, not deep inside the backend.
            folder.mkdir(parents=True, exist_ok=True)
            return str(folder)
        except Exception as exc:
            logger.warning("webview2 user-data folder not set (%s)", exc_tag(exc))
    return None


def _apply_remote_debugging_port(settings: MutableMapping[str, Any]) -> None:
    """Contract: DIAGNOSTICS ONLY. Routes ``ANAST_GUI_REMOTE_DEBUGGING_PORT``
    into pywebview's ``REMOTE_DEBUGGING_PORT`` setting for
    ``packaging/smoke_windows.py``'s CDP attach; unset (default) is a no-op
    and must stay that way in normal operation — an open port gives any
    process on the machine control of the window.

    pywebview overwrites ``AdditionalBrowserArguments`` on every launch, so
    WebView2's own ``WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS`` is always
    ignored; ``REMOTE_DEBUGGING_PORT`` is the one route that survives. Warn,
    never fail, on an invalid value or a missing setting key.
    """
    import os

    raw = os.environ.get(_REMOTE_DEBUG_PORT_ENV)
    if not raw:
        return
    if not raw.isdigit() or not (1 <= int(raw) <= 65535):
        logger.warning("remote debugging port ignored (not a valid port number)")
        return
    try:
        settings[_REMOTE_DEBUG_SETTING] = int(raw)
    except Exception as exc:
        logger.warning("remote debugging port not set (%s)", exc_tag(exc))


def launch(debug: bool = False) -> None:  # pragma: no cover - needs webview + a display
    """Open the desktop GUI window. Requires the ``gui`` extra (pywebview)."""
    try:
        import webview
    except ImportError as exc:
        raise RuntimeError("pywebview is required for the GUI — install anastomosis[gui]") from exc

    _claim_windows_taskbar_identity()
    storage_path = _webview2_user_data_folder()
    _apply_remote_debugging_port(webview.settings)

    sink = _WindowSink()
    controller = GuiController(sink)
    # Bind the FACADE, not the raw controller: only its async/guarded methods
    # are reachable from JS, so a page can't invoke a heavy sync call and freeze the bridge.
    window = webview.create_window(
        _WINDOW_TITLE,
        url=_INDEX.as_uri(),
        js_api=GuiApi(controller),
        width=1100,
        height=820,
        min_size=(820, 600),
    )
    if window is None:
        # pywebview types this Window | None; a refusal here means nothing
        # below can work — fail loudly now rather than crash later on first attribute access.
        raise RuntimeError("webview.create_window returned no window — GUI startup failed")
    sink.attach(window)

    # Close-barrier: pywebview 5.x cancels close when `closing` returns False;
    # daemon workers keep this graceful (OS can still force-kill), not a hard lock.
    def _on_closing() -> bool:
        if not controller.busy:
            return True  # nothing running — allow the close
        _warn_job_running(window)
        return False  # veto: pywebview cancels the close on a False return

    window.events.closing += _on_closing
    # None means pywebview's own default stands (see _webview2_user_data_folder).
    if storage_path is None:
        webview.start(debug=debug)
    else:
        webview.start(debug=debug, storage_path=storage_path)
