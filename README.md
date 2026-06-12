# Anastomosis

> Reconstruct, verify, and re-home clinical records.

**anastomosis** *(n., medicine)* — a surgical connection between two structures.
This toolkit is that connection for electronic health records.

**Demo video:** _(link coming with the CS50 submission)_

## The problem

Every certified US EHR is legally required to export a practice's full
Electronic Health Information (21st Century Cures Act, §170.315(b)(10)) — but
the law doesn't say in what *format*. So practices that exercise their right
get a pile of raw vendor tables, and the things that matter most — the
clinical notes — routinely fail to survive migration to a new system. 73% of
healthcare organizations hit significant complications during EHR migrations;
legacy-archive vendors charge $5,000–$150,000; small practices get priced out
or locked in.

Anastomosis is the missing last mile, free and open source:

1. **Ingest** raw EHI exports (Practice Fusion/Tebra TSV, C-CDA/CCD, Oracle
   Health/Cerner Millennium V500 dumps, more adapters coming) into a lossless
   canonical model — every unmapped field preserved, nothing silently dropped.
2. **Reconstruct** human-readable, pixel-faithful clinical documents
   (template packs; the Practice Fusion SOAP-note pack replicates the original
   to forensic standards — or *learn a new layout from your own sample PDFs*).
3. **Verify** with a multi-layer QA engine (data-integrity, layout, identity
   checks) descended from a production system that reconstructed **12,906
   SOAP notes at a 100% final QA pass rate**.
4. **Deliver** by the shortest available path: vendor API where one exists,
   C-CDA import where supported, verified browser automation (with a
   six-layer wrong-patient defense) where neither does — or build a
   **searchable offline archive** that replaces paid legacy-archive
   subscriptions with plain folders, PDFs, and JSON readable for decades.

Local-first by design: **the core pipeline makes zero network calls.**
Your records never leave your machine.

## Status

**v0.1.0 (alpha)** — the Archivist vertical slice works end to end. See
[CHANGELOG.md](CHANGELOG.md) for what shipped and
[docs/PLAN.md](docs/PLAN.md) for the living roadmap.

| Milestone | State |
|---|---|
| M0 Bootstrap (CI, PHI guardrails, CLI skeleton) | ✅ |
| M1 Archive vertical slice (ingest → reconstruct → QA → archive) | ✅ v0.1.0[^1] |
| M2 Migration mode (verified delivery engine, destination packs, router) | ✅ |
| M3 Pack-from-samples layout learner | ✅ |
| M4 Desktop GUI (liquid-glass) | ✅ |
| M5 CS50 packaging (docs, demo, submission) | in progress |

