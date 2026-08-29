"""Tests for the C-CDA / CCD adapter against the synthetic fixture.

Each test asserts one section's mapping (or one trap) documented in
tests/fixtures/ccda/README.md.
"""

import re
from datetime import UTC, date, timedelta
from pathlib import Path

import pytest

import anastomosis.sources.ccda
import anastomosis.sources.pf_tebra  # noqa: F401 — for the cross-adapter detect test
from anastomosis.core.fhir import from_bundle, to_bundle
from anastomosis.core.model import (
    AllergyCategory,
    IdentifierKind,
    ObservationCategory,
    PatientRecord,
)
from anastomosis.sources import get_source

CCDA_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "ccda"
_BIRTHTIME_RE = re.compile(r'<birthTime value="[^"]*"/>')
PF_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"


@pytest.fixture(scope="module")
def record() -> PatientRecord:
    adapter = get_source("ccda")
    assert adapter.detect(CCDA_FIXTURE)
    loaded = list(adapter.load(CCDA_FIXTURE))
    assert len(loaded) == 1
    return loaded[0]


# --- detection ---------------------------------------------------------------


def test_detect_true_on_ccda_dir() -> None:
    assert get_source("ccda").detect(CCDA_FIXTURE)


def test_detect_false_on_empty_and_pf_dirs(tmp_path: Path) -> None:
    assert not get_source("ccda").detect(tmp_path)
    assert not get_source("ccda").detect(PF_FIXTURE)


def test_pf_adapter_does_not_claim_ccda_dir() -> None:
    assert not get_source("pf-tebra").detect(CCDA_FIXTURE)


def test_patient_id_is_deterministic_across_loads() -> None:
    """The C-CDA patient id is derived from the source (uuid5), not a fresh
    uuid4, so re-parsing the same document yields the same canonical id — like
    the encounter ids already did, and like the PF/Tebra adapter's source-guid
    id. Child records reference that same id, and it is a valid UUID."""
    import uuid

    adapter = get_source("ccda")
    first = next(iter(adapter.load(CCDA_FIXTURE)))
    second = next(iter(adapter.load(CCDA_FIXTURE)))
    assert first.patient.id == second.patient.id  # deterministic, not random
    uuid.UUID(first.patient.id)  # this fixture's id is a uuid5 — a valid UUID
    assert "901-65-4329" not in first.patient.id  # the SSN never becomes the id (PHI-safe)
    assert first.conditions and all(c.patient_id == first.patient.id for c in first.conditions)


def test_utf16_encoded_cda_is_detected_and_loaded(tmp_path: Path) -> None:
    """A UTF-16-encoded C-CDA (some Windows EHRs export UTF-16) is detected and
    loaded rather than silently skipped (zero records, no error). The
    encoding-aware sniff hands it to lxml, which parses UTF-16 natively, and the
    canonical record matches the UTF-8 form byte-for-byte (provenance aside)."""
    text = (CCDA_FIXTURE / "feedface_ccd.xml").read_text(encoding="utf-8")
    u16_dir = tmp_path / "u16"
    u16_dir.mkdir()
    (u16_dir / "feedface_ccd.xml").write_bytes(text.encode("utf-16"))  # adds a BOM

    adapter = get_source("ccda")
    assert adapter.detect(u16_dir)  # the old ASCII byte-sniff missed UTF-16
    loaded = list(adapter.load(u16_dir))
    assert len(loaded) == 1

    utf8 = next(iter(adapter.load(CCDA_FIXTURE)))
    u16 = loaded[0]
    assert u16.patient.birth_date == date(1979, 4, 6)
    # Compare clinical demographics; `id` is a per-load synthetic handle (not
    # source-derived in this adapter) and `provenance` carries the file path.
    drop = {"provenance", "id"}
    assert u16.patient.model_dump(mode="json", exclude=drop) == utf8.patient.model_dump(
        mode="json", exclude=drop
    )
    # The stable source identity is carried and matches across the two encodings.
    assert {(i.kind, i.value) for i in u16.patient.identifiers} == {
        (i.kind, i.value) for i in utf8.patient.identifiers
    }
    assert (len(u16.conditions), len(u16.observations), len(u16.encounters)) == (
        len(utf8.conditions),
        len(utf8.observations),
        len(utf8.encounters),
    )


