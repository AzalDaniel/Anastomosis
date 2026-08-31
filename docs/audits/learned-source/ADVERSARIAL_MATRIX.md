# Learned-source adversarial test matrix

**Audit date:** 2026-08-30 (Asia/Karachi)  
**Baseline:** `bbc48a6`  
**Scope:** learned flat-file ingestion, PDF-to-draft-pack generation, migration
composition, trust/quarantine, GUI surfaces, and C-CDA/FHIR conservation. This
memo is value-free: it intentionally contains no corpus filenames or source
values.

## Evidence collected

- Enumerated the real lanes under `tests/unit`, `tests/e2e`, and
  `tests/gui_e2e`; there is no `test_packgen_analyze.py` lane.
- Re-ran the learned-source gate:

  ```text
  python -m pytest -o addopts="" tests/unit/test_learned_spec.py
  tests/unit/test_learned_transforms.py tests/unit/test_sourcelearn.py
  tests/unit/test_learned_source.py
  ```

  Result: **70 passed in 10.26s**.
- The packgen/quarantine collection consists of 96 tests across
  `test_packgen_extract`, `test_packgen_infer`, `test_packgen_emit`,
  `test_packgen_static_is_frequency`, `test_packtrust`, `test_pf_quarantine`,
  and `test_record_summary`. Its combined runner stream was interrupted before
  it produced a final summary, so it is **not counted as a green gate** here.
- The current suite has strong positive coverage for strict mapping validation,
  normalized aliases and parser collisions, value-level unmapped-field
  conservation, deterministic pack emission, sample-text quarantine, hash
  pinning/snapshot execution, PF row partition/quarantine, whole-record QA,
  and C-CDA/FHIR adapter round trips.

## Priority matrix

