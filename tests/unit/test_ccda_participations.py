"""Who wrote it, who did it, who signed it, and where.

The C-CDA header is where a chart says all of that, and none of it was read:
2,103 audited documents parsed without error and produced not one practitioner
and not one facility between them (#312). A note that arrives with no author has
lost the answer to "who wrote this" while still looking complete, which is the
worst shape a surviving record can take.

So every assertion here is about a DISTINCTION that has to survive: a human
author against the system that generated the document, a legal authenticator
against an informant, an emergency contact against a clinician, the allergen
under an allergy against the participant in a header. Collapsing any of them
would make the code smaller and the record wrong.

Synthetic throughout: ``feedface-`` ids, invented people, the 555 exchange.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anastomosis.core.model import PatientRecord
from anastomosis.sources.ccda.parser import parse_document

_HEADER = """
  <author>
    <time value="20230510150000-0500"/>
    <assignedAuthor>
      <id root="feedface-auth-0000-0000-000000000001"/>
      <addr use="WP"><streetAddressLine>789 Clinic Court</streetAddressLine>
        <city>Springfield</city><state>WA</state><postalCode>98102</postalCode></addr>
      <telecom value="tel:+1(206)555-0133" use="WP"/>
      <assignedPerson><name><given>Quinn</given><family>Authorman</family>
        <suffix>MD</suffix></name></assignedPerson>
      <representedOrganization>
        <id root="feedface-orga-0000-0000-000000000002"/>
        <name>Sample Family Medicine</name>
      </representedOrganization>
    </assignedAuthor>
  </author>
  <author>
    <time value="20230510150100-0500"/>
    <assignedAuthor>
      <id root="feedface-auth-0000-0000-000000000003"/>
      <assignedAuthoringDevice>
        <manufacturerModelName>Synthetic EHR</manufacturerModelName>
        <softwareName>Synthetic EHR Export</softwareName>
      </assignedAuthoringDevice>
    </assignedAuthor>
  </author>
  <dataEnterer><assignedEntity>
    <id root="feedface-dtae-0000-0000-000000000004"/>
    <assignedPerson><name><given>Cora</given><family>Fixture</family></name></assignedPerson>
  </assignedEntity></dataEnterer>
  <informant><relatedEntity classCode="PRS">
    <code code="SPS" displayName="spouse" codeSystem="2.16.840.1.113883.5.111"/>
    <relatedPerson><name><given>Boris</given><family>Sample</family></name></relatedPerson>
  </relatedEntity></informant>
  <informationRecipient><intendedRecipient>
    <id root="feedface-irec-0000-0000-000000000005"/>
    <informationRecipient><name><given>Ada</given><family>Placeholder</family></name></informationRecipient>
    <receivedOrganization>
      <id root="feedface-recv-0000-0000-000000000006"/>
      <name>Sample Cardiology</name>
    </receivedOrganization>
  </intendedRecipient></informationRecipient>
  <legalAuthenticator>
    <time value="20230510160000-0500"/>
    <signatureCode code="S"/>
    <assignedEntity>
      <id root="feedface-lgau-0000-0000-000000000007"/>
      <assignedPerson><name><given>Ada</given><family>Fixture</family></name></assignedPerson>
    </assignedEntity>
  </legalAuthenticator>
  <authenticator>
    <time value="20230510161000-0500"/>
    <signatureCode code="S"/>
    <assignedEntity>
      <id root="feedface-autn-0000-0000-000000000008"/>
      <assignedPerson><name><given>Cleo</given><family>Fixture</family></name></assignedPerson>
    </assignedEntity>
  </authenticator>
  <participant typeCode="IND"><associatedEntity classCode="ECON">
    <id root="feedface-assc-0000-0000-000000000009"/>
    <telecom value="tel:+1(206)555-0155" use="HP"/>
    <associatedPerson><name><given>Gus</given><family>Placeholder</family></name></associatedPerson>
  </associatedEntity></participant>
  <custodian><assignedCustodian><representedCustodianOrganization>
    <id root="feedface-orga-0000-0000-000000000002"/>
    <telecom value="tel:+1(206)555-0133" use="WP"/>
    <addr use="WP"><streetAddressLine>789 Clinic Court</streetAddressLine>
      <city>Springfield</city><state>WA</state><postalCode>98102</postalCode></addr>
  </representedCustodianOrganization></assignedCustodian></custodian>
  <documentationOf><serviceEvent classCode="PCPR">
    <id root="feedface-serv-0000-0000-000000000010"/>
    <effectiveTime><low value="20150101"/><high value="20230510"/></effectiveTime>
    <performer typeCode="PRF"><assignedEntity>
      <id root="feedface-perf-0000-0000-000000000011"/>
      <assignedPerson><name><given>Cleo</given><family>Specimen</family></name></assignedPerson>
    </assignedEntity></performer>
  </serviceEvent></documentationOf>
  <componentOf><encompassingEncounter>
    <id root="feedface-encm-0000-0000-000000000012"/>
    <code code="99213" displayName="Office outpatient visit 15 minutes"
          codeSystem="2.16.840.1.113883.6.12"/>
    <effectiveTime value="20230510"/>
    <responsibleParty><assignedEntity>
      <id root="feedface-resp-0000-0000-000000000013"/>
      <assignedPerson><name><given>Quinn</given><family>Specimen</family></name></assignedPerson>
    </assignedEntity></responsibleParty>
    <location><healthCareFacility>
      <id root="feedface-hcfa-0000-0000-000000000014"/>
      <location><name>Sample Family Medicine East</name>
        <addr><streetAddressLine>12 Example Road</streetAddressLine>
          <city>Springfield</city><state>WA</state><postalCode>98103</postalCode></addr>
      </location>
      <serviceProviderOrganization>
        <telecom value="tel:+1(206)555-0144" use="WP"/>
      </serviceProviderOrganization>
    </healthCareFacility></location>
  </encompassingEncounter></componentOf>
