# C-CDA edge cases

Documents that are *valid* C-CDA but shaped in a way that broke something.
Kept out of `tests/fixtures/ccda/` because the adapter loads a directory by
globbing `*.xml`, so anything living beside `feedface_ccd.xml` joins the
ordinary corpus and changes what every test there sees.

Every byte is synthetic, from the same `feedface-` GUID range as the rest of
the fixtures.

| file | what it exercises |
| --- | --- |
| `feedface_ccd_duplicate_encounter_id.xml` | Two `<encounter>` entries under one `<id root>`. The parser keeps a GUID-shaped root verbatim, so this arrives as two `Encounter` objects — different dates, different CPT codes — carrying one id. Delivery used to write one page and report two. See #121. |
