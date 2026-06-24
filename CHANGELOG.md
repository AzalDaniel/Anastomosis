# Changelog

All notable changes to Anastomosis are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Until 1.0.0,
minor versions may contain breaking changes (noted here when they happen).

## [Unreleased]

## [0.3.0] — 2026-06-24

The third alpha. Packages the toolkit as a downloadable Windows application — a
normal installer that bundles its own Python runtime and Chromium (with no
separate `pip`/`playwright` step) and installs the Edge WebView2 runtime when it
is absent — and clears the last CLI/GUI disparities so the backend CLI and the
desktop GUI drive identical shared command cores rather than parallel
implementations. The packaging build, the installer, and a silent
install-and-self-check are produced and validated on Windows CI. PR numbers in
parentheses.

### Added

- **Downloadable Windows application** (`packaging/`,
  `.github/workflows/windows-package.yml`) — two Nuitka `--mode=standalone`
  executables (the windowed GUI app and the `anast` console CLI), each bundling
  the Python runtime, Chromium, and every data asset, packaged by Inno Setup
  into a single installer with a Start-menu shortcut, an uninstaller, an optional
  "add `anast` to PATH" task, and a silent Edge WebView2 install when the runtime
  is absent. No separate `pip install` or `playwright install` step. Built and
  self-checked on Windows CI — installed silently and re-checked end to end — and
  attached to the GitHub release on a version tag.
