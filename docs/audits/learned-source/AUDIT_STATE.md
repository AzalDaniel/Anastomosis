# Learned Sample and Migration Audit - Durable State

Last updated: 2026-08-30 (Asia/Karachi)

**Primary recovery entry:** read `HANDOFF.md` first. It supersedes stale task
ordering below while retaining this file's historical checkpoints.

This file is the PHI-free recovery source for the ongoing audit. Update it before
and after material changes so the work can resume without relying on chat history.
Never record patient values, original sample filenames, credentials, or private
Tebra repository content here.

## User outcome

Audit and harden the complete user-facing **Learn from sample + Migration toggle**
workflow. A user must be able to select an existing destination-EHR format or teach
Anastomosis from representative documents, map supported source clinical data into
that destination schema, and generate a safe, deterministic, traceable document in
the learned style. Scope includes UI/configuration, source ingestion, semantic
mapping, template learning, rendering, validation, and migration execution.

The target is broad interoperability, not an unsupported claim that every EHR or
every clinical document can be reproduced with 100% clinical accuracy. The safe
contract is: no silent loss or misattribution; exact provenance; explicit ambiguity,
quarantine, and refusal; deterministic output; declared conformance; and measured
visual fidelity against reviewed gold samples.

## Repository checkpoint

- Repository: `AzalDaniel/Anastomosis`
- Local path: `C:\Users\azald\Documents\Codex\2026-08-29\github-plugin-github-openai-curated-remote-3\work\Anastomosis`
- Working branch: `codex/learned-source-integrity`
- Recovery baseline: `e143cfe112187af3a00541867f63433dc70e2678`.
- Current HEAD: `bbc48a6` after a verified non-overlapping fast-forward to
  `origin/main` on 2026-08-30.
- Expected pre-existing modified files:
  - `src/anastomosis/core/sourcelearn.py`
  - `src/anastomosis/packgen/emit.py`
  - `src/anastomosis/sources/learned/reader.py`
  - `tests/unit/test_learned_source.py`
  - `tests/unit/test_packgen_emit.py`
  - `tests/unit/test_sourcelearn.py`

Additional reviewed P0 work now modifies `packgen/__init__.py`,
`packgen/extract.py`, and `sources/learned/interpreter.py`, with new regression
files `test_packgen_image_only.py` and `test_learned_runtime_integrity.py`.
Audit memos and the aggregate corpus probe are untracked until the handoff branch
is committed. See `HANDOFF.md` for the exact complete list.

Do not discard or overwrite these edits. They are P0 integrity fixes awaiting the
full regression gate.

## Architecture established so far

Two distinct learners currently exist:

1. `anast source init` learns mappings from one flat CSV/TSV/JSON/NDJSON table to
   the canonical record model. It is deterministic matching, not machine learning.
2. `anast pack init --from-samples` extracts PDF text/layout signals and emits a
   draft pack. The current emitter is a generic SOAP layout with inferred design
   tokens, not a semantic or pixel-faithful reconstruction engine.

The audit must also locate and trace the migration toggle and every UI/API/CLI path
that composes these components.

## Verified P0 defects and local fixes

- Normalized runtime headers could fingerprint-match but fail authored-key lookup,
  causing missing mappings or synthetic patients. The reader now performs a
  one-to-one normalized alias binding.
- Duplicate, normalization-colliding, blank, and ragged CSV schemas could silently
  conflate or drop data. They are now rejected with value-free diagnostics.
- Duplicate JSON object keys and flattened JSON path collisions were last-write-wins.
  They are now rejected without leaking values.
- XML/HTML/PDF/ZIP/binary content could be misdetected as a flat table. Format
  detection now rejects those inputs before CSV sniffing.
- Recurring PDF sample text, including shared patient/provider values and inferred
  headings, could be copied into reusable pack files. Raw retained sample text is
  now quarantined only in `UNPLACED.txt`; pack YAML/HTML/Markdown use canonical
  labels, numbered placeholders, counts, and role metadata.
- Blank/missing patient identities, duplicate patient-grain rows, conflicting
  patient-scoped values across encounter rows, and duplicate nonblank encounter
  keys now fail closed. Sparse repeated demographics resolve from the first
  nonblank transformed value only when all nonblank values agree.
- Raster pages with no native text now raise a dedicated `OcrRequiredError`;
  fully textless non-raster samples raise `NoExtractableTextError`. Pack init
  writes no partial pack on these failures.

These fixes are not complete until all focused, full-suite, corpus, rendering, and
VM gates pass.

## Test checkpoint

- Pre-fix learned/packgen focused baseline: 126 tests passed.
- Post-fix source-focused gate: 45 tests passed.
- Ruff passed on all six changed files.
- Mypy passed on the three changed source files.
- The most recent combined pytest invocation did not run because it referenced a
  nonexistent file, `tests/unit/test_packgen_analyze.py`. This was a command-list
  error, not a test failure. Resume by enumerating real packgen tests, then rerun.
