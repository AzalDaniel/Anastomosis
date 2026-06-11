# Anastomosis — Living Plan

> The canonical roadmap. Updated as milestones complete. Research findings
> verified June 2026 (live web verification; sources cited in section docs).

## Mission

EHR vendors are legally required to hand practices their data (§170.315(b)(10)
EHI export) but hand over unusable table dumps — and clinical notes routinely
fail to survive cross-vendor migration (73% of orgs hit complications; archive
vendors charge $5K–$150K). Anastomosis closes that gap, free and open:
**parse the dump → rebuild the charts → verify every byte → deliver anywhere.**

An *anastomosis* is the surgical connection between two structures. This
toolkit is that connection between any two EHR systems — by API when both
sides are modern, by reconstruction + browser automation when they aren't.

Three personas, one pipeline:
- **Migrator** — switching EHRs; destination = the new system.
- **Archivist** — left an EHR, must retain records 5–30 years; destination = a
  searchable offline archive (plain folders + PDFs + JSON, readable forever).
- **Responder** — record requests; destination = per-patient chart bundles.

## Architecture

```
INGEST          CANONICAL         RECONSTRUCT       QA            DELIVER
sources/   ->   core/model/   ->  reconstruct/  ->  qa/       ->  deliver/
PF/Tebra TSV    pydantic v2,      template packs    engine +      router picks
C-CDA           FHIR R4-aligned,  (Jinja2 +         pack checks   shortest path:
Epic EHI        extensions dict   Chromium)                       1 vendor API
athena NDJSON   preserves every                                   2 C-CDA import
generic CSV     unmapped field                                    3 browser automation
                                                                  + offline archive
                                                                  + responder bundles
```

**Brain-like modularity (core invariant):** every source adapter, template
pack, QA pack, and destination pack is an isolated, versioned module with its
own manifest, capability declarations, and canary fixtures. The registry
loads packs defensively: a broken/outdated pack is marked unavailable with a
diagnosis (and a re-discovery wizard offer) while everything else keeps
working. A vendor changing their UI, API, or export format is a one-module
event, never a system event.

### Pack contracts
- **Template pack**: `pack.yaml` (locale/timezone, page geometry, partials,
  filename rules, section flags, QA colors/tokens, L3-verify header fields) +
  `template.html` + `context.py` (`build_context(encounter, record, cfg)`).
  Section flags make every section user-togglable (addenda, insurance, social
  history…) — surfaced as the GUI's section-selection checkbox matrix.
- **QA check contract** (preserved verbatim from the battle-tested
  predecessor): `run(pdf_path, ctx) -> CheckResult{pass|warn|fail, findings}`.
- **Destination pack**: classes implementing `destinations/base.py` protocols
  (Session, Selectors, UploadDriver, PatientResolver, BannerCheck,
  ExistingDocsScanner) + `capabilities:` declaration (see router) +
  `config_schema.json` for required user fields.
- Discovery order: `--pack-dir` → entry points (`anastomosis.packs`) → built-ins.

### The shortest-path delivery router (`deliver/router.py`)
Destinations declare capabilities with **verified evidence**, stored as data:

```yaml
# destinations/registry.yaml (every entry carries source URL + verified date)
epic:        {doc_write_api: fhir_documentreference, ccda_import: care_everywhere, browser: none-yet}
athenahealth:{doc_write_api: vendor_rest, ccda_import: yes, browser: none-yet}
drchrono:    {doc_write_api: vendor_rest_post_documents}
canvas:      {doc_write_api: fhir_documentreference}
tebra:       {doc_write_api: none-verified-2026-06, browser: destinations/tebra}
practice_fusion: {doc_write_api: none (FHIR read-only), browser: none-yet}
```

Route preference: **vendor API → C-CDA import → browser automation**, chosen
automatically, shown to the user as a transit map in the wizard. The registry
is data, not code: re-verification updates a YAML file, and entries must never
be asserted without a source URL (no-hallucination rule, enforced in review).

### The pack-from-samples layout learner (`packgen/`) — first-of-kind
Verified gap: no OSS exists that learns a document template from N samples
and re-renders with new data. The PF pack took 5 manual forensic sprints;
deterministic extraction can auto-derive ~60–70% of it:

1. **Extraction pass** (PyMuPDF only — no torch, fully offline):
   font histogram → type scale; x-position clustering (DBSCAN) → column
   grids (the demographics-table alignment that took hours by hand);
   `get_drawings()` fills → design tokens (the #f1f1f1 discovery, automated);
   repeated bold spans across samples → section-heading taxonomy;
   per-section page-position statistics → page-break rules;
   strings present in ALL samples → static text & empty-state strings.
2. **Hard parts get a human**: side-by-side review (original sample vs
   re-rendered synthetic) with live token tweaking; conditional sections
   (present in K<N samples) flagged for confirmation; table structure via
   optional Camelot.
3. **Optional local VLM assist** (Ollama, e.g. granite-vision — never a
   required dependency, never cloud): suggests section names.
4. Output: a complete draft template pack, immediately renderable.

Deliberately NOT used in v1: Docling/LayoutParser/Marker (torch-heavy or
license-restricted); Granite-Docling-258M (Apache-2.0) is the v2 upgrade path.

## Decisions (settled)

1. **License: AGPL-3.0**, no CLA (relicensing impossible — a trust feature),
   DCO sign-off. AGPL's network clause prevents closed-SaaS wrapping, which is
   exactly the protection wanted; PyMuPDF (AGPL) is license-aligned.
2. **CS50 submission = everything**: pipeline + migration engine + GUI.
   CLI satisfies submit50/check50; GUI carries the clinical-user impact.
   Demo on Synthea synthetic data; destination upload shown dry-run/blurred.
3. **CLI**: `anast` (+ `anastomosis` alias). Package `anastomosis`, src layout.
4. **PHI rule (non-negotiable)**: no real PHI ever enters this repo. Never
   copy files from the private predecessor — every port is a re-typed
   refactor. `tools/phi_scan.py` (hashed deny-list + generic patterns) runs
   in pre-commit and CI from commit #1. Fixtures are synthetic
   (`feedface-` GUIDs, 555 phones, never-issued SSN ranges) or Synthea.
5. **GUI**: pywebview v1 preserving the existing liquid-glass design language
   (OKLCH coral/mint/amber, Mona Sans + JetBrains Mono, glass blur tiers,
   `--ease-quart` motion, no-emoji/no-Material anti-pattern locks); Tauri
   sidecar documented as the v2 path if bundle size demands it.
6. **Standards**: canonical model aligns to FHIR R4 (fhir.resources R4B),
   USCDI data classes; C-CDA note LOINC types (Progress 11506-3, H&P 34117-2,
   Discharge 18842-5, Consult 34111-5) define template-pack taxonomy.

## Security backlog (state of the art 2026, ranked)

Woven into milestones; tracked here:
- [x] PHI scanner (hashed deny-list, SHA-256) in pre-commit + CI
- [x] ruff with bandit (S) + naive-datetime (DTZ) rules; mypy --strict
- [x] CI least-privilege permissions; gitleaks pre-commit
- [ ] uv + committed `uv.lock` (hash-pinned deps) — M6
- [ ] CodeQL + semgrep CI lanes — M6
- [ ] PyPI Trusted Publishing + Sigstore attestations (SLSA L2) — at first release
- [ ] OpenSSF Scorecard action + badge — M6
- [x] Log redaction (`core/logutil.py`): RedactionFilter scrubs
      SSN/phone/email/date shapes; `exc_tag()` on error paths — discipline
      stays "log counts and ids, never values"
- [x] Output hygiene (`core/output.py`): output dirs `0o700` + PHI warning
      README (optional zip-AES/age encryption still open — M2)
- [x] Pack trust model v1: built-ins implicitly trusted; external packs need
      explicit `--pack-dir`/allow_external opt-in (hash pinning + signing — M2)
- [x] PHI scanner hardening: untracked-file blind spot closed; allowlist
      ledger (`tools/phi_allowlist.txt`) with justification requirement;
      `tools/check.sh` is the only sanctioned local gate (pipefail)
- [ ] CDP attach: loopback-only, warn on shared machines, never store creds — M2
- [ ] hypothesis property tests on parsers; mutmut on QA suite — M6
- [ ] REUSE/SPDX headers incl. vendored MiniSearch; mkdocs-material; release-please — M6

## Milestones

### M0 — Bootstrap ✅ (this commit)
pyproject (src layout, extras: render/fhir/deliver-browser/gui/dev), Typer
skeleton (`anast --version`, `anast info`), ruff/mypy/pytest config, gitignore
PHI guards, PHI scanner + 92-token hashed deny-list + canary self-tests, CI
(phi-scan, lint, test matrix ubuntu+windows × 3.11/3.12, e2e lane), pre-commit,
this plan, README/SECURITY/CONTRIBUTING/DISCLAIMER.

### M1 — Archive vertical slice (the pipeline proof)
1. ✅ Canonical model (`core/model/`): AnastBase{id, extensions, provenance} —
   every unmapped source column lands in `extensions` (lossless guarantee);
   Patient, Practitioner, Facility, Encounter (SOAP NoteSections + addenda),
   Observation (vitals + 17 social-history subcategories), Condition,
   AllergyIntolerance, MedicationStatement/Request (escript transactions),
   Coverage, FamilyMemberHistory, Immunization, AdvanceDirective,
   DocumentArtifact, PatientRecord.
2. ✅ Core utility ports (`core/{timeutil,textutil,codes}.py`): sentinel cleaning
   (`\N`,`-1`,`1/1/0001`), 7-format date parsing, `to_local(dt, tz)` via
   zoneinfo (DST-oracle tested against the predecessor's hand-rolled math),
   phone/age/HTML sanitizers, LOINC vital map + BMI auto-calc + pain LA codes.
3. ✅ Synthetic PF/Tebra TSV fixture (29 tables, 3 patients/6 encounters,
   built per the public PF EHI data dictionary **v9, 2026-01-12** — verified
   live 2026-06-11; see fixture README for the VERIFIED/INFERRED ledger)
   covering sentinels, BMI trigger, PlanType fallback chain, same-day
   filename collision, addendum, SIMPLE notes, pediatric record. Note:
   v9 has no MRN/PRN column and no dedicated vitals table (vitals are LOINC
   rows in patient-encounter-observations); social history is split across
   per-topic tables — remaining predecessor subcategories ride through
   `extensions` until verified against a real export.
4. ✅ `sources/pf_tebra/` adapter (loader/mapper/escript) — joins the v9
   graph into PatientRecords; lossless extensions enforced per-table;
   BMI auto-calc unit-aware (in/cm, lb/kg); escript status priority
   resolution; no module-level execution (registry import only).
5. `sources/ccda/` + `core/fhir/{export,ingest}.py` + Synthea fixtures.
6. 🔶 `reconstruct/` engine ✅ (renderer recycling, crash relaunch,
   deterministic GUID-suffix collision allocation, idempotent skip, PHI-safe
   failure reporting) + pack registry ✅ (defensive loading, section flags,
   external-pack opt-in) + `generic_soap` built-in ✅. Still open:
   `packs/practice_fusion_soap` (template.html near-verbatim; PROVIDER
   credentials → user site_overrides; logo → neutral placeholder) +
   `generic_chart_summary`. NOTE for the PF pack port:
   `Encounter.date_of_service` is now a calendar date (DateField semantics —
   midnight-UTC datetimes shifted DOS a day west of UTC).
7. 🔶 `qa/` engine ✅ (registry; engine checks: data_integrity,
   layout_pagination, vitals_loinc, date_staleness; boundary-anchored
   matching — substring matching is a proven false-PASS factory; skipped
   docs re-verified on re-runs; mutation-corpus self-tests; CLI `--qa`
   stage, exit 1 on FAIL). Still open: PF pack checks (37 headings, 17
   labels, addenda, visual tokens, insurance) — land with the
   practice_fusion_soap pack (issue #4).
8. `deliver/archive/` offline static site (vendored MiniSearch, CSP-pinned,
   zero network; plain dirs+PDFs+JSON = 30-year durability) + `deliver/bundle/`.
9. 🔶 CLI glue ✅: `anast pipeline run <dir> --out <dir>` (auto-detect,
   --pack/--pack-dir, --section flag overrides, --force; failures exit
   nonzero with exception types only); `anast info` lists sources/packs.
   Still open: golden rendering tests (text+geometry via PyMuPDF, Chromium
   pinned); Synthea e2e.

### M2 — Migration mode
10. `deliver/browser/` port (engine/tracking [WAL SQLite, 15-state machine,
    resumability]/batch/manager/cdp/parallel/errors/reports/manifest/skiplist)
    with FakeDestination test double; kill-and-resume test.
11. `deliver/verify/` L0–L6 port (L2 fuzzy ≥0.88 + DOB hard-fail; L3 pack-driven
    header fields; L4 banner check = the wrong-patient defense).
12. `destinations/tebra/` pack + `anast destination init` discovery wizard +
    capability registry + **`deliver/router.py`** (shortest-path selection).
13. `deliver/fhir_api/` pusher (HAPI/Medplum CI service container) +
    `deliver/ccda_export/` (generate C-CDA for destinations that import it).

### M3 — Pack-from-samples (the layout learner)
14. `packgen/extract.py` (spans/drawings harvest), `packgen/infer.py`
    (type-scale, column grids, tokens, section taxonomy, page-break stats,
    static-text intersection), `packgen/emit.py` (draft pack writer).
15. `anast pack init --from-samples ./samples/*.pdf` + side-by-side review
    rendering; validation: regenerate the PF pack from synthetic PF-style
    samples and diff against the hand-built pack.
16. Optional Ollama VLM hook (section naming), strictly optional.

### M4 — GUI (liquid-glass, pre-submission)
17. Port pywebview shell + controller + web assets; pipeline dashboard
    (ingest→reconstruct→QA→archive with live counters).
18. Migration Wizard (source → destination → transit-map route via router);
    Section-Selection Matrix (checkbox grid → pack section flags).
19. Upload console (patient command sheet Cmd+K, calendar HUD with halos,
    liquid toggles, error-inspector flyout, command palette) per the extracted
    token sheet; pack-from-samples wizard UI; vendor-change detection toasts.

### M5 — CS50 packaging
20. README final (video URL, file-by-file walkthrough, design rationale,
    provenance: predecessor reconstructed 12,906 PDFs at 100% final QA);
    demo storyboard (problem → EHI folder → one command → archive + faithful
    PDF → GUI run → dry-run/blurred destination upload with verification log);
    submit50/check50 dry-run.

### M6 — Post-submission breadth & hardening (the FOSS life)
21. `sources/epic_ehi/` (public table spec + rtfparse), `sources/athenahealth/`
    (NDJSON), `sources/generic_tabular/` YAML mapping DSL (DrChrono CSV,
    ModMed pipe-CSV, Veradigm TSV).
22. API delivery adapters per verified priority: Epic DocumentReference.Create,
    athenahealth Document-Create, DrChrono POST /api/documents, Canvas FHIR.
23. Security backlog completion (uv.lock, CodeQL/semgrep, Scorecard, Trusted
    Publishing + Sigstore, hypothesis/mutmut, REUSE, mkdocs, release-please);
    PyPI release (`pipx install anastomosis[render]`); quarterly registry
    re-verification ritual; Granite-Docling packgen upgrade evaluation;
    OCR ingest for scanned-PDF-only practices; Tauri evaluation; i18n/EHDS.

## PHI scrub map (for porters)

Private-repo locations that must NEVER appear here (enforced by scanner):
`src/matches.json` (entire file), `src/generate_pdfs.py:67-72`,
`docs/GOLD_STANDARD.md:125-128,401,406,719,757`, `docs/CLOSING_REPORT.md:98`,
`qa/checks/layout_pagination.py:38-45,57`, `qa/qa_runner.py:77`,
`upload/config.py:56-58,68`. Mechanisms (credential maps, outlier lists,
owner GUIDs) become user-config files with synthetic examples.

## Verification

- `pytest` (unit), `pytest -m e2e` (pipeline), `python tools/phi_scan.py`
  (full tree), `ruff check . && ruff format --check .`, `mypy`.
- Golden rendering tests pin Chromium; `anast dev regolden` regenerates with
  human-reviewed diff.
- Each destination pack ships canary fixtures; preflight validates selectors
  before any run (vendor-change detection).
