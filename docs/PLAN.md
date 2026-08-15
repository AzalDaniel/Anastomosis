# Anastomosis — Living Plan

> The canonical roadmap: the mission, the standing decisions, what has
> shipped, and what is next. Updated as milestones complete. Research
> findings verified June 2026; sources are cited in the section docs.

## Mission

EHR vendors must hand practices their data (§170.315(b)(10) EHI export) but
hand over unusable table dumps, and clinical notes routinely fail to survive
cross-vendor migration (Miake-Lye et al., JGIM 2023,
doi:10.1007/s11606-023-08276-3); commercial legacy-archive subscriptions put
simple retention behind recurring fees. Anastomosis closes that gap, free and
open:
**parse the dump → rebuild the charts → verify every byte → deliver anywhere.**

Three personas, one pipeline:
- **Migrator** — switching EHRs; destination = the new system.
- **Archivist** — left an EHR, must retain records 5–30 years; destination = a
  searchable offline archive (plain folders + PDFs + JSON, readable forever).
- **Responder** — record requests; destination = per-patient chart bundles.

## Architecture

The five stages (**ingest → canonical model → reconstruct → verify →
deliver**) and the reasoning behind them live in [DESIGN.md](../DESIGN.md);
the package-by-package map lives in [README.md](../README.md). Two rules
govern every addition planned below: everything speaks to the canonical
model rather than to other modules, so the integration matrix stays
(sources + destinations); and every vendor-facing part is an isolated,
versioned module behind a defensive registry, so a vendor changing a UI, an
API, or an export format is a one-module event.

### Pack contracts

The interfaces a new pack must implement — the stable surface future work
builds against:

- **Template pack**: `pack.yaml` (locale/timezone, page geometry, partials,
  filename rules, section flags, QA colors/tokens, L3-verify header fields) +
  `template.html` + `context.py` (`build_context(encounter, record, cfg)`).
  Section flags make every section user-togglable (addenda, insurance, social
  history…) — surfaced as the GUI's section-selection checkbox matrix.
- **QA check**: `run(pdf_path, ctx) -> CheckResult{pass|warn|fail, findings}`.
- **Destination pack**: classes implementing `destinations/base.py` protocols
  (Session, Selectors, UploadDriver, PatientResolver, BannerCheck,
  ExistingDocsScanner) + a `capabilities:` declaration + `config_schema.json`
  for the fields the operator must supply.
- Discovery order: `--pack-dir` → entry points (`anastomosis.packs`) →
  built-ins.

### The delivery router

`deliver/router.py` prefers **vendor API → C-CDA import → browser
automation** and shows the choice to the operator as a transit map.
Destination capabilities are *data*, not code: `destinations/registry.yaml`
entries must carry a source URL and a verified date or registry validation
fails loudly, and an `unverified` capability is never viable. Re-verification
is a YAML edit, not a release.

### The pack-from-samples layout learner (`packgen/`)

Extraction stays deterministic, offline, and explainable (PyMuPDF only, no
torch) so every inferred token traces back to specific spans in specific
samples. The v2 upgrade path under evaluation is Granite-Docling-258M
(Apache-2.0); torch-heavy or license-restricted alternatives (Docling,
LayoutParser, Marker) stay out.

## Decisions (settled)

1. **License: AGPL-3.0**, no CLA (relicensing impossible — a trust feature),
   DCO sign-off. AGPL's network clause prevents closed-SaaS wrapping, which is
   exactly the protection wanted; PyMuPDF (AGPL) is license-aligned.
2. **Scope**: the full toolkit — pipeline + migration engine + GUI. The CLI is
   the automatable surface; the GUI carries the clinical-user impact. All demos
   and tests run on synthetic data only.
3. **CLI**: `anast` (+ `anastomosis` alias). Package `anastomosis`, src layout.
4. **PHI rule (non-negotiable)**: no real PHI ever enters this repo. Never
   copy files from the private predecessor — every port is a re-typed
   refactor. `tools/phi_scan.py` (hashed deny-list + generic patterns) runs
   in pre-commit and CI from commit #1. Fixtures are synthetic
   (`feedface-` GUIDs, 555 phones, never-issued SSN ranges) or Synthea.
5. **GUI**: pywebview preserving the liquid-glass design language
   (OKLCH coral/mint/amber, Mona Sans + JetBrains Mono, glass blur tiers,
   `--ease-quart` motion, no-emoji/no-Material anti-pattern locks); Tauri
   sidecar documented as the v2 path if bundle size demands it.
