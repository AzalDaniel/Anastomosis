# Learn-from-sample and migration architecture trace

**Audit basis:** current `bbc48a6` checkout, source inspection only. This memo
contains no patient values, sample filenames, credentials, or private corpus
content. Documentation is treated as a claim; implementation references below
are the evidence.

## Verdict

Anastomosis has a solid *canonical-record to prepared-artifacts* pipeline, and
it has useful local safety rails around browser uploading. It does **not** yet
implement the advertised end-to-end product implication “teach a destination
format, map a source, and migrate into that destination.” The two teaching
flows are independent authoring utilities; their results are not bound to a
destination, a semantic destination schema, a route, or a deployment approval.
`migrate` prepares a C-CDA directory, chart PDFs, and a browser-upload manifest;
it never executes its selected route.

The safe current statement is: “learn a flat source mapping; draft a generic
SOAP-derived presentation; produce canonical C-CDA and PDFs; then separately
perform either a manually operated C-CDA import, a generic FHIR
DocumentReference upload, or a configured browser filing run.” README’s broader
language (for example [README.md](../../../../README.md) lines 31-39 and 57-62)
must not be read as a proven destination-format migration contract.

## What the UI actually exposes

There is no literal **Migration toggle** that changes the pipeline. The visible
toggle-like elements are:

* Navigation tabs: Charts, **Migrate**, Uploads, Teach
  ([index.html](../../../../src/anastomosis/gui/web/index.html) lines 47-57).
  Migrate is a separate screen, not a mode flag carried through Teach.
* Teach has mutually exclusive **Document layout** and **Export format** tabs
  ([index.html](../../../../src/anastomosis/gui/web/index.html) lines 348-360),
  each with an acknowledgement checkbox ([index.html](../../../../src/anastomosis/gui/web/index.html)
  lines 399-409 and 452-463). Neither captures a destination identifier.
* The shared Migrate run form exposes a destination chooser, source chooser,
  render-representation chooser, section switches, QA switch, force switch,
  one additional layout folder, and “Allow this new layout to run”
  ([wizard.js](../../../../src/anastomosis/gui/web/wizard.js) lines 264-299;
  [shell.js](../../../../src/anastomosis/gui/web/shell.js) lines 1300-1358 and
  1409-1427). The upload-only “Double-check each chart after filing” switch is
  checked by default but can be disabled ([index.html](../../../../src/anastomosis/gui/web/index.html)
  lines 204-209).

The Migrate picker offers `neutral`, `ccda-standard`, and any discoverable Jinja
layout ([wizard.js](../../../../src/anastomosis/gui/web/wizard.js) lines 351-396).
It does not offer “the layout just taught” automatically, does not bind it to the
chosen destination, and does not establish that the sample describes the
destination rather than a source document.

## Verified implementation path

```text
Teach / source example              Teach / destination-looking PDFs
index.html + source.js              index.html + packgen.js
        |                                     |
SourceConsole -> source_init_command      PackgenConsole -> packinit
        |                                     |
validated MappingSpec -> LearnedSource    draft pack (generic SOAP context)
        |                                     |
        +-------------- manual operator selection / --pack-dir -------------+
                                                                          |
Migrate wizard -> GuiController -> MigrationConsole -> MigrationCommand  |
  source + destination + render + pack_dirs + trust_new ------------------+
        |
plan_route (capability data only) -> run_pipeline / C-CDA view
        |
source adapter -> canonical PatientRecord -> Jinja/Chromium PDF + QA
        |                                      + C-CDA XML + upload_manifest
        v
PREPARED only; operator separately uses manual C-CDA import, generic --fhir,
or browser assistant -> UploadCommand -> ledger -> L0–L6 verifier
```

### Source semantics

`SourceAdapter` is the canonical ingestion seam: built-ins are C-CDA, FHIR R4,
Oracle EHI, and PF/Tebra; learned mappings deliberately are not built-ins
([sources/base.py](../../../../src/anastomosis/sources/base.py) lines 105-137).
The learned adapter accepts only one flat CSV/TSV/JSON/NDJSON file, fingerprinted
by normalized columns ([sources/learned/reader.py](../../../../src/anastomosis/sources/learned/reader.py)
lines 1-25, 67-94). It groups rows and maps only closed canonical target paths;
unconsumed values go into `extensions` ([sources/learned/interpreter.py](../../../../src/anastomosis/sources/learned/interpreter.py)
lines 1-24, 130-153, 165-189, 210-217). The authoring flow is deterministic
fuzzy-name/type scoring plus human confirmation, not ML
([core/sourcelearn.py](../../../../src/anastomosis/core/sourcelearn.py) lines
1-20, 466-478, 484-537), with a per-value check only for *unmapped* columns
([core/sourcelearn.py](../../../../src/anastomosis/core/sourcelearn.py) lines
550-586).

This is useful preservation, but it is not an EHI package learner, relational
schema learner, terminology reconciler, encounter-linking verifier, or a
destination-field mapper. A malformed or ambiguous known source can fail
closed, but a confirmed mapping can still be semantically wrong for mapped
values because the confirmation UI presents suggestions rather than an
evidence/provenance review of every clinical transform.

