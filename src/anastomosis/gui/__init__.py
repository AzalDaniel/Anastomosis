"""The desktop GUI (liquid-glass, pywebview).

Headless by construction: behavior lives in
:mod:`anastomosis.gui.controller`, plain Python a fake event sink can drive;
pywebview is lazy-imported only in :mod:`anastomosis.gui.shell`, so importing
this package never requires the ``gui`` extra.
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
