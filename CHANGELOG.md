# Changelog

All notable changes to Anastomosis are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Until 1.0.0,
minor versions may contain breaking changes (noted here when they happen).

## [Unreleased]

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
