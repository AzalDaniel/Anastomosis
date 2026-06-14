# Oracle Health EHI export — schema brief

> Distilled from Oracle-published EHI-export specification material committed
> under `docs/`. **Spec facts only** — every claim cites its source file; no
> web research, no memory. No patient data appears in the source material or
> here: vendor sample fragments (Appendix C of [POP-OVW]) use placeholder
> values (e.g. literal `FNAME`/`LNANE`, epoch birthdates) and were not
> transcribed. Written for the future `sources/oracle_ehi/` adapter
> (PLAN M6+); see `docs/vendor_refs/README.md` for the provenance rules.

## 1. Sources

| Tag | File (under `docs/`) | What it is |
|---|---|---|
| [SP-OVW] | `cerner-corp-single-patient-ehi-export-data-overview.pdf` | Single-patient Millennium export overview & user instructions (4 pp., © 2023) |
| [POP-OVW] | `cerner-corp-patient-population-ehi-export-data-overview.pdf` | Patient-population Millennium export overview & user instructions, with Appendices A–E (28 pp., © 2026) |
| [HDI-OVW] | `health-data-intelligence-ehi-export-data-overview-user-instructions.pdf` | Health Data Intelligence (HDI) single-patient *and* population export overview (3 pp., © 2024) |
| [HDI-SPEC] | `health-data-intelligence-ehi-export-data-format-specifications.pdf` | HDI "Poprecord Entity Types" data dictionary (196 pp., 246 entity types) |
| [SP-ZIP] | `single-patient-ehi-export-data-format-specifications.zip` | Contains `EHI MYSQL DATA MODEL 2026101.zip` (HTML data-model reports, title "Millennium Data Model Reports - 2026.1.01") and `Longitudinal Plan EHI Export - Single Patient.pdf` |
| [POP-ZIP] | `patient-population-ehi-export-data-format-specifications.zip` | Contains `EHI MYSQL DATA MODEL 2025401.zip`, `EHI ORACLE DATA MODEL 2025401.zip`, `Longitudinal Plan EHI Export - Patient Population.pdf`, plus Multimedia-Storage DICOM/non-DICOM definitions, the Document Imaging content-management schema PDF, and `Oracle Health Document Imaging AxAnnotations.xsd` |

`docs/bulk2.pdf` is a **byte-identical duplicate** of [HDI-OVW]
(md5 `b8ad38b7ed71b98aa95bdb173e1c470b` for both); it is not cited further.

Zip-internal citations are written
`[SP-ZIP → EHI MYSQL DATA MODEL 2026101.zip → html/<file>]`.

## 2. Three distinct export systems

Oracle Health publishes EHI-export documentation for **three** separate
systems; the first two share one data model, the third does not:

1. **Millennium single-patient export** — extracts all data for one patient
   from a Millennium system into one or more `.zip` files: SQL dumps of the
   core EHR database plus ancillary stores (multimedia, DICOM, document
   imaging, Longitudinal Plan) [SP-OVW p.1].
2. **Millennium patient-population export** — extracts all data for all
   patients. Multi-tenant systems get SQL files to load into an **Oracle**
   database; single-tenant systems get a *copy of the full Millennium EHR
   database* on a customer-supplied secure encrypted device, shipped
   physically [POP-OVW pp.1–2].
