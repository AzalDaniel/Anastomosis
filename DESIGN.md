# Anastomosis — Design

This document records the design of Anastomosis: the architecture, the data
model, the decisions that were genuinely debated, the hardest problems, and
the verification strategy that holds them in place.

Reading map: [README.md](README.md) documents the product (what it does, how
to install and run it, the file-by-file map). [docs/PLAN.md](docs/PLAN.md) is
the living roadmap. [SECURITY.md](SECURITY.md) is the security policy and
threat model. [paper/paper.md](paper/paper.md) is the scholarly summary. This
file explains *why the software is shaped the way it is*.

## The problem, in one paragraph

US-certified EHRs must export a practice's full Electronic Health Information
(45 CFR §170.315(b)(10)), but no format is mandated, so practices receive
vendor-native table dumps that no other system ingests. The clinical
narrative — the part of the chart clinicians actually read — routinely fails
to survive migration — a systematic review of EHR-to-EHR transitions
identifies data migration and continuity of the legacy record as persistent,
under-addressed risk areas (Miake-Lye et al., JGIM 2023). Commercial rescue
puts retention behind recurring fees; manual re-entry costs months.
Anastomosis is a free, open-source,
local-first toolkit that parses the dump, rebuilds the charts, verifies every
document, and delivers the result — to a new EHR, a decades-readable offline
archive, or per-patient record-request bundles.

## Architecture

One pipeline, five stages, one canonical model:

```
INGEST            CANONICAL          RECONSTRUCT        VERIFY          DELIVER
sources/    --->  core/model/  --->  reconstruct/ --->  qa/       --->  deliver/
PF/Tebra TSV      pydantic v2,       template packs     engine +        router picks the
C-CDA / CCD       FHIR R4-aligned,   (Jinja2 +          pack checks     shortest verified path:
FHIR R4 / Bulk    lossless           Chromium)          + L0-L6         vendor API >
Oracle/Cerner     extensions dict                       upload          C-CDA import >
learned formats                                         ladder          browser automation
                                                                        (+ archive, bundles)
```

Two structural rules do most of the work:

1. **Everything speaks to the canonical model, never to each other.** A new
   source adapter or destination only has to map to/from `core/model/`, so
   the integration matrix collapses from (sources × destinations) to
   (sources + destinations).
2. **Every vendor-facing part is an isolated, versioned module behind a
   defensive registry** — source adapters, template packs, QA checks,
   destination packs. A broken or outdated pack is diagnosed and disabled
   without taking the system down. A vendor changing an export format, a UI,
   or an API is a one-module event, never a system event.

## Data model

The canonical model (`core/model/`) is a set of pydantic v2 records aligned
with FHIR R4: Patient, Practitioner, Facility, Encounter (SOAP note sections
plus addenda), Observation (vitals and social history), Condition,
AllergyIntolerance, MedicationStatement/Request, Coverage,
FamilyMemberHistory, Immunization, AdvanceDirective, DocumentArtifact, and
the aggregate PatientRecord.

The load-bearing feature is the `extensions` dict on every record
(`AnastBase`): any source column an adapter does not map is preserved
verbatim under a namespaced key (`pf_tebra:`, `oracle_ehi:`,
`ccda:section:<loinc>`), and an export carrying rows that cannot be
attributed to a known patient is **refused** (`UnsupportedTablesError`)
rather than silently dropped. FHIR round-trips (`to_bundle` → `from_bundle`)
are proven exact by tests, extensions included. This is the losslessness
invariant: in a medical-records tool, silent data loss is the defect class
that matters most, so the model makes it structurally impossible.

## Design choices that were genuinely debated

- **License: AGPL-3.0-or-later, no CLA.** MIT was considered and rejected:
  the network-use clause prevents closed-SaaS wrapping of a tool whose whole
  point is freeing practices from vendor lock-in, and the absence of a CLA
  makes proprietary relicensing permanently impossible — a trust feature for
  healthcare users. PyMuPDF (AGPL) is license-aligned.
