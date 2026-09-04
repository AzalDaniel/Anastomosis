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
| `feedface_ccd_idless_result_component.xml` | A `30954-2` Results organizer carrying a real `<id root extension>`, whose one component `<observation>` carries only `<id nullFlavor="NI"/>` — no id of its own at all. `feedface_ccd.xml`'s Results section gives every component its own id, so it never reaches this. Export -> ingest used to state the resulting lab fact twice, forever. See #378. |
| `feedface_ccd_organizer_extension_whitespace.xml` | Same shape as `feedface_ccd_idless_result_component.xml`, but the organizer's `<id extension>` is padded (`"  feedface-idls-panel-0001  "`). Before both halves read an id through `core.ccda_codes.first_rooted_id`, the parser stripped it and the builder did not, so the two sides derived different ids and the fact duplicated without bound. See #378 (round two). |
| `feedface_ccd_organizer_root_whitespace.xml` | Same shape, padded `<id root>` instead of `extension`. Same divergence, same unbounded growth before the fix. See #378 (round two). |
| `feedface_ccd_component_id_nullflavor_then_rooted.xml` | The component's own `<id>` is `nullFlavor="NI"` FIRST and a rooted `<id root="feedface-comp-…"/>` SECOND. The parser used to read only a component's first `<id>` child and treat the component as id-less; the builder scanned every `<id>` child and treated it as owning the second, rooted one — so the parser derived an id the builder never stated. See #378 (round two). |
| `feedface_ccd_component_root_whitespace.xml` | The component's own `<id>` has a whitespace-only `root` (`"   "`). Read by truthiness (unstripped) that string is non-empty, so the old builder treated the component as owning a blank-ish root while the (stripping) parser correctly saw no id at all. See #378 (round two). |
| `feedface_ccd_two_idless_components.xml` | Two id-less `<observation>` components — Glucose and Sodium — under one Results organizer. Pins that `organizer_component_source_id`'s `index` tells them apart: two distinct derived ids, not one collision. See #378 (round two). |
| `feedface_ccd_nested_organizer.xml` | An `<organizer>` inside a `<component>` inside another `<organizer>` — both with their own id-less analyte. `_measurements` only ever reads an entry's DIRECT-child organizer, so only the outer analyte is structurally parsed (flat at 1 observation per generation); the inner organizer's own id-less component is exercised at the `_stated_ids`/`_derived_component_ids` unit level, which walks an entry's organizers at any depth. See #378 (round two). |
| `feedface_ccd_zero_date_sentinel.xml` | A `10160-0` medication whose `effectiveTime/low` is an all-zero run (`value="0"`), `high` `nullFlavor="UNK"` — a vendor's own spelling for "no start date" that used to make `parse_dt` raise and abort the whole document. Reads as absent now, and the loss is credited on `patient.extensions["ccda:timestamp_named_no_instant"]`. A `30954-2` result carrying a genuine numeric zero (`PQ value="0"`) sits in the same file as the control: it never routes through `parse_dt`, so the sentinel count must stay at 1. See #385. |
| `feedface_ccd_bare_encounter.xml` | A `46240-8` section whose one `<entry><encounter>` carries `<id root>` and nothing else — no `<code>`, no `<effectiveTime>` — and NO `34109-9` section anywhere in the document. An encounter with neither a type nor note content reached neither section before #388; this is that encounter, arriving from a real document rather than a hand-built record. See #388. |
