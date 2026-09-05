# RULES_CANDIDATES — worker W5 (sources/pf_tebra, oracle_ehi, fhir_r4, learned, _rowutil, base)

One sentence per candidate, with the file:line the sweep cut it from.

1. Learned-source discovery is defensive: a directory without `mapping.json`, a malformed mapping, an un-reviewed mapping, or a mapping id colliding with an already-registered adapter is skipped with a name-only diagnosis, and discovery itself never raises. (`src/anastomosis/sources/learned/__init__.py:60,94`)
2. A learned mapping's trust is lighter than a template pack's: `human_reviewed=False` is a hard skip, and a `source_trust.json` content-hash mismatch after review only warns — it never blocks loading. (`src/anastomosis/sources/learned/__init__.py:44`)
3. An attachment stem matching more than one file in an export is dropped from the index rather than resolved to an arbitrary match: an unresolved attachment is recoverable, a wrongly-guessed one is not. (`src/anastomosis/sources/pf_tebra/loader.py:317`)
4. A cross-patient join used only to resolve one shared fact (e.g. an insurance plan's TYPE) may read another patient's row for that one fact, but must never surface that row's other columns — those are the other patient's own data and would otherwise leak into this patient's chart. (`src/anastomosis/sources/pf_tebra/mapper.py:919 _PlanTypeLookup._own_row`)

## Loose ends

(none found in this worker's files so far)