- **A canonical model instead of format-to-format converters.** Existing
  open tools (FHIR-Converter, cda2fhir) map between document formats.
  Anastomosis needs to own undocumented vendor tables → verified delivered
  documents end-to-end, which is an architectural gap, not a mapping gap —
  hence a new package rather than an extension of a converter.
- **Verification as the core product, not a test suite.** A migration that
  files the right note in the wrong chart is worse than no migration. Every
  rendered document passes a QA engine with boundary-anchored matching
  (naive substring matching demonstrably false-passes missing content), and
  every upload runs the L0–L6 ladder by default with a live wrong-patient
  banner check. The run report names every level that ran or skipped and
  why, so the product claim can never drift wider than the runtime.
- **Browser automation as the delivery path of last resort.** The router
  prefers vendor API, then C-CDA import, and only then verified browser
  automation — and destination capabilities are *data with cited evidence*
  (`destinations/registry.yaml`): a capability without a source URL and
  verification date fails registry validation loudly. No vendor DOM is ever
  invented; selectors are operator-discovered per tenant.
- **pywebview over Tauri/Electron for the GUI.** The GUI is a thin
  vanilla-JS layer over a headless, fully testable controller; pywebview
  keeps the bundle Python-native and small. Tauri remains the documented v2
  path if bundle size ever demands it.
- **Explainable extraction over ML in the layout learner.** `packgen/` infers
  a template pack from sample PDFs with deterministic, explainable methods
  (font histograms, greedy x-position bucketing — DBSCAN was considered and
  rejected; fills → design tokens; recurring bold spans → section taxonomy).
  Every inference can be traced to specific spans in specific samples; a
  black-box model cannot be audited that way, and auditability wins in this
  domain. An optional, local-only VLM assist is deferred.
- **OCR as layout evidence, never as clinical truth.** All 53 sample PDFs
  the product has been shown — 802 pages — carried zero natively extractable
  words, so a native-text-only learner could not learn the one real sample
  set there is. `packgen/ocr.py` adds a pinned, offline Tesseract CLI worker
  (TSV + hOCR, an environment built from nothing, no network ever, finite
  pixel and time caps, one page per process) and `packgen/evidence.py`
  classifies each page as native-only, mixed, image-only, ambiguous or empty.
  The two evidence streams never merge: a span carries its provenance, a
  recognized one carries the engine's score, and where the two describe the
  same pixels the overlap is recorded as a duplicate or a disagreement and
  HELD — nothing here picks a winner. Recognized geometry may suggest lines,
  columns, bands and page breaks; recognized text may not fill a clinical
  field, and a high-risk value needs an independent structured source or a
  person. Absence of the engine is a refusal that names what to install, not
  a crash and not a silent skip. Rationale and the alternatives weighed:
  `docs/audits/learned-source/OCR_DECISION.md`.
- **Plain versions presented as alphas, not PEP 440 pre-releases.** Releases
  ship as `0.x.0` with the Development Status :: 3 - Alpha classifier and
  "alpha" in prose, because a literal PEP 440 pre-release (`0.4.0a1`) would
  make default `pip`/`pipx` installs silently resolve to the previous
  release — the wrong behavior for a tool whose installer story is "download
  and run".
- **Windows hardening via `icacls`, not pywin32.** Output directories are
  chmod `0o700` on POSIX; on Windows the same function strips ACL
  inheritance and grants only the current user, SYSTEM, and Administrators —
  the exact posture CPython adopted for `mkdir(0o700)` (CVE-2024-4030 fix)
  and Win32-OpenSSH uses for key files. Shelling out to `icacls` with
  literal SIDs was chosen over a pywin32 dependency: one fewer supply-chain
  artifact in a PHI tool, and localization-safe.
