# Sample document corpus for the learn-from-source module

**Research memo (PHI-free; checked 2026-08-30)**

## Purpose and honest coverage

`docs/audits/learned-source/RESEARCH_EHR_LAYOUT.md` established the theory: no
interoperability standard specifies a vendor's rendered layout, and a learned
style pack is a tested claim over one reviewed fixture set, never a claim
about every deployment of a vendor's product. This memo does not revisit that
conclusion. It inventories what real, publicly obtainable sample documents
actually exist, so the learn-from-sample module can be tested against them
instead of against assumptions about what a vendor "probably" exports.

The honest picture, stated plainly before the detail:

Structural coverage is good and cleanly licensed. HL7's own example
libraries and ONC's certification test-data repositories are public,
maintained, mostly CC0-1.0 or US-government public domain, and cover C-CDA
templates, sections, and entries broadly enough to exercise schema
validation, template/construct coverage, and participant/provenance
extraction. Synthea adds fully synthetic, Apache-2.0, generator-produced
C-CDA and FHIR output at whatever volume is needed. This is enough to build
a real, repeatable, license-clean regression suite for the semantic/parsing
half of the module.

Vendor-attributed layout evidence is almost entirely absent. Of the vendors
named in the task -- Epic, Oracle Health/Cerner, athenahealth, eClinicalWorks,
NextGen, Allscripts/Veradigm, Practice Fusion, DrChrono, Kareo/Tebra,
AdvancedMD, Greenway, MEDITECH, CureMD, Modernizing Medicine -- not one was
found to publish, itself, a downloadable sample chart, C-CDA file, or PDF
export on its own developer site for public download. Each publishes API
specifications, data dictionaries, or a live OAuth-gated sandbox instead
(catalogued in `docs/EHR_FORMATS.md`'s "Public non-PHI note sample?" column,
which independently reached the same "none found" conclusion for nearly
every one of these vendors). Where a document online is *labeled* with a
vendor's name, it almost always comes from a third-party GitHub repository
that collected contributions from unknown submitters; that label is an
unverified claim, not a vendor's own attestation, and this memo flags every
such case rather than treating the folder name as evidence.

Real, de-identified patient text exists in abundance (i2b2/n2c2, MIMIC) but
sits behind data use agreements and credentialing that this repository's
policy -- not just the DUA -- puts permanently out of reach: none of it may
ever be downloaded, quoted, or fetched by this project, DUA or no DUA.

## Standards-body and certification example libraries

### HL7 C-CDA-Examples (legacy, "SDWG Supported Samples")

- **Publisher / nature**: HL7 Structured Documents Working Group (SDWG).
  Community-submitted, SDWG-reviewed example documents; not vendor exports.
- **URL**: <https://github.com/HL7/C-CDA-Examples>
- **Format(s)**: C-CDA XML (R2.1, upgraded from R1.1 samples loaded 2016-10).
- **Approximate count**: not enumerated by file; organized into 27+
  category folders (allergies, medications, procedures, vital signs,
  immunizations, clinical notes, and more). VERIFIED folder-level structure
  via repository listing; per-file count UNCERTAIN.
- **License**: CC0-1.0, confirmed via the repository's `LICENSE` file
  (public-domain dedication, "as-is", no warranty). The README additionally
  states examples are "governed by the HL7 GOM -- Section 09 Intellectual
  Property," which this memo could not independently reconcile against the
  CC0 grant beyond noting both statements exist; treat the GOM reference as
  an unresolved caveat rather than a contradiction to build against.
- **Synthetic or real**: synthetic/fictional reference examples, not vendor
  exports and not real patients. Feeds (fed, past tense -- see below) the
  public search site.
- **Good for**: C-CDA construct/template coverage, section- and
  entry-level examples for schema and mapping tests. Not vendor-specific
  layout evidence -- these are HL7 reference constructs, not any one EHR's
  rendering.
- **Status note**: as of 2025-09-15, `cdasearch.hl7.org` -- the search UI over
  this content -- is no longer maintained; HL7 states the examples migrated to
  a successor IG (see next entry). VERIFIED via search-indexed HL7 notice;
  the live cdasearch.hl7.org pages were not independently re-fetched.