[^1]: the Practice Fusion–faithful template pack ([#4]) and golden
    rendering tests remain post-release: golden rendering and the Synthea e2e
    lane landed in M1.5 ([#21]), but the PF-faithful pack ([#4]) is still
    deferred (blocked on a citable layout reference). The `generic_soap` pack
    ships and is exercised end to end.

[#4]: https://github.com/AzalDaniel/Anastomosis/issues/4
[#21]: https://github.com/AzalDaniel/Anastomosis/pull/21

## Install

Once published to PyPI (coming with the v0.1.0 release), the recommended
install is [pipx](https://pipx.pypa.io/):

```bash
pipx install "anastomosis[render]"
```

Until then — or for development — install from a clone:

```bash
git clone https://github.com/AzalDaniel/Anastomosis.git
cd Anastomosis
pip install -e ".[render]"        # add [dev] to run the test suite
playwright install chromium      # one-time: the rendering engine's browser
```

## Quickstart

One command takes a raw EHI export to verified chart PDFs and a searchable
offline archive:

```bash
anast pipeline run ./my_ehi_export --out ./charts --archive ./my_archive
```

The source format is auto-detected (or pass `--source pf-tebra` / `--source
ccda`); `--pack` selects the document template; every rendered document is
QA-verified by default; `anast info` lists every available source adapter
and template pack.

## How it works — file-by-file

The pipeline is five conceptual stages — **ingest → canonical model →
reconstruct → verify → deliver** — built as isolated, versioned modules behind
defensive registries. One line per package in `src/anastomosis/`, each naming
the property that was hardest to get right:

```
src/anastomosis/
├── core/
│   ├── model/        canonical, FHIR R4-aligned pydantic v2 records. LOSSLESSNESS:
│   │                 AnastBase carries an `extensions` dict so no unmapped source
│   │                 column is ever dropped; Encounter holds SOAP NoteSections +
│   │                 addenda; Observation covers vitals + social history.
│   ├── fhir/         PatientRecord ↔ FHIR R4 Bundle. EXACT ROUND-TRIP: to_bundle →
│   │                 from_bundle reproduces the record, extensions carried through
│   │                 urn:anastomosis namespaces (fhir.resources R4B available as an extra).
│   ├── timeutil/textutil/codes  sentinel-safe parsing (`\N`/`-1`/`1/1/0001` → None,
│   │                 never a fake value), 7-format dates, zoneinfo local time, LOINC vitals.
│   ├── logutil       RedactionFilter + exc_tag(): logs counts, ids, and exception
│   │                 TYPE names — never patient-derived values.
│   └── output        output dirs created 0o700 with a PHI-warning README.
├── sources/
│   ├── pf_tebra/     Practice Fusion/Tebra EHI v9 adapter. Joins the 29-table
│   │                 (KNOWN_TABLES) export graph; the mapper declares consumed
│   │                 columns and routes EVERY other column to `extensions` under a
│   │                 `pf_tebra:` key; escript resolves status by transaction priority.
│   ├── ccda/         C-CDA R2.1 / CCD ingest. HARDENED XML (resolve_entities=False,
│   │                 no_network=True, load_dtd=False, huge_tree=False); unmapped
│   │                 sections preserved under `ccda:section:<loinc>` extension keys.
│   └── oracle_ehi/   Oracle Health/Cerner Millennium EHI adapter (V500 single-patient
│                     export). Dependency-free MySQL INSERT-dump reader over
│                     `v500/{schema,activity,reference}`; PERSON/ENCOUNTER/CLINICAL_EVENT
│                     spine, CE_BLOB note text + CE_BLOB_RESULT remote refs (never
│                     fetched); unmapped columns to `oracle_ehi:` extensions; undocumented
│                     CE_BLOB compression (brief §8) raises loudly rather than guessing.
├── reconstruct/      Jinja2 + Chromium rendering engine + defensive pack registry.
│                     Renderer recycling, crash relaunch, deterministic
│                     collision-suffixing, idempotent skip; a broken pack is
│                     diagnosed and disabled WITHOUT taking the system down;
│                     `generic_soap` built-in with user-togglable section flags.
├── packgen/          pack-from-samples LAYOUT LEARNER. PyMuPDF-only, fully offline,
│                     no torch: font histogram → type scale, x-position bucketing →
│                     column grids (deliberately explainable greedy clustering, not a
│                     black box), get_drawings() fills → design tokens, bold spans
│                     recurring across ALL samples → section taxonomy + static-text
│                     intersection; emits a loadable draft pack with a same-patient caveat.
├── qa/               every rendered document is verified. CheckResult{pass|warn|fail}
│                     over data_integrity, layout_pagination, vitals_loinc, and
│                     date_staleness checks, with BOUNDARY-ANCHORED matching because
│                     naive substring matching false-passes missing content; a FAIL
│                     exits the pipeline nonzero.
├── deliver/
│   ├── archive/      ARCHIVIST output: static, zero-network, `file://`-openable
│   │                 archive — strict CSP, relative assets only, per-encounter HTML,
│   │                 PDFs, and a FHIR R4 Bundle per patient, readable for decades.
│   ├── bundle/       RESPONDER output: per-patient chart bundle (FHIR Bundle + PDFs)
│   │                 with the QA report SLICED to that one patient's documents.
│   ├── browser/      verified browser-automation upload engine. The 15-STATE LEDGER:
│   │                 a WAL-mode SQLite state machine (UploadState, LEGAL_TRANSITIONS)
│   │                 that survives a hard kill mid-upload and resumes; FakeDestination
│   │                 test double drives a kill-and-resume test with no real I/O.
│   ├── verify/       the L0–L6 verification ladder — the wrong-patient defense.
│   │                 L0 file integrity, L1 page/size, L2 identity fuzzy ≥0.88 + DOB
│   │                 hard-fail, L3 pack-driven header fields, L4 live banner readback,
│   │                 L5 destination metadata, L6 byte/identity round-trip.
│   ├── fhir_api/     FHIR R4 DocumentReference pusher over stdlib urllib (https, or
│   │                 http only for loopback); status codes + resource TYPE names in errors.
│   └── ccda_export/  PatientRecord → C-CDA R2.1, for destinations that import C-CDA;
│   │                 its contract is that THIS repo's own ccda parser reads it back.
│   └── router.py     SHORTEST-PATH router: vendor API → C-CDA import → browser
│                     automation; an `unverified` capability is never viable.
├── destinations/     EVIDENCE-OR-REFUSE capability registry (registry.yaml is DATA):
│                     every non-`none` capability MUST carry a source_url + verified
│                     date or validation fails loudly; the tebra browser pack ships
│                     with DISCOVER placeholder selectors (operator-derived via the
│                     wizard) so no vendor DOM is ever invented.
├── gui/              pywebview shell over a headless, fully testable controller and
│                     thin vanilla-JS pages: pipeline dashboard with live counters,
│                     migration wizard with the transit map, section-selection matrix,
│                     and an upload console reading the 15-state ledger read-only.
├── pipeline.py       the frontend-free pipeline core: emits PHI-safe StageEvents
│                     (detect → ingest → reconstruct → QA); CLI and GUI drive the SAME code.
└── cli.py            the `anast` (and `anastomosis`) CLI: `pipeline run`, `info`,
                      `gui`, `archive`, `bundle`, `destination {list,route,init}`,
                      `pack init`.
```

## Design rationale

- **A five-stage pipeline over one canonical model.** Ingest, canonical
  model, reconstruct, verify, deliver. Routing everything through a single
  lossless, FHIR R4-aligned model (`core/model/`) means every new source
  adapter and every new destination only has to speak to the model — not to
  each other — so the matrix of (sources × destinations) collapses to
  (sources + destinations).
- **Verification is the core product, not a test.** A migration that puts the
  right notes in the wrong chart is worse than no migration. So the QA engine
  verifies every rendered document, and the delivery path runs the L0–L6
  ladder around every upload — boundary-anchored and identity-based, because
  the naive matches (substring, whole-page similarity) demonstrably false-pass
  the exact failures these checks exist to catch.
- **Packs and registries make a vendor change a one-module event.** Source
  adapters, template packs, QA checks, and destinations are versioned modules
  behind defensive registries; the capability registry is data with cited
  evidence. A vendor changing an export format, a UI, or an API touches one
  module — never the system.
- **Local-first PHI posture.** The core pipeline makes zero network calls;
  PHI never leaves the operator's machine. Logs carry counts, ids, and
  exception type names — never values — and output directories are owner-only.

## Privacy & safety

- **No PHI in this repository, ever.** All fixtures are synthetic
  (Synthea-generated or hand-built with `feedface-` GUIDs). A hashed
  deny-list scanner (`tools/phi_scan.py`) runs on every commit and in CI.
- You run this software on machines you control; you are responsible for
  HIPAA compliance in your environment. See [docs/SECURITY.md](docs/SECURITY.md)
  and [docs/DISCLAIMER.md](docs/DISCLAIMER.md).

## License

[AGPL-3.0-or-later](LICENSE). Free for everyone to use, study, and improve —
and anyone who offers it as a service must share their changes back.
No CLA: contributors keep their copyright, which makes proprietary
relicensing permanently impossible.

## The desktop GUI

`anast gui` opens the liquid-glass desktop app (the `gui` extra): a pipeline
dashboard with live ingest/reconstruct/QA/deliver counters, a migration wizard
that shows the destination transit map, a section-selection matrix, and a
read-only upload console over the delivery ledger.

_(GUI screenshots coming with the CS50 submission.)_

## Provenance

Anastomosis generalizes a private production system, built by the same author,
that reconstructed 12,906 encounter documents from one clinic's vendor EHI
export at a 100% final QA pass rate and uploaded them into the destination EHR
with zero wrong-patient events — collapsing an estimated five months of manual
re-entry into hours. This project exists so the next practice doesn't have to
build it again. (Self-reported provenance from the author's predecessor
system; this open-source release has been tested only against synthetic data —
see [docs/DISCLAIMER.md](docs/DISCLAIMER.md).)

*Built as a Harvard CS50x final project — and built to outlive it.*