### Layout teaching and the generic SOAP fallback

The PDF learner is explicitly a **draft** generator. It emits `context.py` that
delegates to the generic SOAP context and a template that mirrors generic SOAP;
inferred tokens change look, not the data contract
([packgen/emit.py](../../../../src/anastomosis/packgen/emit.py) lines 1-38).
It samples fonts, geometry and recurring strings, has a same-patient warning
([packgen/emit.py](../../../../src/anastomosis/packgen/emit.py) lines 87-117),
and substitutes the nearest named Playwright page size for nonstandard geometry
([packgen/emit.py](../../../../src/anastomosis/packgen/emit.py) lines 193-240).
The GUI accurately labels this as a reviewable draft
([packgen.js](../../../../src/anastomosis/gui/web/packgen.js) lines 47-52;
[index.html](../../../../src/anastomosis/gui/web/index.html) lines 405-409), but
the generated pack defaults to relative `packs/` in the GUI
([gui/consoles/packgen.py](../../../../src/anastomosis/gui/consoles/packgen.py)
lines 68-75 and 148-156). A migration discovers external packs only through a
manually supplied `pack_dirs` list ([pipeline.py](../../../../src/anastomosis/pipeline.py)
lines 616-629). That is a real disconnected seam.

Thus `neutral` really is the generic SOAP fallback (`neutral -> generic_soap`)
([core/migrate.py](../../../../src/anastomosis/core/migrate.py) lines 23-30,
254-283); it is not a generic SOAP *destination adapter*. A vendor Jinja skin
is a representation of canonical data, not a learned vendor import schema.
The architecture currently conflates two distinct contracts in the same pack:
presentation tokens/template and `coverage` declarations used by QA
([reconstruct/packs.py](../../../../src/anastomosis/reconstruct/packs.py) lines
112-133, 175-214). It needs separate semantic mapping/conformance and visual
layout artifacts.

### Migration and render/QA

The GUI bridge is a thin facade that delegates migration to `MigrationConsole`
([gui/controller.py](../../../../src/anastomosis/gui/controller.py) lines 120-136,
424-430). It constructs `MigrationCommand` with every displayed run option
([gui/consoles/runs.py](../../../../src/anastomosis/gui/consoles/runs.py) lines
481-536). `run_migration` computes a transit map but branches only on render
mode, not on chosen route ([core/migrate.py](../../../../src/anastomosis/core/migrate.py)
lines 230-251). Pack mode invokes the shared pipeline and always emits C-CDA
plus an upload manifest ([core/migrate.py](../../../../src/anastomosis/core/migrate.py)
lines 254-283); C-CDA-standard mode renders one whole-patient PDF and writes the
same style of manifest ([core/migrate.py](../../../../src/anastomosis/core/migrate.py)
lines 493-528).

The actual pipeline is source resolve -> pack discovery/trust -> canonical load
-> render -> record summaries/attachments -> QA
([pipeline.py](../../../../src/anastomosis/pipeline.py) lines 613-735). It
refuses render failure and uses a conservation check, but missing PyMuPDF makes
render QA a *skipped* stage rather than a refusal
([pipeline.py](../../../../src/anastomosis/pipeline.py) lines 767-825). QA also
relies on the layout manifest’s self-declared carry/omit matrix. It therefore
does not prove destination-import conformance, semantic equivalence, source-to-
destination code mapping, or visual fidelity to a sample.

### Routes and actual delivery

`plan_route` is pure registry selection: vendor API > C-CDA import > browser;
it marks cited capabilities viable and gives browser viability from a pack
declaration only ([deliver/router.py](../../../../src/anastomosis/deliver/router.py)
lines 1-18, 141-200). The registry says its evidence was confirmed via indexed
snippets because direct vendor pages returned 403
([destinations/registry.yaml](../../../../src/anastomosis/destinations/registry.yaml)
lines 21-33). That is not sufficient runtime capability verification.

The UI itself states the crucial limitation: a chosen vendor API “runs from the
Uploads screen” and “is not available from this screen yet”
([wizard.js](../../../../src/anastomosis/gui/web/wizard.js) lines 104-126); its
terminal message says nothing has been sent
([wizard.js](../../../../src/anastomosis/gui/web/wizard.js) lines 313-338).
`MigrationConsole` likewise calls the successful state **PREPARED**, explicitly
saying it executes no delivery route ([gui/consoles/runs.py](../../../../src/anastomosis/gui/consoles/runs.py)
lines 558-594).

The CLI upload command has only two independent mechanisms: browser
`--to PACK --cdp URL`, or generic `--fhir URL` ([cli_commands/upload.py](../../../../src/anastomosis/cli_commands/upload.py)
lines 138-161 and 185-246). It does not consume the selected registry
destination, does not implement `vendor_rest`, and does not execute
`ccda_import`; C-CDA `in_product` is necessarily an operator handoff. The FHIR
route uploads rendered PDFs as generic `DocumentReference`s
([deliver/fhir_api/destination.py](../../../../src/anastomosis/deliver/fhir_api/destination.py)
lines 1-33, 64-79), so it is not proof that a registry’s destination-specific
write API, document type, required extensions, or import workflow was used.

