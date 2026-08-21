"""The desktop GUI (liquid-glass, pywebview) — M4.

Headless-first by construction: every behavior lives in
:mod:`anastomosis.gui.controller` as plain Python the tests drive against a
fake event sink. pywebview appears only in :mod:`anastomosis.gui.shell`
(lazy-imported, so importing this package never requires the ``gui`` extra),
and the web assets in :mod:`anastomosis.gui.web` are bundled, offline, and
network-free.
"""

from .controller import EventSink, GuiController
from .events import done_event, error_event, progress_event, stage_event

__all__ = [
    "EventSink",
    "GuiController",
    "done_event",
    "error_event",
    "progress_event",
    "stage_event",
]
