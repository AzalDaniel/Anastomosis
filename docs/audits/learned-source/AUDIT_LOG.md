# Learned Sample and Migration Audit - Append-Only Log

Keep entries PHI-free and value-free. Record commands by purpose and aggregate
results, never sample contents or original patient-document filenames.

## 2026-08-30 - Recovery checkpoint

- Recovered branch `codex/learned-source-integrity` at `e143cfe` with six intended
  P0 files modified and no unrelated changes detected.
- Confirmed the 6.30 GB local corpus and 53 synthetically named extracted PDFs.
- Confirmed lint and type gates passed; corrected the remembered pytest file list
  because `tests/unit/test_packgen_analyze.py` does not exist.
- User clarified scope as the complete Learn-from-sample plus Migration-toggle
  workflow, including destination-EHR formatting and end-to-end document emission.
- Hyper-V access remained blocked for shell identity `A\codexsandboxoffline`; only
  user `A\azald` was a Hyper-V Administrators member when checked.
- Docker client was present, but no engine pipe was available when checked.
- User subsequently reported starting `Anastomosis-Audit`; runtime recheck pending.

## 2026-08-30 - Adversarial learned-source matrix

- Audited baseline `bbc48a6` without editing implementation or the six existing
  P0 files.
- Enumerated real learned-source, packgen, migration, GUI, C-CDA/FHIR,
  whole-record QA, and PF-quarantine tests. Confirmed the previously referenced
  packgen-analyze test file does not exist.
- Re-ran the learned-source gate: 70 passed in 10.26 seconds.
- Recorded the P0/P1/P2 adversarial matrix in `ADVERSARIAL_MATRIX.md`. The
  highest-priority missing proofs are conflicting duplicate encounter keys,
  blank/ambiguous learned identities, mapped-transform outcome accounting, and
  end-to-end learned-source conservation through C-CDA and FHIR.
- The larger packgen/PF/whole-record focused collection was started but its
  runner output ended without a final summary; it is intentionally not recorded
  as passing and must be rerun to a terminal result.

## 2026-08-30 - Runtime and upstream reconciliation

- Approved read-only runtime checks confirmed the authorized VM and the existing
  `anastomosis-fhir-hapi` container are running normally.
- Fetched GitHub and found five new main commits through `bbc48a6`, including the
  Claude-authored PF quarantine, whole-record QA, PATH, packaging, and supply-chain
  changes. None overlapped the six local P0 files.
- Paused code-reading agents, fast-forwarded cleanly to `bbc48a6`, verified the P0
  diff remained intact, and resumed agents against the current system.
- `git diff --check`, Ruff, and Mypy passed after the fast-forward.
- A focused pytest run showed no failures in partial progress but was terminated by
  the requested Codex restart; it will be rerun and is not recorded as passing.

## 2026-08-30 - Architecture trace completed

- Completed a PHI-free, read-only trace of GUI Teach/Migrate/Uploads through
  controller, migration/pipeline, learned/C-CDA/FHIR/vendor adapters, packgen,
  pack trust, render/QA, routing, and delivery.
- Recorded verified controls, prepared-versus-delivered boundary, disconnected
  learned-layout handoff, generic SOAP/FHIR limitations, and required
  fail-closed target state machine in `ARCHITECTURE_TRACE.md`.
- No source or test files were changed; the six pre-existing P0 modifications
  remain untouched.

## 2026-08-30 - EHR layout research memo

- Added `RESEARCH_EHR_LAYOUT.md`, a PHI-free primary-source memo covering ONC
  EHI, FHIR/US Core, C-CDA, public vendor format/version variability, and
  permissive/open-source layout and visual-regression candidates.
- Defined the interoperability-versus-visual-emulation boundary and measurable
  Learn-from-sample, review, Migration-toggle, semantic-fidelity, provenance,
  determinism, and visual-regression acceptance gates.
- No patient/sample values, filenames, screenshots, or source-code files were
  inspected or changed; this entry records research only and does not assert
  vendor-native or clinical-accuracy equivalence.

