# EHR EHI-export formats — the 12-vendor survey

> Surveyed 2026-06-12 (live web verification by a research agent; primary
> sources only). Every vendor's primary EHI doc URL returned HTTP 403 to
> automated fetches (WAF blocking) but is publicly indexed — content was
> confirmed through search-engine-indexed snippets of the exact official
> pages ("403-but-indexed"). Re-verify with browser access before building
> against any of them, and bump the registry's `verified` dates when you do.
> The regulatory hook: every ONC-certified EHR must publish its
> §170.315(b)(10) EHI-export documentation publicly
> (<https://healthit.gov/test-method/electronic-health-information-export/>).

This document seeds source adapters (PLAN M6+) and the capability registry
(`destinations/registry.yaml` — the routing entries carry their own
citations). Statuses: **VERIFIED** = official-source citation; **UNCERTAIN**
= not confirmable from a primary source, do not build against it.

## The table

| Vendor | EHI format | Clinical-notes representation | Public non-PHI note sample? | Import path |
|---|---|---|---|---|
| **Epic** | TSV tables + RTF/binary sidecars — [open.epic.com/EHITables](https://open.epic.com/EHITables) | Structured fields in TSV (`NOTE_ENC_INFO`, `REG_HX_NOTES`, …); rich text/images as sidecar downloads referenced from rows (VERIFIED) | None found | FHIR R4 DocumentReference POST (in registry) |
| **Oracle Health (Cerner)** | MySQL SQL dump (DDL + data) — [overview PDFs](https://www.oracle.com/health/regulatory/certified-health-it/) | Notes in `BLOB_REFERENCE` + Document Imaging, exported in original stored format (PDF/TXT/DICOM/audio); no plain-text note column (VERIFIED) | Training manual exists on a public hospital site (UNCERTAIN retrievability) | FHIR R4 [DocumentReference POST](https://docs.oracle.com/en/industries/health/millennium-platform-apis/mfrap/op-documentreference-post.html), unstructured notes (VERIFIED → registry) |
| **eClinicalWorks** | CSV via Windows utility — ehi.eclinicalworks.com (cited in the [Nov 2025 disclosure](https://www.eclinicalworks.com/wp-content/uploads/2025/12/ecw-onc-health-it-certification-details-and-additional-costs-disclosures-nov-2025.pdf)) | UNCERTAIN — primary doc 403; note-text shape unconfirmed | YES — [V12 Progress Notes user guide](https://www.scribd.com/document/833425805/360017-V12-EMR-Progress-Notes-User-Guide-Plan-Jan-2025) (vendor training doc) | FHIR R4 read (Connect); proprietary write claimed by third parties only (UNCERTAIN → registry `unverified`) |
| **NextGen** | Enterprise: C-CDA XML; Office: CSV/multi-format ([extract doc](https://docs.nextgen.com/en-US/file-maintenance-help-for-nextgenc2ae-enterprise-8-3239752/extract-patient-ehi-290684)) | **C-CDA export excludes encounter notes** (vendor-documented); notes only via Print-Chart PDF (VERIFIED) | None found | [Data Share module](https://docs.nextgen.com/en-US/nextgenc2ae-enterprise-ehr-help-3270157/data-share-module-369560) imports C-CDA (VERIFIED → registry); FHIR write scope UNCERTAIN |
| **athenahealth** | NDJSON (FHIR R4 Bulk) — [clinical EHI export](https://docs.athenahealth.com/athenaone-dataexports/ambulatory/clinical-ehi-export) | DocumentReference-wrapped attachments; human-readable sidecars XML/JPEG/PNG/TIFF/PDF (VERIFIED) | None found | In registry (clinical-document POST + CCDA Upload API) |
| **Veradigm (Allscripts)** | TSV tables — [versioned docs v2–v6](https://veradigm.com/legal/veradigm-view-ehi-export-documentation/v6/index/) | `patient-encounter-documents.tsv`, `…-assessments.tsv`, `pinned-notes.tsv` (VERIFIED file list) | None found | FHIR read-only; Unity API write behind partnership (UNCERTAIN → registry `unverified`) |
| **AdvancedMD** | Single-patient C-CDA + bulk SQL `.bak` — [data dictionary](https://info.advancedmd.com/rs/332-PCG-555/images/advancedmd-ehrExport-dataDictionary.pdf) | C-CDA Progress Note section; bulk `EHR_PatientNotes` table, WordMerge `<<field>>` text (VERIFIED via doc titles) | None found | Connect APIs (non-FHIR, CRUD) — document-write op unconfirmed (UNCERTAIN) |
| **DrChrono** | CSV zip — [EHI reference guide v2](https://support.drchrono.com/home/ehi-export-reference-guide-v2) | `ClinicalNotes.csv`, `SoapNoteLineItemFieldValue.csv`, `CustomClinicalNoteSections.csv` (VERIFIED file list); text format UNCERTAIN | None found | In registry (`POST /api/documents`) |
| **ModMed (EMA)** | Pipe-delimited CSV + XML/JSON/PDF sidecars — [bulk data dictionary](https://www.modmed.com/wp-content/uploads/2026/03/crp-14270-ModMed-EMA_EHI-Export-Data-Dictionary-for-Bulk-Population.pdf) | Note-text columns UNCERTAIN; gGastro variant: SQL `.bak` + `.mht` visit files | None found | FHIR is read/search/bulk **only** per the [July 2024 API doc](https://www.modmed.com/wp-content/uploads/2024/07/MMI-Certified-FHIR-API-Documentation-July-2024.pdf) (cited negative → registry) |
| **Greenway** | PrimeSuite: decrypted PDF/C-CDA/image/TXT files; Intergy: CSV — [ehi.greenwayhealth.com](https://ehi.greenwayhealth.com/) | `ClinicalBin_*_Document.ext` files (VERIFIED); column detail UNCERTAIN | None found | FHIR + GAPI exist; write scope UNCERTAIN |
| **Practice Fusion** | TSV tables — [versioned docs v1–v9](https://www.practicefusion.com/ehi-export-documentation/v9/index/) | `patient-encounters.tsv`, `…-documents.tsv`, `…-addendums.tsv`, `…-observations.tsv` (VERIFIED — this is the shipped `pf_tebra` adapter's format) | None found | In registry (FHIR read-only, cited negative) |
| **Tebra** | C-CDA XML (.ccda per patient) — [help center](https://helpme.tebra.com/Platform/Practice_Settings/Data_Management/Export_Patient_Clinical_Data) | Notes embedded in C-CDA Summary of Care (VERIFIED); no standalone §(b)(10) spec page found (UNCERTAIN) | None found | In registry (in-product C-CDA import) |

## Ranked next-adapter targets

1. **Oracle Health (Cerner)** — documented relational schema (SQL dump with
   DDL), public overview PDFs, and the only formally documented FHIR
   DocumentReference **write** on this list, with an active developer
   community. Hospital-side market weight (exact share UNCERTAIN).
2. **eClinicalWorks** — largest ambulatory footprint (vendor-claimed
   ~180k providers, UNCERTAIN independently); CSV export cited in its own
   certification disclosure; FHIR Connect public. Risk: the EHI schema page
   is 403 — recover the schema via the export utility or partner channels.
3. **NextGen** — dual product line, public legal PDF + docs pages, public
   FHIR portal. The vendor-documented gap (C-CDA export *excludes* notes)
   makes the adapter scope crisp: structured C-CDA + a separate
   unstructured-notes path — the dual pipeline worth building early.

## What this means for "every EHR, pixel-faithful"

Rendered note **layouts** are not published anywhere (the one partial
exception above is an eCW training guide). That is a structural fact, not a
research gap: pixel fidelity comes from the operator's own sample documents
through the layout learner (`anast pack init --from-samples`), exactly as
the Practice Fusion replica was built. Data-side adapters come from the
tables above; layout-side packs come from samples; the registry routes only
what is cited. Nothing here is assumed.