- A corrected focused suite progressed beyond 100 passing cases with no displayed
  failure, but the Codex restart terminated its process before a final summary.
  It must be rerun; partial progress is not counted as a completed gate.
- After fast-forwarding to `bbc48a6`, `git diff --check`, Ruff, and Mypy passed
  again on the six P0 files.
- After integrating runtime-identity and image-only gates, root formatted all
  changed Python files and reran a combined 161-test learned/packgen/PF/whole-
  record lane. The first run exposed one stale legacy expectation; after updating
  it to assert the stronger early refusal, the identical lane passed 161/161.
  Root Ruff, focused mypy, and `git diff --check` also passed. Full repository
  lanes remain pending.

## Corpus checkpoint

- Local corpus exists: 9 top-level files, including 8 ZIPs, 6,302,818,444 bytes.
- Inventory previously found 2,103 XML, 2,104 HTML, 170 TSV, 53 PDF, 1 XLSX, 4
  nested ZIP, and opaque/no-extension artifacts.
- The 53 PDFs were selectively extracted under synthetic names in
  `tmp/learned-pack-pdf-corpus`; do not expose original filenames.
- Never commit sample documents or patient content.
- All 53 PDFs parsed, totaling 802 Letter pages. Every PDF had zero extractable
  words; 52/53 contained raster images (264 images total). The supplied PDF
  corpus therefore cannot exercise the current native-text learner and must
  fail closed pending a pinned offline OCR/layout worker.
- Aggregate C-CDA probe: 2,103/2,103 candidates parsed and had patient identity.
  It produced 12,277 encounters, 146,015 observations, 31,934 conditions, 1,252
  allergies, 7,564 medications, and 2,115 immunizations. Multiple other model
  collections and all probed encounter note sections were zero. Parse success is
  not semantic-completeness evidence; source-section conservation is the next P0.

## Runtime access checkpoint

- Intended VM: `Anastomosis-Audit` only.
- Codex sandbox identity varies by network mode (`A\codexsandboxoffline` or
  `A\codexsandboxonline`). Direct named-pipe access is not relied upon.
- Approved elevated read-only checks confirmed `Anastomosis-Audit` is running and
  operating normally.
- Approved elevated read-only checks confirmed `anastomosis-fhir-hapi` is running
  and published only on `127.0.0.1:18080`.
- Only control/checkpoint/stop `Anastomosis-Audit`.
- Only create/delete containers whose names begin with `anastomosis-fhir`.

## Research evidence retained

Primary references already collected:

- ONC EHI Export test method: https://healthit.gov/test-method/electronic-health-information-export/
- HL7 US Core clinical notes: https://hl7.org/fhir/us/core/clinical-notes.html
- HL7 C-CDA 5.0.0: https://hl7.org/cda/us/ccda/5.0.0/
- Epic EHI Tables: https://open.epic.com/EHITables
- Oracle Health EHI export overview: https://www.oracle.com/a/ocom/docs/industries/healthcare/health-data-intelligence-ehi-export-data-overview-user-instructions.pdf
- MEDITECH EHI export: https://home.meditech.com/en/d/restapiresources/pages/ehiexportold.htm
- TruBridge EHI export: https://ehi-export.plt.trubridge.com/trubridge/v2107/
- Practice Fusion EHI export: https://www.practicefusion.com/ehi-export-documentation/v2/index/
- HAPI FHIR validator: https://github.com/hapifhir/org.hl7.fhir.core
- DocLayNet: https://arxiv.org/abs/2206.01062
- PubLayNet: https://arxiv.org/abs/1908.07836
- GLAM layout graph model: https://arxiv.org/abs/2308.02051

Research must continue with current official vendor documentation and primary
technical sources. Do not infer vendor layouts from marketing screenshots.

## Immediate resume sequence

1. Complete the active official-research, architecture-trace, and adversarial-test
   agent memos against current main `bbc48a6`.
2. Review the six P0 diffs, rerun lint/type/focused tests, then full regression.
3. Run value-free C-CDA/FHIR corpus conformance and conservation inventories.
4. Run bounded per-file PDF metadata/extraction tests; render representative pages
   and visually inspect them.
5. Build adversarial tests for semantic mapping, provenance, ambiguity, conflicting
   encounters, attachments, resource limits, hostile PDFs, and layout variance.
6. Recheck the VM and Docker; checkpoint the authorized VM before runtime changes.
7. Implement only evidence-backed increments, with tests first and no silent
   degradation of existing UX.
8. Commit/push reviewed units under the user's GitHub identity and maintain this
   ledger for Claude handoff.

The exact current commands, runtime boundary, evidence counts, caveats, and
ordered remaining work are maintained in `HANDOFF.md`.