## Security and integrity findings

1. **External pack code is not sandboxed.** `context.py` is arbitrary Python
   executed with the desktop user’s authority ([reconstruct/packs.py](../../../../src/anastomosis/reconstruct/packs.py)
   lines 259-284). Trust-on-first-use protects only an explicit `--pack-dir`
   path and is operator-bypassable with `trust_new` ([pipeline.py](../../../../src/anastomosis/pipeline.py)
   lines 616-625). It is not a sandbox, signing system, provenance policy, or
   least-privilege runtime.
2. **Trust has a remaining render-time TOCTOU.** Snapshot trust pins
   `context.py`, `pack.yaml`, and the presence/hash of `template.html`, but the
   engine loads `template.html` from disk at render time; partials/images/fonts
   are outside the hash ([packtrust.py](../../../../src/anastomosis/reconstruct/packtrust.py)
   lines 52-73; [packs.py](../../../../src/anastomosis/reconstruct/packs.py)
   lines 348-360). A post-approval swap can change rendered clinical content or
   layout without changing the code that was trusted.
3. **Route declaration is not executable authorization.** Browser route
   planning intentionally does not load or validate the assistant
   ([deliver/router.py](../../../../src/anastomosis/deliver/router.py) lines
   163-180). Upload preflight later checks loopback, manifest and browser-pack
   selectors ([gui/consoles/upload.py](../../../../src/anastomosis/gui/consoles/upload.py)
   lines 47-100), but no equivalent destination-bound preflight exists for the
   generic FHIR option.
4. **Delivery verification has explicit downgrade switches.** Browser upload is
   robust by default: it locks before rereading the manifest, has L0–L6, and
   refuses verification if its dependency is absent
   ([core/upload_command.py](../../../../src/anastomosis/core/upload_command.py)
   lines 228-358). Yet `--no-verify`/the GUI checkbox is available
   ([cli_commands/upload.py](../../../../src/anastomosis/cli_commands/upload.py)
   lines 120-132), and v1/no-pack manifests cause L3 or other checks to skip
   ([core/upload_command.py](../../../../src/anastomosis/core/upload_command.py)
   lines 193-225, 331-338). A production migration policy must not permit those
   overrides silently.

## Required fail-closed target architecture

Make these separate immutable, signed/reviewed artifacts instead of overloading
one Jinja pack:

* **SourceProfile:** reader/parser version, source schema fingerprint, mapping
  decisions, transforms, terminology/codelist versions, per-field provenance,
  ambiguity/quarantine policy, and conservation rules.
* **DestinationProfile:** destination/version, supported document/resource
  schemas, required fields/codes/attachments, patient matching/create policy,
  route adapter identity, evidence/canary results with expiry, and permitted
  delivery contract (`C-CDA import`, vendor API, FHIR API, or browser).
* **LayoutProfile:** non-executable template/asset bundle, precise snapshot hash,
  named layout schema, reviewed sample-fidelity metrics, and a declared link to
  semantic fields. It cannot assert semantic coverage on its own.
* **RunManifest:** immutable digests of all three profiles, canonical-record
  inventory, emitted artifact hashes/page counts, validation reports, route
  authorization, operator approvals, and a resume-safe ledger binding.

The destination profile must be selected before teaching and must be the sole
source for both GUI controls and upload construction. A learned layout stays
`DRAFT` until sandboxed rendering, source-to-canonical conservation, destination
schema validation, terminology checks, deterministic render comparison against
approved fixtures, and destination canary/readback all pass. Do not use a
generic `--fhir URL` as fulfillment of a destination registry route; bind an
adapter to the profile and reject unsupported `vendor_rest`/in-product paths.

### Minimum state machine

```text
NEW
  -> SOURCE_ANALYZED -> SOURCE_REVIEWED -> SOURCE_VALIDATED
  -> DESTINATION_SELECTED -> DESTINATION_CAPABILITY_VERIFIED
  -> LAYOUT_ANALYZED -> LAYOUT_REVIEWED -> LAYOUT_VALIDATED
  -> PLAN_LOCKED (hash all profiles; authorize exactly one route)
  -> INGESTED -> SEMANTIC_VALIDATED -> RENDERED -> VISUAL_QA_PASSED
  -> DESTINATION_PRECHECKED -> DELIVERY_EXECUTING
  -> POST_DELIVERY_VERIFIED -> COMPLETE

Any ambiguity, missing capability/evidence, expired canary, untrusted/sandbox
failure, conservation mismatch, unsupported field/code, QA warning requiring
review, profile/hash change, or failed readback -> QUARANTINED / REFUSED.
Only an explicit, recorded clinical-review decision can move QUARANTINED to a
new reviewed plan; it never resumes as an implicit generic-SOAP/FHIR fallback.
```

Until that exists, block a “migrate to taught destination” claim and label the
current Migrate action **Prepare migration artifacts**. Keep C-CDA handoff,
generic FHIR upload, and browser filing as visibly separate, destination-scoped
operator actions with their own verification coverage shown before execution.