6. **Standards**: canonical model aligns to FHIR R4 (fhir.resources R4B),
   USCDI data classes; C-CDA note LOINC types (Progress 11506-3, H&P 34117-2,
   Discharge 18842-5, Consult 34111-5) define template-pack taxonomy.

## Security backlog

Shipped and now part of the contract (regressions are security findings, see
[SECURITY.md](../SECURITY.md)): the PHI scanner in pre-commit and CI with an
allowlist ledger and a git-free fallback walk; ruff bandit-S + naive-datetime
rules and `mypy --strict`; CI least-privilege permissions and gitleaks;
advanced CodeQL with audited, per-site inline suppressions; log redaction
(`core/logutil.py` — `RedactionFilter`, `exc_tag()`, `safe_log_id()`)
installed at both entry points; output hygiene (`core/output.py` — `0o700`
on POSIX, NTFS ACL hardening on Windows, PHI-warning README everywhere);
the pack trust model (built-ins trusted, external packs opt-in); loopback-only
CDP attach that never stores credentials; PyPI Trusted Publishing with
Sigstore/PEP 740 attestations.

Still open, ranked, all targeted at M6:

- [ ] uv + committed `uv.lock` (hash-pinned dependencies)
- [ ] semgrep lane alongside CodeQL
- [ ] OpenSSF Scorecard action + badge
- [ ] pack hash-pinning and signing (the trust model's v2)
- [ ] hypothesis property tests on every parser; mutmut on the QA suite
- [ ] REUSE/SPDX headers, mkdocs-material, release-please

## Milestones

Shipped:

- **M0 — Bootstrap** ✅ — packaging (src layout, extras), Typer skeleton,
  ruff/mypy/pytest config, PHI scanner + hashed deny-list + canary
  self-tests, CI matrix (ubuntu + windows × 3.11/3.12), pre-commit, the
  initial doc set.
- **M1 — Archive vertical slice** ✅ — the lossless canonical model
  (`extensions` on every record), the core utility ports
  (`timeutil`/`textutil`/`codes`), the PF/Tebra v9 adapter over a synthetic
  29-table fixture, C-CDA ingest, the FHIR bundle round-trip, the
  Chromium rendering engine + defensive pack registry (`generic_soap`,
  `practice_fusion_soap`), the QA engine with boundary-anchored matching,
  the searchable offline archive and per-patient bundles, and
  `anast pipeline run` with golden and e2e lanes.
- **M2 — Migration mode** ✅ — the resumable browser-delivery engine (15-state
  WAL-SQLite ledger, kill-and-resume tested against a fake destination), the
  L0–L6 verification ladder on by default, the Tebra destination pack +
  discovery wizard, the capability registry and shortest-path router, the
  FHIR DocumentReference pusher, and C-CDA export.
- **M3 — Pack-from-samples** ✅ — `packgen/` extract/infer/emit and
  `anast pack init --from-samples` with side-by-side review, emitting a
  loadable draft pack behind a same-patient confirmation gate.
- **M4 — GUI** ✅ — the pywebview shell over a headless, testable controller:
  pipeline dashboard, migration wizard with the transit map, section-selection
  matrix, upload console, and the pack-from-samples wizard.
- **M5 — 0.2.0** ✅ — any-EHR-to-any-EHR generalization: a FHIR R4/US Core
  source adapter, the standard HL7 C-CDA render mode, `anast migrate
  --from … --to …`, and **learn-a-source** (teach a flat export format from
  one example as a validated, data-only mapping).
- **M5.5 — 0.3.0** ✅ — one shared command core per flow behind both
  frontends, non-UTF-8 Windows console safety, and the packaged Windows
  application (two Nuitka standalone executables bundling Chromium, an Inno
  Setup installer, `anast doctor` self-check, all validated on Windows CI).
- **M5.75 — 0.4.0** ✅ — security truth: NTFS ACL hardening on every output
  directory, the redacting log handler actually installed at both entry
  points, advanced CodeQL with audited suppressions, a single-sourced
  Playwright pin, and the design record (`DESIGN.md`).
- **M5.9 — 0.5.0** ✅ — `safe_log_id()` per-process HMAC surrogates in place
  of raw source identifiers everywhere, a git-free PHI-scanner fallback walk
  so source-ZIP users can run the full suite, and a release path that ships
  the Windows installer without a terminal tag push.
- **M5.95 — 0.6.0** ✅ — claims match the runtime: `migrate` reports PREPARED
  (never DELIVERED) for a chosen-but-unexecuted route, every render mode runs
  the document-generic QA checks and records the rest as skipped-with-reason,
  one upload verdict shared by CLI and GUI, flow-scoped GUI events, race-free
  pack trust, and the PF-mapper index hoist.

Next:

### M6 — Post-release breadth & hardening

1. **More source adapters**: `sources/epic_ehi/` (public table spec +
   rtfparse), `sources/athenahealth/` (NDJSON), and a
   `sources/generic_tabular/` YAML mapping DSL covering DrChrono CSV,
   ModMed pipe-CSV, and Veradigm TSV. Oracle Health/Cerner
   (`sources/oracle_ehi/`) graduates from experimental as its remaining
   clinical domains land (see Open work).
2. **API delivery execution**, per the verified priority order: Epic
   `DocumentReference.Create`, athenahealth Document-Create, DrChrono
   `POST /api/documents`, Canvas FHIR — plus the durable delivery receipt
   that lets `migrate` legitimately report DELIVERED.
3. **C-CDA conformance**: CDA R2 XSD structural validity for the exporter
   output (today's C-CDA is round-trip-faithful through this repo's own
   parser but omits `author`/`custodian` and uses non-OID id roots), then an
   HL7 Schematron conformance lane via external validator tooling.
4. **Security backlog completion** (the open list above) and the PyPI
   install story (`pipx install anastomosis[render]`) as the default path.
5. **Breadth**: quarterly capability-registry re-verification, the
   Granite-Docling packgen upgrade evaluation, OCR ingest for
   scanned-PDF-only practices, a Tauri evaluation, and i18n/EHDS.

## Open work (not blocking a release, tracked here)

- **PF pack QA checks** — the pack-specific check set (37 headings, 17
  demographic labels, addenda, visual tokens, insurance) that would sit
  beside the shipped generic checks for `practice_fusion_soap` (issue #4).
- **Layout-learner fixed-point validation** — regenerate the PF pack from its
  own renders and diff against the hand-built pack. The e2e re-discovery
  lane covers section taxonomy and band fill; the full diff is still open.
- **Optional local VLM assist in `packgen/`** (Ollama, section naming) —
  strictly optional and deferred; today's section names come from the
  deterministic bold-span taxonomy with no VLM dependency.
- **Oracle Health adapter breadth** — the shipped adapter reads the seven
  core tables of the single-patient V500 export and maps Patient, Encounter,
  Observation, Condition, AllergyIntolerance, and DocumentArtifact.
  Medications, procedures, and immunizations are unmapped; `CE_BLOB`
  compression raises loudly pending a documented code set.
- **GUI browser-pack readiness chip** — covered by tests but live-unreachable
  until some `destinations/registry.yaml` entry declares
  `browser: {kind: pack}`; re-confirm its live path when one exists.
- **Vendor-change freshness trigger** — `pack_freshness()` fires only when
  discovered selectors predate the registry's evidence by more than 90 days.
  Confirm that semantic against a real pack once an operator-discovered
  selector set and dated browser evidence both exist.
- **Archive search** — the bundled matcher is a small vanilla substring/token
  implementation; vendoring MiniSearch (MIT) waits until the exact build can
  be pinned by hash from a trusted release source. The `index.json` schema is
  forward-compatible either way.

## PHI scrub map (for porters)

Private-repo locations that must NEVER appear here (enforced by scanner):
`src/matches.json` (entire file), `src/generate_pdfs.py:67-72`,
`docs/GOLD_STANDARD.md:125-128,401,406,719,757`, `docs/CLOSING_REPORT.md:98`,
`qa/checks/layout_pagination.py:38-45,57`, `qa/qa_runner.py:77`,
`upload/config.py:56-58,68`. Mechanisms (credential maps, outlier lists,
owner GUIDs) become user-config files with synthetic examples.

## Verification

- `bash tools/check.sh` is the sanctioned local gate and runs exactly what CI
  runs: preflight, `ruff check`, `ruff format --check`, `mypy`, `pytest`, and
  the full-tree PHI scan (pipefail; never piped through `tail`).
- `pytest -m e2e` runs the pipeline lanes. Golden rendering tests pin
  Chromium; `python tools/regen_goldens.py` regenerates them with a
  human-reviewed diff.
- Each destination pack ships canary fixtures; preflight validates selectors
  before any run (vendor-change detection).
