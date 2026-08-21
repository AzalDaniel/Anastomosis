"""The pywebview shell: the ONLY module that touches webview (lazy-imported).

Everything testable lives in :mod:`anastomosis.gui.controller`; this file is
the thin webview adapter, kept under ~80 lines and marked
``# pragma: no cover`` on the lines that need a real window, exactly as
:mod:`anastomosis.deliver.browser.cdp` does for Playwright.

:func:`launch` lazily imports ``webview``; a missing install raises a
``RuntimeError`` naming the ``anastomosis[gui]`` extra (the optional-dependency
error style used across the toolkit). It builds a window over the bundled,
network-free ``web/index.html``, exposes a :class:`GuiApi` wrapping the
:class:`GuiController` as the ``js_api`` (so the front end calls
``pywebview.api.*``), and wires a sink that marshals each event into
``window.evaluate_js("anastEvent(...)")`` — pywebview's ``evaluate_js`` is
thread-safe, so the controller's daemon worker may call it.
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

# An operator's explicit override for the WebView2 profile folder. It is read
# as a VALUE we hand pywebview, not exported to the runtime — the runtime never
# reads it in a pywebview app (see :func:`_webview2_user_data_folder`).
_WEBVIEW2_USER_DATA_ENV = "WEBVIEW2_USER_DATA_FOLDER"
_WEBVIEW2_USER_DATA_PARTS = ("Anastomosis", "WebView2")

# Diagnostics-only opt-in: the port WebView2's Chromium should open for remote
# debugging, and the pywebview setting that is the only route to it.
_REMOTE_DEBUG_PORT_ENV = "ANAST_GUI_REMOTE_DEBUGGING_PORT"
_REMOTE_DEBUG_SETTING = "REMOTE_DEBUGGING_PORT"

# The close-barrier notice surfaced on whatever page is up when the operator
# tries to close the window mid-run (see :func:`launch`). PHI-free by
# construction (a fixed advisory).
_JOB_RUNNING_NOTICE = "a job is still running — stop it before closing"


class _WindowSink:
    """An :class:`~anastomosis.gui.controller.EventSink` backed by a window.

    The window is attached *after* construction (the controller is built first,
    because it backs the window's ``js_api``, and the window is built next). Until
    then ``emit`` is a no-op. Once attached, each JSON-safe event dict is
    marshalled into a single ``anastEvent(<json>)`` call — ``json.dumps`` keeps
    the payload a literal the browser parses, never interpolated JS source.
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

    Routed through the shared shell log surface (``AnastShell.logEvent``), which
    is a safe no-op on a page without a log strip — so the notice reaches the
    current page regardless of which flow it owns, WITHOUT going through the
    flow-guarded ``anastEvent`` channel. Best-effort only: the veto (the ``False``
    return from the ``closing`` handler) is the real guarantee, so an
    ``evaluate_js`` failure here is logged (type name only) and swallowed.
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

    Without an explicit id, Windows derives one from the host process path, so
    the taskbar may group the app under a generic Python identity and Start-menu
    pinning misbehaves. The id must match the ``AppUserModelID`` the installer
    stamps on the Start-menu shortcut (packaging/anastomosis.iss) — one stable
    identity across shortcut, taskbar, and running window. A failure here is
    cosmetic, never fatal.
    """
    import sys

    if sys.platform == "win32":  # pragma: no cover - windows-only branch
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("AzalDaniel.Anastomosis")
        except Exception as exc:
            logger.warning("taskbar identity not set (%s)", exc_tag(exc))


def _webview2_user_data_folder() -> str | None:
    """The WebView2 profile folder to hand ``webview.start`` (Windows only).

    Returns ``%LOCALAPPDATA%\\Anastomosis\\WebView2`` — created, parents and all
    — or the operator's own ``WEBVIEW2_USER_DATA_FOLDER`` value when they set
    one (a deliberate override is never second-guessed). ``None`` means "say
    nothing": off Windows there is no WebView2, and on a failure pywebview keeps
    its own default so the GUI still starts.

    pywebview owns this knob, not the environment. Its Windows backend assigns
    ``CoreWebView2CreationProperties.UserDataFolder`` on every launch, from the
    ``storage_path`` argument of ``webview.start``; WebView2's documented
    ``WEBVIEW2_USER_DATA_FOLDER`` environment variable is never consulted in
    that arrangement, which is why this helper RETURNS a value for the caller
    to pass rather than exporting one.

    What we are replacing is not an unwritable folder — the defaults are all
    writable. Left unset, pywebview picks the folder from its OTHER state:
    ``%APPDATA%\\pywebview`` (shared with every pywebview app on the machine)
    when private mode is off, and a fresh ``%TEMP%`` directory per launch —
    whose name pywebview takes from a ``TemporaryDirectory`` it then drops on
    the floor — when it is on, which is the default this app runs under. Both
    are unstable ground for a runtime that keeps state there. Naming one
    app-owned, per-user folder makes the location stable, reproducible, and
    something a support request can point at. Private mode is untouched, so
    what lands there is WebView2's own runtime state, not preserved cookies or
    local storage.

    Like the taskbar identity above, a failure here is warned about (exception
    TYPE only) and never fatal.
    """
    import os
    import sys

    # Positive platform test (the shape _claim_windows_taskbar_identity uses):
    # mypy narrows ``sys.platform`` statically, so an early ``!= "win32"``
    # return would make everything after it unreachable on the Linux lane.
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
    """Opt-in: route ``ANAST_GUI_REMOTE_DEBUGGING_PORT`` into pywebview's setting.

    DIAGNOSTICS ONLY. The packaged-installer smoke test
    (``packaging/smoke_windows.py``) attaches Playwright over CDP to prove the
    shipped WebView2 window really rendered the dashboard; nothing else needs
    this. An open debugging port lets any process on the machine drive the
    window that is displaying charts, so it must never be set in normal
    operation — unset (the default) is a no-op, and the port is opt-in for that
    reason even though Chromium binds it to loopback.

    Why pywebview's setting and not WebView2's environment variable: pywebview
    assigns ``CoreWebView2CreationProperties.AdditionalBrowserArguments``
    itself on every launch (``--disable-features=ElasticOverscroll`` and
    friends), and WebView2 ignores ``WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS``
    whenever the host app supplies that property — so that variable can never
    open the port in a pywebview app. pywebview appends
    ``--remote-debugging-port=<n>`` to the same property when its
    ``REMOTE_DEBUGGING_PORT`` setting is set, and that is the supported route.

    Warn-never-fail like the preparation helpers above: a non-numeric value, an
    out-of-range port, or a pywebview that no longer carries the key leaves the
    setting untouched and the GUI starts normally.
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
    # Bind the FACADE (not the raw controller) as js_api: only the async/guarded
    # run methods + light read queries the front end calls are reachable from JS,
    # so a page can never invoke a synchronous heavy method and freeze the bridge.
    window = webview.create_window(
        _WINDOW_TITLE,
        url=_INDEX.as_uri(),
        js_api=GuiApi(controller),
        width=1100,
        height=820,
        min_size=(820, 600),
    )
    if window is None:
        # pywebview types create_window as Window | None; a None here means the
        # backend refused the window and nothing below can work. Fail loudly at
        # startup rather than crash later on the first attribute access.
        raise RuntimeError("webview.create_window returned no window — GUI startup failed")
    sink.attach(window)

    # Window-close barrier: while a long-running job is in flight, veto the
    # close so the window can't interrupt an in-flight PDF/ledger write. pywebview
    # 5.x cancels the close when a `closing` subscriber returns False; the daemon
    # workers stay daemon=True, so this is a GRACEFUL guard (the OS can still
    # force-kill a wedged worker), never a hard lock. The operator is told why via
    # the shared shell log surface. If a future pywebview drops closing-veto, the
    # fallback is controller.join_active_job(~5s) here instead of the False return.
    def _on_closing() -> bool:
        if not controller.busy:
            return True  # nothing running — allow the close
        _warn_job_running(window)
        return False  # veto: pywebview cancels the close on a False return

    window.events.closing += _on_closing
    # ``storage_path`` is what pywebview turns into WebView2's UserDataFolder.
    # Omit the argument entirely when there is nothing to say (off Windows, or
    # a folder we could not create) so pywebview's own default stands.
    if storage_path is None:
        webview.start(debug=debug)
    else:
        webview.start(debug=debug, storage_path=storage_path)
