# AI-assisted: written with Claude agents under the author's direction and review; see DESIGN.md.
"""The CLI's command groups: the Typer app's per-surface command modules.

:mod:`anastomosis.cli` was one ~1,650-line module carrying every Typer command
and its helpers. The command BODIES were split here into focused per-surface
modules so each surface stays reviewable, mirroring the GUI's
:mod:`anastomosis.gui.consoles` facade precedent:

* :mod:`~anastomosis.cli_commands.pipeline` — ``anast pipeline run``;
* :mod:`~anastomosis.cli_commands.migrate` — ``anast migrate`` (+ its saved-profile
  resolution and the migration run wrapper);
* :mod:`~anastomosis.cli_commands.upload` — ``anast upload`` (+ the run exit-code
  rule);
* :mod:`~anastomosis.cli_commands.delivery` — ``anast archive`` / ``anast bundle``;
* :mod:`~anastomosis.cli_commands.destination` — ``anast destination
  list``/``route``/``init`` (+ the registry / local-pack / selector-prompt
  helpers);
* :mod:`~anastomosis.cli_commands.packsrc` — ``anast pack init`` / ``anast source
  init`` (+ the synthetic preview record and its render).

The Typer app objects (``app``, ``pipeline_app``, ``destination_app``,
``pack_app``, ``source_app``) and the helpers shared across groups stay DEFINED
in :mod:`anastomosis.cli`; that module imports these command modules at its
BOTTOM (after the apps and shared helpers exist) so their module-level
``@<app>.command(...)`` decorators register against the already-defined apps —
the standard late-import registration pattern. A moved command body resolves
``console`` / ``_glyphs`` and the Playwright-attach seams (``_make_destination``,
``_make_validator``) LATE through the ``cli`` module (a function-scope ``from
anastomosis import cli as _cli``) so every existing monkeypatch seam keeps
working. No command module imports the GUI, and importing the GUI never loads
these modules — the peer-frontend boundary the import-boundary tests pin.
"""

from __future__ import annotations
