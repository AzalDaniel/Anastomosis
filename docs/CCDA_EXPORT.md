# C-CDA export — scope, and what is deliberately not attempted

Reference for `deliver/ccda_export/builder.py`. The module's own docstring
states the contract; this is the material a reader needs once, not on every
visit to the file.

## The contract

`parse(build_ccd(record)) ≈ record`, where `parse` is **this repository's own**
`sources/ccda/parser.py`. Every xpath, LOINC section code, template id and
`xsi:type` in the builder is chosen to match what that parser traverses. **The
parser is the spec, never the other way around.**

## Scope honesty

The output targets two things and only two:

* round-trip fidelity with anastomosis's own parser, proven section by section
  in the test suite, and
* the structural shape that parser consumes — a well-formed HL7 v3
  `ClinicalDocument` with the US-Realm header, the CCD template ids, and the
  entry shapes the parser traverses.

It does **not** target, and does not achieve:

| Not attempted | Why the document would fail it |
| --- | --- |
| HL7 CDA R2 **XSD schema validity** | omits mandatory header participations the parser ignores (`author`, `custodian`); uses non-OID `root` values for source identifiers (a vendor system name, or a `urn:anastomosis:id:*` MRN root) |
| C-CDA R2.1 **Schematron** conformance | `codeSystemName` coverage, value-set binding strength, narrative `<reference>` linkage |
| **ONC certification** | follows from the two above |

XSD-structural and Schematron conformance are a separate, later effort via
external validator tooling (`docs/PLAN.md` M6). Today the deliverable is the
own-parser round trip and nothing broader. **Do not represent this output as
schema-valid C-CDA to a destination that will validate it.**

## Encounters and Notes: a partition, not two filters

`_structured_encounters` and `_notes` decide together which section(s) one
encounter reaches, and between them every encounter must land somewhere. The
Notes section (`34109-9`) takes an encounter with note content; the
Encounters section (`46240-8`) takes every encounter the Notes section does
not already stand for — one with an `encounter_type`, or one with neither a
type nor note content. A visit with both a type and a note reaches both
sections at once (the parser reads them from different sections, so nothing
is duplicated); a typeless, noteless encounter — a real visit with nothing
charted about its kind, or an unattributed one the source names but never
describes — gets `<code nullFlavor="NI"/>` and, absent a date,
`<effectiveTime nullFlavor="NI"/>`: no information, not a fabricated OTH
value with nothing behind it.

`build_ccd` checks that this partition actually held, reading the emitted
tree the same way `measure_ccd` reads emitted bytes: every `record.encounters`
entry is classified by whether its id turns up under `46240-8`, `34109-9`, or
both, and `core.conservation.Conservation` raises if one lands in neither —
the loud failure an artifact-only check cannot produce, because an artifact
that never arrived leaves nothing to inspect.

## Declared losses — the no-silent-drop rule, made explicit

A canonical `PatientRecord` carries far more than standard CDA has structured
slots for, and the losslessness invariant forbids dropping any of it *silently*.
Two tiers:

### 1. The loss narrative (recoverable)

Every populated source field no structured emitter consumes is serialized as
deterministic `path = value` lines into a namespaced `<text>` block on a
dedicated extensions section (LOINC `51899-3`), one `<paragraph>` per entry.
That covers:

* native canonical fields with no CDA slot — `Encounter.chief_complaint`,
  `Patient.gender_identity`, `Immunization.expires`, …
* record-level lists the parser cannot produce — `prescriptions`, `coverages`,
  `family_history`, …
* every `extensions` key other than the four this format round-trips natively
  onto their models (`_NATIVE_EXT_KEYS`) — including the `ccda:section:*`
  narratives an earlier CDA ingest captured, which no emitter here re-derives.

The one family that is neither narrated nor lost is `ccda:entries:<code>`: the
`<entry>` elements a C-CDA ingest parked verbatim, because prose about a
section is not a copy of the entries beneath it. Those are **delivered** —
re-emitted as the entries of the section carrying that code, or of a carrier
section when this exporter emits none for it, so they leave as the entries they
arrived as and a re-ingest parks the same bytes. Narrating them instead would
serialise XML into `path = value` lines no emitter consumes, which the next
generation would park and narrate again: measured at ~15 KB per round trip,
without bound.