"""

_ALLERGY_SECTION = """
  <component><section>
    <code code="48765-2" codeSystem="2.16.840.1.113883.6.1"/>
    <title>Allergies</title>
    <entry><act classCode="ACT" moodCode="EVN">
      <id root="feedface-alrg-0000-0000-000000000020"/>
      <entryRelationship typeCode="SUBJ">
        <observation classCode="OBS" moodCode="EVN">
          <id root="feedface-alro-0000-0000-000000000021"/>
          <value code="416098002" displayName="Drug allergy"
                 codeSystem="2.16.840.1.113883.6.96"/>
          <participant typeCode="CSM"><participantRole classCode="MANU">
            <playingEntity classCode="MMAT">
              <code code="7980" displayName="Penicillin G"
                    codeSystem="2.16.840.1.113883.6.88"/>
            </playingEntity>
          </participantRole></participant>
        </observation>
      </entryRelationship>
    </act></entry>
  </section></component>
"""

_NOTE_SECTION = """
  <component><section>
    <code code="34109-9" codeSystem="2.16.840.1.113883.6.1"/>
    <title>Notes</title>
    <entry><act classCode="ACT" moodCode="EVN">
      <id root="feedface-note-0000-0000-000000000030"/>
      <code code="34109-9" displayName="Note" codeSystem="2.16.840.1.113883.6.1"/>
      <text>Patient returns for routine follow-up.</text>
      <author>
        <time value="20230510150000-0500"/>
        <assignedAuthor>
          <id root="feedface-nota-0000-0000-000000000031"/>
          <assignedPerson><name><given>Synthia</given><family>Probe</family></name></assignedPerson>
        </assignedAuthor>
      </author>
    </act></entry>
  </section></component>
"""

_ENCOUNTER_SECTION = """
  <component><section>
    <code code="46240-8" codeSystem="2.16.840.1.113883.6.1"/>
    <title>Encounters</title>
    <entry><encounter classCode="ENC" moodCode="EVN">
      <id root="feedface-encr-0000-0000-000000000040"/>
      <code code="99213" displayName="Office outpatient visit 15 minutes"
            codeSystem="2.16.840.1.113883.6.12"/>
      <effectiveTime value="20230510"/>
      <performer><assignedEntity>
        <id root="feedface-encp-0000-0000-000000000041"/>
        <assignedPerson><name><given>Cora</given><family>Specimen</family></name></assignedPerson>
      </assignedEntity></performer>
    </encounter></entry>
  </section></component>