- **CLI and GUI drive one shared command core per flow.** Earlier versions
  had parallel implementations that drifted; now migration-status, upload,
  source-init, and pack-init each have a single core (`core/*_command.py`)
  consumed by both frontends, with drift pinned by tests.

## The hardest problems

1. **The wrong-patient defense.** Fuzzy identity matching (L2, ≥0.88 with a
   DOB hard-fail), pack-driven header checks (L3), live banner readback
   during upload (L4), and destination metadata/round-trip checks (L5/L6) —
   layered because each individual check has demonstrated false-pass modes.
2. **Losslessness against undocumented exports.** Vendor sentinel values
   (`\N`, `-1`, `1/1/0001`) must map to `None`, never to a fabricated value
   that could collide with a real clinical value; unmapped tables must
   survive verbatim or refuse the run.
3. **Resumable browser delivery.** A 15-state upload state machine over a
   WAL-mode SQLite ledger that survives a hard kill mid-upload and resumes,
   proven by a kill-and-resume test against a fake destination.
4. **Rendering fidelity worth trusting.** Chromium-pinned golden tests
   (text + geometry), renderer recycling and crash relaunch, deterministic
   filename-collision handling, and a QA engine that fails the pipeline
   nonzero on any document failure.
5. **PHI-safe engineering in the open.** The repository can never contain
   real PHI, yet the tool must be developed against realistic data — so all
   fixtures are synthetic (Synthea or hand-built with `feedface-` GUIDs), a
   hashed deny-list scanner runs on every commit and in CI, and logging is
   redacted by construction (counts, field names, and run-scoped HMAC
   surrogates for source identifiers — never values, never raw GUIDs;
   output paths and patient-derived filenames never enter logs).
6. **Being a real Windows product.** Non-UTF-8 console safety (cp1252),
   Nuitka standalone packaging with bundled Chromium, a self-checking
   installer (`anast doctor` runs against the frozen and the installed
   executable in CI), and NTFS ACL hardening for every output directory.

## Authorship

Anastomosis is designed, directed, and reviewed by its author, Azal Daniel:
the problem selection, the five-stage architecture, the canonical-model and
losslessness invariants, the L0–L6 verification ladder, the
evidence-or-refuse destination rule, the PHI-safe engineering process, and
every decision recorded in this file. It generalizes a private production
system by the same author; nothing was copied from that system — every
port is a re-typed refactor, a rule the PHI scanner enforces.

Substantial portions of the implementation were produced with AI coding
tools working under that direction. Every change — however it was written —
passes the same review gate before it lands: `bash tools/check.sh` (ruff
including the bandit-S and naive-datetime rules, `mypy --strict`, the full
test suite, and the full-tree PHI scan), plus review against the invariants
above.

Third-party material is vendored and pinned, never re-authored: HL7's
`CDA.xsl` and siblings under `reconstruct/ccda_standard/vendor/` (see that
directory's `PINNED.md`), C-CDA fixtures generated by MITRE Synthea, and the
archive assets covered by `deliver/archive/assets/NOTICE.txt`. All other
fixtures are hand-built synthetic data following the conventions in
[SECURITY.md](SECURITY.md).

## Verification strategy

Four layers, because each catches what the others miss: (1) unit and
property tests over parsers and models; (2) Chromium-pinned golden tests
over rendered output; (3) end-to-end pipeline lanes over synthetic fixtures
(including kill-and-resume for uploads); (4) drift tests that pin
cross-file contracts — CLI ↔ GUI parity, `tools/check.sh` ↔ CI parity, the
Playwright pin, frontend ↔ backend constants, import boundaries. The full
local gate is one command: `bash tools/check.sh`.

## Releases

Versions are plain `0.x.0`, presented as numbered alphas (`0.4.0-alpha` is
the fourth), tagged `v<version>`, released to PyPI via Trusted Publishing
with SLSA provenance, and packaged as a self-checking Windows installer.
The GitHub release for an alpha is flagged "pre-release".