# --- demographics ------------------------------------------------------------


def test_demographics(record: PatientRecord) -> None:
    p = record.patient
    assert p.display_name == "Cora Specimen"
    assert p.birth_date == date(1979, 4, 6)
    assert p.sex == "Female"
    assert p.race == ["Asian"]
    assert p.ethnicity == ["Not Hispanic or Latino"]
    assert p.language == "en"
    assert p.identifier(IdentifierKind.SSN) == "901-65-4329"
    phones = {t.kind.value: t.value for t in p.telecom}
    assert phones["phone_home"] == "(206) 555-0177"
    assert phones["email"] == "cora.specimen@example.com"
    assert p.addresses[0].line1 == "456 Sample Way"
    assert p.addresses[0].postal_code == "98102"


# --- problems ----------------------------------------------------------------


def test_conditions(record: PatientRecord) -> None:
    htn = next(c for c in record.conditions if c.snomed == "38341003")
    assert htn.icd10 == "I10"
    assert htn.active is True
    assert htn.display == "Hypertensive disorder"
    migraine = next(c for c in record.conditions if c.snomed == "37796009")
    assert migraine.active is False  # has effectiveTime/high
    assert migraine.stopped == date(2020, 9, 1)
    assert migraine.icd10 is None


# --- allergies ---------------------------------------------------------------


def test_allergies(record: PatientRecord) -> None:
    by_substance = {a.substance: a for a in record.allergies}
    penicillin = by_substance["Penicillin G"]
    assert penicillin.category == AllergyCategory.DRUG
    assert penicillin.reactions == ["Hives"]
    assert penicillin.severity == "Moderate"
    assert penicillin.extensions["ccda:allergen_code"] == "7980"
    peanut = by_substance["Peanut"]
    assert peanut.category == AllergyCategory.FOOD
    assert peanut.reactions == ["Anaphylaxis"]
    assert peanut.severity == "Severe"
    assert peanut.extensions["ccda:allergen_code"] == "256349002"


# --- medications -------------------------------------------------------------


def test_medications(record: PatientRecord) -> None:
    by_rxnorm = {m.rxnorm: m for m in record.medications}
    lisinopril = by_rxnorm["314076"]
    assert lisinopril.active is True
    assert lisinopril.start == date(2023, 1, 1)
    assert lisinopril.stop is None  # high nullFlavor="UNK"
    assert lisinopril.extensions["ccda:route"] == "Oral"
    amoxicillin = by_rxnorm["308182"]
    assert amoxicillin.active is False  # statusCode completed
    assert amoxicillin.stop == date(2022, 3, 14)


# --- immunizations -----------------------------------------------------------


def test_immunizations(record: PatientRecord) -> None:
    flu = next(i for i in record.immunizations if "Influenza" in (i.vaccine or ""))
    assert flu.administered_on == date(2022, 10, 3)
    assert flu.lot_number == "FLU2022A"
    assert flu.comment is None
    refused = next(i for i in record.immunizations if i.comment == "Refused")
    assert refused.extensions["ccda:negationInd"] == "true"


# --- vitals + results --------------------------------------------------------


def test_vitals(record: PatientRecord) -> None:
    vitals = [o for o in record.observations if o.category == ObservationCategory.VITAL_SIGNS]
    assert len(vitals) >= 4
    by_code = {o.code: o for o in vitals}
    assert by_code["8480-6"].value == "122"  # systolic
    assert by_code["8462-4"].value == "78"  # diastolic
    assert by_code["8480-6"].unit == "mm[Hg]"
    effective = by_code["8480-6"].effective_at
    assert effective is not None
    assert effective.utcoffset() == timedelta(hours=-5)
    assert effective.astimezone(UTC).hour == 19  # 14:00 -0500 → 19:00 UTC


def test_results(record: PatientRecord) -> None:
    labs = [o for o in record.observations if o.category == ObservationCategory.LABORATORY]
    glucose = next(o for o in labs if o.code == "2345-7")
    assert glucose.value == "92"
    assert glucose.unit == "mg/dL"
    creatinine = next(o for o in labs if o.code == "2160-0")
    assert creatinine.value == "0.9"


# --- social history ----------------------------------------------------------


