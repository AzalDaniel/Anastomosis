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
3. The GUI's browser-upload console (`upload_start`) never calls
   `ManagedDestination.close` on the operator's own logged-in browser (it
   closes only its own ledger handle); `upload_stop` is cooperative and
   honored only at item boundaries, never mid-item; a re-start naturally
   resumes via the ledger (`recover` rewinds mid-flight items, terminal
   items are not re-driven). (`gui/consoles/upload.py:220-346`, formerly a
   longer "Safety model" docstring)

## Loose ends

- `gui/shell.py`'s close-barrier veto assumes pywebview's `closing` handler
  honors a `False` return; the deleted comment's noted fallback if a future
  pywebview drops that is `controller.join_active_job(~5s)` in `_on_closing`
  instead of returning `False` (was `gui/shell.py:242-248` before the sweep).
