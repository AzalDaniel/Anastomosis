"""The CLI's command groups: per-surface Typer command modules.

Each resolves ``console``/``_glyphs`` late, via a function-scope ``from
anastomosis import cli as _cli``, and reaches an attach seam through the module
that owns it. No module here imports the GUI, and importing the GUI never
imports these (peer-frontend boundary)."""

from __future__ import annotations