def test_smoking_observation(record: PatientRecord) -> None:
    social = [o for o in record.observations if o.category == ObservationCategory.SOCIAL_HISTORY]
    smoking = next(o for o in social if o.display == "Tobacco use")
    assert smoking.value == "Never smoker"


# --- encounters + notes ------------------------------------------------------


def test_encounters_include_office_visit_and_note(record: PatientRecord) -> None:
    assert len(record.encounters) >= 2
    office = next(
        e for e in record.encounters if (e.encounter_type or "").startswith("Office outpatient")
    )
    assert office.date_of_service == date(2023, 5, 10)
    note_encounter = next(
        e for e in record.encounters if any(s.kind.value == "narrative" for s in e.sections)
    )
    assert note_encounter.date_of_service == date(2023, 5, 10)
    narrative = note_encounter.sections[0]
    assert narrative.html is None
    assert narrative.text is not None
    assert "routine blood pressure follow-up" in narrative.text


# --- losslessness ------------------------------------------------------------


# A Problems section (LOINC 11450-4 — structurally parsed) whose single entry
# is NOT the act/entryRelationship/observation shape `_conditions` requires, so
# the structural parser yields nothing and the narrative is the only record of
# what the section said.
_UNSUPPORTED_PROBLEMS_CCD = """<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3">
  <id root="feedface-0000-0000-0000-00000000cda2"/>
  <title>Unsupported-entry CCD</title>
  <recordTarget><patientRole>
    <id root="feedface-0000-0000-0000-000000000001"/>
    <patient><name><given>Ada</given><family>Fixture</family></name></patient>
  </patientRole></recordTarget>
  <component><structuredBody>
    <component><section>
      <code code="11450-4" codeSystem="2.16.840.1.113883.6.1"/>
      <title>Problems</title>
      <text>SENTINEL-UNSUPPORTED-PROBLEM (active, onset 2021-02-15)</text>
      <entry><observation classCode="OBS" moodCode="EVN">
        <value code="38341003" codeSystem="2.16.840.1.113883.6.96"/>
      </observation></entry>
    </section></component>
  </structuredBody></component>
</ClinicalDocument>
"""


def test_known_section_narrative_survives_unsupported_entries(tmp_path: Path) -> None:
    """A structurally-parsed section whose entries the parser cannot take apart
    still keeps its narrative: the <text> of EVERY section is captured, not only
    an unknown section's, so an unsupported entry shape cannot erase the chart."""
    from anastomosis.sources.ccda.parser import parse_document

    doc = tmp_path / "unsupported_problems.xml"
    doc.write_text(_UNSUPPORTED_PROBLEMS_CCD, encoding="utf-8")
    parsed = parse_document(doc)
    assert parsed.conditions == []  # the structural parser is unchanged: it still skips
    section = parsed.patient.extensions["ccda:section:11450-4"]
    assert section["title"] == "Problems"
    assert "SENTINEL-UNSUPPORTED-PROBLEM" in section["text"]


# Two sections sharing one LOINC (split Problems (Active)/(Resolved) is ordinary
# C-CDA) plus two sections with no code at all — four narratives that must land
# on four distinct keys.
_REPEATED_SECTIONS_CCD = """<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3">
  <id root="feedface-0000-0000-0000-00000000cda9"/>
  <title>Repeated-section CCD</title>
  <recordTarget><patientRole>
    <id root="feedface-0000-0000-0000-000000000001"/>
    <patient><name><given>Ada</given><family>Fixture</family></name></patient>
  </patientRole></recordTarget>
  <component><structuredBody>
    <component><section>
      <code code="11450-4" codeSystem="2.16.840.1.113883.6.1"/>
      <title>Problems (Active)</title>
      <text>SENTINEL-FIRST-PROBLEM-NARRATIVE</text>
    </section></component>
    <component><section>
      <code code="11450-4" codeSystem="2.16.840.1.113883.6.1"/>
      <title>Problems (Resolved)</title>
      <text>SENTINEL-SECOND-PROBLEM-NARRATIVE</text>
    </section></component>
    <component><section>
      <title>No-code section A</title>
      <text>SENTINEL-NOCODE-A</text>
    </section></component>
    <component><section>
      <title>No-code section B</title>
      <text>SENTINEL-NOCODE-B</text>
    </section></component>
  </structuredBody></component>
</ClinicalDocument>
"""


