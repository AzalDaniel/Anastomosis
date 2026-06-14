# Changelog

All notable changes to Anastomosis are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Until 1.0.0,
minor versions may contain breaking changes (noted here when they happen).

## [Unreleased]

Migration mode (M2), the pack-from-samples layout learner (M3), and the
desktop GUI (M4), built on the v0.1.0 Archivist slice. PR numbers in
parentheses.

### Added

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
  C-CDA parser as the read-back contract; completes M2. (#20)
- **Golden rendering tests + Synthea e2e lane** (M1.5) — text-and-geometry
  golden tests pinning Chromium output, plus an end-to-end pipeline lane over a
  vendored synthetic Synthea C-CDA sample. (#21)
- **Layout-learner harvest + inference** (`packgen/extract.py`,
  `packgen/infer.py`) — PyMuPDF-only, fully offline span/drawing harvest and
  deterministic, explainable inference (type scale, column grids, design tokens,
  section taxonomy, static-text intersection). (#22)
- **Layout-learner draft-pack emitter + wizard** (`packgen/emit.py`,
  `anast pack init --from-samples`) — writes a loadable draft template pack
  (mirroring `generic_soap`) with a same-patient confirmation gate and a DRAFT
  provenance note; completes M3. (#23)
- **GUI shell + headless controller + pipeline dashboard** (`gui/`) — a
  pywebview shell over a fully testable, never-raising controller and thin
  vanilla-JS pages; the liquid-glass dashboard drives the *same* pipeline core
  as the CLI with live ingest/reconstruct/QA counters. (#24)
- **Migration wizard, section-selection matrix, upload console, and
  pack-init UI** (`gui/web/`, `gui/controller.py`) — the transit map as the
  wizard centerpiece, section-flag toggles on the run form, a read-only upload
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

### Fixed

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

First release: the complete **Archivist vertical slice** — one command from a
raw EHI export to verified, human-readable chart documents and a searchable
offline archive. Everything below shipped across PRs
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

[Unreleased]: https://github.com/AzalDaniel/Anastomosis/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/AzalDaniel/Anastomosis/releases/tag/v0.1.0