"""

_DOCUMENT = """<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <realmCode code="US"/>
  <templateId root="2.16.840.1.113883.10.20.22.1.1" extension="2015-08-01"/>
  <id root="feedface-docu-0000-0000-000000000000"/>
  <code code="34133-9" codeSystem="2.16.840.1.113883.6.1"/>
  <title>Synthetic Document</title>
  <effectiveTime value="20230510150000-0500"/>
  <recordTarget><patientRole>
    <id root="feedface-pati-0000-0000-000000000099"/>
    <patient>
      <name><given>Synthia</given><family>Probe</family></name>
      <birthTime value="19800102"/>
    </patient>
  </patientRole></recordTarget>
  {header}
  <component><structuredBody>{body}</structuredBody></component>
</ClinicalDocument>
"""


def _write(tmp_path: Path, *, header: str = _HEADER, body: str = "") -> Path:
    path = tmp_path / "doc.xml"
    path.write_text(_DOCUMENT.format(header=header, body=body), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def record(tmp_path_factory: pytest.TempPathFactory) -> PatientRecord:
    directory = tmp_path_factory.mktemp("ccda_participations")
    return parse_document(
        _write(directory, body=_ALLERGY_SECTION + _NOTE_SECTION + _ENCOUNTER_SECTION)
    )


def _by_role(record: PatientRecord, role: str) -> list[object]:
    return [p for p in record.practitioners if p.extensions.get("ccda:participation") == role]


def _sole(record: PatientRecord, role: str) -> object:
    matches = _by_role(record, role)
    assert len(matches) == 1, f"{role}: expected one, got {len(matches)}"
    return matches[0]


# --- the distinctions that have to survive ------------------------------------


def test_a_human_author_and_a_generating_system_are_not_the_same_answer(
    record: PatientRecord,
) -> None:
    """Both answer "who wrote this" and they are not interchangeable: an
    automated summary attributed to a clinician is a statement nobody made. The
    document's own element name is what keeps them apart on the object."""
    authors = _by_role(record, "author")
    entities = {p.extensions.get("ccda:entity") for p in authors}
    assert entities == {"assignedPerson", "assignedAuthoringDevice"}

    device = next(p for p in authors if p.extensions["ccda:entity"] == "assignedAuthoringDevice")
    assert device.display_name == "Synthetic EHR Export"
    assert device.extensions["ccda:manufacturerModelName"] == "Synthetic EHR"
    assert device.given_name is None and device.family_name is None

    human = next(
        p
        for p in authors
        if p.extensions["ccda:entity"] == "assignedPerson" and p.family_name == "Authorman"
    )
    assert human.name == "Quinn Authorman"


@pytest.mark.parametrize(
    ("role", "family_name"),
    [
        ("dataEnterer", "Fixture"),
        ("informationRecipient", "Placeholder"),
        ("legalAuthenticator", "Fixture"),
        ("authenticator", "Fixture"),
        ("participant", "Placeholder"),
        ("responsibleParty", "Specimen"),
    ],
)
def test_each_participation_keeps_the_role_it_carried(
    record: PatientRecord, role: str, family_name: str
) -> None:
    """A legal authenticator is not an authenticator and neither is an
    informant. Folding them into one "provider" would leave the record unable to
    say who signed the chart and who merely supplied the history."""
    assert _sole(record, role).family_name == family_name


def test_an_informant_who_gave_only_a_relationship_still_arrives_as_one(
    record: PatientRecord,
) -> None:
    """CDA's ``relatedEntity`` has no id, so this one can never be credited by
    the ingest ledger — but the document did say a spouse supplied the history,
    and dropping that for want of an identifier would lose the fact itself."""
    informant = _sole(record, "informant")
    assert informant.extensions["ccda:code"] == "spouse"
    assert informant.extensions["ccda:classCode"] == "PRS"
    assert informant.provenance is not None and informant.provenance.source_id is None


def test_an_emergency_contact_is_not_filed_as_a_clinician(record: PatientRecord) -> None:
    """The header participant is a person the chart names, not one who treated
    the patient, and the role it carried is what says so."""
    contact = _sole(record, "participant")
    assert contact.extensions["ccda:classCode"] == "ECON"
    assert contact.extensions["ccda:typeCode"] == "IND"
    assert contact.extensions["ccda:telecom"] == ["tel:+1(206)555-0155"]