def test_repeated_section_code_keeps_every_narrative(tmp_path: Path) -> None:
    """A second section sharing a LOINC must not overwrite the first: each
    occurrence gets its own key, suffixed in document order, so neither the
    Active nor the Resolved Problems narrative vanishes. Two code-less sections
    are disambiguated the same way."""
    from anastomosis.sources.ccda.parser import parse_document

    doc = tmp_path / "repeated_sections.xml"
    doc.write_text(_REPEATED_SECTIONS_CCD, encoding="utf-8")
    ext = parse_document(doc).patient.extensions
    assert ext["ccda:section:11450-4"] == {
        "title": "Problems (Active)",
        "text": "SENTINEL-FIRST-PROBLEM-NARRATIVE",
    }
    assert ext["ccda:section:11450-4#2"] == {
        "title": "Problems (Resolved)",
        "text": "SENTINEL-SECOND-PROBLEM-NARRATIVE",
    }
    assert ext["ccda:section:unknown"]["text"] == "SENTINEL-NOCODE-A"
    assert ext["ccda:section:unknown#2"]["text"] == "SENTINEL-NOCODE-B"


def test_single_occurrence_sections_keep_the_bare_key(record: PatientRecord) -> None:
    """The suffix appears only on a REPEAT: a document with one section per code
    reads exactly as before (no ``#1``)."""
    keys = [k for k in record.patient.extensions if k.startswith("ccda:section:")]
    assert keys and not any("#" in key for key in keys)


def test_parsed_section_keeps_entries_and_narrative(record: PatientRecord) -> None:
    """Narrative capture is additive: the fixture's Problems section still maps
    to typed conditions AND now keeps the section text it renders from."""
    assert {c.snomed for c in record.conditions} >= {"38341003", "37796009"}
    problems = record.patient.extensions["ccda:section:11450-4"]
    assert problems["title"] == "Problems"
    assert "Essential hypertension" in problems["text"]


def test_unparsed_section_and_document_metadata_survive(record: PatientRecord) -> None:
    ext = record.patient.extensions
    plan = ext["ccda:section:18776-5"]
    assert plan["title"] == "Plan of Treatment"
    assert "recheck blood pressure in three months" in plan["text"]
    assert ext["ccda:documentId"] == "feedface-0000-0000-0000-00000000cda1"
    assert "ccda:title" in ext


# --- idempotency -------------------------------------------------------------


def test_encounter_ids_are_deterministic_across_reparses() -> None:
    # The engine's idempotent-skip relies on encounter.id being stable across
    # re-runs (same-day collision suffixing keys off encounter.id[:8]). A
    # plain uuid4 fallback would re-render every encounter on every pass.
    from anastomosis.sources.ccda.parser import parse_document

    first = parse_document(CCDA_FIXTURE / "feedface_ccd.xml")
    second = parse_document(CCDA_FIXTURE / "feedface_ccd.xml")
    assert [e.id for e in first.encounters] == [e.id for e in second.encounters]


# --- cross-adapter FHIR round trip -------------------------------------------


def _dumps(models: list) -> list[dict]:
    return [m.model_dump(mode="json", exclude={"provenance"}) for m in models]


def test_fhir_round_trip_is_lossless(record: PatientRecord) -> None:
    rebuilt = from_bundle(to_bundle(record))
    assert rebuilt.patient.model_dump(mode="json", exclude={"provenance"}) == (
        record.patient.model_dump(mode="json", exclude={"provenance"})
    )
    assert _dumps(rebuilt.conditions) == _dumps(record.conditions)
    assert _dumps(rebuilt.allergies) == _dumps(record.allergies)


