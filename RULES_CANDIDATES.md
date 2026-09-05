# RULES_CANDIDATES — W10 (gui)

One sentence per candidate, with the file:line the docstring/comment used to
carry it at. The orchestrator adjudicates; this file does not ship.

1. Importing `anastomosis.gui` (including `gui.__main__`) never requires the
   `gui` extra: pywebview is lazy-imported only inside `gui/shell.py`'s
   `launch`. (`gui/__init__.py:3-6`, `gui/__main__.py:3-5`)
2. No `gui.consoles` module imports `gui.controller` at module load; the
   upload console resolves its `_attach_destination` monkeypatch seam late,
   from inside the worker body, to stay import-cycle-free.
   (`gui/consoles/__init__.py:4-7`)

## Loose ends

(none found in `gui/**/*.py` so far)