def test_the_custodian_is_a_place_and_not_a_person(record: PatientRecord) -> None:
    """A custodian names an organization; nothing about it says a human held
    the chart, so nothing here pretends one did."""
    assert not _by_role(record, "custodian")
    assert "Sample Family Medicine" in {f.name for f in record.facilities}


def test_a_statement_that_names_who_told_us_is_read_where_it_stands(tmp_path: Path) -> None:
    """An informant is read wherever it appears, and the others are not.

    A clinical statement may name who supplied it, and the header's informant
    does not answer for that one — so the informant walk is document-wide, which
    is also the scope the ingest ledger counts it at. Every other participation
    stays a direct child of ``ClinicalDocument``, because a walk that reached
    into the entries would harvest an allergy's own participant.
    """
    body = """
      <component><section>
        <code code="11450-4" codeSystem="2.16.840.1.113883.6.1"/>
        <title>Problems</title>
        <entry><act classCode="ACT" moodCode="EVN">
          <id root="feedface-prob-0000-0000-000000000070"/>
          <informant><assignedEntity>
            <id root="feedface-einf-0000-0000-000000000071"/>
            <assignedPerson><name><given>Gus</given><family>Sample</family></name></assignedPerson>
          </assignedEntity></informant>
        </act></entry>
      </section></component>
    """
    parsed = parse_document(_write(tmp_path, header="", body=body))
    informants = [
        p for p in parsed.practitioners if p.extensions["ccda:participation"] == "informant"
    ]
    assert [p.name for p in informants] == ["Gus Sample"]
    assert informants[0].provenance is not None
    assert informants[0].provenance.source_id == "feedface-einf-0000-0000-000000000071"


def test_the_allergen_under_an_allergy_is_still_the_allergen(record: PatientRecord) -> None:
    """A nested ``<participant>`` is the substance, and the header-scoped one is
    a person. Reading the header's by walking every participant in the document
    would have turned a drug into a practitioner."""
    assert [a.substance for a in record.allergies] == ["Penicillin G"]
    assert not any(p.display_name == "Penicillin G" for p in record.practitioners)
    assert len(_by_role(record, "participant")) == 1


# --- the visit the document is about ------------------------------------------


def test_the_encompassing_encounter_becomes_the_visit_it_describes(
    record: PatientRecord,
) -> None:
    framed = [e for e in record.encounters if "ccda:componentOf" in e.extensions]
    assert len(framed) == 1
    visit = framed[0]
    assert visit.encounter_type == "Office outpatient visit 15 minutes"
    assert visit.date_of_service is not None and visit.date_of_service.isoformat() == "2023-05-10"
    assert record.practitioner(visit.provider_id) is _sole(record, "responsibleParty")
    assert record.facility(visit.facility_id) is not None


def test_the_visits_place_is_read_from_wherever_the_document_split_it(
    record: PatientRecord,
) -> None:
    """C-CDA splits a location across ``healthCareFacility``, the ``location``
    beneath it and the ``serviceProviderOrganization`` beside it; a reader that
    looked in only one of them would report a visit with no place."""
    visit = next(e for e in record.encounters if "ccda:componentOf" in e.extensions)
    place = record.facility(visit.facility_id)
    assert place is not None
    assert place.name == "Sample Family Medicine East"
    assert place.address_line1 == "12 Example Road"
    assert place.phone == "(206) 555-0144"


def test_a_service_event_is_not_charted_as_a_visit(record: PatientRecord) -> None:
    """``documentationOf/serviceEvent`` carries the care-provision PERIOD —
    eight years wide here — and charting its low bound as a date of service
    would invent a visit on a day nothing happened. Its performer is the answer
    worth keeping; its own facts ride `extensions` rather than a fabricated
    encounter.
    """
    assert not any(e.date_of_service and e.date_of_service.year == 2015 for e in record.encounters)
    event = record.patient.extensions["ccda:serviceEvent"]
    assert event == [
        {
            "id": "feedface-serv-0000-0000-000000000010",
            "classCode": "PCPR",
            "low": "20150101",
            "high": "20230510",
        }
    ]
    assert any(
        p.provenance is not None
        and p.provenance.source_id == "feedface-perf-0000-0000-000000000011"
        for p in _by_role(record, "performer")
    )


