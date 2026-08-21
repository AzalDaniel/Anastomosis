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
from pathlib import Path
from typing import Any

from anastomosis.core.logutil import exc_tag
from anastomosis.gui.controller import GuiApi, GuiController

__all__ = ["launch"]

logger = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).resolve().parent / "web"
_INDEX = _WEB_DIR / "index.html"
_WINDOW_TITLE = "Anastomosis"

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


def launch(debug: bool = False) -> None:  # pragma: no cover - needs webview + a display
    """Open the desktop GUI window. Requires the ``gui`` extra (pywebview)."""
    try:
        import webview
    except ImportError as exc:
        raise RuntimeError("pywebview is required for the GUI — install anastomosis[gui]") from exc

    _claim_windows_taskbar_identity()

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
    webview.start(debug=debug)
