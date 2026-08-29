# Reference data — what the vendor says, not what we assumed

Files here are the outside world's answer to a question this repository kept
answering for itself. They are not fixtures: nothing renders from them and no
test builds a record out of them. They exist to be **compared against**.

## `pf_v9_columns.json`

Every column name in every table of the Practice Fusion / Tebra **EHI v9**
export: 85 tables, 1,165 columns, names only.

### Why it is here

`tests/fixtures/pf_tebra_v9/` was written by hand, and the mapper was written
against the fixture. So the fixture and the code agreed with each other about
column names that the vendor's export does not have — 25 of them across 12
tables — and the whole suite passed against a schema that does not exist. The
first contact with a real export refused with `OrphanRowsError` and migrated
nothing (#247).

That is the failure mode where synthetic test data is *plausible* rather than
*real*: the tests confirm the code matches the fixture, and nothing checks that
the fixture matches the vendor. This file is the missing side of that loop, and
`tests/unit/test_v9_schema_reference.py` is the check that closes it.

### Provenance

Extracted from the v9 data dictionary published in
`jmandel/ehi-export-analysis` (`results/practice-fusion--practice-fusion-ehr/`),
which mirrors the vendor's own EHI export documentation. Field names only — the
data types and prose descriptions are left behind, because the guard needs the
names and nothing else needs the bulk.

### Why it carries no PHI

A column name is the vendor's published documentation, identical for every
practice that ever ran the export. Not one byte here came from anyone's chart:
no rows, no values, no filenames, no paths. `PatientPracticeGuid` is the name of
a column, not anybody's identifier.

### Keeping it current

If the vendor publishes a v10, add it as a sibling file rather than editing this
one. A schema is a statement about a point in time, and an export taken under v9
stays a v9 export after the vendor moves on.