### HL7 CDA-Examples (current, cdasearch successor)

- **Publisher / nature**: HL7 SDWG, "examples reviewed and approved by the
  Structured Document Work Group." Built as a FHIR-tooling-based
  ImplementationGuide (ballot/build artifact), not a plain file drop.
- **URL**: <https://github.com/HL7/CDA-Examples> (build output intended at
  `build.fhir.org/ig/HL7/CDA-Examples/`, which this research could not fetch
  directly, and the license question below is unresolved for the same reason).
- **Format(s)**: CDA/C-CDA XML, validated against C-CDA 2.1/3.0/4.0
  schematron plus FHIR StructureDefinitions.
- **Approximate count**: not enumerated here.
- **License**: **unclear**. No `LICENSE` file is present in the repository
  root as of this check (root listing confirmed: `.husky`, `examplesTemplate`,
  `input`, `scripts`, `.gitignore`, `CDA-examples.xml`, `README.md`, build
  scripts, schematron files, `package.json` -- no `LICENSE`). As a work of an
  ANSI-accredited standards body rather than the US government, this
  repository does not inherit the public-domain status that applies to
  ONC's own repositories (17 U.S.C. Sec. 105 applies only to US federal
  government works). Do not assume CC0 carries over from the legacy sibling
  repository; verify with HL7 before use.
- **Synthetic or real**: reference examples, not vendor exports, presumed
  synthetic like the legacy repository, but not independently confirmed here.
- **Good for**: same role as the legacy repository once the license question
  is resolved; currently reference-only pending a license answer.

### ONC certification test data (current: USCDI v1-v4)

- **Publisher / nature**: ONC, official certification test-data
  repositories under the `onc-healthit` GitHub organization, used for
  Sec. 170.315(b)(1)/(g)(6) C-CDA certification testing against USCDI v1
  through v4.
- **URL**: <https://github.com/onc-healthit/ccda-uscdi-certification-testdata>
  (folders `uscdi-v1-testdata` through `uscdi-v4-testdata`). A companion
  repository, `onc-healthit/2015-edition-cures-update-uscdi-v3-testdata`,
  also exists under the same organization; its content was not separately
  audited here.
- **Format(s)**: C-CDA XML certification test payloads.
- **Approximate count**: not enumerated; four USCDI-version folders.
- **License**: no `LICENSE` file found in the repository. As a work
  prepared by ONC (a US federal agency) as part of official duties, it is
  presumptively not subject to US copyright under 17 U.S.C. Sec. 105 -- this is
  general public-domain-for-federal-works law, verified via the statute
  text, not a license statement made by this specific repository. Treat as
  "very likely public domain, not explicitly so stated" rather than
  "confirmed permissive license."
- **Synthetic or real**: constructed certification test payloads, not real
  patients -- this is inferred from the purpose (certification conformance
  testing requires deterministic, reusable test patients) and from ONC's
  general certification-test-data practice, not from an explicit README
  statement seen in this research.
- **Good for**: C-CDA conformance/schema testing, and -- because
  certification test decks deliberately include edge cases -- a plausible
  source of adversarial/malformed-input-adjacent fixtures (unconfirmed
  without opening individual files; malformed-on-purpose test cases would
  need to be identified file by file before use).

### ONC/SITE legacy 2015-Edition C-CDA test data

- **Publisher / nature**: ONC, superseded (2015 Edition) certification test
  data, "Receiver SUT Test Data" and "Sender SUT Test Data."
- **URL**: <https://github.com/siteadmin/2015-Certification-C-CDA-Test-Data>.
  The same payloads are also reachable through the live SITE Edge Testing
  Tool at `ttpedge.sitenv.org` (CCDA R2.1 Validator download), per a search
  snippet of that tool's own documentation; `site.healthit.gov` and
  `sitenv.org` were blocked by this environment's egress proxy and could not
  be fetched directly to confirm current availability.
- **Format(s)**: C-CDA XML.
- **Approximate count**: not enumerated; two top-level directories, 190
  commits.
