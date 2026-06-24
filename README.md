# Anastomosis

> Reconstruct, verify, and re-home clinical records.

**anastomosis** *(n., medicine)* — a surgical connection between two structures.
This toolkit is that connection for electronic health records.

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

1. **Ingest** raw EHI exports — Practice Fusion/Tebra TSV, C-CDA/CCD, FHIR R4
   (Bundle or Bulk-Data NDJSON), Oracle Health/Cerner Millennium V500 dumps —
   into a lossless canonical model: every unmapped field, and every unmapped
   table the export carries, is preserved verbatim in `extensions`; an export
   carrying data that cannot be placed is refused rather than silently dropped.
   Meet an export it doesn't recognize? **Teach it that format from a single
   example**, and it maps to the canonical model from then on.
2. **Reconstruct** human-readable clinical documents from template packs: a
   neutral SOAP layout, a standard HL7 C-CDA stylesheet view, or a
   sample-learned vendor replica (the Practice Fusion pack reproduces that
   chart's layout and typography — or learn a new layout from your own sample
   PDFs).
3. **Verify** every rendered document with a multi-layer QA engine
   (data-integrity, layout, and identity checks), and guard every upload with the
   full L0–L6 verification ladder — on by default (`--no-verify` to skip) — plus a
   live wrong-patient banner check that runs regardless.
4. **Deliver** by the shortest available path: a vendor API where one exists,
   C-CDA import where supported, or verified browser automation where neither
   does. A cross-EHR migration moves the **structured C-CDA/FHIR payload** the
   destination imports natively; the rendered PDF is the human-readable archive
   and upload fallback, never a forgery of another vendor's house style. Or
   build a **searchable offline archive** — plain folders, PDFs, and JSON
   readable for decades — in place of a paid legacy-archive subscription.

Local-first by design: **the core pipeline makes zero network calls.**
Your records never leave your machine.

## Status

