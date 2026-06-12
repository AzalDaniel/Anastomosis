# Synthetic Oracle Health / Cerner Millennium EHI fixture (V500)

**Every byte here is synthetic.** No real export, patient, provider, or
practice data was used. Repo conventions apply throughout: synthetic Cerner-
style numeric ids in an obviously-fake range (`900000001`+), `Testpatient`-
style names, the `1/1/0001 12:00:00 AM` SQL min-date sentinel where the
adapter must drop it, and no real PHI of any kind. (There are no phone/SSN
columns in the tables this fixture covers; the 555 exchange / area-≥900 SSN
conventions would apply if there were.)

## Provenance

Table names, column names, V500 ACTIVITY/REFERENCE classification, the
`v500/{schema,activity,reference}` packaging, and the join/blob/code-resolution
semantics follow **`docs/vendor_refs/ORACLE_EHI_SCHEMA.md`** — this project's
distilled brief over Oracle's published single-patient Millennium EHI-export
specification material. Section citations appear inline in every `.sql` file
and in the adapter source. This is a tiny useful subset: 8 tables, 2 patients,
3 encounters.

### VERIFIED vs SYNTHETIC-because-undocumented

Column spellings are grounded in the brief **except** where the brief does not
enumerate them; those are this fixture's synthetic placeholders and the adapter
handles them losslessly rather than asserting meaning:

* **`PERSON_ALIAS` columns** (`PERSON_ALIAS_ID`, `ALIAS`,
  `PERSON_ALIAS_TYPE_CD`) — the brief (§3.2) names the table for the MRN but
  enumerates only PERSON/ENCOUNTER columns. The adapter never claims a column
  is "the MRN"; every alias value becomes an `OTHER` identifier whose `system`
  records its source column, and the whole row also rides to the patient's
  `extensions`. **Reported gap.**
* **`CE_BLOB.COMPRESSION_CD` code set / algorithm** — listed as could-not-
  determine in the brief (§8). Row `EVENT_ID 900300001 / BLOB_SEQ_NUM 3`
  carries a non-NULL `COMPRESSION_CD` so the decode path raises a loud
  `NotImplementedError` (caught, logged PHI-safely, preserved in extensions).
* Audit columns (`UPDT_*`, §3.2) and any column the mapper does not consume —
  routed verbatim to `extensions` under `oracle_ehi:` keys.

Verified-grounded facts this fixture honors: the spine is `PERSON` +
`ENCOUNTER` + `CLINICAL_EVENT` (§3.2); there is **no plain-text note column** —
document text lives in `CE_BLOB` and remote handles in `CE_BLOB_RESULT` (§4);
`*_CD` columns resolve through `CODE_VALUE` keyed on `CODE_SET` + `CDF_MEANING`
(§3.2); blob `STORAGE_CD` resolves in code set 25 (`DICOM_SIUID` => DICOM study
UID) (§4.2); clinical-event rows are versioned and the current version has an
open `VALID_UNTIL_DT_TM` (§3.2).

## Traps deliberately baked in (what the adapter tests assert)

| Trap | Where |
| --- | --- |
| Multi-file-per-table dump (§5.1) | `ENCOUNTER_01.sql` + `ENCOUNTER_02.sql` |
| INSERT names its own reordered column list | `ENCOUNTER_02.sql` |
| Multi-row INSERT | `PERSON.sql`, `CLINICAL_EVENT.sql` |
| `''` and `\n` string escapes | `CE_BLOB.sql` note body |
| SQL `NULL` cell | `PERSON.sql` patient 2 name, encounter `DISCH_DT_TM` |
| Multi-blob document concatenation (§4.1) | `CE_BLOB.sql` seq 1 + 2 on event 900300001 |
| Compressed blob -> loud-but-caught (§8) | `CE_BLOB.sql` seq 3, `COMPRESSION_CD` set |
| Remote document reference, not fetched (§4.2) | `CE_BLOB_RESULT.sql` DICOM handle |
| `STORAGE_CD` resolved via code set 25 (§4.2) | `CODE_VALUE.sql` 2501 = DICOM_SIUID |
| Current-version filter on open `VALID_UNTIL_DT_TM` (§3.2) | `CLINICAL_EVENT.sql` event 900300010 superseded |
| `1/1/0001 12:00:00 AM` date sentinel -> None | `CLINICAL_EVENT.sql` event 900300010 `EVENT_END_DT_TM` |
| `*_CD` resolved through `CODE_VALUE` (§3.2) | `SEX_CD`, `ENCNTR_TYPE_CD` |
| Deceased patient | `PERSON.sql` patient 900000002 |
| PERSON_ALIAS lossless (undocumented columns) | `PERSON_ALIAS.sql` |
| Unmapped columns -> extensions | audit `UPDT_*` columns everywhere |