- **License**: CC0-1.0, confirmed via repository license metadata.
- **Synthetic or real**: certification test payloads, not real patients
  (same reasoning as the current USCDI repository above).
- **Good for**: same role as the current USCDI test-data repository, but
  superseded -- prefer the maintained `ccda-uscdi-certification-testdata`
  repository for anything beyond historical/2015-Edition-specific testing.

### SITE / Edge Testing Tool source

- **Publisher / nature**: ONC's Edge Testing Tool (ETT) source code, the
  tool that historically drove SITE's C-CDA/Direct testing UI.
- **URL**: <https://github.com/onc-healthit/ett>
- **Format(s)**: application source, not a data corpus by itself.
- **License / content**: not independently audited here; listed for
  completeness because the task named it explicitly. It is a testing tool,
  not itself a sample-document source -- the actual payloads live in the
  ONC test-data repositories above.

## Synthetic generators and reference implementations

### Synthea

- **Publisher / nature**: Synthetic Patient Population Simulator, originally
  MITRE-led, now `synthetichealth/synthea` on GitHub.
- **URL**: <https://github.com/synthetichealth/synthea>. Pre-generated
  downloads (rather than running the generator) are advertised on
  `synthea.mitre.org/downloads`, which this environment's egress proxy
  blocked; the specific historical link
  `syntheticmass.mitre.org/downloads/2017_11_06/synthea_sample_data_ccda_nov2017.zip`
  (~1,000 sample patients in C-CDA) surfaced only via a search-engine
  snippet quoting that page and was not independently re-fetched -- treat its
  continued availability as unconfirmed.
- **Format(s)**: FHIR JSON bundles (native), plus C-CDA XML export via the
  MDHT CDA Tools library and `health-data-standards` templates (C-CDA export
  requires a MongoDB backend per the project's own generation notes), plus
  flat CSV.
- **Approximate count**: generator has no fixed count -- any population size
  can be produced locally; the historical pre-built download bundled roughly
  1,000 patients (from the search-snippet-only source above, unconfirmed).
- **License**: Apache-2.0, confirmed via repository footer/LICENSE.
- **Synthetic or real**: explicitly synthetic. The project's own framing:
  "synthetic, realistic (but not real), patient data."
- **Good for**: bulk, license-clean C-CDA and FHIR structural coverage at
  any volume; not vendor-specific layout, since Synthea's C-CDA renderer is
  Synthea's own template pipeline, not any commercial EHR's.

### SMART Health IT EHI Export reference server

- **Publisher / nature**: `smart-on-fhir/ehi-server`, a reference
  implementation of the Argonaut EHI Export API Implementation Guide,
  described by its own documentation as containing synthetic patient data
  created using Synthea. A companion client, `smart-on-fhir/ehi-app`, is the
  reference EHI-export client ("Second Opinion App").
- **URL**: <https://github.com/smart-on-fhir/ehi-server> and
  <https://github.com/smart-on-fhir/ehi-app>. The project's own landing page
  at `smarthealthit.org` was blocked by this environment's egress proxy.
- **Format(s)**: FHIR-based EHI export bundle/manifest shape per the
  Argonaut EHI Export IG, not any specific commercial vendor's EHI schema.
- **Approximate count**: server-generated per Synthea population, not a
  fixed static file set; exact bundled dataset size was not confirmed
  (repository `/data` folder observed to exist, contents not enumerated).
- **License**: Apache-2.0, confirmed via repository header.
- **Synthetic or real**: synthetic (Synthea-derived).
- **Good for**: exercising the *shape* of an ONC-EHI-style export
  (manifest, per-category files, provenance links) without claiming any
  particular vendor's EHI schema -- useful as a generic EHI-export fixture,
  explicitly not a stand-in for Epic's TSV tables, Oracle's SQL dump,
  Practice Fusion's TSV set, etc. (those vendor-specific shapes are
  catalogued with citations in `docs/EHR_FORMATS.md` and
  `docs/vendor_refs/`).

### Inferno US Core data script / data sets

- **Publisher / nature**: `inferno-framework/uscore-data-script`, used to
  build the "Inferno US Core Data Sets" bundled with ONC's (g)(10)
  certification test kit.
