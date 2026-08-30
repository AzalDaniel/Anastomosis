# Learn-from-sample audit: lossless handoff

> **2026-08-31 live-state override:** read `COORDINATION.md` first. It records
> Claude's post-handoff review, the current remote SHAs, merged conservation
> harness, live PR dependency order, and the crash-safe commit/push protocol.
> Historical “uncommitted” and `bbc48a6` statements below describe the original
> checkpoint and must not be mistaken for current GitHub state.

Last reconciled: 2026-08-30, Asia/Karachi. This is the first file a future
Codex or Claude session should read. It is deliberately PHI-free: do not add
patient values, original sample names, screenshots, credentials, or private
Tebra content.

## Resume in under five minutes

1. Open this repository:
   `C:\Users\azald\Documents\Codex\2026-08-29\github-plugin-github-openai-curated-remote-3\work\Anastomosis`.
2. Read, in order:
   `docs/audits/learned-source/HANDOFF.md`, `AUDIT_STATE.md`, `AUDIT_LOG.md`,
   `ARCHITECTURE_TRACE.md`, `ADVERSARIAL_MATRIX.md`,
   `RESEARCH_EHR_LAYOUT.md`, and `OCR_DECISION.md`.
3. Confirm `git status --short --branch`; do not discard the documented dirty
   changes. The working branch is `codex/learned-source-integrity` and the
   reconciled base is `bbc48a6`, which was exactly `origin/main` when fetched.
4. Run the gates in **Exact validation commands** below with a new isolated
   `--basetemp`. Do not count an interrupted test process as passing.
5. Before changing runtime state, checkpoint only the Hyper-V VM
   `Anastomosis-Audit`. Only create/delete Docker containers named
   `anastomosis-fhir*`.

There is no callable Supermemory service in the 2026-08-30 Codex session. The
Git-tracked directory above is the authoritative durable memory. After a branch
push, GitHub is the redundant copy. This avoids depending on chat history,
plugins, the VM, Docker, or a single local temporary directory.

## User outcome and safe claim

Audit and harden the entire Learn-from-sample plus migration workflow: ingest a
source export, preserve and map its clinical semantics into a canonical record,
select a versioned destination profile, learn or select a reviewed layout, render
deterministically, prove semantic/provenance conservation, and prepare or execute
an explicitly selected delivery route.

Do not promise a universal one-to-one EHR replica, “100% accurate,” “SOTA,” or
vendor-native output. Public standards do not define vendor PDF appearance, and
vendor output varies by product, release, tenant, and configuration. The defensible
contract is named/versioned fixtures, zero unexplained semantic loss, value-level
provenance, explicit ambiguity/refusal, deterministic manifests, and separately
measured visual thresholds.

## Current repository state

