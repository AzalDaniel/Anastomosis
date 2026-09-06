# RULES_CANDIDATES.md — worker W6 (core/*.py except model/, fhir/)

Candidate rules found while cutting over-cap docstrings, not already covered
by `docs/RULES.md`. One sentence with `file:line`, for the orchestrator to
adjudicate into `RULES.md` or reject.

- The archive's orphan sweep globs `*.pdf` only, so `_reap_dead_temps`'
  dot-prefixed temp files are invisible to it and must be cleaned by the
  atomic writer itself, never the orphan pass (`core/atomic.py:74`).
- The C-CDA reader and deliverer share `media_type_suffix` so an embedded
  artifact's on-disk suffix always matches the sidecar the deliverer writes,
  never two independent derivations that could disagree (#373)
  (`core/textutil.py:169`).
- Every frontend field that names a folder or a file goes through
  `core/output.py`'s `typed_path` (never a bare `Path(arg)`), enforced by
  `tests/unit/test_gui_console_paths.py` walking each console's AST
  (`core/output.py:71`).
- The L2/L3 delivery verifier and the QA `DataIntegrityCheck` share
  `all_date_spellings` as the single source of accepted date spellings, so
  they can never diverge on which chart rendering counts as present
  (`core/timeutil.py:175`).
- A command that stops because the operator declined its own confirmation
  exits 0 like success does; it must call `core/outcome.py`'s `declined`
  so the caller can tell the two apart, and the read via `take_declined`
  is destructive so a stale outcome never frames the next run
  (`core/outcome.py:1`).

## Loose ends

(none found in this worker's files)