- **URL**: <https://github.com/inferno-framework/uscore-data-script>
- **Format(s)**: FHIR JSON transaction bundles and FHIR Bulk Data
  (NDJSON), generated via Synthea and filtered to a small set covering all
  required US Core v3.1.0 Must Support elements.
- **Approximate count**: described as a "minimal set" chosen for coverage
  rather than volume; exact patient count not confirmed here.
- **License**: Apache-2.0, confirmed via repository header.
- **Synthetic or real**: synthetic (Synthea-derived, algorithmically
  selected for elemental coverage rather than realism at scale).
- **Good for**: FHIR/US Core Must-Support element coverage testing --
  closer to a targeted conformance fixture than a realistic chart.

## Federal sandbox with synthetic beneficiaries

### CMS Blue Button 2.0 developer sandbox

- **Publisher / nature**: CMS. A live, OAuth-gated clone of the production
  Blue Button 2.0 API, backed by synthetic Medicare Part A/B/D data for
  10,000 synthetic enrollees (`BBUser00000` through `BBUser29999` in CMS's
  own sandbox documentation).
- **URL**: <https://bluebutton.cms.gov/api-documentation/developer-sandbox/>
  (blocked by this environment's egress proxy; content summarized from a
  search-engine snippet of that page, not independently re-fetched).
- **Format(s)**: FHIR (HL7 FHIR standard) resources -- claims/EOB-oriented,
  not C-CDA and not a clinical note/chart format.
- **Approximate count**: 10,000 synthetic beneficiary accounts.
- **License / access**: requires app registration and OAuth2 authorization;
  no bulk static download of the sandbox dataset was identified -- it is a
  live query API, not a file corpus.
- **Synthetic or real**: synthetic, per CMS's own sandbox description.
- **Good for**: claims/coverage-shaped FHIR resource coverage if the module
  ever needs to model payer-side data; not useful for chart/note layout
  learning, since Blue Button is a claims API, not a document export.

## Vendor developer portals and sandboxes (live services, not static corpora)

Every vendor named in the task was checked for a **downloadable, static,
first-party sample document** -- something a developer could fetch once and
keep, distinct from a live sandbox that requires ongoing credentials. None
was found. What each vendor publishes instead (API specifications, data
dictionaries, OAuth-gated sandboxes, EHI table schemas) is already
catalogued with primary-source citations in
`RESEARCH_EHR_LAYOUT.md`'s vendor table and in `docs/EHR_FORMATS.md`'s
per-vendor rows; this memo does not repeat those citations, only confirms
that they describe access mechanisms and schemas, not downloadable example
charts. The one partial exception on record anywhere in this repository's
research is an eClinicalWorks *training-manual* screenshot-adjacent guide
(cited in `docs/EHR_FORMATS.md`), which is vendor training material, not a
released sample document, and was not re-verified here.

Two vendors publish a schema-only artifact worth naming because it is easy
to mistake for a sample: Epic's EHI Tables index
(<https://open.epic.com/EHITables>, blocked by this environment's proxy,
summarized from search snippets) documents the *column names* of every
released EHI table as of the current release, and links to a technical
specification, but this is schema documentation, not a filled example row --
there is no patient-shaped sample content there. The same distinction
applies to every other vendor's EHI/API documentation surveyed in
`RESEARCH_EHR_LAYOUT.md`.

## Third-party aggregations attributing specific vendors (provenance not verifiable)

These are the only places this research found documents publicly labeled
with individual EHR vendor names outside of the vendors' own API/schema
docs. Both are unofficial, community-contributed GitHub repositories with no
vendor endorsement and no independent audit trail for any individual file.
Treat every vendor label in these repositories as an unverified claim by an
unknown submitter, not as vendor-attested output.

### `chb/sample_ccdas`

- **Publisher / nature**: community repository (`chb` = Children's Hospital
  Boston GitHub account associated with the SMART Health IT project),
  "Repository of Sample CCDA Documents -- all comers welcome."