- **`anast doctor`** (`core/selfcheck.py`) — a bundled-asset self-check that
  resolves and reads every shipped asset (the destination registry and tebra
  pack, both built-in template packs, the GUI web pages and fonts, the HL7
  `CDA.xsl` and its siblings, the learned-source synonym and schema files, the
  archive assets) and, in a frozen build, confirms the bundled Chromium is
  present. CI runs it against the FROZEN executable before packaging and again
  against the INSTALLED executable after, so a mis-bundled asset fails the build
  instead of reaching an operator. (#63)
- A standalone GUI entry point (`anastomosis.gui.__main__` plus a `gui-scripts`
  console entry) that the installed Start-menu shortcut targets. (#63)
- **L0–L6 verification ladder around uploads** (`anast upload` + a GUI "Verify
  uploads" toggle) — the implemented `LayeredVerifier` (`deliver/verify/`) is now
  reachable from both frontends through the shared upload command: L0 file
  integrity, L1 page/size, L2 document identity (fuzzy name ≥0.88 + DOB
  hard-fail), and, after upload, L5 metadata and L6 round-trip read-back. It runs
  by default (see Fixed); the engine's live wrong-patient banner abort runs on
  every upload regardless.

### Changed

- **One shared command core per flow, consumed by both the CLI and the GUI**, so
  the two frontends cannot diverge: migration-status classification
  (`core/migration_status.py`, #57), the upload command (`core/upload_command.py`,
  #58), and the source-init command (`core/source_init_command.py`, #59). The GUI
  learn-a-source wizard now runs asynchronously, like the pipeline, migrate, and
  pack flows. (#59, #60)
- The Practice Fusion pack's `build_context` is decomposed into focused,
  output-preserving helpers, and the flowsheet's vital-by-encounter scan is built
  once per record instead of once per encounter. Rendered output is byte-identical.
  (#61, #62)

### Fixed

- **The CLI no longer crashes on a non-UTF-8 (e.g. CP-1252) Windows console.** A
  new `core/presentation.py` resolves Unicode versus ASCII glyphs from the output
  stream's encoding; the transit map and every arrow-printing line use it, and
  the ASCII markers are bracket-free so Rich does not strip them. UTF-8 output is
  unchanged. (#56)
- The GUI surfaces a no-automated-route migration as an error and manual-import
  path instead of a silent success, matching the CLI (which already writes the
  C-CDA payload and exits non-zero). (#57)
- Upload `max_attempts` is unified to 3 across the CLI and GUI, the GUI gains a
  `--skiplist`, and the GUI now acquires the busy-guard and output lock BEFORE
  reading the upload manifest, closing a lock-then-read race. (#58)
- All five GUI async methods (`run_pipeline_async`, `run_migration_async`,
  `pack_init_async`, `source_init_async`, `upload_start`) now guard the
  worker-thread spawn: if `Thread.start()` fails, they release the busy flag and
  return a clean error dict instead of propagating to the bridge and wedging the
  GUI in "Busy".
- **No silent table loss in the Practice Fusion adapter.** The loader now reads
  EVERY `*.tsv` in an export (not just the 30 it maps); the mapper preserves each
  unmapped table's rows verbatim in the owning patient's `extensions`, and refuses
  the run (`UnsupportedTablesError`) when a table's rows cannot be attributed to a
  known patient — failing closed rather than discarding clinical data (e.g. an
  unmapped `patient-procedures` table).
- **Upload verification is ON by default and fails closed.** `anast upload` (and
  the GUI) now run the L0-L6 wrong-chart/wrong-patient ladder unless the operator
  explicitly passes `--no-verify`; if the render extra the ladder needs is absent,
  the run is refused rather than filing unverified. Filing into the wrong chart is
  worse than not filing.
- **Upload lock fences the migrate layout too.** `run_upload_command` now locks
  both the output dir AND the resolved manifest root (a `migrate` writes/locks the
  manifest under `<out>/charts`, a different lock dir), closing the lock-then-read
  race for that layout.
- **Windows installer "add to PATH" correctness:** the optional task writes the
  MACHINE `Path` (the install is per-machine/elevated, so the per-user HKCU hive
  would have been the elevating admin's, not the user's); it records an
  installer-owned marker and strips the entry on uninstall ONLY when that marker
  is present (delimiter-anchored), so a pre-existing or manually-added entry is
  never removed; and `ChangesEnvironment=yes` broadcasts the change so a new shell
  sees `anast` without a logout.
- **Windows package integrity:** CI now self-checks the FROZEN GUI bundle
  (`Anastomosis.exe --self-check`, the Start-menu target), not only the CLI bundle;
  the `anast doctor` tebra-pack check targets the BUNDLED pack specifically (a user
  pack can no longer mask a missing built-in); the WebView2 bootstrapper download
  is Authenticode-verified (signer = Microsoft); the release action is pinned to a
  commit SHA; and a release tag is asserted to equal `v<version>`.

## [0.2.0] — 2026-06-15

The second alpha. Generalizes ingest and output so a migration is "from any EHR
to any EHR": a FHIR R4 source adapter, a standard HL7 C-CDA render view, an
`anast migrate` from→to command, and — when the toolkit meets a structured export
it has never seen — the ability to learn that format from a single example. Adds
the browser-upload delivery engine (a CLI and a GUI console that drives it), a
cited destination-capability registry with a shortest-path router, the
pack-from-samples layout learner, and the desktop GUI, all on the v0.1.0
foundational pipeline. PR numbers in parentheses.

### Added

- **Learn a new source format from one example** (`anast source init`,
  `sources/learned/`, `core/sourcelearn.py`, `core/model_paths.py`) — when a flat
  CSV/TSV/JSON/NDJSON export is not recognized, teach it once: a local, PHI-safe
  analysis profiles each column (counts, inferred types, digit/letter-masked
  shapes — never a raw value) and a deterministic matcher (column-name similarity
  via `rapidfuzz` + a shipped synonym table + value-type affinity) proposes a
  mapping to the canonical model, which the operator confirms. The mapping is
  saved as declarative DATA — a validated `MappingSpec` with a closed transform
  verb table, no executable code — auto-detected thereafter by a column
  fingerprint and shareable by copying its directory. Unmapped columns are
  preserved losslessly in `extensions`, and a round-trip proves no column is
  dropped before the mapping is saved. A matching GUI wizard ships too. (#49, #50)
- **`anast migrate --from <source> --to <destination>`** (`core/migrate.py`) —
  the from→to composition: ingest any source, plan the delivery route, and emit
  BOTH the human-readable charts AND the structured C-CDA payload the target
  imports. Re-runnable migration profiles persist in `~/.anastomosis`. PF→Tebra
  becomes the special case `--from pf-tebra --to tebra`. (#46)
- **FHIR R4 / US Core source adapter** (`sources/fhir_r4/`) — ingests a FHIR R4
  Bundle or a Bulk-Data `$export` NDJSON directory into canonical records,
  deterministically and source-traced; unmapped fields → `extensions`. (#44)
- **Standard C-CDA render mode** (`reconstruct/ccda_standard/`,
  `anast migrate --render ccda-standard`) — renders the structured C-CDA payload
  through HL7's own vendored `CDA.xsl` stylesheet to a neutral, vendor-agnostic
  per-patient PDF with no network egress, so a migration is never dressed in
  another vendor's house style. (#45)
- **`anast upload` + a GUI upload console that drives it** (`cli.py`,
  `gui/web/console.{html,js}`) — file reconstructed charts into a destination EHR
  through its web UI over a loopback-only DevTools attach, resumable across a hard
  kill; the GUI console starts/stops/monitors a run against the same ledger and
  never closes the operator's browser. (#47, #54)
- **Browser-delivery safety spine** (`deliver/browser/`) — the 15-state upload
  state machine (`UploadState` + legal-transition graph) over a WAL-mode SQLite
  ledger that survives a hard kill mid-upload, with a `FakeDestination` test
  double and a kill-and-resume test. (#13)
- **Upload engine** (`deliver/browser/engine.py`) — drives one item through the
  state machine: patient resolve, duplicate scan, pre/post verification, upload,
  bounded retry, and a skiplist; loud, PHI-safe permanent vs. transient failure
  classes. (#14)
- **Parallel workers, session manager, CDP attach, and run reports**
  (`deliver/browser/{parallel,manager,cdp,reports}.py`) — bounded concurrency,
  a session/manifest manager, loopback-only Chrome DevTools Protocol attach
  (never stores credentials), and PHI-safe run reports. (#15)
- **L0–L6 verification ladder** (`deliver/verify/`) — the wrong-patient
  defense: L0 file integrity, L1 page/size, L2 identity fuzzy match (≥0.88) with
  a date-of-birth hard-fail, L3 pack-driven header fields, L4 live patient-banner
  readback, L5 destination metadata, L6 byte/identity round-trip; stacked behind
  the engine's verifier seam. (#16)
- **Capability registry + shortest-path router** (`destinations/registry.py`,
  `destinations/registry.yaml`, `deliver/router.py`) — destinations declare
  capabilities as cited data; the router picks vendor API → C-CDA import →
  browser automation, and never routes an `unverified` capability. (#17)
- **Browser destination packs + discovery wizard** (`destinations/browserpack.py`,
  `destinations/wizard.py`, `destinations/tebra/`, `anast destination init`) —
  the Tebra pack ships with DISCOVER-placeholder selectors discovered by the
  operator against their own session; no vendor DOM is ever invented. (#18)
- **FHIR R4 API pusher** (`deliver/fhir_api/`) — a stdlib-`urllib` FHIR R4 REST
  client that files charts as `DocumentReference` resources (https, or http only
  for loopback), validated against a HAPI/Medplum-style integration service. (#19)
- **C-CDA export deliverer** (`deliver/ccda_export/`) — `PatientRecord` →
  C-CDA R2.1 / CCD XML for destinations that import C-CDA, with this repo's own
  C-CDA parser as the read-back contract. (#20)
- **Golden rendering tests + Synthea e2e lane** — text-and-geometry
  golden tests pinning Chromium output, plus an end-to-end pipeline lane over a
  vendored synthetic Synthea C-CDA sample. (#21)
- **Layout-learner harvest + inference** (`packgen/extract.py`,
  `packgen/infer.py`) — PyMuPDF-only, fully offline span/drawing harvest and
  deterministic, explainable inference (type scale, column grids, design tokens,
  section taxonomy, static-text intersection). (#22)
- **Layout-learner draft-pack emitter + wizard** (`packgen/emit.py`,
  `anast pack init --from-samples`) — writes a loadable draft template pack
  (mirroring `generic_soap`) with a same-patient confirmation gate and a DRAFT
  provenance note. (#23)
- **GUI shell + headless controller + pipeline dashboard** (`gui/`) — a
  pywebview shell over a fully testable, never-raising controller and thin
  vanilla-JS pages; the liquid-glass dashboard drives the *same* pipeline core
  as the CLI with live ingest/reconstruct/QA counters. (#24)
- **Migration wizard, section-selection matrix, upload console, and
  pack-init UI** (`gui/web/`, `gui/controller.py`) — the transit map as the
  wizard centerpiece, section-flag toggles on the run form, an upload
  console over the 15-state ledger (exception-TYPE histograms only, opaque item
  keys in the Cmd+K palette), a vendor-change freshness toast, and the
  pack-init page with the same-patient confirmation gate. (#25)
- **Frontend-free pipeline core** (`pipeline.py`) — extracted from the CLI so
  the CLI and GUI drive identical code, emitting PHI-safe `StageEvent`s. (#24)
- **Oracle Health / Cerner Millennium EHI adapter** (`sources/oracle_ehi/`) —
  ingests the single-patient V500 export (`v500/{schema,activity,reference}`
  MySQL dumps) via a dependency-free, tolerant `INSERT`-statement reader that
  raises loudly on malformed SQL. Maps the PERSON/ENCOUNTER/CLINICAL_EVENT
  spine plus the §4 notes pathway (CE_BLOB local text, CE_BLOB_RESULT remote
  document *references* — never fetched), resolves `*_CD` through CODE_VALUE,
  filters to current row versions, and routes every unconsumed column to
  `oracle_ehi:` extensions. CE_BLOB compression (brief §8 could-not-determine)
  is a loud `NotImplementedError`, not a guess; PHI-safe logging throughout.
- **Practice Fusion SOAP-note template pack** (`packs/practice_fusion_soap/`) —
  the 35-section forensic PF chart replica, re-typed from the predecessor's
  gold standard: 3-column PATIENT/FACILITY/ENCOUNTER header, the unified 6-column
  demographics table, active/inactive insurance + payment, vitals + vitals
  flowsheet, diagnoses, drug/food/environmental allergies, current/historical
  medications with the ESCRIPT/SCRIPT prescription lines, immunizations, the 17
  social-history sub-categories, PMH, family/advance-directive/devices/health-
  concerns/goals, SOAP, orders, screenings, observations, quality of care, care
  plan, and the conditional addenda table. Honors the documented engine lessons
  exactly (forensic `#f1f1f1` band, `print-color-adjust: exact`, the
  border-collapse "3 lines not 4" rule, the `orphans/widows: 2` + page-break
  rules, Letter geometry with the `.6/.38/.44/.39in` margins) with all real
  clinic identity synthesized (neutral placeholder logo + footer URL, providers
  from synthetic fixtures). Ships a PF golden lane and a packgen fixed-point
  re-discovery e2e (the learner recovers the pack's section taxonomy + band fill
  from its own renders). `RULES.md` records the forensics; `tools/regen_goldens.py`
  now regenerates every pack's golden. (#4)

### Changed

- **Shared pack-init command core** (`core/packinit.py`) — `anast pack init` and
  the GUI now run one analyze→confirm→emit flow; the GUI variant runs off the
  bridge thread so the window stays responsive. (#51)
- **GUI migrate-wizard parity** — the wizard exposes the same pack-dir / trust /
  force / section / QA levers the CLI's `migrate` does, threaded through to the
  same migration core. (#52)
- **Per-record render index built once per record**, not once per encounter — a
  pure-performance change; rendered output stays byte-identical (the e2e goldens
  prove it). (#53)

### Fixed

- **Clean errors on bad / empty / no-route input** — a malformed or empty export
  now fails with a clean exit 2 (PHI-safe, exception-TYPE name only) instead of a
  raw traceback or a silent zero-document "success"; an `anast migrate` to a
  destination with no viable automated route still writes the importable C-CDA
  but exits 1 loudly; and a run locks every output directory, not just the charts
  dir. (#48)
- **Guarantor mapping read invented columns** — the `pf_tebra` adapter's
  `patient-guarantor.tsv` mapping now reads the predecessor-verified column
  set (`BillingPatientRelationshipOption`, `BillingPaymentType`,
  `DateOfBirth`, `BillingGenderOption`, `SSNumber`, bare `City`/`State`/`Zip`,
  `PrimaryPhoneNumber`/`SecondaryPhoneNumber`), so payment preference, DOB,
  sex and SSN populate on a real export instead of silently coming up empty;
  unmapped guarantor columns stash losslessly into the new
  `Guarantor.extensions`. The PF pack's payment cells render the
  predecessor's exact empty states (`-` everywhere, `Primary Insurance`
  preference default) — a present-but-sparse guarantor previously printed
  literal `None` into the PDF. (#4)
- **Windows tracking race** — set the SQLite `busy_timeout` before switching to
  WAL `journal_mode`, fixing a Windows CI race in the upload ledger. (#15)
- **Tracking busy-timeout on slow CI** — raised the ledger busy timeout to 30s
  because `synchronous=FULL` commits could starve the prior 5s window on CI. (#20)

### Security

- **CDP attach is loopback-only** — the DevTools Protocol attach refuses
  non-loopback hosts, warns on shared machines, and never stores credentials. (#15)
- **FHIR client URL guard** — the FHIR base URL must be https (or http only for
  a loopback host); errors carry status codes and resource TYPE names, never
  patient-derived values. (#19)
- **No-hallucination capability registry** — any non-`none` destination
  capability must carry a `source_url` and `verified` date or registry
  validation fails loudly; `unverified` capabilities never route. (#17)
- **No invented vendor DOM** — the Tebra browser pack ships only DISCOVER
  placeholders; real selectors are operator-discovered per tenant via the
  wizard and stored in a user overlay file. (#18)
- **PHI-safe layout learner** — sample PDFs may be named after patients and
  contain per-patient data, so `packgen` stores opaque sample indices, suppresses
  single-sample static/per-patient inference, and restates the same-patient
  caveat in the emitted `DRAFT.md`. (#22, #23)
- **Pack logo cannot reach the network or the filesystem at large** — the
  PF pack's `tokens.logo_data_uri` override accepts only inline `data:` URIs
  (an http/https/file URL would make Chromium fetch it while rendering PHI),
  and `tokens.logo_asset` refuses paths that resolve outside the pack root. (#4)

## [0.1.0] — 2026-06-11

First release: the complete foundational pipeline — one command from a raw EHI
export to verified, human-readable chart documents and a searchable offline
archive. Everything below shipped across PRs
[#1](https://github.com/AzalDaniel/Anastomosis/pull/1),
[#8](https://github.com/AzalDaniel/Anastomosis/pull/8),
[#9](https://github.com/AzalDaniel/Anastomosis/pull/9), and
[#10](https://github.com/AzalDaniel/Anastomosis/pull/10).

### Added

- **Canonical clinical model** (`core/model/`) — lossless, FHIR R4-aligned
  pydantic v2 core: Patient, Practitioner, Facility, Encounter (SOAP note
  sections + addenda), Observation (vitals + social history), Condition,
  AllergyIntolerance, MedicationStatement/Request (e-script transactions),
  Coverage, FamilyMemberHistory, Immunization, AdvanceDirective,
  DocumentArtifact, PatientRecord. Every model carries an `extensions` dict so
  no source field is ever silently dropped. (#1)
- **Core utilities** (`core/`) — sentinel-safe parsing (`\N`, `-1`,
  `1/1/0001` return `None`, never fake values), 7-format date parsing,
  zoneinfo-based local-time conversion, phone/age/HTML sanitizers, LOINC
  vitals map with unit-aware BMI auto-calculation. (#1)
- **Practice Fusion / Tebra source adapter** (`sources/pf_tebra/`) — joins the
  29-table PF EHI v9 export graph into patient records; lossless `extensions`
  enforced per table; e-script status priority resolution. Built and tested
  against a fully synthetic fixture set. (#1)
- **C-CDA / CCD source adapter** (`sources/ccda/`) — ingests C-CDA R2.1
  continuity-of-care documents: problems, medications, allergies,
  immunizations, vitals, results, encounters, notes, social history;
  unmapped sections preserved under namespaced extension keys. (#9)
- **FHIR R4 export/ingest** (`core/fhir/`) — standard resources with exact
  round-trip: export a PatientRecord to a FHIR R4 Bundle and re-ingest it
  back to an identical record, proven by tests. (#8)
- **Reconstruction engine + template packs** (`reconstruct/`, `packs/`) —
  Jinja2 + Chromium rendering with renderer recycling, crash relaunch,
  deterministic filename-collision handling, and idempotent skip; defensive
  pack registry (a broken pack is diagnosed and disabled without taking the
  system down); built-in `generic_soap` pack with user-togglable section
  flags. (#1)
- **QA engine** (`qa/`) — every rendered document is verified:
  data-integrity (placeholder/unresolved-template leak detection),
  layout/pagination, LOINC vitals presence, and date-staleness checks with
  boundary-anchored matching; mutation-corpus self-tests; `--qa` pipeline
  stage exits nonzero on FAIL. (#1)
- **Offline archive deliverer** (`deliver/archive/`) — static, zero-network
  searchable archive openable from `file://`: plain folders, per-encounter
  HTML, rendered PDFs, and FHIR R4 Bundle JSON per patient — readable for
  decades without a database. (#10)
- **Per-patient bundle deliverer** (`deliver/bundle/`) — chart bundles for
  record requests, with per-patient sliced QA reports. (#10)
- **CLI** (`anast`, alias `anastomosis`) — `anast pipeline run <export-dir>
  --out <dir>` with source auto-detection, `--pack`/`--pack-dir`, section
  flag overrides, and `--force`; `anast info` lists available sources and
  packs. (#1)
- CI across ubuntu + windows × Python 3.11/3.12 with a dedicated PHI-scan
  lane and an e2e lane. (#1)

### Security

- **PHI scanner** (`tools/phi_scan.py`) — full-tree scan with a SHA-256
  hashed deny-list and generic PHI patterns, running in pre-commit and CI
  from the first commit; untracked-file blind spot closed; allowlist ledger
  requires written justification per entry. (#1, #9)
- **Log redaction** (`core/logutil.py`) — a logging filter scrubs
  SSN/phone/email/date shapes; error paths log counts, ids, and exception
  type names via `exc_tag()`, never patient-derived values. (#1)
- **Output hygiene** (`core/output.py`) — output directories created `0o700`
  with a PHI-warning README. (#1)
- **Hardened XML parsing** — the C-CDA parser disables entity resolution,
  network access, DTD loading, and huge trees
  (`resolve_entities=False, no_network=True, load_dtd=False,
  huge_tree=False`). (#9)
- **Pack trust model v1** — built-in packs are implicitly trusted; external
  packs load only with explicit `--pack-dir` opt-in. (#1)
- Strict gates: `mypy --strict`, ruff with bandit (S) and naive-datetime
  (DTZ) rules, gitleaks pre-commit, least-privilege CI permissions. (#1)
- `SECURITY.md` — reporting policy, threat model, and security posture. (#9)

[Unreleased]: https://github.com/AzalDaniel/Anastomosis/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/AzalDaniel/Anastomosis/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/AzalDaniel/Anastomosis/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/AzalDaniel/Anastomosis/releases/tag/v0.1.0
