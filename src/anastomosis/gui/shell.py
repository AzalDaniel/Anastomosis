"""The pywebview shell: the ONLY module that touches webview (lazy-imported).

Everything testable lives in :mod:`anastomosis.gui.controller`; this file is
the thin webview adapter, kept under ~80 lines and marked
``# pragma: no cover`` on the lines that need a real window, exactly as
:mod:`anastomosis.deliver.browser.cdp` does for Playwright.

:func:`launch` lazily imports ``webview``; a missing install raises a
``RuntimeError`` naming the ``anastomosis[gui]`` extra (the optional-dependency
error style used across the toolkit). It builds a window over the bundled,
network-free ``web/index.html``, exposes a :class:`GuiController` as the
``js_api`` (so the front end calls ``pywebview.api.*``), and wires a sink that
marshals each event into ``window.evaluate_js("anastEvent(...)")`` — pywebview's
``evaluate_js`` is thread-safe, so the controller's daemon worker may call it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from anastomosis.gui.controller import GuiController

__all__ = ["launch"]

_WEB_DIR = Path(__file__).resolve().parent / "web"
_INDEX = _WEB_DIR / "index.html"
_WINDOW_TITLE = "Anastomosis"


class _WindowSink:
    """An :class:`~anastomosis.gui.controller.EventSink` backed by a window.

    The window is attached *after* construction (the controller is built first,
    because it is the window's ``js_api``, and the window is built next). Until
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


def launch(debug: bool = False) -> None:  # pragma: no cover - needs webview + a display
    """Open the desktop GUI window. Requires the ``gui`` extra (pywebview)."""
    try:
        import webview
    except ImportError as exc:
        raise RuntimeError("pywebview is required for the GUI — install anastomosis[gui]") from exc

    sink = _WindowSink()
    controller = GuiController(sink)
    window = webview.create_window(
        _WINDOW_TITLE,
        url=_INDEX.as_uri(),
        js_api=controller,
        width=1100,
        height=820,
        min_size=(820, 600),
    )
    sink.attach(window)
    webview.start(debug=debug)