- **URL**: <https://github.com/chb/sample_ccdas>
- **Format(s)**: C-CDA XML.
- **Folders observed** (root listing, folder names only -- no file contents
  were opened): `Allscripts Samples`, `Cerner Samples`, `EMERGE`,
  `Greenway Samples`, `HL7 Samples`, `Kareo Samples`, `Kinsights Samples`,
  `NIST Samples`, `NextGen Samples`, `Partners HealthCare`,
  `PracticeFusion Samples`, `Transitions of Care Samples`, `Vitera`,
  `mTuitive OpNote Samples`.
- **Approximate count**: not enumerated; 14 folders, per-folder file counts
  unknown.
- **License**: **unclear at the repository level.** No `LICENSE` file
  exists in the repository root (confirmed: only `CDA.xsl` and `README.md`
  besides the folders). The README states a *contribution policy* --
  "Sample documents should be available under an open license and should
  not involve PHI" -- which is a request made of contributors, not a
  verified property of every file already in the repository.
  Contribution/submission accepted by fork-and-pull-request or direct email;
  no maintainer audit process is described.
- **Synthetic or real**: **not established.** The stated policy asks
  contributors not to include PHI, but this research found no evidence that
  submissions are checked. One folder name -- `Partners HealthCare` -- names
  a real health system (now part of Mass General Brigham) and is flagged
  specifically: this repository's origin is associated with the same
  Boston-area academic-medical-center research environment that also
  produced the real, DUA-gated i2b2/n2c2 corpora from Partners HealthCare
  (see below). That association does not mean the folder contains real
  patient data -- it plausibly contains synthetic test-patient output from a
  pilot system -- but this research did not open the folder's contents to
  check, consistent with the no-patient-data rule, and no one should treat
  the folder as safe without a human opening and clearing each file first.
- **Good for**: at most, a starting point for a human reviewer to manually
  vet individual files, one at a time, before any of them could be
  considered for use. Not a source to fetch or vendor programmatically as-is.

### `Smcner/CCDA`

- **Publisher / nature**: single-contributor community repository.
- **URL**: <https://github.com/Smcner/CCDA>
- **Format(s)**: C-CDA XML.
- **Vendors named**: eClinicalWorks, Allscripts Enterprise EHR
  (Touchworks), Epic.
- **License**: none found -- no `LICENSE` file, no license statement in the
  visible README.
- **Synthetic or real**: **higher risk than the repository above.** The
  README states "each document was either exported directly from the EHR
  source or obtained as PHR [personal health record]" -- i.e., the stated
  provenance is real exports or a real person's own PHR, not a stated
  synthetic-only policy. No PHI-exclusion statement was found. One example
  filename observed in the repository's own file listing --
  `brucewayne.xml` -- is an obviously fictional name and suggests at least
  some content is test/placeholder data, but a filename is not evidence
  about the document body, which was not opened.
- **Good for**: nothing, without a human first opening and individually
  clearing every file for real patient content. Given the stated
  "PHR or direct EHR export" provenance and absence of a no-PHI policy,
  this repository should be treated as a plausible real-data risk until
  proven otherwise, file by file -- this project's rule against any real
  patient data touching the repository is stricter than "GitHub allowed it
  to be posted publicly."

## Conformance-testing services (not downloadable corpora)

### AEGIS Touchstone

- **Publisher / nature**: AEGIS.net's commercial/community FHIR
  conformance-testing platform ("over 1500 tests," per the vendor's own
  marketing copy), referenced in the task as a candidate source.
- **URL**: <https://touchstone.aegis.net/touchstone/>
- **Findings**: this is an account-gated testing *service* that runs
  TestScripts against a system under test; this research found no evidence
  of a downloadable, static C-CDA or FHIR sample corpus published from it
  for anonymous public download. Not confirmed either way for FHIR example
  resources bundled with specific IG test plans a registered user could
  export -- that would require an account this research did not create.
- **Good for**: live conformance testing against a running server, not
  static fixture sourcing. UNCERTAIN whether it offers exportable sample
  data to a registered account; not verified.

## Real or possibly-real patient text corpora -- reference only, never vendored

