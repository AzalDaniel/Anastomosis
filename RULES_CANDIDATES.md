# Rule candidates from worker W7 (core/model, core/fhir, pipeline.py, __init__.py, cli.py)

One sentence each, with the `file:line` the prose it replaced used to live at
(current, post-sweep line numbers). Orchestrator adjudicates into
`docs/RULES.md` or rejects.

1. `CHARTABLE_KINDS` omits vitals because they are encounter-scoped, not
   patient-scoped, and already covered by two separate checks
   (`core/model/bundle.py:27`).
2. `EXT_INLINE_CONTENT` is model-level, not source-namespaced, because the
   pipeline (not the source adapter) writes it — it holds an artifact's own
   bytes when the source (e.g. a C-CDA Unstructured Document) has no separate
   file to copy (`core/model/document.py:23`).
3. FHIR export names a canonical field with no clean FHIR home
   `urn:anastomosis:field:<name>` and the source's own `extensions` dict
   `urn:anastomosis:ext`; `provenance` is local lineage and is never exported
   (`core/fhir/__init__.py:1`).
4. `EXT_FOLDED_RECORDS` records a COUNT of source records folded into one
   chart, never a filename — there is no PHI-safe precedent for carrying a
   source document's name into a delivered artifact (`pipeline.py:326`).

## Loose ends

None found (`TODO`/`FIXME`/`XXX`) in this worker's assigned files.
