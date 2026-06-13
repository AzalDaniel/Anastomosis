# Synthetic US Core R4 fixture

`uscore_bundle.json`

- **Origin:** hand-authored for this repository. It is **not** an export from any
  real system and contains no real patient data. Every identifier is a
  `feedface-`-prefixed synthetic GUID (the repo's fixture convention), the SSN
  uses the never-issued `900` area, phone numbers use the `555` exchange, and
  the names follow the synthetic vocabulary used elsewhere in `tests/fixtures/`
  (`Specimen`, `Placeholder`).
- **Shape:** a FHIR R4 `Bundle` (`type: collection`) carrying **US Core R4**
  profile-shaped resources — the structure a certified EHR's
  `Patient/$everything` or Bulk-Data (`$export`) produces. It deliberately uses
  *standard* US Core codings (LOINC, ICD-10-CM, SNOMED CT, RxNorm, CVX) and US
  Core extensions (race/ethnicity), **not** this project's own
  `urn:anastomosis:*` export extensions — so it exercises the
  `sources.fhir_r4` adapter's real-world mapping, not the round-trip inverse in
  `core.fhir.ingest` (`from_bundle`).
- **Coverage:** two patients. Patient 1 (`…0001`, Dexter Q. Specimen Jr.) carries
  two encounters, vitals (height/weight/BP-with-components/glucose lab), a
  social-history smoking observation, two problems (ICD-10 + SNOMED), a
  medication (RxNorm), a drug allergy, an immunization (CVX), insurance
  coverage, a care goal, a family history, a procedure (which has no canonical
  home and must round-trip losslessly into `extensions`), and two clinical-note
  `DocumentReference`s whose narratives attach to their encounters. Patient 2
  (`…0002`, Wendell Placeholder) is minimal (one encounter, one problem, one
  vital) to exercise multi-patient grouping and deterministic ordering.

Why hand-authored rather than a Synthea download: the adapter's correctness
hinges on per-field mappings (which coding system lands in which canonical
field, how categories are resolved, what is preserved losslessly), so the
fixture is kept small and every value is asserted in `test_fhir_r4_source.py`.
The independently-generated Synthea sample lives next door in
`tests/fixtures/synthea/` (C-CDA) for the C-CDA parser's external-evidence lane.