Everything in this section is cited so a future engineer knows it exists and
knows exactly why it cannot be used here. None of it may be downloaded,
copied, quoted, or fetched by any script in this repository, regardless of
DUA status, because the project's own no-PHI rule is stricter than any
individual DUA's terms.

### i2b2 / n2c2 (DBMI Data Portal)

- **Publisher / nature**: Harvard DBMI's National NLP Clinical Challenges
  (n2c2), successor to the original i2b2 shared-task datasets. Both draw
  discharge summaries and clinical notes from real institutions -- Partners
  HealthCare, Beth Israel Deaconess Medical Center, and the University of
  Pittsburgh Medical Center are named as sources across the 2006-2014
  challenge years, per the challenges' own published descriptions.
- **URL**: <https://n2c2.dbmi.hms.harvard.edu/data-sets>,
  <https://portal.dbmi.hms.harvard.edu/>
- **Format(s)**: de-identified free-text clinical notes (discharge
  summaries, progress notes), annotated for various NLP shared tasks.
- **License / access**: gated behind a Data Use Agreement (DUA); both an
  academic and a corporate DUA are offered, reviewable before signing.
- **Synthetic or real**: **real, de-identified patient data.** Never enters
  this repository.
- **Good for**: nothing here. Cataloged for completeness only.

### MIMIC-IV-Note (PhysioNet)

- **Publisher / nature**: MIT Laboratory for Computational Physiology,
  hosted on PhysioNet.
- **URL**: <https://physionet.org/content/mimic-iv-note/2.2/>
- **Format(s)**: de-identified free-text clinical notes; over 300,000 notes
  per PhysioNet's own dataset description.
- **License / access**: PhysioNet Credentialed Health Data Use Agreement
  1.5.0; requires a PhysioNet account, credentialed-access approval, and
  completion of CITI "Data or Specimens Only Research" training before any
  file can be downloaded.
- **Synthetic or real**: **real, de-identified patient data.** Never enters
  this repository.
- **Good for**: nothing here. Cataloged for completeness only.

### MTSamples

- **Publisher / nature**: mtsamples.com, a long-running medical
  transcription sample-report site aimed at MT students and professionals.
- **URL**: <https://www.mtsamples.com/> (blocked by this environment's
  egress proxy; findings below are from search-engine-indexed snippets of
  the site's own pages, not an independently re-fetched primary source --
  re-verify with direct browser access before relying on this entry).
- **Format(s)**: free-text transcribed reports, HTML.
- **Approximate count**: reported as 5,043 reports across roughly 40
  specialties, per a search-indexed snippet of the site's own claim.
- **License**: **unclear.** A search-indexed fragment of the site's terms
  states reports may be printed, shared, or linked "for educational
  purposes" with attribution; nothing found addresses redistribution into a
  software repository or commercial/engineering use, and the dedicated
  disclaimer page (`mtsamples.com/site/pages/disclaimer.asp`) could not be
  fetched directly to confirm.
- **Synthetic or real**: **unclear and treated as a real-data risk.**
  Search-indexed summaries describe the reports as "real transcribed
  examples"; whether that means real dictations from real (if de-identified)
  encounters, or transcriptionist practice/training dictations that were
  never real encounters, was not established from sources this research
  could reach. Given the ambiguity, this project's policy treats MTSamples
  as a possible real-patient-data source and does not use it.
- **Good for**: nothing here pending a clear, directly-verified statement
  of both provenance and license. Cataloged for completeness only.

## What we can and cannot obtain, by vendor

