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

Column spellings are verified against the v9 dictionary **except** the
following, where the public dictionary doesn't enumerate the columns and the
spellings here are this project's best inference (the adapter reads these
tolerantly — corrections from a real export only require fixing this fixture):

* `providers.tsv`, `facilities.tsv` — name/address column spellings
* `patient-guarantor.tsv`, `patient-education.tsv`,
  `patient-financial-resources.tsv` — all columns
* `patient-allergy-reactions.tsv` — link/`Reaction` columns
* `patient-immunizations.tsv` — `DateAdministered`/`ExpirationDate`/`Comment`
* `patient-family-history-diagnoses.tsv` — `Diagnosis` display column
* `DiagnosisCodeEquivalents` *format* (`SYSTEM:code|SYSTEM:code`) — the column
  is verified, its serialization is not publicly documented
* Null literal (`\N`), date spellings, and the `1/1/0001 12:00:00 AM`
  sentinel — **deliberately mixed** here (also empty cells and ISO dates)
  because the real serialization is publicly undocumented; the adapter must
  tolerate all of them anyway.

Verified-absent facts this fixture honors: there is **no MRN/PRN column**
anywhere in v9 (identity is `PatientPracticeGuid`); there is **no dedicated
vitals table** (vitals are LOINC-coded rows in
`patient-encounter-observations.tsv`); SOAP narrative lives directly on
`patient-encounters.tsv` (`Subjective`/`Objective`/`Assessment`/`Plan`).

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
| PlanType fallback chain | "(PPO)" parseable / not parseable / Medicare |
| Addendum on a signed note | Encounter 3, `AmendmentStatus=Accepted` |
| SIMPLE (non-SOAP) note | Encounter 4, `IsSoapNote=false`, only Subjective filled |
| Escript status resolution | Rx 1: Sent→Verified→Dispensed; Rx 2: Printed |
| Multi-race patient | Ada Fixture: White + Asian |
| Pediatric record | Cleo Placeholder (DOB 12/1/2021): head circumference, months-old age |
| Empty table with header | `tribal-affiliation.tsv` |
| Unmapped-column losslessness | e.g. `IsMultipleBirth`, `PreferredName` → `extensions` |