- Repository: `AzalDaniel/Anastomosis`.
- Branch: `codex/learned-source-integrity`.
- Base/HEAD at reconciliation: `bbc48a6` (`origin/main`, “One build, two doors
  (#295)”). Five upstream Claude/main commits were fetched and found not to
  overlap the original six P0 files before a clean fast-forward.
- Intended source/test changes, not yet committed at this checkpoint:
  - `src/anastomosis/core/sourcelearn.py`
  - `src/anastomosis/packgen/__init__.py`
  - `src/anastomosis/packgen/emit.py`
  - `src/anastomosis/packgen/extract.py`
  - `src/anastomosis/sources/learned/interpreter.py`
  - `src/anastomosis/sources/learned/reader.py`
  - `tests/unit/test_learned_source.py`
  - `tests/unit/test_learned_runtime_integrity.py` (new)
  - `tests/unit/test_packgen_emit.py`
  - `tests/unit/test_packgen_image_only.py` (new)
  - `tests/unit/test_sourcelearn.py`
- Audit evidence under `docs/audits/learned-source/` and the aggregate-only
  corpus probe under `docs/audits/learned-source/tools/` are also uncommitted.
- Configure eventual commits as
  `Azal Daniel <166051114+AzalDaniel@users.noreply.github.com>` so authorship is
  explicit. Do not merge until integrated tests and review complete.
- GitHub recovery/tracking issue:
  https://github.com/AzalDaniel/Anastomosis/issues/308. It was created by the
  authenticated `AzalDaniel` account and contains the verified architecture,
  corpus evidence, prepared P0 patch summary, remaining P0/P1 work, and safe
  acceptance contract.

## Verified defects and implemented P0 changes

1. Runtime column headers could fingerprint-match after normalization but not
   bind to mapping-authored keys. The learned reader now performs a one-to-one
   actual-to-authored normalized alias binding and refuses ambiguity.
2. Blank, duplicate, normalization-colliding, and ragged CSV schemas could be
   conflated or truncated. They are now rejected with structural/value-free
   diagnostics.
3. Duplicate JSON keys and dotted-path collisions were last-write-wins. They
   are now rejected.
4. XML/HTML/PDF/ZIP/binary input could fall through the flat-table detector.
   It is now refused; extensionless inspection reads one bounded 64-KiB prefix
   instead of allocating the complete file.
5. Blank/missing patient identity, duplicate patient-grain rows, conflicting
   patient fields across encounter-grain rows, and duplicate nonblank encounter
   keys could synthesize, collapse, or silently discard identity. The learned
   runtime now fails closed. For valid repeated demographics it resolves the
   first nonblank transformed value and proves all later nonblank values agree.
6. Repeated patient/provider values and inferred headings from sample PDFs could
   be embedded in reusable YAML/HTML/Markdown. Raw retained sample strings now
   exist only in owner-protected `UNPLACED.txt`; executable/reusable files use a
   fixed vocabulary, numbered placeholders, counts, and role metadata.
7. Image-only PDFs produced a plausible generic pack after extracting no text.
   `OcrRequiredError` now refuses raster pages with zero text spans; a separate
   `NoExtractableTextError` refuses fully textless non-raster samples. Pack init
   writes no partial pack on either path.

Review caveats before merging:

- The image gate proves the actual 53-PDF corpus refuses safely, but a page with
  a large clinical raster plus a small native text overlay still has spans and
  is not yet classified by raster coverage. OCR/layout work must explicitly
  handle this mixed-content case.
- Learned JSON/NDJSON parsing still reads full files into memory. CSV reading
  also returns all rows. Resource budgets/streaming are not yet implemented.
- Duplicate encounter keys are now a safe refusal, but legitimate one-to-many
  joined exports need an explicit relational/list schema rather than being
  silently deduplicated.
- Errors may contain operator paths, mapping display names, and column names.
  Values are excluded, but a future logging boundary must distinguish protected
  operator errors from PHI-free audit logs.

## Architecture truth (do not infer more)

- There is no literal “migration toggle.” The GUI exposes separate Migrate and
  Teach flows; Teach itself separates Document layout from Export format.
- `anast source init` learns one flat CSV/TSV/JSON/NDJSON mapping into a limited
  canonical path set. It is deterministic matching, not ML; it has no joins,
  lists, attachments, or arbitrary CDA/FHIR ingestion.
- `anast pack init --from-samples` emits a reviewed draft generic SOAP template
  from PDF signals. It is not a one-to-one reconstruction engine.
- Learned source and learned layout artifacts are independent and are not bound
  to a selected destination/version.
- Migrate prepares PDFs, C-CDA, manifests, and a route plan. It does not execute
  the selected delivery route. Generic FHIR upload is not destination-bound;
  C-CDA import remains manual.
- External pack Python executes with desktop-user authority. Trust snapshots
  code, while render-time template/asset immutability and sandbox boundaries
  require further proof/hardening.

The target architecture is four immutable, hash-addressed artifacts:
`SourceProfile`, `DestinationProfile`, `LayoutProfile`, and `RunManifest`.
Select the destination before teaching; bind all four identifiers/hashes into
the run; keep semantic mapping, visual rendering, and delivery validation as
separate fail-closed gates. See `ARCHITECTURE_TRACE.md` for the traced state
machine and exact code paths.

## Corpus evidence

Top-level local corpus: nine files including eight ZIPs, 6,302,818,444 bytes.
Inventory (aggregate only): 2,103 XML, 2,104 HTML, 170 TSV, 53 PDF, one XLSX,
four nested ZIPs, plus opaque artifacts. Never commit the corpus.

PDF results:

- 53/53 parsed by Poppler and PyMuPDF; 802 pages; all Letter size; no encrypted,
  rotated, timed-out, or unreadable file in this set.
- Zero extractable words across every PDF. Fifty-two of 53 contain raster
  images, 264 images total. Representative page renders were visually inspected
  and showed real structured chart layouts with bands, tables, columns, spacing,
  pagination, and footers. Do not expose the rendered content in logs/issues.
- Therefore the current native-text learner cannot learn the supplied PDF
  corpus. Until a pinned offline OCR/layout path exists, refusal is correct.

C-CDA results from `tools/probe_ccda_corpus.py`:

- Eight archives, 2,103 XML members/candidates, 2,103 parsed successfully,
  2,103 records with patient ID, zero parse failures.
- Aggregate model totals: 12,277 encounters; 146,015 observations; 31,934
  conditions; 1,252 allergies; 7,564 medications; 2,115 immunizations.
- The following parsed collections were all zero: advance directives,
  coverages, documents, facilities, family history, goals, health concerns,
  past medical history, practitioners, prescriptions, and screening events.
- All 12,277 encounters had ID, patient ID, date, type, and provenance, but the
  aggregate probe found no SOAP/note sections or chief-complaint-style fields.
- Parse success is not semantic completeness. The next mandatory probe must
  inventory XML section template IDs/titles/narrative presence and compare
  aggregate source sections/entries with canonical output and extensions,
  without emitting values or filenames.

Synthetic extracted PDFs were staged at
`C:\Users\azald\Documents\Codex\2026-08-29\github-plugin-github-openai-curated-remote-3\tmp\learned-pack-pdf-corpus`.
Representative renders were staged at
`C:\Users\azald\Documents\Codex\2026-08-30\you-ve-lost-some-previous-context\tmp\pdf-render-review`.
These are temporary, contain source-derived data, and are not recovery state.
Delete them safely after corpus work by validating the exact absolute paths and
using PowerShell `Remove-Item -LiteralPath ... -Recurse`; never delete a computed
or broad parent path.

## OCR decision

`OCR_DECISION.md` recommends native extraction first, then a separate pinned
offline Tesseract 5 CLI worker for layout observations, with RapidOCR/ONNX CPU
only as an optional disagreement/polygon pass and Docling/Paddle only as an
optional heavy layout/table adjudicator. No package installation is authorized
by that memo. The engine/binary/models/config/rasterizer must be hash-pinned,
network-disabled, bounded by pixels/time/RSS/concurrency, and recorded in a
manifest. OCR is layout evidence, never automatically promoted clinical truth.
High-risk values require independent structured evidence or review.

## Runtime and authority

- Approved read-only checks confirmed Hyper-V VM `Anastomosis-Audit` is running,
  operating normally, Generation 2, six CPUs.
- Approved read-only Docker checks confirmed existing container
  `anastomosis-fhir-hapi` is running at `127.0.0.1:18080`.
- The Codex identity changes between `A\codexsandboxonline` and
  `A\codexsandboxoffline`; direct pipe membership is not assumed. Elevated,
  narrowly scoped commands worked after user approval.
- Before any runtime mutation: checkpoint only `Anastomosis-Audit`. Do not
  control any other VM. Only create/delete `anastomosis-fhir*` containers.
- No VM mutation, checkpoint, package installation, or container change had
  occurred at this handoff.

## Test evidence and exact validation commands

Terminal results already obtained (do not broaden them into full-suite claims):

- Pre-fix learned/packgen focused baseline: 126 passed.
- Learned-source gate: 70 passed.
- Sourcelearn after bounded-prefix fix: 20 passed.
- Packgen/PF/whole-record lane: 96 passed.
- Runtime-integrity agent lane: 32 passed.
- Image-only gate agent lane: 23 passed.
- Root integrated learned/packgen/PF/record-summary lane after formatting:
  **161 passed in 37.65 seconds**. Its first run was 160 passed/one stale test
  failure; the regression was updated to assert the stronger early duplicate-
  patient-grain refusal, then the complete identical lane passed.
- Root Ruff check, focused mypy (five source files), and `git diff --check` passed.
- This is not yet a repository-wide unit/integration/E2E/GUI/packaging claim.

Always disable the pytest cache and use an isolated writable base, for example:

```powershell
$repo = 'C:\Users\azald\Documents\Codex\2026-08-29\github-plugin-github-openai-curated-remote-3\work\Anastomosis'
$bt = 'C:\Users\azald\Documents\Codex\2026-08-30\you-ve-lost-some-previous-context\tmp\pytest-resume-01'
Set-Location -LiteralPath $repo
.\.venv\Scripts\ruff.exe format --check src\anastomosis\core\sourcelearn.py src\anastomosis\packgen src\anastomosis\sources\learned tests\unit\test_sourcelearn.py tests\unit\test_learned_source.py tests\unit\test_learned_runtime_integrity.py tests\unit\test_packgen_emit.py tests\unit\test_packgen_image_only.py
.\.venv\Scripts\ruff.exe check src\anastomosis\core\sourcelearn.py src\anastomosis\packgen src\anastomosis\sources\learned tests\unit\test_sourcelearn.py tests\unit\test_learned_source.py tests\unit\test_learned_runtime_integrity.py tests\unit\test_packgen_emit.py tests\unit\test_packgen_image_only.py
.\.venv\Scripts\mypy.exe src\anastomosis\core\sourcelearn.py src\anastomosis\packgen\extract.py src\anastomosis\packgen\emit.py src\anastomosis\sources\learned\reader.py src\anastomosis\sources\learned\interpreter.py
.\.venv\Scripts\pytest.exe -p no:cacheprovider --basetemp $bt tests\unit\test_sourcelearn.py tests\unit\test_learned_source.py tests\unit\test_learned_runtime_integrity.py tests\unit\test_packgen_extract.py tests\unit\test_packgen_image_only.py tests\unit\test_packgen_infer.py tests\unit\test_packgen_emit.py tests\unit\test_packinit.py tests\unit\test_packgen_static_is_frequency.py tests\unit\test_packtrust.py tests\unit\test_pf_quarantine.py tests\unit\test_record_summary.py
git diff --check
```

Use a different `$bt` on every invocation. Then enumerate and run the complete
unit, integration, E2E, GUI, C-CDA, FHIR, renderer, and packaging suites rather
than guessing filenames. Record terminal counts and failures in `AUDIT_LOG.md`.

## Ordered remaining work

P0 before merge:

1. Format/review all current diffs; rerun combined focused lint/type/tests and
   then the repository's complete test lanes.
2. Add value-free C-CDA semantic-conservation inventory and prove where the
   missing note sections/clinical collections went. Parsing alone is not a pass.
3. Add end-to-end learned-source -> canonical -> PDF/C-CDA/FHIR outcome ledgers:
   every input field/value receives an emitted, transformed, quarantined,
   rejected, or explicitly unsupported disposition with provenance.
4. Test HAPI FHIR against the existing local container, including transaction
   failure, referential integrity, idempotency, and round-trip conservation.
5. Fix the architecture memo's repository links if they contain four parent
   traversals; from `docs/audits/learned-source/`, repo-root links need three.
6. Create narrowly scoped GitHub issues for verified gaps, cross-link this
   evidence, and push the branch only after the integrated gate is terminal.

P1 architecture/implementation:

1. Destination-first UI state and immutable profile/run manifests.
2. Relational/multi-file/list/attachment source schema with explicit grains and
   joins; streaming/resource budgets for every reader.
3. Pinned offline OCR observation worker and mixed-content page classifier;
   visual fixtures must be synthetic/de-identified and reviewed.
4. Semantic versus visual QA gates; short/long/empty/multiline/table/attachment/
   pagination/font-fallback fixtures and deterministic pixel/geometry metrics.
5. Snapshot/hash template and assets used at render time; execute external pack
   code in a restricted worker or remove executable extension points.
6. Bind route execution to the reviewed destination and run manifest; distinguish
   “prepared” from “delivered/verified” in CLI and GUI.

Stop conditions: never install/download a model, mutate the VM, expose corpus
content, push/merge, or weaken a fail-closed gate merely to make a test green
without recording the exact authority and evidence.