| Vendor | Genuine first-party sample corpus? | What actually exists publicly |
|---|---|---|
| Epic | No | Live FHIR sandbox (registration required); EHI table *schema* (column names, no sample rows); no downloadable sample chart found. |
| Oracle Health / Cerner | No | Live FHIR sandbox (code console); EHI specification documents describing format, no sample export found. Third-party `chb/sample_ccdas` has a "Cerner Samples" folder of unverified origin. |
| athenahealth | No | API/IG documentation and tenant-specific sandboxes only; no downloadable sample export found. |
| eClinicalWorks | No, with one caveat | A training/user-guide PDF exists (cited in `docs/EHR_FORMATS.md`) that is instructional material, not a released sample file. Third-party `Smcner/CCDA` claims an eCW export of unverified provenance. |
| NextGen | No | API documentation only. Third-party `chb/sample_ccdas` has a "NextGen Samples" folder of unverified origin. |
| Allscripts / Veradigm | No | Live FHIR sandboxes and versioned EHI/TSV documentation only. Third-party repositories claim "Allscripts Samples"/"Allscripts Enterprise EHR (Touchworks)" content of unverified origin. |
| Practice Fusion (Veradigm) | No | Versioned EHI (TSV schema) documentation and FHIR API docs only. Third-party `chb/sample_ccdas` has a "PracticeFusion Samples" folder of unverified origin. |
| DrChrono | No | API/export documentation only; no sample file found. |
| Kareo / Tebra | No | API/export/help documentation only. Third-party `chb/sample_ccdas` has a (pre-rename) "Kareo Samples" folder of unverified origin. |
| AdvancedMD | No | API access itself is NDA/Developer-Agreement-gated per the vendor's own developer portal; no public sample found. |
| Greenway | No | EHI documentation site only. Third-party `chb/sample_ccdas` has a "Greenway Samples" folder of unverified origin. |
| MEDITECH | No | API documentation only, under terms that explicitly disclaim reliability guarantees. |
| CureMD | No | A public FHIR API specification PDF exists; no sample document corpus found. |
| Modernizing Medicine (ModMed/EMA) | No | Public API specification PDFs exist (several dated versions); no sample document corpus found. |

Read that table the way it is meant to be read: for every named commercial
vendor, the honest answer to "can we obtain a genuine sample of this
vendor's own document output, today, publicly, with a clear license" is no.
What exists instead is either a live, credentialed, ever-changing sandbox
(useful for API-shape testing, useless for pinning a static layout fixture),
or an unverified third-party claim that a given file came from that vendor.

## What this means for the module's promise

This does not change the conclusion already reached in
`RESEARCH_EHR_LAYOUT.md` -- it sharpens it. That memo already established
that a learned style pack can only ever be a tested claim over a named,
reviewed fixture set, never a guarantee about "every deployment of vendor
X." This corpus survey shows that for nearly every named vendor, the
project cannot even obtain *one* genuinely vendor-attested fixture publicly,
which means the reviewed-fixture-set discipline that memo requires is not
optional caution -- it is the only thing standing between this module and a
false claim, because there is no public "ground truth" file to check a
learned pack against for most vendors. Where a vendor-labeled document shows
up online, it is unverified, and treating it as ground truth would be worse
than having no fixture at all, because it would produce false confidence
backed by an unauditable source.

The module's realistic, defensible test posture, given what actually
exists:

- **Construct/parsing/coverage testing** can be strong and license-clean,
  built from HL7's own example libraries, ONC's certification test-data
  repositories, and Synthea-generated output. This exercises schema
  validity, C-CDA template/section/entry coverage, and participant/
  provenance extraction against real published specifications.
- **Layout-learning testing against a genuine vendor sample** can only
  happen where an actual physician-in-the-loop supplies one -- the module's
  own stated workflow -- because no public, license-clean, vendor-attested
  sample exists to seed it for most vendors. Any claim that the module has
  been validated against "Epic's layout" or "athenahealth's layout" using a
  public sample is not supportable from anything catalogued here.