_VALUE_TYPES_CCD = """<?xml version="1.0"?>
<ClinicalDocument xmlns="urn:hl7-org:v3" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <recordTarget><patientRole><id root="feedface-0000-0000-0000-000000000001"/>
    <patient><name><given>Ada</given><family>Fixture</family></name>
    <administrativeGenderCode code="F" displayName="Female" codeSystem="2.16.840.1.113883.5.1"/>
    <birthTime value="19850314"/></patient></patientRole></recordTarget>
  <component><structuredBody><component><section>
    <code code="30954-2" codeSystem="2.16.840.1.113883.6.1"/><title>Results</title>
    <entry><organizer classCode="BATTERY" moodCode="EVN"><statusCode code="completed"/>
      <component><observation classCode="OBS" moodCode="EVN">
        <code code="2345-7" displayName="Glucose" codeSystem="2.16.840.1.113883.6.1"/>
        <effectiveTime value="20230510"/>
        <value xsi:type="PQ" value="99" unit="mg/dL"/></observation></component>
      <component><observation classCode="OBS" moodCode="EVN">
        <code code="5811-5" displayName="Specific gravity" codeSystem="2.16.840.1.113883.6.1"/>
        <effectiveTime value="20230510"/>
        <value xsi:type="ST">1.015</value></observation></component>
      <component><observation classCode="OBS" moodCode="EVN">
        <code code="664-3" displayName="Poikilocytosis" codeSystem="2.16.840.1.113883.6.1"/>
        <effectiveTime value="20230510"/>
        <value xsi:type="CD" code="260385009" displayName="Negative"/></observation></component>
      <component><observation classCode="OBS" moodCode="EVN">
        <code code="6690-2" displayName="WBC" codeSystem="2.16.840.1.113883.6.1"/>
        <effectiveTime value="20230510"/>
        <value xsi:type="IVL_PQ"><low value="4.0" unit="10*3/uL"/>
        <high value="11.0" unit="10*3/uL"/></value></observation></component>
    </organizer></entry></section></component></structuredBody></component>
</ClinicalDocument>
"""


def test_every_ccda_value_type_keeps_its_result(tmp_path: Path) -> None:
    """Only ``xsi:type="PQ"`` used to be read (#243).

    Every other form left ``Observation.value`` as None while the Observation was
    still created carrying its LOINC code — a finalized result that says nothing.
    A receiving EHR reads that as "no result" rather than as the Negative or
    Trace the document actually recorded, and qualitative results are a large
    fraction of any real lab feed.
    """
    (tmp_path / "values.xml").write_text(_VALUE_TYPES_CCD, encoding="utf-8")

    (record,) = list(get_source("ccda").load(tmp_path))
    readings = {o.display: (o.value, o.unit) for o in record.observations}

    assert readings["Glucose"] == ("99", "mg/dL")  # PQ, the one that always worked
    assert readings["Specific gravity"] == ("1.015", None)  # ST — element text
    assert readings["Poikilocytosis"] == ("Negative", None)  # CD — the coded display
    assert readings["WBC"] == ("4.0-11.0", "10*3/uL")  # IVL_PQ — a range with its unit
    assert not [d for d, (v, _) in readings.items() if v is None], "a result lost its value"


def test_one_unreadable_document_is_refused_by_position_not_anonymously(tmp_path: Path) -> None:
    """A document the adapter cannot parse refuses the run — a partial migration
    that silently omits a patient is the failure this project exists to prevent.

    But refusing has to say WHICH document to repair, and it did not: the
    exception escaped as an arbitrary error, so the pipeline could show only its
    type — "Could not read the ccda export (ValueError)." Against 2,103 real
    documents that leaves bisecting by hand as the only recourse (#243).
    """
    from anastomosis.pipeline import PipelineError, load_records

    good = (CCDA_FIXTURE / "feedface_ccd.xml").read_text(encoding="utf-8")
    for index in range(1, 6):
        body = good.replace(
            "feedface-0000-0000-0000-000000000001",
            f"feedface-0000-0000-0000-00000000000{index}",
        )
        if index == 3:
            body = _BIRTHTIME_RE.sub('<birthTime value="not-a-date"/>', body)
        (tmp_path / f"patient_{index:02d}.xml").write_text(body, encoding="utf-8")

    with pytest.raises(PipelineError) as caught:
        load_records(get_source("ccda"), tmp_path)

    message = str(caught.value)
    assert "document 3 of 5" in message, "the refusal must name which document"
    assert "ValueError" in message
    # The file name is a patient value in a real C-CDA export, so it must not appear.
    assert "patient_03" not in message