3. **Health Data Intelligence (HDI)** — a different platform ("Longitudinal
   Record" on HDI). Export is requested via REST APIs and delivered as JSON
   inside a `.tar.gz`; no SQL at all [HDI-OVW pp.1–3]. Section 6.

## 3. Millennium core data model ("V500")

### 3.1 Shape and size

- The data model is documented as per-table HTML reports inside the inner
  `EHI * DATA MODEL *.zip` of each spec package; entry point
  `start_cerner_millennium_data_model_reports.html` [SP-OVW p.4].
- Counting the table reports (method: parsed every `html/dms_*.html` table
  page): the 2026.1.01 single-patient MySQL model documents **6,606 tables**
  (3,051 ACTIVITY + 3,555 REFERENCE); the 2025.4.01 population models
  (MySQL and Oracle variants) each document **6,590 tables** (3,037 ACTIVITY
  + 3,553 REFERENCE). The two population variants list the *same* table set;
  the newer single-patient model adds 16 tables (e.g. `GROUP_ORDER_RELTN`,
  `OBSERVATION_EXT_IDENT`, several `LH_E_*_2026_METRICS`)
  [SP-ZIP → EHI MYSQL DATA MODEL 2026101.zip → html/*;
  POP-ZIP → EHI MYSQL/ORACLE DATA MODEL 2025401.zip → html/*].
- Every table is classified **ACTIVITY** (patient/event rows, exported under
  `../v500/activity`) or **REFERENCE** (dictionary rows, exported under
  `../v500/reference`) [SP-OVW pp.1–2; table reports].
- MySQL vs Oracle model variants are the same logical schema with dialect
  type mapping, e.g. `CE_BLOB.BLOB_CONTENTS` is `LONGBLOB` (MySQL) vs
  `LONG RAW` (Oracle); numeric ids are `DOUBLE` vs `NUMBER`
  [POP-ZIP → EHI MYSQL DATA MODEL 2025401.zip and
  EHI ORACLE DATA MODEL 2025401.zip → html/dms_clinical_events1.html].

### 3.2 Cross-cutting conventions (from the table reports)

All citations in this subsection:
[SP-ZIP → EHI MYSQL DATA MODEL 2026101.zip → html/<named file>].

- **Coded values:** almost every `*_CD` column is a numeric key into
  `CODE_VALUE` (`dms_code_sets2.html`, REFERENCE, 27 cols): key columns
  `CODE_VALUE` + `CODE_SET`, with `DISPLAY` VARCHAR(40), `DESCRIPTION`
  VARCHAR(60), `DEFINITION` VARCHAR(100), and `CDF_MEANING` VARCHAR(12)
  ("the actual string value for the cdf meaning") — the spec's queries
  resolve storage locations via `code_set = 25` + `CDF_MEANING` (§4, §5).
- **Audit columns on every table:** `UPDT_DT_TM`, `UPDT_ID`, `UPDT_TASK`,
  `UPDT_APPLCTX`, `UPDT_CNT` (optimistic-locking counter).
- **Versioned rows:** activity tables carry `VALID_FROM_DT_TM` /
  `VALID_UNTIL_DT_TM`; "Current version of the result has an open 'Until
  Dt Tm' value" (`dms_clinical_events1.html`, CE_BLOB/CE_BLOB_RESULT column
  notes). Adapters must filter to current rows, not assume one row per fact.
- **Key spine tables:**
  - `PERSON` (`dms_person3.html`, 89 cols) — "may or may not represent a
    person who is a patient and/or personnel"; `PERSON_ID` PK,
    `NAME_FULL_FORMATTED` VARCHAR(100), `BIRTH_DT_TM`, `SEX_CD`,
    `DECEASED_DT_TM`.
  - `ENCOUNTER` (`dms_encounter17.html`, 159 cols) — `ENCNTR_ID` PK,
    `PERSON_ID` FK, `ENCNTR_TYPE_CD`, `REG_DT_TM`, `DISCH_DT_TM`,
    `REASON_FOR_VISIT` VARCHAR(255) free text.
  - `CLINICAL_EVENT` (`dms_clinical_events10.html`, 77 cols) — the clinical
    spine: "Stores patient related clinical information exclusively for
    clinical decision making… part of the official medical record."
    `EVENT_ID` ("uniquely identifies a logical clinical event row. There may
    be more than one row with the [same id]"), `PERSON_ID`, `ENCNTR_ID`,
    `EVENT_CD` (basic unit, "i.e. RBC, discharge summary, image"),
    `EVENT_CLASS_CD` ("specifies how the event is stored in and retrieved
    from the event table's sub-tables"), `EVENT_TITLE_TEXT` VARCHAR(255)
    ("the title for document results"), `PARENT_EVENT_ID` + `EVENT_RELTN_CD`
    (parent/child/orphan grouping), `RESULT_VAL` VARCHAR(255),
    `RESULT_STATUS_CD` (authenticated/modified/…), `EVENT_END_DT_TM`
    ("should always be used as the [clinically relevant] date/time"),
    `SERIES_REF_NBR` (blob series context).
  - `ORDERS` (`dms_orders3.html`, 117 cols, ACTIVITY).

## 4. Where notes and documents live (Millennium)

The schema has **no plain-text "note" column on the clinical spine**;
document text and images hang off `CLINICAL_EVENT` and `BLOB_REFERENCE`
through four mechanisms:

1. **Locally stored document text — `CE_BLOB`**
   (`dms_clinical_events1.html`, 12 cols): "contains the actual contents of
   a locally stored document." `BLOB_CONTENTS` LONGBLOB ("Text of the
   blob"), `COMPRESSION_CD` ("Specifies type of compression applied to the
   blob"), `EVENT_ID` FK to the event table, `BLOB_SEQ_NUM` (multi-blob
   documents). I.e. note text exists in the SQL dump itself, but as a
   possibly-compressed blob keyed to a clinical event — not as a varchar
   [SP-ZIP → EHI MYSQL DATA MODEL 2026101.zip → html/dms_clinical_events1.html].
2. **Remotely stored documents — `CE_BLOB_RESULT`** (same report, 18 cols):
   one row per physical document; "Blob results having a common {event_id,
   contributor_system_cd, series_ref_nbr} constitute a logical document."
   `BLOB_HANDLE` VARCHAR(2000) ("Handle to remote blob"), `FORMAT_CD`
   (blob type), `STORAGE_CD` ("location/device where blob is stored —
   Blob Table, Dictation System, Image Server, HSM, etc."). The export
   resolves `STORAGE_CD` against `code_value` rows in **code set 25**:
   `CDF_MEANING = 'DICOM_SIUID'` → the handle is a DICOM study UID;
   `CDF_MEANING = 'OTG'` → the handle is a Document Imaging document id
   [SP-OVW pp.2–3; POP-OVW p.3].
3. **Image/attachment rows — `BLOB_REFERENCE`**
   (`dms_imaging_document1.html`, 25 cols): one row per image; `BLOB_HANDLE`
   VARCHAR(255), `BLOB_TITLE`, `BLOB_TYPE_CD` (code set 26820),
   `PARENT_ENTITY_NAME`/`PARENT_ENTITY_ID` generic parent link,
   `CHARTABLE_NOTE_ID` and `NON_CHARTABLE_NOTE_ID` — each "the id of the
   long_text table row" holding the chartable / non-chartable note for the
   row [SP-ZIP → EHI MYSQL DATA MODEL 2026101.zip → html/dms_imaging_document1.html].
4. **Long free text — `LONG_TEXT`** (14 cols): `LONG_TEXT` LONGTEXT column
   plus generic `PARENT_ENTITY_NAME`/`PARENT_ENTITY_ID` back-pointer —
   the target of `BLOB_REFERENCE`'s note ids
   [SP-ZIP → EHI MYSQL DATA MODEL 2026101.zip → html/dms_data_mgmt25.html].

**Document Imaging (EDM/"OTG") internals** — for documents whose
`BLOB_HANDLE` resolves to Document Imaging, [POP-OVW pp.3–8, Appendix E]
documents the separate content-management schema: `BLOB_HANDLE` ≈
`<BLOB_UID>#<version>` (≈40-char string; `#2.00` → version 2; absent →
version 1); applications in `AE_APPS`, per-application `AE_DT#` (document
index; `BLOB_UID` column located via `AE_ADEFS`; **index fields like
patient name/MRN are not maintained — "Millennium Platform is the only
source of truth for all index fields"**), `AE_DL#` (pages/objects),
`AE_RH#` (revisions, `REVNUM = version × 65536`), `AE_PATHS` (storage
paths). Page binaries are `.bin` files in their native format, identified
by signature bytes (TIFF/JPEG/PDF); compressed-text binaries start
`COM1.0` and are extracted to `.txt`; "foreign" (e.g. MS Office) binaries
start `FFL1.0` and become `.FOREIGN` files. Annotations are converted to
XML per `Oracle Health Document Imaging AxAnnotations.xsd` [POP-ZIP];
on-demand OCR text rides in `.tx` files ("very few pages will be OCR'd";
Oracle suggests consumers re-OCR) [POP-OVW pp.4–8]. Pending/in-process
documents also appear on `CDI_PENDING_DOCUMENT` (its `BLOB_HANDLE` is the
external system's document GUID), and Millennium's content-management
applications are listed on `CDI_CM_FOLDER` [POP-OVW pp.3–4;
SP-ZIP → EHI MYSQL DATA MODEL 2026101.zip →
html/dms_imaging_document9.html (CDI_PENDING_DOCUMENT),
html/dms_imaging_document4.html (CDI_CM_FOLDER)].

**Multimedia (CAMM) linkage:** exported media files are named by
`media_object_identifier` from `DMS_MEDIA_IDENTIFIER`; `DMS_MEDIA_XREF`
links each media id to its parent row via `PARENT_ENTITY_NAME` +
`PARENT_ENTITY_ID` (e.g. `ENCOUNTER`/`encounter_id`) [SP-OVW p.2;
POP-OVW Appendix A pp.9–10].

## 5. Export packaging (confirmed/refined by the overview PDFs)

### 5.1 Single-patient (Millennium) — [SP-OVW pp.1–4]

One or more `.zip` files; directory layout relative to the base extract dir:

| Dir | Contents | File naming |
|---|---|---|
| `v500/schema` | MySQL DDL | `V500TableSchema*.sql`, `V500PrimaryKeySchema*.sql`, `V500IndexSchema*.sql`, `V500ForeignKeySchema*.sql` |
| `v500/activity` | INSERT dumps, ACTIVITY tables | `<table_name><_##>.sql` (1–N files per table) |
| `v500/reference` | INSERT dumps, REFERENCE tables | `<table_name><_##>.sql` |
| `camm` | Multimedia Storage files in original stored format | `<media_identifier_id>.<ext>`; undeterminable type → `.unknown` |
| `dicom/<study_uid>` | DICOM instances | `<sop_instance_uid>.dcm` |
| `edm/<document_id>` | Document Imaging page files | `<page_number>.<ext>` |
| `longplan` | One Plan / Longitudinal Plan, **five `.ndjson` files** (health concerns, goals, activities, strengths, care plans) | `<output_id>.ndjson` |

Prescribed load procedure: MySQL, database charset **latin1**, max packet
1 GiB, FK checks disabled; load order TableSchema → PrimaryKeySchema →
IndexSchema → ForeignKeySchema → activity+reference data [SP-OVW p.4].
Multimedia is delivered "in the originating format provided" (audio, text,
reports, photos, PDF…) [SP-OVW p.3].

### 5.2 Patient-population (Millennium) — [POP-OVW pp.1–12]

- **Multi-tenant** (CommunityWorks/ASP): same `v500` SQL-file scheme but
  targeted at an **Oracle** database (`set define off`; duplicate-row and
  'object already exists' errors expected and ignorable). Base directory is
  `<storage_location>/<request_id>`, one `<request_id>` per batch of
  **typically 100–1000 persons**; only one request dir (typically lowest
  numbered) carries the schema files; activity/reference files come from
  *every* request dir [POP-OVW pp.1–2, 8–12].
- **Single-tenant**: full Millennium EHR database copy on a
  customer-supplied secure encrypted device, restored with Oracle
  backup/restore tooling — there are **no per-table dump files** in this
  path [POP-OVW p.2].
- Multimedia: DICOM as `tar.gz` of per-patient folders of `.xml`/`.json`
  study + person metadata files named by DICOM study instance UID
  (Appendix C defines field↔DICOM-tag mapping); non-DICOM as `tar.gz` of
  `.csv` manifests (columns: `ObjectIdentifier`, `CompressionCode` — XZ,
  GZIP or BZIP2 — `GroupIdentifier`, `MimeTypeCode`, `OrigStoredTimeStamp`,
  `S3Path`, `SourceName`, `TransformationCode`), with files fetched via
  secure URL / S3 object keys [POP-OVW pp.2–3, 20, Appendix D].
- Missing-pointer sentinel: ancillary files that no longer exist in the
  archive export as `.missing` files containing the V500 identifier and
  root-cause message per line [POP-OVW Appendix A pp.9–11].
- Longitudinal Plan (One Plan): delivered as **`.CSV` files** (format per
  the in-zip `Longitudinal Plan EHI Export – Patient Population.pdf`),
  downloadable via the public HealtheIntent Data Syndication APIs or sent
  to customer AWS storage [POP-OVW p.8] — note the single-patient
  counterpart uses `.ndjson` [SP-OVW p.3].
- Expect **extraneous data**: content-management DB/filesystem will contain
  data not referenced by Millennium (e.g. deleted-after-validation
  documents) [POP-OVW pp.7–8].

## 6. Health Data Intelligence (HDI) — the third export system

### 6.1 What it is and how it's delivered — [HDI-OVW pp.1–3]

- Exports a single patient's or a population's EHI from an ONC-certified
  **HDI system's Longitudinal Record** as **one or more JSON files**
  [HDI-OVW p.1].
- Request via the **Longitudinal Record Bulk Extract API**
  (`https://{tenant}.api.{region}.healtheintent.com/longitudinal-record/...`,
  resource `/populations/{population}/bulk-extracts`): POST with no body =
  population extract; POST with a body holding a filter with a single
  `patientId` valued with the target patient's `empiId` = patient extract;
  plus cancel/list/status endpoints [HDI-OVW p.2].
- Download via the **Data Syndication API** (`/deliveries/{delivery}`,
  `/downloads/{delivery}`): a DELIVERED delivery downloads as binary bytes
  assembling into a **standard `.tar.gz` containing the Longitudinal
  Records**; download available for **21 days** [HDI-OVW pp.1, 3].
- Auth: bearer token (preferred) or two-legged OAuth 1.0a; Oracle must
  pre-enable the Bulk Extract API and a Data Syndication feed/channel per
  tenant [HDI-OVW p.1].

### 6.2 The data model — [HDI-SPEC]

- The format spec is a dictionary of **246 "Poprecord Entity Types"**, each
  with an Avro-style `FullName` of the form
  `com.cerner.pophealth.program.models.avro.poprecord.<Type>` and a
  field/type/description table (nullability expressed as `nullable <T>`;
  enums listed as `Symbols`, typically including `_NOT_VALUED` /
  `_NOT_RECOGNIZED`) [HDI-SPEC pp.1–196]. **Not tables** — nested
  records/arrays, i.e. document-shaped JSON, no join keys to reconstruct.
- Root entity: **`PopulationRecord`** — one record per person, keyed by
  `empiId`, with arrayed collections for the entire chart: `demographics`,
  `conditions`, `procedures`, `results`, `encounters`, `medications`,
  `claims`, `immunizations`, `allergies`, `carePlans`, `questionnaires`,
  `documentReferences`, `radiologyDocumentReferences`, `appointments`,
  `communications`, `referralRequests`, `consents`,
  `medicationAdministrations`, `medicationDispensations`, `careTeams`,
  `serviceRequests`, `deviceRequests`, `adverseEvents`,
  `diagnosticReports`, `summaryPersonRecords`, etc. [HDI-SPEC pp.128–130].
  Several fields are explicitly DEPRECATED in place "for passivity reasons"
  (e.g. `derivedEncounters`, `riskScores`) — consumers must tolerate them.
- ISO-8601 strings for timestamps throughout; coded values use `Code` /
  `CodeableConcept` / `RawCode` entities; record lineage via `RecordId`,
  `Provenance`, `Source`, and `processingVersion`
  (format `yyyy/MM/dd/HHmm`) fields on most entities [HDI-SPEC, entity
  tables passim].

### 6.3 How notes appear in HDI

- **`DocumentReference`**: "It provides most important information related
  to the document **but not content of the document**." Fields include
  `documentId`, `uid`, `personId`, `encounterId`, `type` (admission,
  discharge summary…, "most likely defined by LOINC or HL7"), `docStatus`,
  `authors`/`authenticators`/`reviewers` (arrays of `DocumentAction`),
  `serviceDate`, `description`, `securityLabels`, plus `content`
  (nullable `Attachment`) and `contents` (array of `Content`)
  [HDI-SPEC pp.65–67].
- **`Attachment`**: carries `mimeType`, `title`, `byteSize`,
  `creationDateTime`, and *references* to the body — `uri` (format
  `/partitions:{partitionId}/persons:{personId}/documents:{documentId}/`)
  and `binaryId`/`binaryUid` ("uniquely identifies binary content **in the
  binary store**") [HDI-SPEC pp.14–15]. `Content` wraps an `Attachment`
  plus a `format` code [HDI-SPEC p.52]. So in the HDI JSON, note **bodies
  are pointers into a binary store, not inline text**.
- Specialized variants: `RadiologyDocumentReference` (radiology reports)
  [HDI-SPEC pp.150–151] and `DiagnosticDocumentReference` (reports attached
  to a `DiagnosticServiceRequest`, `content: Attachment`)
  [HDI-SPEC pp.58–59].
- Inline free text *does* exist in small forms: `Comment.text` (required
  string) [HDI-SPEC pp.41–42] and result values via `TextValue.value`
  ("the textual result value" when `ValueType = TEXT`) [HDI-SPEC p.192].
- `SummaryPersonRecord` represents a snapshot document (e.g. **a CCD**) as
  *structured* collections (demographics/allergies/conditions/…), not as
  an XML payload [HDI-SPEC pp.189–190].

## 7. Adapter implications

The question "does `oracle_ehi` need to handle two or three input shapes?"
resolves to: **three shapes exist; two belong in `oracle_ehi`; the third
(HDI) is a different adapter.**

1. **Millennium MySQL dump** (single-patient; also population per the older
   [SP-OVW]-era docs cited in `docs/EHR_FORMATS.md`): `v500/{schema,
   activity,reference}/*.sql`, MySQL dialect, latin1 [SP-OVW pp.1–4].
2. **Millennium Oracle dump** (population, multi-tenant): same logical
   schema and file scheme, Oracle dialect, batched `<request_id>` dirs
   [POP-OVW pp.1–2, 8–12]. — Shapes 1 and 2 are the *same data model*
   (§3.1: identical table sets per the 2025401 MySQL/Oracle model reports),
   so one `oracle_ehi` loader with a dialect-aware SQL ingester covers
   both. The single-tenant full-database-on-device path is operationally
   different but lands in the same schema once restored [POP-OVW p.2].
3. **HDI JSON** (`PopulationRecord` documents in a `.tar.gz`): different
   platform, different vocabulary, no SQL, note bodies referenced via
   `binaryId` rather than carried in `CE_BLOB`-style rows [HDI-OVW p.1, 3;
   HDI-SPEC pp.14–15, 65–67, 128–130]. This wants its own
   `sources/hdi_longrecord/`-style adapter; forcing it into `oracle_ehi`
   would conflate two unrelated data models behind one flag.

Concrete consequences for `oracle_ehi`:

- **Note extraction is a multi-source resolver, not a column read** (§4):
  CE_BLOB local blobs (decompress per `COMPRESSION_CD`), CE_BLOB_RESULT /
  BLOB_REFERENCE handles resolved through code set 25 (`OTG`,
  `DICOM_SIUID`) to `edm/`, `dicom/`, `camm/` files, and LONG_TEXT rows via
  `CHARTABLE_NOTE_ID`. Losslessness requires carrying the sidecar files,
  exactly like the Epic TSV+sidecar pattern in `docs/EHR_FORMATS.md`.
- **Filter to current row versions** via open `VALID_UNTIL_DT_TM` and
  respect `RESULT_STATUS_CD`/`RECORD_STATUS_CD` (§3.2).
- **Resolve every `*_CD` through CODE_VALUE** (reference data ships in the
  same dump), keying logic on `CODE_SET` + `CDF_MEANING`, not on display
  strings (§3.2).
- **Plan for scale and dialects**: ~6.6k documented tables, multi-file
  per-table dumps, population batches of 100–1000 persons per
  `<request_id>`, and tolerated duplicate-insert errors mean idempotent,
  resumable loading (§3.1, §5.2).
- For HDI (future adapter): treat `PopulationRecord` as the unit, tolerate
  DEPRECATED-but-present fields, and surface attachments as unresolved
  binary references unless/until the binary-store retrieval path is
  documented (§6.3, §8).

## 8. Could not determine from these docs

- The **code-set number/value list for `CE_BLOB.COMPRESSION_CD`** and the
  actual compression algorithm of locally stored CE_BLOB contents (the
  column says only "type of compression applied").
- Whether the **HDI `.tar.gz` includes the binary document bodies** that
  `Attachment.binaryId`/`uri` point at, or how the "binary store" is
  accessed — neither [HDI-OVW] nor [HDI-SPEC] documents a binary retrieval
  endpoint or file layout for it.
- The **JSON file naming/partitioning inside the HDI `.tar.gz`** (one file
  per person? per entity collection?) — [HDI-OVW p.3] says only "standard
  .tar.gz file containing the Longitudinal Records".
- The full **`ValueType` enumeration context for `TextValue`** (the
  `ResultValue`/`ResultValueType` wiring is documented, but per-type
  rendering rules are not).
- The Longitudinal Plan JSON/CSV column schemas themselves (the in-zip
  Longitudinal Plan PDFs document them; not yet distilled here — the
  single-patient overview's per-file links are hyperlinks without URLs in
  the PDF text layer [SP-OVW p.3]).
- Anything about **rendered note layout** — consistent with the
  survey-wide finding in `docs/EHR_FORMATS.md`, no layout/pixel facts are
  published in any of these files.