- **Adversarial/malformed-input testing** has a plausible but unconfirmed
  source in the ONC certification test decks (certification testing
  routinely includes deliberately invalid or edge-case payloads to test a
  validator's rejection behavior) -- this would need file-by-file
  identification before use, not a blanket assumption that the whole
  corpus is "the malformed set."

## Recommendation: how to consume this corpus

Chosen per source, from its license and provenance, not applied uniformly:

- **Vendor into `tests/fixtures/` (small, pinned, hash-recorded)**: HL7
  `C-CDA-Examples` (CC0-1.0, confirmed license), ONC's
  `ccda-uscdi-certification-testdata` and `2015-Certification-C-CDA-Test-Data`
  (federal work / CC0-1.0 respectively), Synthea-generated output produced
  locally by this project's own pinned Synthea version (Apache-2.0 tool,
  output is this project's own generation, not a redistribution question),
  and `smart-on-fhir/ehi-server`'s Apache-2.0 reference fixtures. For each,
  record the upstream URL, retrieval date, exact commit/release, and a
  per-file sha256 in the fixture's own header or an adjacent manifest -- the
  same discipline `RESEARCH_EHR_LAYOUT.md`'s source-manifest requirement
  already demands of any ingested sample.
- **Fetch by script at test time, not vendored**: the current ONC USCDI
  v1-v4 test-data repository if it is expected to keep changing with future
  USCDI versions (fetch pinned to a commit SHA, not a branch), and any
  Inferno/`uscore-data-script`-generated bundle regenerated against a pinned
  Synthea/script version rather than committed as a stale binary.
- **Kept external, referenced only, never fetched programmatically**:
  every vendor's live sandbox (Epic, Cerner/Oracle Health, athenahealth,
  Allscripts/Veradigm, CMS Blue Button 2.0) -- these require live credentials
  tied to a registered application under that vendor's own developer-program
  terms, return data that can change without notice, and vendoring a
  snapshot would misrepresent it as static vendor truth when RESEARCH
  already establishes it cannot be.
- **Reference URL only, human-gated, never automated**: `chb/sample_ccdas`
  and `Smcner/CCDA` -- a human reviewer may open and individually clear
  specific files from these repositories before any single file is
  considered as a manually-reviewed synthetic-or-cleared fixture, but no
  script in this repository should fetch, mirror, or trust them
  automatically, and the `Partners HealthCare` and PHR-sourced content
  described above should not be opened at all without explicit written
  clearance.
- **Never fetched, never vendored, reference-only forever**: i2b2/n2c2,
  MIMIC-IV-Note, and MTSamples. Cataloged above so no one re-discovers them
  and assumes DUA compliance is sufficient -- it is not, for this
  repository.

## Source index

Standards and certification: [HL7 C-CDA-Examples](https://github.com/HL7/C-CDA-Examples),
[HL7 CDA-Examples](https://github.com/HL7/CDA-Examples),
[ONC ccda-uscdi-certification-testdata](https://github.com/onc-healthit/ccda-uscdi-certification-testdata),
[ONC 2015-Certification-C-CDA-Test-Data](https://github.com/siteadmin/2015-Certification-C-CDA-Test-Data),
[ONC Edge Testing Tool source](https://github.com/onc-healthit/ett),
[AEGIS Touchstone](https://touchstone.aegis.net/touchstone/).

Synthetic generators and reference servers:
[Synthea](https://github.com/synthetichealth/synthea),
[SMART Health IT ehi-server](https://github.com/smart-on-fhir/ehi-server),
[SMART Health IT ehi-app](https://github.com/smart-on-fhir/ehi-app),
[Inferno uscore-data-script](https://github.com/inferno-framework/uscore-data-script).

Federal sandbox: [CMS Blue Button 2.0 developer sandbox](https://bluebutton.cms.gov/api-documentation/developer-sandbox/).

Vendor schema documentation (not sample data): [Epic EHI Tables](https://open.epic.com/EHITables).

Third-party aggregations, unverified provenance:
[chb/sample_ccdas](https://github.com/chb/sample_ccdas),
[Smcner/CCDA](https://github.com/Smcner/CCDA).

Real/possibly-real patient corpora, reference-only:
[n2c2 data sets](https://n2c2.dbmi.hms.harvard.edu/data-sets),
[DBMI Data Portal](https://portal.dbmi.hms.harvard.edu/),
[MIMIC-IV-Note](https://physionet.org/content/mimic-iv-note/2.2/),
[MTSamples](https://www.mtsamples.com/).

Internal cross-references: `docs/audits/learned-source/RESEARCH_EHR_LAYOUT.md`
(theory and per-vendor API/integration citations),
`docs/EHR_FORMATS.md` (per-vendor EHI-format survey and its own
"Public non-PHI note sample?" findings), `docs/vendor_refs/` (vendor schema
detail cited from the shipped source adapters).
