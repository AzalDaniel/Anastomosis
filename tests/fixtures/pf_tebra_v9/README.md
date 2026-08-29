# Synthetic Practice Fusion / Tebra EHI fixture (v9 schema)

**Every byte here is synthetic.** No real export, patient, provider, or
practice data was used. Repo conventions apply throughout: `feedface-` GUIDs,
555-exchange phones, never-issued SSN areas (≥900), `example.com` emails,
fictional names ("Fixture", "Sample", "Placeholder", "Providerson").

## Provenance

Table and column names follow Practice Fusion's **official public EHI export
data dictionary, v9 (2026-01-12)** — the §170.315(b)(10) documentation at
`practicefusion.com/ehi-export-documentation/v9/` (85 tables; verified
2026-06-11 via the complete scrape in
`github.com/jmandel/ehi-export-analysis`). This fixture reproduces a useful
subset: 29 tables, 3 patients, 6 encounters.

### VERIFIED vs INFERRED

**Every column name in this fixture is now checked against the vendor**, by
`tests/unit/test_v9_schema_reference.py` against the column list in
`tests/reference/pf_v9_columns.json`. Nothing here is spelled on inference any
more, and nothing can go back to being spelled on inference without the suite
saying so.

This section used to list nine exemptions, on the belief that the public
dictionary "doesn't enumerate the columns" for those tables. It enumerates all
85 of them, and the exemption was doing real damage: 25 invented column names
across 12 tables, self-consistent between fixture and mapper, green in every
test, and refused outright by the first real export (#247). The lesson worth
keeping is that one — an exemption from checking is where the drift lives, and
"the vendor doesn't say" is worth re-testing before it is believed.

What genuinely remains undocumented is **serialization, not names**:

* `DiagnosisCodeEquivalents` *format* (`SYSTEM:code|SYSTEM:code`) — the column
  is in the dictionary; how its value is packed is not.
* Null literal (`\N`), date spellings, and the `1/1/0001 12:00:00 AM`
  sentinel — **deliberately mixed** here (also empty cells and ISO dates)
  because the real serialization is publicly undocumented; the adapter must
  tolerate all of them anyway.
* Cell *values* throughout are synthetic, and always were. Only the names came
  from the vendor.

Verified-absent facts this fixture honors: patient identity is
`PatientPracticeGuid` and `patient-demographics` carries **no record-number
column**; there is **no dedicated
vitals table** (vitals are LOINC-coded rows in
`patient-encounter-observations.tsv`); SOAP narrative lives directly on
`patient-encounters.tsv` (`Subjective`/`Objective`/`Assessment`/`Plan`).

This ledger used to say there is no MRN/PRN column *anywhere* in v9. That is
wrong, and the PF pack's blank PRN header is the consequence: exactly one column
in the 85-table dictionary carries a patient's record number,
`patient-superbills.PatientContactCode` ("Code assigned to a patient's medical
record"). No column anywhere is spelled `PRN`. The pack reads
`PatientContactCode` and nothing else; wiring it means mapping
`patient-superbills`, which this fixture does not yet carry — a 70-column
identity-and-billing table that needs its own review, not a column added to a
table that already exists.

Social history (issue #7, **verified against a real Tebra/PF v9 export**):
free-prose history is the `patient-med-history.tsv` table
(`PatientPracticeGuid`, `HistoryType`, `ReportedHistory`) — `HistoryType` tags
each block social / family / major-events, and the adapter maps every block to
`PastMedicalHistory(kind, text)` (the PF pack renders the `social` block as the
social-history freetext). Smoking is `patient-smokingstatus.tsv`
(`TobaccoUseDescription`, date `EffectiveDate`→`RecordedDate` — the clinical
assessment date is preferred over the administrative entry date; the non-chosen
date is preserved in `extensions`). **Verified-absent:**
the structured social-history subcategories the predecessor UI showed as empty
placeholders — **alcohol use, drug/substance use, physical activity/exercise,
diet/nutrition, sexual activity, stress, social isolation, violence, pregnancy
status/intent, food insecurity** — have **no source table or column** in the
export (the predecessor emitted them as empty strings), so the adapter maps them
to nothing rather than inventing a column. Education, financial resources,
occupation/industry, and tribal affiliation DO have their own documented v9
tables (mapped via `_SOCIAL_TABLES`).

Education and financial resources are shaped as a questionnaire rather than a
single field — `Question`, `Answer`, `SnomedCode` — so the answer is the
observation's value and the question rides beside it in extensions. Neither
carries an assessment date, only a LastModified stamp, so both observations
come through undated: when a row was last edited is not when the patient was
asked, and dating the answer from the stamp would state something the export
does not.

## Traps deliberately baked in (what the adapter tests assert)

| Trap | Where |
| --- | --- |
| Same-day filename collision | Ada Fixture has two encounters on 5/10/2023 |
| BMI auto-calc trigger | Encounter 1 charts height+weight, **no** 39156-5 row |
| Explicit BMI must not be recomputed | Encounter 5 carries its own 39156-5 row |
| Unsigned-note sentinel | Encounter 6: `SignedDateTimeUtc = 1/1/0001 12:00:00 AM` |
| `\N` null escapes + empty cells | Boris Sample's demographics, encounter 6 chief complaint |
| `-1` numeric sentinel | `NumberOfRefills` on the printed prescription |
| Mixed date spellings | slash, 12-hour, and ISO forms across tables |
| PlanType superbill join | Medicare via PIPG tier-1 join; Evergreen Basic via plan-name tier-2 join; Cascadia "(PPO)" via regex last-resort (`superbill-insurances.tsv`) |
| Addendum on a signed note | Encounter 3, `AmendmentStatus=Accepted` |
| SIMPLE (non-SOAP) note | Encounter 4, `IsSoapNote=false`, only Subjective filled |
| Escript status resolution | Rx 1: Order sent→Refill approved→Dispensed = DISPENSED |
| Escript refill must NOT override VERIFIED | Rx 3: Order sent + Refill approved = VERIFIED (no dispense) |
| Empty-SOAP encounter excluded from render | Encounter 7 (Boris): all four sections blank — skipped, preserved in record `extensions` |
| Adult growth-chart encounter excluded | Encounter 8 (Boris, adult): CC "growth chart" — skipped, preserved in `extensions` |
| Multi-race patient | Ada Fixture: White + Asian |
| Pediatric record | Cleo Placeholder (DOB 12/1/2021): head circumference, months-old age |
| Empty table with header | `tribal-affiliation.tsv` |
| Unmapped-column losslessness | e.g. `IsMultipleBirth`, `PreferredName` → `extensions` |
