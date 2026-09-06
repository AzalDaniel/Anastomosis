# RULES_CANDIDATES.md — worker T3 (tests/, slice S-1)

One sentence each, with `file:line` from the pre-sweep source. Orchestrator adjudicates.

- The GUI must never import the CLI (or vice versa) — they are peer frontends over one shared core, and a GUI-to-CLI dependency is a one-way ratchet toward a CLI-shaped GUI (`tests/unit/test_import_boundaries.py:8`).

## Loose ends

(none found in this worker's files)
