"""The GUI's focused consoles: the controller's five operator surfaces.

Each is a small class the controller constructs once and delegates to, so
the controller stays a thin facade; async choreography lives in
:mod:`anastomosis.gui.jobs`. No console imports
:mod:`anastomosis.gui.controller` at module load — the upload console
resolves its ``_attach_destination`` monkeypatch seam late, from inside the
worker body, to stay import-cycle-free.
"""

from __future__ import annotations

from anastomosis.gui.consoles.packgen import PackgenConsole
from anastomosis.gui.consoles.runs import MigrationConsole, PipelineConsole, SummaryStore
from anastomosis.gui.consoles.source import SourceConsole
from anastomosis.gui.consoles.upload import UploadConsole

__all__ = [
    "MigrationConsole",
    "PackgenConsole",
    "PipelineConsole",
    "SourceConsole",
    "SummaryStore",
    "UploadConsole",
]
