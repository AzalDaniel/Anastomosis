# AI-assisted: written with Claude agents under the author's direction and review; see DESIGN.md.
"""The GUI's focused consoles: the controller's five operator surfaces.

:class:`~anastomosis.gui.controller.GuiController` was one ~1,700-line class
carrying every JS-facing method, split here into focused per-surface modules
so each surface stays reviewable. The async-job choreography was extracted
first (:mod:`anastomosis.gui.jobs`); this package holds the remaining
surfaces, each a small class the controller constructs once and delegates to:

* :class:`~anastomosis.gui.consoles.upload.UploadConsole` — browser-delivery
  driving + read-only ledger views;
* :class:`~anastomosis.gui.consoles.packgen.PackgenConsole` — the
  pack-from-samples wizard backend;
* :class:`~anastomosis.gui.consoles.source.SourceConsole` — the learn-a-source
  wizard backend;
* :class:`~anastomosis.gui.consoles.runs.PipelineConsole` /
  :class:`~anastomosis.gui.consoles.runs.MigrationConsole` — the two long run
  flows, sharing a :class:`~anastomosis.gui.consoles.runs.SummaryStore` for the
  per-run per-patient roll-up.

Every console takes the controller's ``emit`` callable and the shared
:class:`~anastomosis.gui.jobs.GuiJobRunner`, so async and sync entries contend
on the SAME busy guard. No console imports the controller at module load (the
upload worker resolves the ``_attach_destination`` monkeypatch seam late, from
inside its worker body), so the package stays cycle-free.
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
