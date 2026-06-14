# Synthetic C-CDA R2.1 Continuity of Care Document fixture

**Every byte here is synthetic.** No real export, patient, provider, or
practice data was used. Repo conventions apply throughout: `feedface-` GUIDs,
the 555 exchange (`tel:+1(206)555-0177`), a never-issued SSN area
(`901-65-4329`, area ≥ 900), `example.com` email, and a fictional patient
("Cora Specimen").

## Provenance

`feedface_ccd.xml` is a single hand-built C-CDA R2.1 / CCD document. Its
structure — namespaces (`urn:hl7-org:v3`, `sdtc`), the US Realm Header plus
CCD `templateId`s, section/entry template OIDs, code systems, and the
clinical-statement nesting — follows the public HL7 C-CDA R2.1 example
material, reproduced from a clean read of the specification (no real document
was copied):

* HL7 CDA / C-CDA R2.1 example XML — <https://github.com/HL7/CDA-ccda-2.1>
* HL7 C-CDA worked examples — <https://github.com/HL7/C-CDA-Examples>
* C-CDA search / template browser — <https://cdasearch.hl7.org>

All identifiers, names, codes-in-context, dates, and narrative prose are
fabricated for this project. SNOMED / RxNorm / LOINC / CVX / ICD-10-CM codes
are real published codes used here only as realistic-looking placeholders on
synthetic data.

## Patient

Cora Specimen, female, DOB 1979-04-06, race Asian, ethnicity Not Hispanic or
Latino, language `en`, 456 Sample Way / Springfield / WA / 98102. Identified
by an SSN (`901-65-4329`, OID `2.16.840.1.113883.4.1`) and a document-source
id (`feedface-0000-0000-0000-00000000cda1`).

## Sections and what each trap exercises

| Section (LOINC) | Trap exercised by the adapter test |
| --- | --- |
| Problems (11450-4) | SNOMED→`snomed` + ICD-10 `translation`→`icd10`; active concern; a resolved problem (`effectiveTime/high`) flips `active` and sets `stopped` |
| Allergies (48765-2) | `value@code` → category (drug / food); RxNorm vs SNOMED allergen code → `extensions["ccda:allergen_code"]`; MFST reactions list; nested severity observation |
| Medications (10160-0) | active vs completed `statusCode`; `IVL_TS` low/high with `high nullFlavor="UNK"` → `stop=None`; RxNorm `@code`/`@displayName`; `doseQuantity`/`routeCode` → extensions |
| Immunizations (11369-6) | CVX vaccine given with `lotNumberText`; `negationInd="true"` refusal → `comment="Refused"` + `extensions["ccda:negationInd"]` |
| Vital Signs (8716-3) | organizer `effectiveTime` with a UTC offset (`-0500`) → tz-aware `effective_at`; BP/HR/weight/height `PQ` value+unit |
| Results (30954-2) | BATTERY organizer → laboratory `Observation`s (glucose, creatinine) with value+unit |
| Social History (29762-2) | smoking status `72166-2` → social-history `Observation` "Tobacco use" / "Never smoker" |
| Encounters (46240-8) | encounter → `Encounter` with `date_of_service` and `encounter_type` from `code@displayName` |
| Notes (34109-9, ext 2016-11-01) | Note Activity → `Encounter` carrying a NARRATIVE `NoteSection` (the note prose) + author/time date |
| Plan of Treatment (18776-5) | **the adapter does NOT parse this section** — its narrative is captured verbatim into `patient.extensions["ccda:section:18776-5"]`, proving the losslessness rule for unmapped sections |

Document-level metadata (`ccda:documentId`, `ccda:effectiveTime`,
`ccda:title`) is also stored in `patient.extensions`.

## Conventions honored

* All GUID-shaped ids start with `feedface-` (PHI scanner requirement).
* `nullFlavor` on an element means "absent" to the adapter (the
  `high nullFlavor="UNK"` on the active medication leaves `stop=None`).
* The document is intentionally minimal but valid: realmCode, typeId, the two
  CCD `templateId`s, `author`, and `custodian` are all present so the file is
  a well-formed US Realm CCD, not a fragment.