**v0.3.0 (alpha)** — on [PyPI](https://pypi.org/project/anastomosis/) and
[GitHub](https://github.com/AzalDaniel/Anastomosis/releases). See
[CHANGELOG.md](CHANGELOG.md) for what shipped and [docs/PLAN.md](docs/PLAN.md)
for the roadmap.

| Capability | State |
|---|---|
| Foundational pipeline — ingest → reconstruct → QA → searchable archive | ✅ v0.1.0 |
| Migration mode — `migrate` from→to, verified delivery engine, destination registry + router | ✅ v0.2.0 |
| Learn a source format from one example; FHIR R4 ingest; standard C-CDA render | ✅ v0.2.0 |
| Pack-from-samples layout learner | ✅ v0.2.0 |
| Desktop GUI — pipeline dashboard, migration wizard, upload console | ✅ v0.2.0 |
| Windows desktop installer — bundles Chromium offline, installs WebView2 if absent, with an installed self-check | ✅ v0.3.0 |

Built and tested entirely against synthetic data; see
[docs/DISCLAIMER.md](docs/DISCLAIMER.md) for production-readiness notes.

## Install

Anastomosis is [live on PyPI](https://pypi.org/project/anastomosis/). The
recommended install is [pipx](https://pipx.pypa.io/) (or plain pip):

```bash
pipx install "anastomosis[render]"      # CLI + rendering
pip install "anastomosis[render,gui]"   # … or with the desktop GUI
playwright install chromium             # one-time: the rendering browser
```

For development, install from a clone:

```bash
git clone https://github.com/AzalDaniel/Anastomosis.git
cd Anastomosis
pip install -e ".[render]"        # add [dev] to run the test suite
playwright install chromium      # one-time: the rendering engine's browser
```

### Windows app (no Python required)

Prefer a normal installer? Download `Anastomosis-Setup-<version>.exe` from the
[Releases page](https://github.com/AzalDaniel/Anastomosis/releases) and run it.
It bundles its own Python runtime and the Chromium render engine, so there is no
separate `pip` or `playwright` step. The GUI renders through the Edge WebView2
runtime; if your machine lacks it the installer fetches and installs it silently
(most Windows 10/11 machines already have it). It installs the desktop GUI (a
Start-menu shortcut) and the `anast` command-line tool (an optional "add to
PATH" task), and registers a normal uninstaller.

> **Offline/air-gapped machines:** the WebView2 step downloads from Microsoft, so
> on a machine without internet *and* without WebView2 already present, install
> the [Edge WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)
> (the Evergreen Standalone Installer) separately first. The `anast` CLI does not
> need WebView2; only the desktop GUI does.

The alpha installer is **not yet code-signed**, so Windows SmartScreen will show
a "Windows protected your PC" warning. To proceed, click **More info → Run
anyway**. (Code signing is planned for the general-availability release.) After
installing, you can verify the install is intact with `anast doctor`.

## Quickstart

One command takes a raw EHI export to verified chart PDFs and a searchable
offline archive:

```bash
anast pipeline run ./my_ehi_export --out ./charts --archive ./my_archive
```

The source format is auto-detected (or pass `--source pf-tebra`/`ccda`/`fhir-r4`/
`oracle-ehi`); `--pack` selects the document template; every rendered document is
QA-verified by default; `anast info` lists every available source adapter and
template pack.

Migrate from one EHR to another — emitting both the structured C-CDA payload the
target imports and human-readable charts:

```bash
anast migrate ./my_ehi_export --from pf-tebra --to tebra --out ./migration
```

Meet an export format it doesn't recognize? Teach it once from a single example,
then ingest that format like any built-in source:

```bash
anast source init ./example_export.csv --name acme_clinic
```

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
│   ├── oracle_ehi/   Oracle Health/Cerner Millennium EHI adapter (V500 single-patient
│   │                 export). Dependency-free MySQL INSERT-dump reader over
│   │                 `v500/{schema,activity,reference}`; PERSON/ENCOUNTER/CLINICAL_EVENT
│   │                 spine, CE_BLOB note text + CE_BLOB_RESULT remote refs (never
│   │                 fetched); unmapped columns to `oracle_ehi:` extensions; undocumented
│   │                 CE_BLOB compression (brief §8) raises loudly rather than guessing.
│   ├── fhir_r4/      FHIR R4 / US Core ingest — a Bundle or a Bulk-Data `$export`
│   │                 NDJSON directory → canonical records; unmapped fields → `extensions`.
│   └── learned/      LEARN-A-SOURCE: a single generic adapter that runs a saved,
│                     validated mapping (DATA, no code) for a structured export taught
│                     from one example via `anast source init`; auto-detected by a
│                     column fingerprint; unmapped columns → `extensions`.
├── reconstruct/      Jinja2 + Chromium rendering engine + defensive pack registry.
│                     Renderer recycling, crash relaunch, deterministic
│                     collision-suffixing, idempotent skip; a broken pack is
│                     diagnosed and disabled WITHOUT taking the system down;
│                     `generic_soap` built-in with user-togglable section flags.
│                     `ccda_standard/` renders the structured C-CDA through HL7's
│                     vendored CDA.xsl to a neutral per-patient PDF — no network egress —
│                     for the `migrate --render ccda-standard` mode.
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
│                     thin vanilla-JS pages: pipeline dashboard with live counters, a
│                     migration wizard (transit map + the full CLI lever set), a
│                     learn-a-source wizard, and an upload console that DRIVES the engine
│                     over the 15-state ledger (loopback-only; never closes your browser).
├── pipeline.py       the frontend-free pipeline core: emits PHI-safe StageEvents
│                     (detect → ingest → reconstruct → QA); CLI and GUI drive the SAME code.
└── cli.py            the `anast` (and `anastomosis`) CLI: `pipeline run`, `info`,
                      `doctor`, `gui`, `migrate`, `upload`, `archive`, `bundle`,
                      `destination {list,route,init}`, `pack init`, `source init`.
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
  verifies every rendered document, and the delivery path runs the full L0–L6
  ladder around every upload by default (`--no-verify` to skip; a missing render
  extra refuses rather than files unverified) plus a live wrong-patient banner
  check — boundary-anchored and identity-based, because the naive matches
  (substring, whole-page similarity) demonstrably false-pass the exact failures
  these checks exist to catch.
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

`anast gui` opens the desktop app (the `gui` extra): a pipeline dashboard with
live ingest/reconstruct/QA/deliver counters, a migration wizard that shows the
destination transit map and the full set of run levers, a learn-a-source wizard,
and an upload console that drives the delivery engine over its ledger.

## Provenance

Anastomosis generalizes a private production system, by the same author, that
migrated a clinic's full EHI export — reconstructing its encounter documents and
filing them into the destination EHR with no wrong-patient events. Those results
are self-reported and specific to that deployment; this open-source release has
been built and tested only against synthetic data (see
[docs/DISCLAIMER.md](docs/DISCLAIMER.md)).