## 2026-08-30 - Offline OCR decision record

- Added `OCR_DECISION.md`, a PHI-free, official-source-only decision record for
  offline OCR/layout evidence on image-only and mixed raster/native clinical
  PDF pages.
- Recommended a pinned Tesseract CLI TSV+hOCR worker as the lightweight default;
  RapidOCR/ONNX as an optional cross-platform polygon/score pass; and Docling
  or PaddleOCR PP-StructureV3 only as separately packaged heavy layout options.
- Recorded Windows VM and cross-platform packaging, model/license manifests,
  CPU/RAM/time/network gates, deterministic coordinate handling, OCR confidence
  limits, and the rule that OCR is layout evidence rather than clinical truth.
- Added explicit mixed-page policy: preserve native text separately, OCR only
  raster/ambiguous regions when transforms are known, deduplicate/conflict-hold
  overlaps, and fail closed when provenance is ambiguous. No source/P0 files,
  existing research memo, patient/sample values, filenames, or screenshots were
  inspected or changed.

## 2026-08-30 - PDF and C-CDA corpus evidence

- Parsed all 53 synthetically indexed PDFs: 802 Letter pages, zero encrypted,
  rotated, timed-out, or unreadable samples. Native extraction returned zero
  words for every file; 52/53 had raster images, 264 images total.
- Visually inspected representative rasterized pages under the PDF skill's
  render-and-inspect rule. They contain structured clinical layouts, but no
  source-derived values or filenames are recorded here.
- Added a fail-closed image-only sample gate and synthetic regressions. The gate
  does not yet classify large clinical rasters with small native-text overlays.
- Ran the aggregate-only C-CDA corpus probe: all 2,103 candidates parsed with
  patient identity. Recorded aggregate collection counts in `HANDOFF.md`; note
  sections and several clinical collections were zero, so semantic-conservation
  analysis remains mandatory.

## 2026-08-30 - Learned runtime integrity

- Added value-free refusal for blank patient keys, duplicate patient-grain rows,
  duplicate nonblank encounter keys, and conflicting transformed patient values
  across encounter-grain rows.
- Added deterministic first-nonblank resolution for sparse repeated demographics
  when all nonblank transformed values agree.
- Agent gates reported Ruff and mypy passing and 32 focused tests passing. Root
  still must mechanically format and run the combined integrated lane.

## 2026-08-30 - Offline OCR decision

- Added `OCR_DECISION.md` from primary/official project documentation. It selects
  a pinned, network-disabled Tesseract 5 CLI worker as the smallest default OCR
  observation layer, RapidOCR/ONNX as an optional disagreement/polygon pass, and
  Docling/Paddle only as optional heavy layout adjudicators.
- OCR output is explicitly non-clinical observation evidence. Engine, model,
  config, rasterizer, coordinates, resource limits, and hashes must be recorded;
  high-risk values require structured corroboration or review.

## 2026-08-30 - Lossless handoff checkpoint

- Added `HANDOFF.md` as the authoritative PHI-free resume document with exact
  repository path, branch/base, dirty files, implemented defects, caveats,
  aggregate evidence, runtime permissions, test commands, and ordered P0/P1 work.
- No Supermemory API/tool was available in this session; Git-tracked audit files
  are the durable source of truth and will be redundantly preserved on GitHub
  after the validated branch is pushed.

## 2026-08-30 - First root integrated gate

- Applied Ruff formatting to all 11 changed Python source/test files: seven were
  mechanically reformatted and four were already formatted.
- Root `ruff check`, focused mypy (five source files), and `git diff --check`
  all passed.
- The 161-test integrated learned/packgen/PF/record-summary lane completed with
  160 passes and one failure. The failing legacy regression expected duplicate
  patient-grain rows to collapse first and appear as a dropped column. The new
  P0 runtime correctly refused the invalid duplicate grain before building any
  record. Updated the regression to assert this stronger, value-free refusal;
  the same full lane is being rerun and is not yet recorded as green.