def test_the_care_and_the_visit_each_name_who_delivered_them(record: PatientRecord) -> None:
    """Two performers: the one on the service event and the one on the
    Encounters entry. Both are "who delivered the care", and neither answers for
    the other."""
    performers = {p.family_name for p in _by_role(record, "performer")}
    assert performers == {"Specimen"}
    assert len(_by_role(record, "performer")) == 2
    entry_visit = next(e for e in record.encounters if e.encounter_type and not e.extensions)
    assert record.practitioner(entry_visit.provider_id) is not None


def test_a_note_points_at_the_author_who_wrote_that_note(record: PatientRecord) -> None:
    """The header's author did not write the note; the note's own author did,
    and for a reader of the note that is the only answer that helps."""
    note = next(e for e in record.encounters if e.note_type == "Note")
    author = record.practitioner(note.provider_id)
    assert author is not None
    assert author.name == "Synthia Probe"
    assert author.provenance is not None
    assert author.provenance.source_id == "feedface-nota-0000-0000-000000000031"


def test_the_documents_frame_does_not_strand_the_days_measurements(tmp_path: Path) -> None:
    """A document may state one visit twice — as its own frame and as an entry
    in the Encounters section — and if both counted as candidates the day would
    read as ambiguous and every measurement taken on it would go unfiled.
    """
    body = (
        _ENCOUNTER_SECTION
        + """
      <component><section>
        <code code="8716-3" codeSystem="2.16.840.1.113883.6.1"/>
        <title>Vital Signs</title>
        <entry><organizer classCode="CLUSTER" moodCode="EVN">
          <effectiveTime value="20230510150000-0500"/>
          <component><observation classCode="OBS" moodCode="EVN">
            <id root="feedface-vita-0000-0000-000000000050"/>
            <code code="8480-6" displayName="Systolic blood pressure"
                  codeSystem="2.16.840.1.113883.6.1"/>
            <value value="118" unit="mm[Hg]"/>
          </observation></component>
        </organizer></entry>
      </section></component>
    """
    )
    parsed = parse_document(_write(tmp_path, body=body))
    charted = [o for o in parsed.observations if o.encounter_id]
    assert len(charted) == 1
    assert parsed.encounters[0].id != charted[0].encounter_id  # the entry, not the frame


# --- provenance, identity and the losses that are refused ---------------------


@pytest.mark.parametrize(
    ("role", "source_id"),
    [
        ("dataEnterer", "feedface-dtae-0000-0000-000000000004"),
        ("informationRecipient", "feedface-irec-0000-0000-000000000005"),
        ("legalAuthenticator", "feedface-lgau-0000-0000-000000000007"),
        ("authenticator", "feedface-autn-0000-0000-000000000008"),
        ("participant", "feedface-assc-0000-0000-000000000009"),
        ("responsibleParty", "feedface-resp-0000-0000-000000000013"),
    ],
)
def test_a_participation_carries_the_id_root_the_document_gave_it(
    record: PatientRecord, role: str, source_id: str
) -> None:
    """Provenance names the ``<id root>`` the construct carried. That is the
    evidence the ingest ledger credits a parse on, so a practitioner without it
    is a parse nobody can verify happened."""
    practitioner = _sole(record, role)
    assert practitioner.provenance is not None
    assert practitioner.provenance.source_id == source_id


def test_one_organization_named_twice_is_one_facility(record: PatientRecord) -> None:
    """The author's practice IS the custodian in most exports, and two Facility
    objects under one id is a collision the archive refuses a whole patient for.
    Neither naming overwrites the other: the second fills the first's gaps."""
    practice = [f for f in record.facilities if f.name == "Sample Family Medicine"]
    assert len(practice) == 1
    # Name and id came from the author's header; address and phone only from
    # the custodian's block. Both halves survived.
    assert practice[0].address_line1 == "789 Clinic Court"
    assert practice[0].phone == "(206) 555-0133"
    assert len({f.id for f in record.facilities}) == len(record.facilities)