Delivering them changes what the structured emitters write. A parked entry is
the source's own statement of a clinical fact and the canonical object read out
of it states the same fact in this exporter's words, so emitting both would say
it twice — and a re-ingest would read two objects where the chart has one, four
the generation after. Each emitter therefore skips the object whose source id a
preserved entry carries (`_Preserved.own`) and emits the rest as usual. What the
section preserved leaves as the entry it arrived as; what it did not leaves as
this exporter's own entry; the section's human narrative still lists both.

The match `_Preserved.own` runs (`_stated_ids`) is an any-depth walk of a
preserved entry's `<id root>`s, and a component `<observation>` under a Results
or Vitals organizer can carry none of its own — a real vendor shape, an
organizer stamped and each analyte left `<id nullFlavor="NI"/>`. Pairing that
case only by shared absence (every id-less component matching every other) is
what let one such fact duplicate on every generation (#378). The fix gives it a
real id instead: `core.ccda_codes.organizer_component_source_id(root,
extension, index)`, derived from the organizer's own id and the component's
0-based position, document-intrinsic so it survives a rename between export and
re-ingest. `sources/ccda/parser.py::_measurements` computes the same id as
`source_id` on ingest when a component states none; `_stated_ids` adds the
identical id to what a preserved entry is taken to state. A component that
carries its own id is untouched either way — the derived id is purely additive,
never a substitute for one a component actually states.

Deriving the SAME id on both sides depends on both sides reading "what id does
this organizer/component state" identically, and hand-mirroring that reading
reopened the duplication for four shapes a whitespace-padded root or extension,
or a component's first `<id>` being `nullFlavor` with a second, rooted `<id>`
behind it — before it was worth its own name. `core.ccda_codes.first_rooted_id`
is that one reading: every `<id>` child in document order, `nullFlavor` skipped,
`root`/`extension` stripped, the first survivor wins. Both
`_measurements`'s organizer/component id and `_derived_component_ids`'s
organizer/component id call it, so the two sides agree on the pair by
construction — there is no second, independent reading left to drift.

The section is stamped with `LOSS_NARRATIVE_TEMPLATE_ROOT` so a later ingest can
tell this tool's loss ledger from a third party's 51899-3 section.
`sources/ccda` reads a stamped section back into
`patient.extensions["ccda:prior_loss_narrative"]` as discrete entries — so the
data is **visible in the document and recoverable from re-ingest**, as narrative
text, not back onto its original typed models.

What each structured emitter *does* consume is declared in `_EXPORTED_FIELDS`,
kept adjacent to each emitter so drift is caught in review. Everything outside
that allowlist flows to the narrative automatically — no per-field
whack-a-mole.

### Generations, and why the ledger stops growing

Export → ingest → export is a loop a migration legitimately runs more than
once, and the ledger must not grow without bound around it.

Re-ingesting a stamped section as one `ccda:section:51899-3` narrative made
generation N's ledger swallow generation N−1's whole text as a single line: an
ever-growing blob that drowned the real entries. The entries now come back
discrete and are re-emitted as a carry-forward appendix, deduplicated against
this generation's own by `_carried_forward` — identical entries collapse,
distinct ones survive at their multiplicity, and the document carries exactly
one 51899-3 section stamped with its generation number.

A chart ingested from C-CDA settles the same way: measured over three
generations of parse -> `build_ccd` on the three CDA fixtures, the 51899-3
section runs 8,400 -> 9,857 -> 9,857 bytes (`feedface_ccd.xml`),
11,191 -> 13,573 -> 13,573 (`synthea_ccda_sample.xml`) and
8,499 -> 9,991 -> 9,991 (`feedface_ccd_duplicate_encounter_id.xml`), still a
fixed point at generation five.

**The ledger is bounded: it stops growing once the record's own ids stop
moving, and never grows again.** Which generation that lands on depends on the
chart. A chart whose canonical ids survive re-ingest verbatim settles at
generation 2. A chart whose patient id the parser re-derives on first ingest
settles one generation later, because `document_id` defaults to a uuid5 over
the patient id, so the derived id shifts once alongside it and narrates one
extra entry. On the repository's richest test record the entry counts run
26 → 66 → 67 → 67 → 67.

The bound is the property worth having and the one the tests pin; the exact
generation is not a promise this format can make about an arbitrary chart.

### Documents are delivered, never inlined

A record's source documents — a scanned referral, a faxed discharge summary,
a lab report PDF — are the one thing the loss narrative cannot carry: a
narrated base64 blob would be re-ingested and re-narrated every generation,
and would put tens of megabytes of a scan inside a document a destination has
to accept whole.

So `deliver_ccda` writes them, and the CCD names them. Each delivered document
gets an `<observationMedia>` entry in the Notes section, stamped
`urn:anastomosis:ccda:artifact`, carrying:

| what | where | why |
| --- | --- | --- |
| the artifact's canonical id | `<id root>` | so a re-ingest restores the same identity, and the conservation ledger can attribute the entry (a root shared by two artifacts is attributable to neither) |
| the declared media type | the ED's `@mediaType` | as DECLARED; this tool never re-types somebody's scan |
| the SHA-256 | the ED's `@integrityCheck` (+ `@integrityCheckAlgorithm`) | HL7 v3's own slot for it, so a swapped or truncated file is caught by the document that names it |
| the delivered filename | the ED's `<reference value>` | resolved beside the document, exactly as a referenced `nonXMLBody` is |

**Why not a re-embedded `nonXMLBody`.** CDA R2 gives a `ClinicalDocument`
exactly one `<component>`, so a CCD carrying a `structuredBody` cannot also
carry a non-XML body; the C-CDA R2.1 Unstructured Document template
(`2.16.840.1.113883.10.20.22.1.10`) describes a WHOLE document, not an
attachment to one. Splitting each artifact into its own Unstructured Document
would also deliver several files per patient into a directory whose names are
one per record, and each would re-ingest as another record for the same
patient. `<observationMedia>` with an ED value is base CDA R2's own mechanism
for a non-XML artifact inside a structured body, and C-CDA's templates are
open, so an entry the section's own template does not name is permitted rather
than forbidden.

**Filenames.** A delivered document is named after its own pseudonymous
artifact id, never after the source's filename — a C-CDA export names its
attachments after the patient, and this directory is the one most likely to
travel. The source's own `documents[].path` is therefore NOT consumed by the
emitter: it narrates in the 51899-3 ledger like every other unmapped field.

**Loud where it cannot conserve.** A document the run resolved but that is not
in the directory the run put it in, one whose delivered bytes do not hash to
what the record witnesses, and inline bytes that will not decode each stop the
run before it reports success (`ArtifactNotDelivered`, exit 1). The reader
holds the same line: a delivered document that did not travel with its CCD, or
that was edited afterwards, refuses the re-ingest (`DeliveredArtifactError`).
An artifact the SOURCE never resolved — a document reference with no bytes
anywhere — is not delivered and is not an error; its fields narrate.

### 2. Truly unrecoverable losses (`DECLARED_LOSSES`)

A small mapping of field-path patterns to reasons, covering only what cannot
even ride the narrative:

* the SOAP note `kind` split — subjective/objective/assessment/plan collapse
  into one `narrative` section on re-ingest;
* structural plumbing the narrative deliberately omits — per-object
  `id`/`provenance`, regenerated or non-deterministic on ingest;
* a document artifact's BYTES, which are delivered as a file rather than
  narrated (above). `build_ccd` alone writes no files, so a caller that
  delivers nothing gets a document with no artifact entries and the artifact's
  path, media type and digest narrated instead — nothing is dropped either way,
  but only the DELIVERER conserves the bytes.

## Determinism

Same record in, byte-identical bytes out: stable element order, no wall-clock
or random ids, the loss narrative sorted, and `document_id` defaulting to a
uuid5 over the patient id so a record with no explicit id still produces a fixed
document. Non-deterministic fields (`provenance.ingested_at`, uuid4 ids) are
excluded from the narrative by `_STRUCTURAL_SKIP`.