## 2026-08-30 - Root integrated gate green

- Reformatted the adjusted regression and reran the identical combined lane with
  a fresh isolated pytest base. Terminal result: **161 passed in 37.65 seconds**.
- No interrupted/partial run was counted. This proves only the enumerated focused
  learned-source, packgen, pack-init, pack-trust, PF-quarantine, and whole-record
  tests; full repository unit/integration/E2E/GUI/packaging lanes remain pending.

## 2026-08-30 - GitHub recovery issue

- Queried the connected GitHub account and confirmed writes authenticate as
  `AzalDaniel`. The local `gh` CLI is absent, so the connected GitHub API was
  used directly rather than treating that as a blocker.
- Inspected all open repository issues to avoid duplication, then created #308:
  https://github.com/AzalDaniel/Anastomosis/issues/308.
- #308 records the verified architecture and aggregate corpus findings, current
  P0 patch, exact remaining P0/P1 work, no-PHI rule, measured acceptance contract,
  and Claude-implementation/Codex-review handoff.

## 2026-08-31 - Claude work recovered and local branch synchronized

- Read both supplied Claude transcripts and independently compared them with the
  local Git history, connected GitHub PR metadata, and live commit graph.
- Confirmed the original 19-file Codex audit is safe in commit `dae8938` and is
  recognized by GitHub as authored/committed by `AzalDaniel`.
- Fetched GitHub after the ordinary sandboxed fetch was denied access to
  `.git/FETCH_HEAD`; the narrowly approved `git fetch` succeeded.
- Verified a clean fast-forward from `dae8938` to remote audit head `71e7a51`.
  No local change was discarded or merged heuristically.
- Live GitHub main was `28b219e`; PR #310 was open/draft, six commits ahead and
  two behind. PR #318 and PR #320 were open/draft/mergeable. PRs #306, #307,
  #311, #316, and #319 were independently confirmed merged.
- Added `COORDINATION.md` as the new write-ahead recovery record. It captures the
  exact refs, Claude review changes, conservation findings, merge train, owner +
  Codex attribution rule, and idempotent resume commands.

## 2026-08-31 - PR #318 independent security review blocker

- Verified the exact PR #318 workflow and policy test from remote head
  `6277d8488185ea1e34ff0b86a4fe9372d4b3e3fc`; `git diff --check` passed.
- Verified from the pinned actions' own metadata that
  `advanced-security/dismiss-alerts` accepts `sarif-id` and `sarif-file`, and
  that `github/codeql-action/analyze` accepts `output` and emits `sarif-id`.
  Those input names are not the defect.
- Found a merge-blocking trust-boundary defect: the dismissal condition is true
  for every non-PR event while the workflow triggers on `claude/**` pushes.
  Consequently, unmerged feature-branch code can use
  `security-events: write` to dismiss repository alerts. Same-repository PRs
  are also permitted to run the mutation step.
- The policy regression only checks for the words `fork` or `head.repo`; it
  therefore passes the unsafe guard. Required repair: make alert mutation run
  only from accepted `main` code and assert that exact condition in the test.
  PR #318 is not approved or mergeable by policy until the repair and focused
  tests are pushed and independently verified.

## 2026-08-31 - PR #318 repaired and fully gated

- GitHub would not accept `REQUEST_CHANGES` from the owner account on its own
  pull request (HTTP 422), so the same evidence was submitted as a blocking
  `COMMENT` review, ID `5061819799`; no false review state was recorded.
- Repaired `.github/workflows/codeql.yml` so `advanced-security/dismiss-alerts`
  can run only when `github.event_name == 'push'` and
  `github.ref == 'refs/heads/main'`. PRs, feature-branch pushes, and schedules
  still analyze/upload SARIF but cannot mutate alert state.
- Updated the CodeQL config header and `SECURITY.md` to state the same trust
  boundary. Strengthened `test_codeql_policy.py` to require exactly one
  dismissal step and the exact push-to-main guard rather than a word-presence
  proxy. Focused result: **9 passed**; Ruff and format checks green.