| Priority | Adversarial case and current evidence | Deterministic acceptance test |
| --- | --- | --- |
| **P0** | **Conflicting duplicate encounter key.** `LearnedSourceAdapter._build_encounters` suppresses a repeated encounter key after the first row. Existing tests prove per-row preservation without an encounter key and un-mapped value loss at patient grain, but do not prove that a duplicate keyed encounter with conflicting *mapped* fields is rejected or explicitly preserved. A silently discarded clinical row is unsafe. | Make two encounter-grained rows for one patient with the same encounter key and a different mapped clinical field. `source init`/`round_trip` must refuse with a value-free `duplicate/conflicting encounter` code, or the mapping must preserve both rows in a deterministic extension/ledger. Assert every input row has exactly one accounted fate. |
| **P0** | **Identity-key collision and blanks.** The interpreter intentionally assigns a synthetic patient id to a blank key. This avoids dropping data, but there is no test of multiple blank keys plus mapped demographics, nor an operator-visible quarantine/ambiguity ledger. A learned mapping must not imply that two absent identities are known people or merge them. | Supply several blank-key rows, including two with the same visible demographics and one nonblank control. Assert a distinct deterministic synthetic identity per blank row, no merge with the control, provenance marks identity as absent, and the run produces a count-only ambiguity/quarantine artifact. Re-run and assert byte-identical ids/order. |
| **P0** | **Mapped-field transform loss is outside `round_trip` proof.** `round_trip` deliberately trusts mapped columns, so sentinel normalization, `split`, parse failures, translations, or a constant transform can remove or change a source value without a loss ledger. Closed transform parsing is covered in `test_learned_transforms.py`; mapped-value conservation is not. | Parameterize every transform and translation with normal, empty, sentinel, malformed, and collision inputs. Require a per-column outcome ledger: `preserved`, `normalized`, `intentionally-null`, or `refused`, with source row counts conserved. Any non-bijective transform requires an explicit reviewed loss/normalization declaration. |
| **P0** | **End-to-end learned-source conservation into migration formats is absent.** Unit tests separately cover learned mapping, C-CDA export, FHIR export, and migration. There is no test that starts with a learned mapping and checks patient/encounter identity, extensions, provenance, and facts after C-CDA and FHIR round trips. | Build a tiny synthetic learned input with mapped and unmapped fields, then run learned adapter → canonical model → FHIR and C-CDA → their readers. Compare a normalized conservation inventory (patient identities, encounter identities, canonical fields, extension keys/values, and provenance source IDs). Any unsupported field must appear in the declared loss/quarantine ledger, never disappear. |
| **P1** | **Schema defenses are good but incomplete at scale.** `test_learned_source.py` now covers duplicate/blank/normalized CSV headers, overflow rows, duplicate JSON keys, flattened-path collisions, and alias binding. Missing: short CSV rows, BOM/encoding boundary behavior, header-only/zero-row JSON, heterogeneous JSON schemas, deeply nested JSON, and many-column/row resource bounds. | Add bounded generated tests for CSV/TSV/JSON/NDJSON. Assert short cells become explicit nulls only where declared; schema union is deterministic; depth/row/byte/column limits refuse with a value-free code; no exception or summary contains a cell value. |
| **P1** | **Provenance stops at source file/id.** The learned interpreter records source system, file, and identifiers but not row ordinal, mapping hash, header fingerprint, alias bindings, transform/version, or an ambiguity decision. That makes post-migration dispute resolution weaker than the safety claim. | For reordered input and normalized runtime headers, assert provenance contains a stable mapping digest, input fingerprint, actual-to-authored header binding, and one-based row locator (or immutable row digest). Reorder only the rows and verify provenance follows the record, not its transient position. |
| **P1** | **PDF sample harvesting has no explicit resource budget.** `extract_document` iterates all pages, spans, and drawings; `extract_samples` retains every result. Existing tests cover encryption, malformed PDFs, geometry, curves, deterministic inference, and mixed page sizes, but not oversized/page-bomb/vector-bomb/recursive-object behavior. This is reachable from the GUI. | Use synthetic PDFs at the configured page, span, drawing, byte, and wall-time boundaries. At each `limit + 1`, refuse before emitting a draft, return only a count/index/error code to GUI/CLI, and prove no partial pack/output directory remains. Include a cancellation test while extraction is active. |
| **P1** | **Layout variance is only structurally assessed.** Packgen inference tests modal geometry and e2e rediscovery, but there is no visual regression metric for rotation, crop-box differences, multicolumn reading order, image-only/scanned pages, forms, unusual writing direction, clipped text, or outlier samples. The emitter also documents that it is a draft rather than a faithful reconstruction. | Establish a synthetic, non-PHI visual corpus with fixture IDs only. For each layout class, assert a deterministic capability result: supported with thresholded geometry/text-order score, or explicitly unsupported/low-confidence. Render the draft and compare layout landmarks/page count using a fixed renderer; require reviewed golden update for change. |
| **P1** | **Sample-text quarantine is strong but needs whole-directory and failure-path proof.** Tests ensure retained text is confined to `UNPLACED.txt`, including delimiter/comment/YAML hazards and single-sample withholding. They do not scan temporary/failed/preview artifacts or test user deletion/re-run against stale text. | Generate a draft from synthetic canary strings, recursively scan every emitted and temporary artifact except the quarantine file, then delete quarantine and rerun/render. Assert no canary survives in the working pack, preview, logs, exception text, manifest, or cache; assert a failed run cleans partial outputs. |
| **P1** | **Trust has hash and snapshot tests, but not a sandbox.** `test_packtrust.py` proves untrusted code is not imported, changed hashes are refused, records merge atomically, and a snapshot resists a simple on-disk swap. A trusted external pack still executes Python with process authority; imports, symlinks/reparse points, replacement of non-hashed dependencies, and multi-process trust-store races are not tested. | State and enforce one contract: trusted packs are either sandboxed with an explicit capability allow-list, or visibly warned as arbitrary local code. Add symlink/reparse-point refusal, import/dependency allow-list tests, concurrent writer tests, and a race harness that repeatedly swaps every pack artifact while discovery/render occurs; execution must use only the approved immutable snapshot. |
| **P1** | **PF quarantine has excellent row partition coverage but lacks a general migration conservation gate.** `test_pf_quarantine.py` verifies exact attribution or verbatim quarantine, ambiguous joins, stale-state reset, artifact determinism, and migration settlement. It does not prove other source adapters use the same per-table offered/accounted-for invariant. | Add an adapter-neutral conservation interface: each loader reports offered, attributed, quarantined, and refused counts per table/resource. Run it for PF, learned, C-CDA, and FHIR fixtures; require `offered = attributed + quarantined + explicitly-not-applicable` and require an artifact for nonzero quarantine. |
| **P2** | **GUI behavior is tested, visual composition is not.** `test_teach.py`, `test_migrate.py`, controller tests, and bridge-surface tests cover flows and event wiring. There is no screenshot/accessibility regression for long/hostile error codes, low-confidence and quarantine warnings, repeated completion events, cancellation, or a source-to-migration handoff in a real rendered page. | In the GUI E2E lane, use synthetic bridge responses at boundary sizes. Capture deterministic screenshots and accessibility trees for source analysis, confirmation refusal, low confidence, row quarantine, renderer failure, and completion. Assert warnings remain visible, controls remain operable, paths/values are not echoed, and one operation produces one terminal state. |
| **P2** | **Cross-format fuzzing and replay are missing.** Existing property tests and adapter round trips are valuable, but there is no seeded generator that combines aliases, grouping, transforms, migration toggles, and format serialization. | Add a fixed-seed property suite that generates valid mapping specs and small synthetic tables, then runs ingestion plus both export paths. Persist only seed, shape, and hash on failure; shrink failures while preserving the no-value logging fence. |

## Existing controls worth retaining

- `test_learned_spec.py`: closed schema/target/transform boundary and invalid
  grouping rejection.
- `test_learned_source.py`: alias binding, malformed CSV/JSON rejection, and
  absence of source cells from failures.
- `test_sourcelearn.py`: PHI-safe profiling and unmapped value-level
  conservation before save.
- `test_packgen_emit.py` and `test_packgen_static_is_frequency.py`: draft
  determinism and raw sample text restricted to the quarantine file.
- `test_packtrust.py`: explicit trust, content hash, atomic trust state, and
  snapshot execution versus a basic TOCTOU swap.
- `test_pf_quarantine.py`: direct/indirect ownership partition, ambiguous-join
  quarantine, deterministic artifact, and migration-stage count propagation.
- `test_record_summary.py`: whole-patient PDF emission and QA failure when
  record-level facts do not reach a whole-record document.
- `test_ccda_export.py`, `test_ccda.py`, `test_fhir.py`, and
  `test_synthea_pipeline.py`: adapter-specific round-trip controls.

## Recommended execution order

1. P0 duplicate-encounter, identity-ambiguity, mapped-transform ledger, and
   learned-to-C-CDA/FHIR conservation tests.
2. P1 resource limits, provenance/audit envelope, pack trust boundary, and
   general migration conservation interface.
3. P1 visual/hostile-PDF fixtures and P2 GUI composition plus seeded fuzzing.

No source or test implementation files were changed by this audit workstream.