def test_a_role_identified_only_by_its_npi_claims_no_source_id(tmp_path: Path) -> None:
    """The NPI arc is a code system, not an instance identifier — every provider
    in the country shares that root — so crediting a parse to it would attribute
    an object by a value that says nothing about this document. The NPI still
    lands on its own field; the parse is recorded as unattributable."""
    header = """
    <author><time value="20230510150000-0500"/><assignedAuthor>
      <id root="2.16.840.1.113883.4.6" extension="1234567893"/>
      <assignedPerson><name><given>Ada</given><family>Sample</family></name></assignedPerson>
    </assignedAuthor></author>
    """
    parsed = parse_document(_write(tmp_path, header=header))
    author = _sole(parsed, "author")
    assert author.npi == "1234567893"
    assert author.provenance is not None and author.provenance.source_id is None


def test_a_wrapper_naming_nobody_does_not_become_somebody(tmp_path: Path) -> None:
    """CDA requires the wrapper even when it is empty, and this repo's own
    exporter writes a lone ``<assignedAuthor/>`` to satisfy the schema. A
    practitioner built from one would be a provider on the chart that no
    document claims exists."""
    header = '<author><time value="20230510150000-0500"/><assignedAuthor/></author>'
    parsed = parse_document(_write(tmp_path, header=header))
    assert parsed.practitioners == []


def test_what_practitioner_has_no_field_for_still_survives(record: PatientRecord) -> None:
    """The lossless rule at this seam: the author's suffix, working address,
    phone and the time they wrote have no Practitioner field, and a chart that
    dropped them would lose how to reach the clinician who wrote the note."""
    author = next(p for p in record.practitioners if p.family_name == "Authorman")
    assert author.extensions["ccda:suffix"] == "MD"
    assert author.extensions["ccda:time"] == "20230510150000-0500"
    assert author.extensions["ccda:telecom"] == ["tel:+1(206)555-0133"]
    assert author.extensions["ccda:addr"] == [
        {"line1": "789 Clinic Court", "city": "Springfield", "state": "WA", "postal_code": "98102"}
    ]
    assert author.extensions["ccda:representedOrganization"] == (
        "feedface-orga-0000-0000-000000000002"
    )


def test_a_signature_says_when_it_was_signed(record: PatientRecord) -> None:
    signer = _sole(record, "legalAuthenticator")
    assert signer.extensions["ccda:signatureCode"] == "S"
    assert signer.extensions["ccda:time"] == "20230510160000-0500"


def test_actor_ids_are_deterministic_across_reparses(tmp_path: Path) -> None:
    """Re-parsing the same document must yield the same ids — the engine's
    idempotent-skip invariant rides on that, and an actor whose id moved would
    be delivered as a second person every run."""
    path = _write(tmp_path, body=_NOTE_SECTION)
    first, second = parse_document(path), parse_document(path)
    assert [p.id for p in first.practitioners] == [p.id for p in second.practitioners]
    assert [f.id for f in first.facilities] == [f.id for f in second.facilities]
    # Three authors in this document — two in the header and the note's own —
    # and an id that did not count them apart would file three people as one.
    assert (
        len([p for p in first.practitioners if p.extensions["ccda:participation"] == "author"]) == 3
    )
    assert len({p.id for p in first.practitioners}) == len(first.practitioners)


def test_one_person_in_two_roles_stays_two_answers(tmp_path: Path) -> None:
    """A clinician who wrote the note and then signed it carries the same
    ``assignedEntity`` id twice. Keying practitioners on that root would put two
    objects under one id — the collision the archive refuses a patient for — and
    folding them would lose one of the two roles."""
    shared = "feedface-shar-0000-0000-000000000060"
    header = f"""
    <author><time value="20230510150000-0500"/><assignedAuthor>
      <id root="{shared}"/>
      <assignedPerson><name><given>Ada</given><family>Fixture</family></name></assignedPerson>
    </assignedAuthor></author>
    <legalAuthenticator><time value="20230510160000-0500"/><signatureCode code="S"/>
      <assignedEntity><id root="{shared}"/>
        <assignedPerson><name><given>Ada</given><family>Fixture</family></name></assignedPerson>
      </assignedEntity>
    </legalAuthenticator>
    """
    parsed = parse_document(_write(tmp_path, header=header))
    roles = {p.extensions["ccda:participation"] for p in parsed.practitioners}
    assert roles == {"author", "legalAuthenticator"}
    assert len({p.id for p in parsed.practitioners}) == 2
    assert all(
        p.provenance is not None and p.provenance.source_id == shared for p in parsed.practitioners
    )