- Committed repair `d81e4799d8f3aeff878815111fa2610b2c751b8e` as
  `AzalDaniel`, with `Co-authored-by: Codex <noreply@openai.com>`, and pushed it
  to `claude/a-suppression-that-does-nothing`. GitHub independently confirmed
  the author, committer, trailer, and PR head.
- The literal `bash tools/check.sh` wrapper could not start in this Windows
  checkout because Git had materialized the shell file with CRLF
  (`pipefail\r`). Ran its exact gates with the repository `.venv` instead. The
  first pytest attempt exposed only an inaccessible default Windows pytest temp
  root; reran with an explicit isolated writable `--basetemp` and cache plugin
  disabled. Terminal results: preflight passed; Ruff passed; 318 files formatted;
  mypy passed across 148 source files; **2,248 passed, 7 skipped in 385.86s**;
  complexity passed; PHI scan clean; `git diff --check` passed.
- Removed only two test-created disposable artifacts after inspection:
  `pytest-pr318` under the writable coordination workspace and an untracked
  Chromium `debug.log` containing only transport warnings. Repository checkout
  returned clean.
- GitHub Actions on the repaired head completed successfully: CI run
  `33334006283` and CodeQL run `33334006311`. Explicitly retargeted PR #318 from
  merged stack parent `claude/two-halves-of-one-action` to `main`; compare then
  showed four intended changed files, five commits ahead, zero behind. Actual
  suppression dismissal remains unproved until the repaired workflow executes
  on `main` after merge.
- GitHub recomputed the retargeted PR as mergeable. The connected
  ready-for-review mutation failed without changing state because its GraphQL
  response query asks for removed field `Repository.fullDatabaseId`. The
  in-app GitHub browser was signed out, with no authenticated Chrome, Edge, or
  extension browser available. A squash merge locked to head `d81e479` then
  returned the expected HTTP 405 (`Pull Request is still a draft`); no merge or
  branch mutation occurred. Requested one exact owner action: click `Ready for
  review` on PR #318, but do not merge. Resume by rechecking the head/checks and
  running the already-prepared locked squash merge with Claude and Codex
  co-author trailers.

## 2026-08-31 - PR #320 participation identifier integrity

- Independently reviewed PR #320 at original head
  `9232980c6cc971f597dabd6ef206764fe70bbdbf`. The implementation extracted the
  new C-CDA participation actors, but treated an HL7 CDA `II` as its `root`
  alone. The normative model makes `extension` the identifier within the scope
  of `root`; root-only handling could collapse distinct providers or facilities
  under one assigning authority and silently discarded actor extensions.
- Repaired facility identity to use the complete root-plus-extension pair while
  retaining the established root link. Provider and organization extensions
  are now conserved in `ccda:id`; facilities with one root and different
  extensions remain distinct. An exact duplicate II carrying conflicting
  canonical facility facts now raises a value-free error rather than selecting
  the first value. Three regressions pin these cases.
- Committed the repair as `a7e1932b64ec76789a098978c683679bac8668b7`
  with the repository owner identity and transparent Codex co-authorship, then
  pushed it to the existing `claude/who-wrote-this-note` PR branch.
- Focused C-CDA/FHIR verification completed at **147 passed**; Ruff, formatting,
  focused mypy for the parser, and `git diff --check` were green. Added an
  independent serialization boundary test using the installed FHIR R4B model;
  all **39 participation tests passed**. Committed and pushed that proof as
  `5ef91d37374b5ef5a6418336f3731a87f7c0c71f`.
- GitHub confirms PR #320 remains open, draft, and mergeable at the exact
  `5ef91d3` head. It still requires synchronization with current `main`, the full
  repository gate, and a PHI-free real-corpus aggregate check after PR #318
  lands. The conservation ledger still credits id-bearing actors by root only,
  and the internal FHIR round trip does not preserve provenance; neither
  limitation is represented as solved.
