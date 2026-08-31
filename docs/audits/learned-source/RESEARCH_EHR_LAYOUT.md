# EHR export, document formats, and reviewed layout emulation

**Research memo (PHI-free; checked 2026-08-30)**

## Purpose and conclusion

This memo defines a defensible contract for the complete **Learn-from-sample ->
Review -> Migration-toggle** workflow. It covers structured clinical input from
C-CDA, FHIR, and vendor EHI exports, and a separate, reviewable destination-EHR
document-style layer.

The central boundary is:

> Interoperability standards can make content computable and can constrain
> document metadata, resource structure, and provenance. They do not specify a
> vendor's screen CSS, fonts, spacing, pagination policy, custom template, or
> proprietary rendering engine. A reviewed style pack can emulate an observed
> document structure; it cannot establish that generated output is the native
> destination-EHR UI or that one sample represents every deployment.

Therefore, the pipeline must keep semantic migration and presentation emulation
as independent artifacts. A style match is a tested visual claim over a named,
approved fixture set, never an implicit claim of clinical or vendor equivalence.

No patient names, values, source filenames, screenshots, or local fixture data
are reproduced here. Vendor examples and marketing images are not layout truth.

## What the standards do and do not guarantee

### ONC EHI Export

[ONC Electronic Health Information Export](https://healthit.gov/test-method/electronic-health-information-export/)
(Certification Companion Guide v1.2, issued 2024-03-11; page last updated
2025-07-23 when checked) defines EHI as electronic protected health information
to the extent it is in the designated record set, with the familiar exclusions
for psychotherapy notes and information compiled for legal proceedings. The
scope is the data the certified product can store at the time of certification;
it includes data stored by the certified module and by non-certified
capabilities, and varies by developer and product.

The ONC guidance explicitly does **not** prescribe one transport, medium,
predefined dataset, internal data model, or export format. An export need not
match the source product's internal format and the developer need not publish a
proprietary data model. Stored images/imaging information in scope must be
exported; where the product stores only a link, the link is what is required.
The files must be electronic and computable. Single-patient and population
exports have different technical outcomes, and a user should be able to run
the export without developer assistance.

Implication: “EHI export” is a scope and availability obligation, not a visual
document contract. An implementation must capture the exact product/release,
export specification, manifest, MIME types, link behavior, and data dictionary
for each source. It must not assume that two certified products expose the same
fields or serialization.

[ONC Understanding EHI](https://healthit.gov/information-blocking/understanding-electronic-health-information-ehi/)
(current guidance checked 2026-08-30) is the companion scope reference. The
regulatory text is [45 CFR 170.315](https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-D/part-170/subpart-C/section-170.315).

### FHIR, US Core, and clinical notes

[FHIR R4 Composition](https://hl7.org/fhir/R4/composition.html) describes the
structured composition of a document but is not a document by itself. A
document is normally a [FHIR R4 Bundle](https://hl7.org/fhir/R4/bundle.html)
with `type=document`, a Composition as its first entry, and supporting
resources. [FHIR R4 DocumentReference](https://hl7.org/fhir/R4/documentreference.html)
indexes a serialized object and may point to CDA, a clinical note, scanned
paper, PDF, image, or office file. [Attachment](https://hl7.org/fhir/R4/datatypes.html)
records content type, inline data or URL, size/hash/title/creation metadata;
it does not prescribe the destination renderer.

[US Core](https://www.hl7.org/fhir/us/core/) is currently published as v9.0.0
(STU9, based on FHIR R4; maturity 3, Trial-use, checked 2026-08-30). It defines
minimum constraints, required elements, and REST interaction expectations.
Implementations may choose profile-only or profile-plus-interaction modes.
Examples are informative and are not a complete representation of production
data. A source's CapabilityStatement and exact US Core version must be pinned.

[US Core Clinical Notes](https://hl7.org/fhir/us/core/clinical-notes.html)
(v9.0.0, checked 2026-08-30) is particularly important for this workflow. It
does not define new note types or prescribe the content of every note. It says
that note content varies by system, location, and facility requirement. A
clinical note can be exposed in `DocumentReference` or a coded
`DiagnosticReport`; the actual content is commonly an attachment in text,
XHTML, PDF, or CDA. Some systems classify a narrative/scanned report one way
and some the other. A report may contain history, impressions, and conclusions,
or only an impressions section. A discharge summary is similarly
facility-dependent.

Implication: a FHIR-conformant resource can be semantically valid while having
different note content, attachment formats, section order, or page geometry.
“FHIR supported” cannot be used as evidence of a particular EHR visual style.

### C-CDA

[C-CDA 5.0.0](https://hl7.org/cda/us/ccda/5.0.0/) (STU5, current publication
checked 2026-08-30) is a library of CDA R2 templates for documents such as CCD,
care plan, consultation, discharge, history and physical, operative,
procedure, progress, referral, transfer summary, and unstructured documents.
It incorporates USCDI and implementation changes; C-CDA 2.1 has otherwise
received errata since 2015. C-CDA is not a FHIR R5 resource bundle merely
because the guide uses FHIR StructureDefinitions in its publication tooling.

[C-CDA Supporting Guidance](https://hl7.org/cda/us/ccda/5.0.0/supportingguidance.html)
is explicit about the narrative block: it is human-readable, should represent
the originating system's content, and need not be formatted identically to the
origin. A receiving application may render local conventions and is not
required to honor style hints. Where structured content is derived from a
narrative, or narrative from structured entries, the process/provenance should
be identified. `originalText` links are preferred where they prevent a coded
entry from drifting from its source narrative.

Implication: preserve both the narrative and structured entries, retain
template IDs, `nullFlavor`, coded-entry links, and provenance, and test them
separately from CSS/layout. “Renders like the source” is not a C-CDA
requirement.

### ONC standardized API and test material

[ONC Standardized API for Patient and Population Services](https://healthit.gov/test-method/standardized-api-for-patient-and-population-services-acb-atl/)
(Companion Guide v1.12, issued 2024-03-11; page checked 2026-08-30) ties
certification to specified FHIR/US Core versions and mandatory/must-support
expectations. The certified service is a read service; the guide explicitly
excludes write capability. Supported certification combinations include USCDI
v3 + US Core 6.1, USCDI v4 + US Core 7.0, and USCDI v5 + US Core 8.0.1, subject
to the applicable test/version rules. A product's deployed CapabilityStatement
still controls what is actually available. The ONC test kit does not provide
the implementer's production dataset; the developer supplies test data.

[Inferno g(10) certification tests](https://fhir.healthit.gov/suites/g10_certification)
(version 8.0.0, updated 2026-03-09 when checked) are an implementation test
kit/demonstration, not a guarantee of a vendor's note content or visual output.
[ONC SVAP](https://isp.healthit.gov/standards-version-advancement-process?page=1)
and the [FHIR R4.0.1 SVAP entry](https://www.healthit.gov/isp/svap-standard/170215a1)
are the version history to use when a certification release claims an advanced
standard.

## Public vendor evidence and integration variability

The following are official vendor resources checked 2026-08-30. They are
useful for source inventory and contract discovery; they are not permission to
copy proprietary code, patient examples, screenshots, or UI assets.

| Product | Public format/API evidence | Version/date observed | Integration and license/terms note |
|---|---|---|---|
| **Epic** | [Epic on FHIR](https://fhir.epic.com/) provides a public developer resource and free sandbox with example data. [Epic integration documentation](https://fhir.epic.com/Documentation?docId=epicidtypes) shows R4 endpoint/base-URL and client-ID/`aud` details. | Public resource describes R4, STU3, and DSTU2 support at resource/API level; the exact production base URL and supported operations are integration-specific. | Sandbox access is public; production access is arranged with the health-system/customer and Epic's applicable program/terms. Do not infer layout from the sandbox or examples. |
| **Oracle Health/Cerner** | [Millennium Platform APIs](https://docs.oracle.com/en/industries/health/millennium-platform-apis/apis.html) list FHIR R4/EHR APIs and resources including DocumentReference, DiagnosticReport, Binary, Bundle, and Bulk Data. [FHIR FAQ](https://docs.oracle.com/en/industries/health/millennium-platform-apis/fhir-faqs-common-issues/) notes that some services remain additional where no standard equivalent exists. [Oracle certified-health-IT/EHI specs](https://www.oracle.com/health/certified-health-it/) describe multiple storage locations and original formats for multimedia/document exports. | Oracle docs state DSTU2 is no longer supported and has been replaced by R4; EHI specifications are release/product-specific. | API tenant, OAuth/SMART setup, and customer access are required. The EHI specification's release variation and original-file behavior must be recorded; “Oracle/Cerner” is not one universal renderer. |
| **athenahealth** | [Developer portal](https://www.athenahealth.com/developer-portal) advertises HL7, C-CDA, custom interfaces, Data View/bulk extraction, athenaOne EHI Export, and athena-to-athena migration. [FHIR R4 REST guide](https://docs.mydata.athenahealth.com/fhir-r4/restapi.html) is tenant/sandbox-specific. [athena Core implementation guide](https://fhir.athena.io/athenacoreext/index.html) documents vendor profiles/extensions. [Ambulatory clinical EHI export](https://docs.athenahealth.com/downloads/exports-ambulatory-clinical-ehi-export) lists datasets including clinical/medical-record documents and notes. | REST guide observed as version 25.0.0; athena Core IG v5.13.0, FHIR R4/US Core STU3-based, release shown 2026-08-20. API-count footnote on portal is dated September 2022. | Multi-tenant access, credentials, product release, and the applicable interface/export terms control. Vendor profiles can make fields optional or unsupported; do not substitute generic US Core assumptions. |
| **MEDITECH** | [MEDITECH API documentation](https://home.meditech.com/en/d/restapiresources/pages/apidoc.htm) covers US Core FHIR R4 and Argonaut FHIR R2 across Expanse and other platform/version combinations. | The page lists Expanse 2.2/2.1, 6.1/6.0, Client/Server, and MAGIC combinations with CHPL identifiers. | [API terms](https://home.meditech.com/en/d/restapiresources/pages/apiterms.htm) require HCO registration and reserve the right to modify, limit, disable, or discontinue APIs; backward compatibility is not guaranteed. The terms provide the strongest caution here: the API is “as is” and makes no representation about suitability, reliability, or accuracy. Pin HCO, product, CHPL, and API revision. |
| **TruBridge** | [TruBridge certifications](https://trubridge.com/certifications/) list TruBridge EHR and Provider EHR v22 certification, including EHI export and standardized API criteria. [FHIR developer site](https://fhir-developer.plt.trubridge.com/) describes read-only USCDI access. [EHI export overview](https://ehi-export.plt.trubridge.com/trubridge/v2200/) and [database schema](https://ehi-export.plt.trubridge.com/trubridge/v2200/trubridge-database/) describe clinical data, JSON database data, documents/images, and insurance/eligibility categories. | EHR/Provider EHR v22 certification date shown as 2024-12-10; EHI documentation path v2200. | The [export root](https://ehi-export.plt.trubridge.com/) says method/format varies by TruBridge product. Registration/terms and release-specific definitions apply. Store the schema URL/hash; never treat the example file naming convention as a visual contract. |
| **NextGen** | [NextGen APIs](https://www.nextgen.com/api) explicitly distinguishes FHIR patient access from proprietary Enterprise JSON APIs (800+ routes). It lists NextGen Enterprise FHIR DSTU2/R4 and NextGen Office FHIR R4, Smart App Launch, and Bulk FHIR. | Enterprise support is described for v5.9+; Office support for 5.0+; exact routes and certification differ by product. | Public overview plus [Office FHIR guide](https://www.nextgen.com/-/media/files/ngo/nextgen-office-fhir-r4-api-developer-guide) and [patient auth guide](https://dev-cm.nextgen.com/-/media/files/api/NGE-Patient-API-Auth-Guide.pdf). Developer onboarding/instance configuration controls access. “FHIR” and proprietary JSON are distinct contracts, and neither defines a UI layout. |
| **Practice Fusion (Veradigm)** | [Developer Center](https://www.practicefusion.com/developer-center/) links FHIR, labs/imaging/billing APIs, and EHI documentation. [FHIR specifications](https://www.practicefusion.com/fhir/api-specifications/) identify FHIR 4.0.1/US Core capability and DocumentReference/CCD support. [EHI Export v9](https://www.practicefusion.com/ehi-export-documentation/v9/index/) documents computable TSV exports and categories such as patient documents, clinical, billing, labs, referrals, and messaging. | EHI v9 page published 2026-01-12; EHI index lists prior versions. | Developer registration, terms, and base-URL onboarding apply. TSV EHI is a different source contract from FHIR/C-CDA and must be mapped with a vendor schema and coverage report. |
| **Tebra (formerly Kareo)** | [Tebra FHIR API User Guide](https://www.tebra.com/wp-content/uploads/2025/05/Tebra-FHIR-API-User-Guide.pdf) describes a patient/third-party API for USCDI v1, US Core STU3 3.1.1 on FHIR R4, with R4 work continuing. [Tebra export help](https://helpme.tebra.com/Platform/Practice_Settings/Data_Management/Export_Patient_Clinical_Data) describes individual XML Summary of Care files and patient documents. [Import help](https://helpme.tebra.com/Platform/Practice_Settings/Data_Management/Import_Patient_Clinical_Data) says `.ccda` imports may involve a data service and fees. | User guide updated May 2025; help pages observed updated 2025-12-19 and 2026-01-08. Tebra's 2025 [real-world test plan](https://www.tebra.com/wp-content/uploads/2024/11/Tebra-5.0-Real-World-Test-Plan-2025.pdf) and [2024 results](https://www.tebra.com/wp-content/uploads/2025/02/2024_test_results.pdf) are additional official evidence of EHI/C-CDA/document variation. | Migration is often provider/data-service-mediated; export and import capabilities, document types, filters, and fees vary. Do not claim that a C-CDA export/import round trip preserves native page styling. |
| **AdvancedMD** | [Developer portal](https://developer.advancedmd.com/) and [Get Started](https://developer.advancedmd.com/get-started) describe FHIR R4-based read-only CEHRT APIs, proprietary Connect APIs for CRUD, outbound C-CDA/HIE, and ODBC for bulk extraction. | Documentation is current portal content; FHIR/API version and tenant behavior are contract-specific. | [API connection request](https://www.advancedmd.com/api-connection-request/) says full docs require an NDA/Developer Agreement and describes sandbox/connection costs. Production integration therefore has contractual/licensing gates; use only approved docs and fixtures. |
| **DrChrono** | [Current API docs](https://app.drchrono.com/api-docs/) expose v11.0 resources including clinical-note templates. [Stable v3 documentation](https://app.drchrono.com/api-docs-old/v3/documentation) describes JSON endpoints for note templates, notes, field types, values, pagination, and version behavior. | Current site labels v11.0; v3 docs retain the detailed historical endpoint contract. The v3 policy documents 500 calls/hour default rate limiting and version/deprecation behavior. | App registration and permissions apply. Template APIs expose structured field/order data, not the proprietary rendered CSS or a guarantee of native visual output. Pin API version and preserve 429/rate-limit handling. |

The common pattern is deliberate: the same product family can expose more than
one API, product generation, tenant, release, export mode, or customer
configuration. A certification badge or FHIR label is not a layout specification.

## Candidate deterministic layout/reconstruction stack

These are open-source or research-primary candidates, not claims of medical
accuracy. Pin every library, model, model hash, binary, font, browser, and
container image. Keep OCR/layout confidence and coordinates as evidence, never
as clinical truth. Avoid copying proprietary EHR assets.

| Component | Evidence and license | Appropriate role and caveat |
|---|---|---|
| [pdfplumber](https://github.com/jsvine/pdfplumber) | MIT; extracts PDF characters, words, rectangles, lines, tables, and coordinates. | First choice for born-digital PDF geometry and deterministic word/region extraction. `layout=True` is experimental; test against approved fixtures. |
| [pdfminer.six](https://github.com/pdfminer/pdfminer.six) | MIT; Python text/layout analysis with location, font, and color information. | Cross-check native PDF geometry/fonts; preserve source coordinates. Do not infer semantics solely from position. |
| [Tesseract](https://github.com/tesseract-ocr/tesseract) | Apache-2.0 engine (Leptonica dependency is BSD-2-Clause); stable major line is 5; supports hOCR, TSV, ALTO, PAGE, and PDF with positions. | Deterministic OCR fallback for scans. Pin executable and traineddata; confidence/bounding boxes require review thresholds and can be wrong. |
| [LayoutParser](https://github.com/Layout-Parser/layout-parser) and [paper](https://arxiv.org/abs/2103.15348) | Apache-2.0 toolkit; repository release v0.3.4 dated 2022-04-06 when checked. Model/data licenses are separate. | Reusable layout-region data structures and model adapters. It is a research toolkit with an aging release; pin dependencies and do not assume EHR-specific classes. |
| [Docling](https://github.com/docling-project/docling), [technical report](https://arxiv.org/abs/2408.09869) | MIT code; runs locally/offline; model licenses are separate. Current site/repository versions evolve quickly; pin exact commit. [DocLayNet](https://github.com/DS4SD/DocLayNet) is CDLA-Permissive-1.0, separate from code/model licenses. | Strong option for complex PDF reading order, tables, OCR, and structured intermediate representation. Heavier and probabilistic; use only when native extraction is insufficient and record model/hash. |
| [PaddleOCR PP-StructureV3](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/pipeline_usage/PP-StructureV3.en.md) | Apache-2.0 project; model/data terms must be checked separately. Official docs describe layout, OCR, table, and reading-order pipelines. | Optional higher-recall scan/table pipeline; heavier CPU/GPU footprint and model variance make it less suitable for a tiny baseline. |
| [deepdoctection](https://github.com/deepdoctection/deepdoctection) | Apache-2.0; current v1.0 page checked 2026-08-30; integrates OCR/layout/table backends with substantial dependencies. | Optional orchestration for difficult documents. More operational surface area than pdfplumber/pdfminer/Tesseract; model and backend licenses remain separate. |
| [Playwright screenshot assertions](https://playwright.dev/docs/next/test-snapshots) | Playwright repository is Apache-2.0. Assertions support `maxDiffPixels`, `maxDiffPixelRatio`, and `threshold`; screenshots stabilize by repeated capture. | Recommended visual-regression harness for generated HTML/PDF preview. Pin OS/browser/fonts/viewport, disable animations/caret/time/randomness, and mask only approved dynamic non-semantic regions. |
| [pixelmatch](https://github.com/mapbox/pixelmatch) | MIT; small dependency-free image diff with anti-aliasing handling and threshold/diff-density options. | Useful for rasterized PDF comparison or a simple independent diff. Combine with semantic/geometry checks; pixel identity can vary with rasterizer. |

Avoid introducing GPL/AGPL or model-weight obligations accidentally (for
example, some PDF/layout tools have dual or commercial terms). A legal review
is required before shipping any non-permissive dependency, model, font, or
vendor asset. Dataset and model licenses are independent of the wrapper
library's license.

## Proposed Learn -> Review -> Migrate contract

### 1. Ingest and normalize

1. Record a PHI-free source manifest: source kind (C-CDA, FHIR, EHI, or vendor
   API), product/release, exact specification/IG URL and retrieval date/hash,
   CapabilityStatement URL/hash where applicable, attachment MIME type and
   cryptographic hash, parser version, and configuration hash. Do not put
   patient values or original filenames in logs.
2. Produce a canonical semantic IR separate from layout. Preserve source IDs,
   section/template identifiers, coded values, `nullFlavor`, narrative text,
   `originalText` links, attachment metadata, resource references, and Bundle /
   Composition order. Store source provenance for every output field.
3. Validate against the exact selected version: C-CDA schema/templates and
   template IDs; FHIR R4 (4.0.1) plus the named US Core profiles and
   CapabilityStatement; or the vendor EHI data dictionary/manifest. Unknown,
   empty, unsupported, and malformed values must remain distinguishable.

### 2. Learn a style pack, without learning clinical content

The learning stage emits only a layout IR/style pack: page size/orientation,
margins, header/footer bands, repeated regions, table/grid geometry, reading
order, typography tokens, line/paragraph spacing, pagination rules, asset
hashes, and renderer parameters. It must not persist raw sample values.

The style pack is **unreviewed** until a human approves it against synthetic or
de-identified fixtures representing long, short, empty, multiline, table,
attachment, and page-break cases. The approved pack has an immutable ID,
version, evidence-fixture hashes, reviewer, review date, source/deployment
scope, and declared limitations. A pack may be labeled “reviewed style
similarity”; it may not be labeled “native vendor output” unless the vendor
itself has supplied and authorized a conformance oracle.

### 3. Apply the Migration toggle explicitly

The toggle selects a reviewed pack by ID/version. It must not alter the semantic
IR, source provenance, field mapping, or clinical values. Toggle-off uses the
safe generic renderer. Toggle-on records pack ID, source schema/version,
renderer/config/model/font/browser hashes, timestamp, and reviewer/review
state in the output manifest. The preview and final output use the same pack;
an unreviewed or out-of-scope pack is held for approval.

## Measurable acceptance criteria

The following are proposed engineering gates for this workflow. They are local
acceptance thresholds, not guarantees made by ONC, HL7, or any vendor.

| Area | Gate |
|---|---|
| **Source inventory** | Every run has a source-kind/product/release/spec hash, exact FHIR/US Core or C-CDA version where applicable, attachment MIME/hash, parser/config hash, and pack ID. Audit output contains aggregate counts/hashes only; a PHI scan finds no patient values or sample filenames. |
| **Schema/conformance** | C-CDA XML/schema/template validation and FHIR R4/profile validation run before rendering. Vendor EHI exports require a versioned manifest/data dictionary. A conformance failure blocks final output or is visibly marked as held. |
| **Coverage/no silent loss** | For every source field/class in scope, the report says `preserved`, `canonicalized`, `transformed`, `source-empty`, `unsupported`, or `invalid`, with reason and provenance. Zero unexplained drops; every unsupported in-scope field appears in a reviewer queue. |
| **High-risk values** | Patient identity, dates/times, author/organization, allergies, medications, results, status, and document type are exact after an explicitly documented canonicalization. Round-trip comparison has zero differences in these fields on the approved fixture set. |
| **Narrative/attachments** | C-CDA narrative and FHIR `presentedForm`/DocumentReference attachments retain source MIME/hash/reference and provenance. If content is reflowed, the source bytes remain available by hash and the output says “reflowed,” never “identical.” |
| **Semantic isolation** | Toggle on/off and pack changes produce identical canonical semantic-IR hashes and field-coverage reports; only layout/style/renderer manifests may differ. Applying a pack to a different source cannot change clinical values. |
| **Learned-pack evidence** | Pack output contains only layout tokens/geometry/order/assets-by-hash and no raw clinical values. It is not eligible for migration until a human approves synthetic/de-identified short/long/empty/table/attachment/page-break fixtures. |
| **Determinism** | With identical source bytes, config, parser/model hashes, font files, browser, OS/container, and viewport, run at least three times: zero semantic differences; geometry is byte-identical for native vector inputs or within 0.5 px (or 0.01 mm) after normalization; normalized manifest hash is identical. PDF metadata timestamps may be excluded only by an explicit normalizer. |
| **Visual regression** | Rasterize at a fixed DPI/viewport and compare with Playwright/pixelmatch. Start with `maxDiffPixelRatio <= 0.001` (0.1%) for the full page and an explicit per-region policy; critical identity/date/author/note/medication/allergy/result/footer/page-count regions require 100% semantic agreement and geometry within the declared tolerance. Any masked region is listed and must be dynamic/non-semantic. Thresholds are calibrated per renderer and fixture, not universal. |
| **Pagination/layout** | Approved fixtures assert page count, page size/orientation, header/footer bounds, table column count/row order, reading order, and no clipped/overlapping text. Long content and empty optional fields are required fixtures. A visual diff alone cannot waive a semantic or overflow failure. |
| **Confidence/safety** | OCR or ML regions retain confidence/bounding boxes/model hash. Below-threshold mappings, missing target style evidence, unsupported fields, and ambiguous document classification hold output for review. No clinical advice or invented text is generated; derived text is marked generated with provenance. |
| **Auditability** | Each final artifact points to source manifest hash, semantic-IR hash, style-pack ID/version, renderer/config/model/font/browser hashes, test suite version, reviewer, and review status. Logs remain PHI-free and value-free. Where appropriate, output metadata/banner says it is generated from source data and is not native destination-EHR UI. |

## Claims that must be prohibited or qualified

- Do not claim “FHIR/C-CDA compliant, therefore same as the destination EHR.”
- Do not claim “EHI complete” without naming the product/release/export mode,
  enumerating the tested in-scope classes, and reporting unsupported/source-empty
  fields.
- Do not claim “100% accurate migration” from one sample, one screenshot, or a
  single round trip. Use “zero observed loss on the named fixture set” and
  publish the coverage boundary.
- Do not claim pixel identity across browsers, operating systems, fonts,
  rasterizers, or printer/PDF engines without pinning those dependencies.
- Do not infer a universal template from an empty/short sample. A sample cannot
  reveal all optional fields, long-content wrapping, custom templates,
  responsive behavior, pagination, tenant configuration, or future release
  changes.
- Do not represent an observed emulation as a vendor-certified/native output,
  and do not use a marketing screenshot as the conformance oracle.

The defensible product statement is narrower: **for source specification X and
destination style-pack Y, the system preserved the enumerated semantic fields,
reported every unsupported/empty field, and met the declared geometry and visual
thresholds on reviewed fixture set Z in pinned environment W.**

## Direct source index

Standards: [ONC EHI Export](https://healthit.gov/test-method/electronic-health-information-export/),
[ONC EHI scope](https://healthit.gov/information-blocking/understanding-electronic-health-information-ehi/),
[45 CFR 170.315](https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-D/part-170/subpart-C/section-170.315),
[ONC standardized API](https://healthit.gov/test-method/standardized-api-for-patient-and-population-services-acb-atl/),
[Inferno g(10)](https://fhir.healthit.gov/suites/g10_certification),
[US Core](https://www.hl7.org/fhir/us/core/),
[US Core Clinical Notes](https://hl7.org/fhir/us/core/clinical-notes.html),
[C-CDA 5.0.0](https://hl7.org/cda/us/ccda/5.0.0/),
[C-CDA Supporting Guidance](https://hl7.org/cda/us/ccda/5.0.0/supportingguidance.html),
[FHIR R4 Composition](https://hl7.org/fhir/R4/composition.html),
[FHIR R4 Bundle](https://hl7.org/fhir/R4/bundle.html),
[FHIR R4 DocumentReference](https://hl7.org/fhir/R4/documentreference.html),
and [FHIR R4 Attachment](https://hl7.org/fhir/R4/datatypes.html).

Vendor resources: [Epic FHIR](https://fhir.epic.com/), [Oracle Millennium APIs](https://docs.oracle.com/en/industries/health/millennium-platform-apis/apis.html),
[Oracle EHI/certified health IT](https://www.oracle.com/health/certified-health-it/),
[athena developer portal](https://www.athenahealth.com/developer-portal),
[MEDITECH API docs](https://home.meditech.com/en/d/restapiresources/pages/apidoc.htm),
[MEDITECH API terms](https://home.meditech.com/en/d/restapiresources/pages/apiterms.htm),
[TruBridge EHI](https://ehi-export.plt.trubridge.com/), [NextGen APIs](https://www.nextgen.com/api),
[Practice Fusion EHI v9](https://www.practicefusion.com/ehi-export-documentation/v9/index/),
[Tebra FHIR guide](https://www.tebra.com/wp-content/uploads/2025/05/Tebra-FHIR-API-User-Guide.pdf),
[AdvancedMD developer portal](https://developer.advancedmd.com/), and [DrChrono API docs](https://app.drchrono.com/api-docs/).

Layout/visual tools: [pdfplumber](https://github.com/jsvine/pdfplumber),
[pdfminer.six](https://github.com/pdfminer/pdfminer.six), [Tesseract](https://github.com/tesseract-ocr/tesseract),
[LayoutParser](https://github.com/Layout-Parser/layout-parser), [Docling](https://github.com/docling-project/docling),
[PaddleOCR PP-StructureV3](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/pipeline_usage/PP-StructureV3.en.md),
[deepdoctection](https://github.com/deepdoctection/deepdoctection), [Playwright snapshots](https://playwright.dev/docs/next/test-snapshots),
and [pixelmatch](https://github.com/mapbox/pixelmatch).

