"""The CLI's command groups: per-surface Typer command modules.

Each resolves ``console``/``_glyphs`` and the Playwright-attach seams
(``_make_destination``, ``_make_validator``) late, via a function-scope
``from anastomosis import cli as _cli``, so ``cli``'s monkeypatch seams keep
working. No module here imports the GUI, and importing the GUI never imports
these (peer-frontend boundary, `RULES_CANDIDATES.md` #1)."""

from __future__ import annotations