_NARRATIVE_REFS_CCD = """<?xml version="1.0"?>
<ClinicalDocument xmlns="urn:hl7-org:v3" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <recordTarget><patientRole><id root="feedface-0000-0000-0000-000000000001"/>
    <patient><name><given>Ada</given><family>Fixture</family></name>
    <administrativeGenderCode code="F" displayName="Female" codeSystem="2.16.840.1.113883.5.1"/>
    <birthTime value="19850314"/></patient></patientRole></recordTarget>
  <component><structuredBody>
    <component><section>
      <code code="11450-4" codeSystem="2.16.840.1.113883.6.1"/><title>Problems</title>
      <text><list><item ID="prob1">Chronic obstructive pulmonary disease</item></list></text>
      <entry><act classCode="ACT" moodCode="EVN"><entryRelationship typeCode="SUBJ">
        <observation classCode="OBS" moodCode="EVN">
          <code code="55607006" codeSystem="2.16.840.1.113883.6.96"/>
          <effectiveTime><low value="20230510"/></effectiveTime>
          <value xsi:type="CD" code="13645005" codeSystem="2.16.840.1.113883.6.96">
            <originalText><reference value="#prob1"/></originalText>
          </value>
        </observation></entryRelationship></act></entry>
    </section></component>
    <component><section>
      <code code="34109-9" codeSystem="2.16.840.1.113883.6.1"/><title>Notes</title>
      <text><paragraph ID="n1">SUBJECTIVE: cough x3d. PLAN: fluids and rest.</paragraph></text>
      <entry><act classCode="ACT" moodCode="EVN">
        <id root="feedface-0000-4000-8000-00000000e001"/>
        <code code="34109-9" displayName="Note" codeSystem="2.16.840.1.113883.6.1"/>
        <text><reference value="#n1"/></text>
        <author><time value="20230510"/></author>
      </act></entry>
    </section></component>
  </structuredBody></component>
</ClinicalDocument>
"""


def test_a_narrative_reference_is_read_as_the_words_it_points_at(tmp_path: Path) -> None:
    """Linking a coded entry to its narrative by ``<reference value="#id"/>`` is
    THE standard C-CDA mechanism, and a reference element carries no text of its
    own (#243).

    So a problem whose originalText is a reference arrived unnamed — a blank row
    on the chart's problem list — and a note whose body is a reference arrived
    empty, rendering the visit as a blank SOAP note, while the words sat a few
    elements away in the same document.
    """
    (tmp_path / "refs.xml").write_text(_NARRATIVE_REFS_CCD, encoding="utf-8")

    (record,) = list(get_source("ccda").load(tmp_path))

    (condition,) = record.conditions
    assert condition.display == "Chronic obstructive pulmonary disease", "the problem lost its name"

    (encounter,) = record.encounters
    (section,) = encounter.sections
    assert section.text == "SUBJECTIVE: cough x3d. PLAN: fluids and rest.", "the note body was lost"


def test_a_reference_naming_an_id_the_document_lacks_is_left_alone(tmp_path: Path) -> None:
    """Unresolvable is not the same as resolvable-to-nothing: a dangling
    reference must not invent text, and must not take the document down.
    """
    dangling = _NARRATIVE_REFS_CCD.replace('value="#prob1"', 'value="#nosuchid"')
    (tmp_path / "refs.xml").write_text(dangling, encoding="utf-8")

    (record,) = list(get_source("ccda").load(tmp_path))

    (condition,) = record.conditions
    assert condition.display is None, "a dangling reference must not fabricate a name"
    # The note half still resolves — one bad reference does not poison the rest.
    (encounter,) = record.encounters
    assert encounter.sections[0].text == "SUBJECTIVE: cough x3d. PLAN: fluids and rest."


def test_a_reference_carrying_its_own_fallback_text_keeps_it(tmp_path: Path) -> None:
    """Some writers put the words inline AND point at the narrative. The inline
    text is what the document already says, so resolving must not overwrite it.
    """
    inline = _NARRATIVE_REFS_CCD.replace(
        '<reference value="#prob1"/>', '<reference value="#prob1">Emphysema</reference>'
    )
    (tmp_path / "refs.xml").write_text(inline, encoding="utf-8")

    (record,) = list(get_source("ccda").load(tmp_path))

    (condition,) = record.conditions
    assert condition.display == "Emphysema", "the document's own words were overwritten"
