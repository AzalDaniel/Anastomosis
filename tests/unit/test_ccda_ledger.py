"""The ingest ledger has to be right in BOTH directions, or it is decoration.

An instrument that can only report loss is as blind as no instrument: it agrees
with every pessimist, it agrees with itself after a fix that did nothing, and
nobody can tell the difference. So every construct class here is proved twice —
a thing the adapter really parses must come back ``structurally_parsed``, and a
thing it really drops must come back ``unsupported`` — and the participation
case is proved by handing the ledger a record that DOES carry the author, so it
is visibly measuring the record rather than reciting a table of what the parser
is believed to do.

Synthetic throughout (``feedface-`` ids, invented names, the 555 exchange).
"""

from __future__ import annotations

import dataclasses
import itertools
import random
import re
from collections import Counter
from pathlib import Path

import pytest

from anastomosis.core.ccda_codes import (
    LOSS_NARRATIVE_TEMPLATE_ROOT,
    LOSS_NARRATIVE_TITLE,
)
from anastomosis.core.conservation import ConservationError
from anastomosis.core.model import PatientRecord, Practitioner, Provenance
from anastomosis.sources.ccda import ledger
from anastomosis.sources.ccda.ledger import (
    ID_LESS_CONSTRUCTS,
    PARTICIPATION_PATHS,
    Disposition,
    aggregate,
    assert_emittable,
    document_ledger,
    physician_reading,
)
from anastomosis.sources.ccda.parser import parse_document

CCDA_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "ccda" / "feedface_ccd.xml"

_DOCUMENT = """<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <realmCode code="US"/>
  <templateId root="2.16.840.1.113883.10.20.22.1.1" extension="2015-08-01"/>
  <id root="feedface-docu-0000-0000-000000000001"/>
  <code code="34133-9" displayName="Summarization of Episode Note"
        codeSystem="2.16.840.1.113883.6.1"/>
  <title>Synthetic Document</title>
  <effectiveTime value="20230510150000-0500"/>
  <recordTarget>
    <patientRole>
      <id root="feedface-pati-0000-0000-000000000001"/>
      <patient>
        <name><given>Synthia</given><family>Probe</family></name>
        <birthTime value="19800102"/>
      </patient>
    </patientRole>
  </recordTarget>
  {header}
  <component><structuredBody>{body}</structuredBody></component>
</ClinicalDocument>
"""


def _write(tmp_path: Path, *, body: str = "", header: str = "") -> Path:
    path = tmp_path / "doc.xml"
    path.write_text(_DOCUMENT.format(body=body, header=header), encoding="utf-8")
    return path


def _unparked(record: PatientRecord) -> PatientRecord:
    """``record`` with the verbatim entry copies taken away.

    The parser parks EVERY section's entries, so an entry's own bytes answer
    for it before anything else is consulted. The citation tests below are
    about what a ``<reference>`` into narrative proves, and answering them with
    the wrong evidence would leave that whole rule untested — so they take the
    copies away first, the same move
    ``test_the_same_entries_unparked_read_unsupported`` makes about the copies
    themselves. It is not a hypothetical shape either: the entries the parser
    parks nothing for — our own stamped loss ledger, and a subsection nested
    deeper than its walk reaches — are settled by exactly this rule.
    """
    for key in [k for k in record.patient.extensions if k.startswith("ccda:entries:")]:
        del record.patient.extensions[key]
    return record


def _row(ledger: object, construct: str) -> object:
    """The one merged row for ``construct``.

    Read through ``aggregate`` because a document ledger holds one row per
    SECTION OCCURRENCE — two Problems sections are two obligations, not one —
    and the merge is what turns those back into a single line to assert on.
    """
    corpus = aggregate([ledger])  # type: ignore[list-item]
    rows = [row for row in corpus.rows if row.construct == construct]
    assert rows, f"no row for {construct}"
    assert len(rows) == 1, f"{construct} split across template variants: {rows}"
    return rows[0]


def _sole(ledger: object, construct: str) -> Disposition:
    """The one disposition a single-instance construct ended in."""
    instances = {
        disposition: count
        for disposition, count in _row(ledger, construct).instances.items()  # type: ignore[attr-defined]
        if count
    }
    assert len(instances) == 1, f"{construct} is not a single instance: {instances}"
    return next(iter(instances))


# --- the four dispositions, on the repo's own verified fixture ----------------


@pytest.fixture(scope="module")
def fixture_ledger() -> object:
    return document_ledger(CCDA_FIXTURE)


@pytest.fixture(scope="module")
def fixture_record() -> PatientRecord:
    return parse_document(CCDA_FIXTURE)


def test_a_section_this_adapter_takes_apart_is_counted_as_parsed(fixture_ledger: object) -> None:
    """Problems (11450-4) becomes Condition objects, and the ledger says so —
    the direction that fails silently if the instrument is wired to report loss
    no matter what it is shown."""
    row = _row(fixture_ledger, "section:11450-4")
    assert row.counted(Disposition.STRUCTURALLY_PARSED) == 1  # type: ignore[attr-defined]
    assert row.entries == {Disposition.STRUCTURALLY_PARSED: 2}  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "construct",
    [
        "section:48765-2",  # allergies
        "section:10160-0",  # medications
        "section:11369-6",  # immunizations
        "section:8716-3",  # vital signs
        "section:30954-2",  # results
        "section:29762-2",  # social history
        "section:46240-8",  # encounters
        "section:34109-9",  # notes
    ],
)
def test_every_structurally_mapped_section_reads_as_parsed(
    fixture_ledger: object, construct: str
) -> None:
    assert _sole(fixture_ledger, construct) is Disposition.STRUCTURALLY_PARSED


def test_a_section_the_adapter_only_narrates_is_counted_as_narrative(
    fixture_ledger: object,
) -> None:
    """Plan of Treatment (18776-5) has no structural parser, and its words are
    in ``patient.extensions``. That is not nothing and it is not a parse, and
    the ledger has a word for exactly that."""
    assert _sole(fixture_ledger, "section:18776-5") is Disposition.NARRATIVE_PRESERVED


def test_the_author_and_the_custodian_now_become_canonical_objects(
    fixture_record: PatientRecord,
) -> None:
    """The 2,103-document finding, closed on the repo's own reference document.

    The author and the custodian were right there in the header and no canonical
    object came from either. Both do now: the author as a Practitioner carrying
    the role the document gave them, the practice that holds the chart as a
    Facility. Asserted against the RECORD rather than the ledger because on this
    fixture the ledger cannot credit either — see the next test, which is that
    blind spot named.
    """
    authors = [
        p
        for p in fixture_record.practitioners
        if p.extensions.get("ccda:participation") == "author"
    ]
    assert [p.name for p in authors] == ["Quinn Authorman", "Quinn Authorman"]
    assert [f.name for f in fixture_record.facilities] == ["Sample Family Medicine"]


@pytest.mark.parametrize("construct", ["participation:author", "participation:custodian"])
def test_a_construct_whose_id_root_is_shared_is_still_not_credited(
    fixture_ledger: object, construct: str
) -> None:
    """One root on two constructs credits neither, even now that both are parsed.

    This fixture stamps one provider id on the header author and again on the
    note that author wrote, and one organization OID on the author's practice
    and again on the custodian — both ordinary C-CDA. A root two constructs
    share cannot say which of them an object came from, so the ledger credits
    neither and counts the instances in ``unlinkable``: its own blind spot,
    reported rather than resolved in the flattering direction.
    """
    row = _row(fixture_ledger, construct)
    assert _sole(fixture_ledger, construct) is Disposition.UNSUPPORTED
    assert row.unlinkable == row.offered  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "construct",
    [
        "participation:legalAuthenticator",
        "participation:informant",
        "participation:serviceEvent",
        "participation:encompassingEncounter",
        "body:nonXMLBody",
    ],
)
def test_a_construct_the_document_never_had_reads_as_source_empty(
    fixture_ledger: object, construct: str
) -> None:
    """ "None offered" is a different fact from "all of them lost", and a report
    that cannot say the first one cannot be believed about the second."""
    row = _row(fixture_ledger, construct)
    assert row.offered == 0  # type: ignore[attr-defined]
    # Present, at zero: the ledger looked. A missing key would read as "did not
    # look", which is the ambiguity this whole module exists to remove.
    assert row.instances == {Disposition.SOURCE_EMPTY: 0}  # type: ignore[attr-defined]


def test_the_fixtures_books_balance(fixture_ledger: object) -> None:
    fixture_ledger.conservation().check()  # type: ignore[attr-defined]
    fixture_ledger.entry_conservation().check()  # type: ignore[attr-defined]


# --- the instrument measures the record, not a table of beliefs ---------------


def test_an_author_the_record_DOES_carry_reads_as_parsed(tmp_path: Path) -> None:
    """The proof that ``unsupported`` above is a measurement.

    One author, on an id root nothing else in this document shares. The parser
    produces a Practitioner whose provenance names that root, and the ledger
    reads STRUCTURALLY_PARSED — where the same XML read UNSUPPORTED before the
    extraction landed. Then the same document is measured against a record
    stripped of its practitioners and the verdict goes back: the ledger is
    grading the RECORD, not reciting a table of what the parser is believed to
    do, which is the only way it could grade a fix at all.
    """
    header = """
    <author>
      <time value="20230510150000-0500"/>
      <assignedAuthor>
        <id root="feedface-auth-0000-0000-000000000009"/>
        <assignedPerson><name><given>Quinn</given><family>Authorman</family></name></assignedPerson>
      </assignedAuthor>
    </author>
    """
    path = _write(tmp_path, header=header)
    record = parse_document(path)
    assert [p.name for p in record.practitioners] == ["Quinn Authorman"]
    author = record.practitioners[0]
    assert author.provenance is not None
    assert author.provenance.source_id == "feedface-auth-0000-0000-000000000009"
    assert (
        _sole(document_ledger(path, record), "participation:author")
        is Disposition.STRUCTURALLY_PARSED
    )

    record.practitioners = []
    assert _sole(document_ledger(path, record), "participation:author") is Disposition.UNSUPPORTED


def test_an_id_two_constructs_share_credits_neither(tmp_path: Path) -> None:
    """One organization OID on the author and on the custodian is ordinary
    C-CDA. Crediting both from one object's provenance would turn a single
    parsed fact into a header that "survived", so a shared root links nothing
    and is counted as the blind spot it is."""
    shared = "feedface-orga-0000-0000-000000000001"
    header = f"""
    <author><assignedAuthor><id root="{shared}"/></assignedAuthor></author>
    <custodian><assignedCustodian><representedCustodianOrganization>
      <id root="{shared}"/>
    </representedCustodianOrganization></assignedCustodian></custodian>
    """
    path = _write(tmp_path, header=header)
    record = parse_document(path)
    record.practitioners.append(
        Practitioner(provenance=Provenance(source_system="ccda", source_id=shared))
    )
    ledger = document_ledger(path, record)
    assert _sole(ledger, "participation:author") is Disposition.UNSUPPORTED
    assert _row(ledger, "participation:author").unlinkable == 1  # type: ignore[attr-defined]
    assert _row(ledger, "participation:custodian").unlinkable == 1  # type: ignore[attr-defined]


# --- evidence for the constructs CDA leaves without an id ---------------------

#: An author that is a system rather than a person. CDA R2's ``Device`` has no
#: ``<id>``, so the only thing this construct can be recognised by is what it
#: says it is.
_DEVICE_AUTHOR = """
    <author>
      <time value="20230510150000-0500"/>
      <assignedAuthor>
        <id root="feedface-auth-0000-0000-000000000021"/>
        <assignedAuthoringDevice>
          <manufacturerModelName>Synthetic EHR</manufacturerModelName>
          <softwareName>Synthetic EHR Export</softwareName>
        </assignedAuthoringDevice>
      </assignedAuthor>
    </author>
"""

#: A spouse who supplied the history. ``relatedEntity`` has no ``<id>`` either.
_INFORMANT = """
    <informant><relatedEntity classCode="PRS">
      <code code="SPS" displayName="spouse" codeSystem="2.16.840.1.113883.5.111"/>
      <relatedPerson><name><given>Boris</given><family>Sample</family></name></relatedPerson>
    </relatedEntity></informant>
"""


@pytest.mark.parametrize(
    ("construct", "header"),
    [
        ("participation:assignedAuthoringDevice", _DEVICE_AUTHOR),
        ("participation:informant", _INFORMANT),
    ],
)
def test_an_actor_cda_gives_no_id_is_credited_on_what_it_states(
    tmp_path: Path, construct: str, header: str
) -> None:
    """The half of the blind spot that was schema, not adapter.

    Neither of these can ever carry an ``<id root>``, so for as long as an id
    was the only evidence admitted, both were reported lost in every document
    that had one — a permanent under-reading of the parser rather than a finding
    about it. The record states what the document stated, and that is the
    evidence. Then the same document is measured against a record with its
    actors removed and the verdict goes back: still the RECORD being graded, not
    a table of what the parser is believed to do.
    """
    path = _write(tmp_path, header=header)
    record = parse_document(path)
    ledger = document_ledger(path, record)
    assert _sole(ledger, construct) is Disposition.STRUCTURALLY_PARSED
    assert _row(ledger, construct).unlinkable == 0  # type: ignore[attr-defined]

    record.practitioners = []
    stripped = document_ledger(path, record)
    assert _sole(stripped, construct) is Disposition.UNSUPPORTED
    assert _row(stripped, construct).unlinkable == 1  # type: ignore[attr-defined]


def test_an_actor_that_states_nothing_still_cannot_be_credited(tmp_path: Path) -> None:
    """The other half, and the reason the first half is not a loophole.

    An informant with no name, no number and no relationship states nothing for
    a record to state back, so nothing about it can be proved and it stays in
    the blind spot — even though the record here does hold an actor that states
    just as little. Matching two empty statements would make absence of evidence
    into evidence, which is the one direction this instrument may not fail in.
    """
    path = _write(tmp_path, header='<informant><relatedEntity classCode="PRS"/></informant>')
    record = parse_document(path)
    record.practitioners.append(
        Practitioner(provenance=Provenance(source_system="ccda", source_id=None))
    )
    ledger = document_ledger(path, record)
    assert _sole(ledger, "participation:informant") is Disposition.UNSUPPORTED
    assert _row(ledger, "participation:informant").unlinkable == 1  # type: ignore[attr-defined]


@pytest.mark.parametrize("recorded", ["synthetic ehr export", " Synthetic EHR Export "])
def test_an_actor_the_record_spells_differently_is_not_credited(
    tmp_path: Path, recorded: str
) -> None:
    """Content evidence is worth having only while it is exact.

    A comparison that folded case or trimmed padding would credit a device the
    record does not actually carry — and every loosening of it moves the reading
    in the flattering direction, where an instrument built to detect loss can
    afford to be wrong least.
    """
    path = _write(tmp_path, header=_DEVICE_AUTHOR)
    record = parse_document(path)
    record.practitioners[0].extensions["ccda:softwareName"] = recorded
    ledger = document_ledger(path, record)
    assert _sole(ledger, "participation:assignedAuthoringDevice") is Disposition.UNSUPPORTED
    assert _row(ledger, "participation:assignedAuthoringDevice").unlinkable == 1  # type: ignore[attr-defined]


def test_two_identical_actors_and_one_object_credit_one_parse(tmp_path: Path) -> None:
    """Two obligations and one object is one parse and one loss.

    The document names the same spouse twice, and the record kept one of them.
    Crediting both from the single object would report a document that lost half
    its informants as one that lost none — the arithmetic that makes a loss
    detector agree with whatever it is shown.
    """
    path = _write(tmp_path, header=_INFORMANT * 2)
    record = parse_document(path)
    assert len(record.practitioners) == 2
    del record.practitioners[1]
    row = _row(document_ledger(path, record), "participation:informant")
    assert row.counted(Disposition.STRUCTURALLY_PARSED) == 1  # type: ignore[attr-defined]
    assert row.counted(Disposition.UNSUPPORTED) == 1  # type: ignore[attr-defined]
    assert row.unlinkable == 1  # type: ignore[attr-defined]


def test_a_shared_id_root_is_still_refused_where_content_would_match(tmp_path: Path) -> None:
    """Content evidence answers only where an id was never possible.

    An ``informant`` playing an ``assignedEntity`` DOES carry an id, and this
    one shares its root with the author — the ordinary C-CDA ambiguity the
    ledger refuses. Both actors are in the record and both would match by
    content, so admitting a class CDA does give an id would quietly convert the
    ambiguity refusal into a credit and no other test would notice.
    """
    shared = "feedface-shar-0000-0000-000000000001"
    name = "<name><given>Robin</given><family>Sample</family></name>"
    person = f"<assignedPerson>{name}</assignedPerson>"
    header = f"""
    <author><assignedAuthor><id root="{shared}"/>{person}</assignedAuthor></author>
    <informant><assignedEntity><id root="{shared}"/>{person}</assignedEntity></informant>
    """
    path = _write(tmp_path, header=header)
    ledger = document_ledger(path)
    for construct in ("participation:author", "participation:informant"):
        assert _sole(ledger, construct) is Disposition.UNSUPPORTED
        assert _row(ledger, construct).unlinkable == 1  # type: ignore[attr-defined]


def test_only_the_classes_cda_leaves_without_an_id_are_admitted() -> None:
    """The scope is a list, so widening it is an edit somebody has to make.

    "This construct had no id in this document" is a fact about one document;
    "this construct can never have an id" is a fact about CDA, and only the
    second may relax the evidence rule. Inferring the difference at runtime
    would let a document decide how it is graded.
    """
    assert set(ID_LESS_CONSTRUCTS) == {"assignedAuthoringDevice", "informant"}
    assert set(ID_LESS_CONSTRUCTS) <= set(PARTICIPATION_PATHS)


def test_a_content_credited_reading_still_names_nobody(tmp_path: Path) -> None:
    """The comparison happens in memory and none of it may travel.

    A device's software name is the vendor's, but a spouse's name and number are
    the patient's household, and they are now read on every document with an
    informant in it. The report is the thing that leaves, so it is the thing
    that is checked.
    """
    path = _write(tmp_path, header=_DEVICE_AUTHOR + _INFORMANT)
    report = aggregate([document_ledger(path)]).as_report()
    assert_emittable(report)
    flat = repr(report)
    for value in ("Synthetic", "Export", "Boris", "Sample", "spouse", "SPS"):
        assert value not in flat


# --- what the parser's own traversal cannot see -------------------------------


def test_a_subsection_the_parser_never_reaches_is_still_counted(tmp_path: Path) -> None:
    """C-CDA nests subsections one ``<component>`` deeper, and the parser's
    section XPath stops at depth one. A ledger that borrowed that XPath would
    have reported a document with no subsection at all — it would agree with the
    parser about everything, including the part the parser cannot see."""
    body = """
    <component><section>
      <code code="11450-4" codeSystem="2.16.840.1.113883.6.1"/>
      <title>Problems</title>
      <text><paragraph>Hypertension.</paragraph></text>
      <component><section>
        <code code="10154-3" codeSystem="2.16.840.1.113883.6.1"/>
        <title>Chief Complaint</title>
        <text><paragraph>Blood pressure follow-up.</paragraph></text>
      </section></component>
    </section></component>
    """
    ledger = document_ledger(_write(tmp_path, body=body))
    assert _sole(ledger, "section:10154-3") is Disposition.UNSUPPORTED
    assert _sole(ledger, "section:11450-4") is Disposition.NARRATIVE_PRESERVED


# --- the empty / present distinction -----------------------------------------


def test_a_section_with_nothing_in_it_is_source_empty_not_a_loss(tmp_path: Path) -> None:
    body = """
    <component><section>
      <code code="48768-6" codeSystem="2.16.840.1.113883.6.1"/>
    </section></component>
    """
    assert _sole(document_ledger(_write(tmp_path, body=body)), "section:48768-6") is (
        Disposition.SOURCE_EMPTY
    )


def test_an_entry_with_no_statement_in_it_is_source_empty(tmp_path: Path) -> None:
    body = """
    <component><section>
      <code code="48768-6" codeSystem="2.16.840.1.113883.6.1"/>
      <title>Payers</title>
      <text><paragraph>Synthetic Mutual.</paragraph></text>
      <entry/>
    </section></component>
    """
    ledger = document_ledger(_write(tmp_path, body=body))
    assert _row(ledger, "section:48768-6").entries == {Disposition.SOURCE_EMPTY: 1}  # type: ignore[attr-defined]


def test_a_stored_title_does_not_rescue_the_entries_underneath_it(tmp_path: Path) -> None:
    """The Payers refusal, kept — and sharpened by #314.

    A section with entries and no ``<text>`` stores ``{"title": ..., "text":
    None}``, and the word "Payers" is not a recovery of the coverage that was
    in the entries. Since #314 the parser ALSO stores the entries themselves,
    and crediting them against their own bytes is not the flattering
    arithmetic this test refuses — it is a real preservation. The refusal is
    proved by deletion: with the stored copies gone and only the title left,
    the entries fall straight back to unsupported, exactly as before. The
    title, alone, still rescues nothing.
    """
    body = """
    <component><section>
      <code code="48768-6" codeSystem="2.16.840.1.113883.6.1"/>
      <title>Payers</title>
      <entry><observation classCode="OBS" moodCode="EVN">
        <id root="feedface-genr-0000-0000-000000000001"/>
        <code code="75326-9" codeSystem="2.16.840.1.113883.6.1"/>
      </observation></entry>
    </section></component>
    """
    path = _write(tmp_path, body=body)
    record = parse_document(path)
    ledger = document_ledger(path, record)
    assert _row(ledger, "section:48768-6").entries == {  # type: ignore[attr-defined]
        Disposition.NARRATIVE_PRESERVED: 1
    }, "the entry's own stored bytes are the evidence"

    del record.patient.extensions["ccda:entries:48768-6"]
    stripped = document_ledger(path, record)
    assert _row(stripped, "section:48768-6").entries == {  # type: ignore[attr-defined]
        Disposition.UNSUPPORTED: 1
    }, "the title alone rescues nothing"
    # The section itself did keep something — its title — and says so.
    assert _sole(stripped, "section:48768-6") is Disposition.NARRATIVE_PRESERVED


def test_two_sections_sharing_a_code_are_two_obligations(tmp_path: Path) -> None:
    """Problems (Active) and Problems (Resolved) are both 11450-4, and the
    parser keeps the second at ``...#2``. Two sections must be two rows' worth
    of accounting, not one narrative answering for both."""
    body = """
    <component><section>
      <code code="11450-4" codeSystem="2.16.840.1.113883.6.1"/>
      <title>Problems (Active)</title>
      <text><paragraph>Hypertension.</paragraph></text>
    </section></component>
    <component><section>
      <code code="11450-4" codeSystem="2.16.840.1.113883.6.1"/>
      <title>Problems (Resolved)</title>
      <text><paragraph>Appendicitis.</paragraph></text>
    </section></component>
    """
    ledger = document_ledger(_write(tmp_path, body=body))
    assert _row(ledger, "section:11450-4").instances == {Disposition.NARRATIVE_PRESERVED: 2}  # type: ignore[attr-defined]


def test_an_unstructured_document_arrives_with_its_chart(tmp_path: Path) -> None:
    """The shape that used to parse perfectly and yield a chart with nothing on
    it: a scanned referral whose whole clinical content is one embedded
    artifact. It leaves with that artifact, and the row says so."""
    path = tmp_path / "scan.xml"
    path.write_text(
        _DOCUMENT.replace(
            "<component><structuredBody>{body}</structuredBody></component>",
            '<component><nonXMLBody><text mediaType="application/pdf" '
            'representation="B64">JVBERi0xLjQK</text></nonXMLBody></component>',
        ).format(header=""),
        encoding="utf-8",
    )
    ledger = document_ledger(path)
    assert _sole(ledger, "body:nonXMLBody") is Disposition.STRUCTURALLY_PARSED
    record = parse_document(path)
    # Still no coded content — there was none in the source — which is exactly
    # why the one artifact has to arrive.
    assert len(record.documents) == 1
    assert not record.encounters and not record.conditions


# --- the books, and what happens when they do not balance ---------------------


def test_a_section_the_row_builder_skipped_stops_the_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression this conservation exists for: a classifier that quietly
    drops a construct still produces a tidy report, and every number in it is
    computed over the survivors."""
    from anastomosis.sources.ccda import ledger as module

    body = """
    <component><section>
      <code code="11450-4" codeSystem="2.16.840.1.113883.6.1"/>
      <title>Problems</title>
      <text><paragraph>Hypertension.</paragraph></text>
    </section></component>
    """
    path = _write(tmp_path, body=body)
    document_ledger(path)  # balanced as written

    offered = module._sections
    monkeypatch.setattr(module, "_sections", lambda root: offered(root)[1:], raising=True)
    with pytest.raises(ConservationError, match="ccda xml -> canonical"):
        document_ledger(path)


def test_an_entry_that_reached_no_column_stops_the_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from anastomosis.sources.ccda import ledger as module

    body = """
    <component><section>
      <code code="11450-4" codeSystem="2.16.840.1.113883.6.1"/>
      <title>Problems</title>
      <entry><act classCode="ACT" moodCode="EVN">
        <id root="feedface-prob-0000-0000-000000000001"/>
      </act></entry>
    </section></component>
    """
    path = _write(tmp_path, body=body)
    document_ledger(path)

    monkeypatch.setattr(
        module, "_entry_dispositions", lambda *args, **kwargs: ({}, 0), raising=True
    )
    with pytest.raises(ConservationError, match="1 entry\\(s\\) went in"):
        document_ledger(path)


def test_aggregating_two_documents_keeps_both_sets_of_books(tmp_path: Path) -> None:
    body = """
    <component><section>
      <code code="11450-4" codeSystem="2.16.840.1.113883.6.1"/>
      <title>Problems</title>
      <text><paragraph>Hypertension.</paragraph></text>
    </section></component>
    """
    first = _write(tmp_path, body=body)
    second = tmp_path / "other.xml"
    second.write_text(first.read_text(encoding="utf-8"), encoding="utf-8")
    corpus = aggregate([document_ledger(first), document_ledger(second)])
    assert corpus.documents == 2
    assert corpus.constructs_offered == 2 * document_ledger(first).constructs_offered
    corpus.check()


# --- nothing patient-derived may leave ---------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "Cora Specimen",
        "cora.specimen@example.com",
        "901-65-4329",
        "Persistent cough for three weeks",
        "Specimen_Cora_2023-05-10.xml",
        "section:Cora",
        "2023-05-10",
        3.5,
        None,
    ],
)
def test_a_patient_value_cannot_leave_in_a_report(value: object) -> None:
    """The vocabulary IS the control: a name is not a construct name, a LOINC
    code, an OID or an integer, and those are all the whitelist admits."""
    with pytest.raises(ValueError, match="refusing to emit"):
        assert_emittable({"constructs": [value]})


def test_a_section_code_that_is_not_a_code_is_reported_as_nonstandard(tmp_path: Path) -> None:
    """A section's ``@code`` is under the document author's control, and a
    document is not obliged to put a LOINC code there. The COUNT still travels;
    whatever the author put in the attribute does not."""
    body = """
    <component><section>
      <code code="Cora Specimen chart" codeSystem="2.16.840.1.113883.6.1"/>
      <title>Chart</title>
      <text><paragraph>Notes.</paragraph></text>
    </section></component>
    """
    ledger = document_ledger(_write(tmp_path, body=body))
    assert _sole(ledger, "section:nonstandard") is Disposition.NARRATIVE_PRESERVED
    assert_emittable(aggregate([ledger]).as_report())


def test_a_real_corpus_report_carries_only_structure() -> None:
    report = aggregate([document_ledger(CCDA_FIXTURE)]).as_report()
    assert_emittable(report)
    flat = repr(report)
    for value in ("Cora", "Specimen", "901-65-4329", "cough", "feedface"):
        assert value not in flat
    assert not re.search(r"[A-Za-z]{2,}\s+[A-Za-z]{2,}", flat)  # no free text anywhere


# --- the places themselves ---------------------------------------------------
#
# These three tests are the reason #329 was worth doing. None of them can be
# written against the shape this module had before: spending lived inside two
# private methods on a six-field object that needed a parsed document and a
# built record to construct at all, so "can this place be asked twice" was a
# question you answered by reading, not by running.


def test_a_fact_answers_as_often_as_it_is_asked() -> None:
    """An id is true, not owed. Ten constructs carrying it all link."""
    facts = ledger._Facts(frozenset({"2.16.840.1.113883.19"}))
    assert [facts.holds("2.16.840.1.113883.19") for _ in range(10)] == [True] * 10
    assert [facts.any_of({"2.16.840.1.113883.19"}) for _ in range(10)] == [True] * 10


def test_a_fact_has_no_way_to_be_spent() -> None:
    """The guarantee is the type's, not the caller's discipline.

    Frozen dataclass over a ``frozenset``: there is no method that removes a
    member, and rebinding the field is an error rather than a surprise. A
    future edit cannot quietly turn an id into something that answers once.
    """
    facts = ledger._Facts(frozenset({"a"}))
    with pytest.raises(dataclasses.FrozenInstanceError):
        facts.held = frozenset()  # type: ignore[misc]
    assert not hasattr(facts, "take")
    assert not any(name for name in vars(type(facts)) if name in {"take", "spend", "pop", "remove"})


def test_a_pool_yields_no_more_than_it_holds_however_it_is_asked() -> None:
    """Conservation, provable on the place alone — no document, no record.

    Both pools decrement before answering, so the count of yeses can never
    exceed what went in. That is what stops two identical informants against
    one object crediting two parses.
    """
    narrative = ledger._KeyedPool(Counter({("Allergies", "none known"): 2}))
    draws = [narrative.take(("Allergies", "none known")) for _ in range(5)]
    assert draws == [True, True, False, False, False]

    one = Practitioner(family_name="Reyes")
    objects = ledger._MatchedPool([one, Practitioner(family_name="Okafor")])
    hits = [objects.take(lambda obj: obj is one) for _ in range(4)]
    assert hits == [True, False, False, False]


def test_a_pool_cannot_be_consulted_without_spending_it() -> None:
    """There is deliberately no way to look without taking.

    A caller that could ask without spending could write a predicate crediting
    one stored thing twice, which is the arithmetic this ledger exists to
    refuse. Taking is the entire query surface of both pools — ``take_all``
    joined it for a caller that needs several claims to answer one question,
    and it obeys the same law: a yes costs, and a no costs nothing precisely
    because it credited nothing.
    """
    places = (ledger._KeyedPool(Counter()), ledger._MatchedPool([]), ledger._Anchors({}))
    for pool in places:
        surface = {name for name in vars(type(pool)) if not name.startswith("_")}
        # ``demand`` is allowed to ONE place, and only because what it reads is
        # the static address map. Allowing it everywhere would let a counted
        # pool grow a method that reports what is still unspent, which is the
        # arithmetic all of this exists to refuse.
        allowed = {"take", "take_all"} | (
            {"demand"} if isinstance(pool, ledger._Anchors) else set()
        )
        assert surface <= allowed, f"{type(pool).__name__} offers more than taking"

    # ``demand`` is the one reader, and it reads the ADDRESS map: it says how
    # much an entry is asking for so a section can serve its claims from both
    # ends, and it can never report what is still unspent.
    addressed = ledger._Anchors({"row": frozenset({"cell"}), "cell": frozenset({"cell"})})
    assert addressed.demand(["row"]) == addressed.demand(["cell"]) == 1
    assert addressed.demand(["row", "cell"]) == 1, "two addresses over one cell asked for two"
    assert addressed.demand(["gone"]) == 0
    assert addressed.take(["row"]) is True
    assert addressed.demand(["row"]) == 1, "demand fell when the claim was spent"
    assert addressed.take(["cell"]) is False, "one cell answered two addresses"

    stocked = ledger._KeyedPool(Counter({"a": 1, "b": 1}))
    assert stocked.take_all(["a", "b"]) is True
    assert stocked.take("a") is False, "a successful take_all did not spend"

    partial = ledger._KeyedPool(Counter({"a": 1}))
    assert partial.take_all(["a", "b"]) is False
    assert partial.take("a") is True, "a failed take_all spent something anyway"

    twice = ledger._KeyedPool(Counter({"a": 1}))
    assert twice.take_all(["a", "a"]) is False, "one stored item answered for two claims"


# --- the sixth question: entries preserved verbatim (#314) --------------------

_TEXTLESS_PROBLEMS = """
<component><section>
  <templateId root="2.16.840.1.113883.10.20.22.2.5.1"/>
  <code code="11450-4" codeSystem="2.16.840.1.113883.6.1"/>
  <entry>
    <act classCode="ACT" moodCode="EVN">
      <id root="feedface-prob-0000-0000-000000000031"/>
      <code code="CONC" codeSystem="2.16.840.1.113883.5.6"/>
    </act>
  </entry>
  <entry>
    <act classCode="ACT" moodCode="EVN">
      <id root="feedface-prob-0000-0000-000000000032"/>
      <code code="CONC" codeSystem="2.16.840.1.113883.5.6"/>
    </act>
  </entry>
</section></component>
"""


def test_a_textless_sections_entries_ARE_preserved_and_read_as_such(tmp_path: Path) -> None:
    """The shape #314 names, proved on what the record actually holds.

    A section with entries and no <text> has no narrative for its entries to
    be preserved BY. The parser now keeps the entries themselves, verbatim,
    and the ledger credits each entry against its own stored bytes — spending
    them, so the credit is exactly as large as the preservation.
    """
    path = _write(tmp_path, body=_TEXTLESS_PROBLEMS)
    record = parse_document(path)
    parked = [k for k in record.patient.extensions if k.startswith("ccda:entries:")]
    assert parked == ["ccda:entries:11450-4"]
    stored = record.patient.extensions["ccda:entries:11450-4"]
    assert isinstance(stored, list) and len(stored) == 2
    assert all(item.lstrip().startswith("<entry") for item in stored)

    row = _row(document_ledger(path, record), "section:11450-4")
    assert row.entries == {Disposition.NARRATIVE_PRESERVED: 2}  # type: ignore[attr-defined]
    assert _sole(document_ledger(path, record), "section:11450-4") is (
        Disposition.NARRATIVE_PRESERVED
    )


def test_the_same_entries_unparked_read_unsupported(tmp_path: Path) -> None:
    """Proved twice: take the stored copies away and the credit disappears.

    The disposition follows the EVIDENCE, not the document's shape — a ledger
    that credited these entries with the extensions key deleted would be
    reporting a preservation nobody performed.
    """
    path = _write(tmp_path, body=_TEXTLESS_PROBLEMS)
    record = parse_document(path)
    del record.patient.extensions["ccda:entries:11450-4"]
    row = _row(document_ledger(path, record), "section:11450-4")
    assert row.entries == {Disposition.UNSUPPORTED: 2}  # type: ignore[attr-defined]
    assert _sole(document_ledger(path, record), "section:11450-4") is Disposition.UNSUPPORTED


def test_one_stored_copy_credits_one_entry_not_two(tmp_path: Path) -> None:
    """Spending, at the entry grain: two identical entries, one stored copy.

    The pool is a multiset of exact strings. Halving the stored list must
    halve the credit — a pool that answered twice for one copy would credit an
    entry that was dropped, which is the arithmetic this ledger refuses.
    """
    entry = """
    <entry>
      <observation classCode="OBS" moodCode="EVN">
        <code code="75326-9" codeSystem="2.16.840.1.113883.6.1"/>
      </observation>
    </entry>
    """
    body = f"""
    <component><section>
      <code code="11450-4" codeSystem="2.16.840.1.113883.6.1"/>
      {entry}{entry}
    </section></component>
    """
    path = _write(tmp_path, body=body)
    record = parse_document(path)
    stored = record.patient.extensions["ccda:entries:11450-4"]
    assert len(stored) == 2, "two identical entries are two stored copies"
    record.patient.extensions["ccda:entries:11450-4"] = stored[:1]
    row = _row(document_ledger(path, record), "section:11450-4")
    assert row.entries == {  # type: ignore[attr-defined]
        Disposition.NARRATIVE_PRESERVED: 1,
        Disposition.UNSUPPORTED: 1,
    }


@pytest.mark.parametrize("prose", ["", "<text>Prose that cites nothing.</text>"])
def test_an_entry_reads_the_same_whether_or_not_its_section_renders_prose(
    tmp_path: Path, prose: str
) -> None:
    """One entry, two documents, one reading.

    The same coded observation this adapter has no dispatch for used to be
    preserved or dropped by nothing but whether the section above it happened to
    carry a sentence — and a sentence about a section is not a copy of the
    entries beneath it, which is the finding this ledger already applies
    everywhere it counts. Both halves park now, so both say the same thing about
    the same entry.
    """
    body = f"""
    <component><section>
      <code code="18776-5" codeSystem="2.16.840.1.113883.6.1"/>
      <title>Plan of Treatment</title>
      {prose}
      <entry><observation classCode="OBS" moodCode="EVN">
        <id root="feedface-plan-0000-0000-000000000911"/>
        <code code="75326-9" codeSystem="2.16.840.1.113883.6.1"/>
        <value displayName="No current problems"/>
      </observation></entry>
    </section></component>
    """
    path = _write(tmp_path, body=body)
    record = parse_document(path)

    assert record.patient.extensions["ccda:entries:18776-5"]
    row = _row(document_ledger(path, record), "section:18776-5")
    assert row.entries == {Disposition.NARRATIVE_PRESERVED: 1}  # type: ignore[attr-defined]


def test_a_sections_prose_does_not_answer_for_an_entry_it_never_states(
    tmp_path: Path,
) -> None:
    """The section keeps its narrative; what answers for the entry is its bytes.

    This test used to assert that the prose credited the entry, and the
    assumption underneath it was that a section's ``<text>`` is a copy of what
    its entries say. C-CDA makes no such promise, and the corpus this repo
    generates disproves it in its own documents: a Plan of Treatment whose prose
    reads "Continue lisinopril and recheck blood pressure in three months"
    carries an entry stating the coded value "No current problems".

    So the section's narrative answers for the SECTION, and the entry is asked
    at its own address. It has an answer there now — the parser parks every
    section's entries verbatim, prose or no prose — and the second half of this
    test is the same document with that copy taken away: the prose is still
    sitting there, and it still credits nothing.
    """
    body = """
    <component><section>
      <code code="11450-4" codeSystem="2.16.840.1.113883.6.1"/>
      <title>Problems</title>
      <text>Prose about the section that states nothing the entry states.</text>
      <entry><act classCode="ACT" moodCode="EVN">
        <code code="CONC" codeSystem="2.16.840.1.113883.5.6"/>
      </act></entry>
    </section></component>
    """
    path = _write(tmp_path, body=body)
    record = parse_document(path)
    assert [k for k in record.patient.extensions if k.startswith("ccda:entries:")]
    row = _row(document_ledger(path, record), "section:11450-4")
    assert row.entries == {Disposition.NARRATIVE_PRESERVED: 1}  # type: ignore[attr-defined]
    assert row.instances == {Disposition.NARRATIVE_PRESERVED: 1}  # type: ignore[attr-defined]

    stripped = _row(document_ledger(path, _unparked(parse_document(path))), "section:11450-4")
    assert stripped.entries == {Disposition.UNSUPPORTED: 1}, (  # type: ignore[attr-defined]
        "the prose credited an entry it states nothing of"
    )
    assert stripped.instances == {Disposition.NARRATIVE_PRESERVED: 1}  # type: ignore[attr-defined]


# --- the reading a physician gets ---------------------------------------------
#
# #315: the instrument shipped in every build and nothing ran it. Now a load
# ends with these sentences, so they are held to the same two-directional
# standard as the rows they summarize: right about what arrived, right about
# what did not, and incapable of carrying a document's own words.


def test_the_reading_speaks_the_fixture_in_chart_vocabulary(fixture_ledger: object) -> None:
    """The reference document, said the way the issue asked for it: sections
    and entries credited, the three shared-root participations reported as
    dropped, and the blind spot published beside them rather than buried."""
    lines = physician_reading(aggregate([fixture_ledger]))  # type: ignore[list-item]
    assert lines == (
        "Across 1 document the source offered 10 sections: 9 became data, "
        "1 kept as text only, 0 not credited as data, 0 empty in the source.",
        "Those sections carried 13 coded entries: 13 became data, 0 kept as text only.",
        "People and devices around the chart — its authors, informants, performers — "
        "were named 3 times: 0 became data, 3 not credited as data.",
        "3 constructs impossible to check — no identifier this reading can follow — "
        "never credited as data, so the loss above can only be overstated, not understated.",
    )


def test_the_readings_numbers_add_back_up(fixture_ledger: object) -> None:
    """Every accounting sentence's columns sum to the total it opened with.

    The same conservation the rows are held to, asserted on the prose — a
    sentence that dropped a column for reading smoothly would break here."""
    sections, entries, people, _blind = physician_reading(
        aggregate([fixture_ledger])  # type: ignore[list-item]
    )
    for line, leading in ((sections, 2), (entries, 1), (people, 1)):
        numbers = [int(n) for n in re.findall(r"\d+", line)]
        offered = numbers[leading - 1]
        assert offered == sum(numbers[leading:]), line


def test_a_reading_carries_no_document_value(tmp_path: Path) -> None:
    """No name, id, code, or title from the document can reach a sentence.

    Proved on a document that states all four, not argued from the composer's
    shape: the reading is the one artifact written to be pasted somewhere."""
    body = """
    <component><section>
      <code code="11450-4" codeSystem="2.16.840.1.113883.6.1"/>
      <title>Problems For Synthia</title>
      <text>Synthia Probe still has hypertension.</text>
    </section></component>
    """
    header = """
    <author><time value="20230510"/><assignedAuthor>
      <id root="feedface-auth-0000-0000-000000000001"/>
      <assignedPerson><name><given>Quinn</given><family>Authorman</family></name></assignedPerson>
    </assignedAuthor></author>
    """
    path = _write(tmp_path, body=body, header=header)
    blob = " ".join(physician_reading(aggregate([document_ledger(path)])))
    for stated in ("Synthia", "Probe", "Quinn", "Authorman", "feedface", "11450-4", "hypertension"):
        assert stated not in blob, f"reading leaked {stated!r}"


def test_an_unstructured_document_reads_as_its_body(tmp_path: Path) -> None:
    """The scanned-referral shape gets the sentence that matters: no sections
    to speak of, and the one body it does have accounted for. The blind-spot
    line flips to its good-news form — said, not omitted — because this
    document's constructs could all be checked."""
    path = tmp_path / "scan.xml"
    path.write_text(
        _DOCUMENT.replace(
            "<component><structuredBody>{body}</structuredBody></component>",
            '<component><nonXMLBody><text mediaType="application/pdf" '
            'representation="B64">JVBERi0xLjQK</text></nonXMLBody></component>',
        ).format(header=""),
        encoding="utf-8",
    )
    lines = physician_reading(aggregate([document_ledger(path)]))
    assert lines[0] == "Across 1 document the source offered no sections."
    assert (
        "The whole chart travelled as a scanned or non-XML body 1 time: "
        "1 became data, 0 not credited as data." in lines
    )
    assert lines[-1] == "Every construct the source offered could be checked one way or the other."


def test_a_section_without_entries_says_so(tmp_path: Path) -> None:
    """ "Carried no coded entries" is a statement, not a skipped line — the
    reading has to be believable about absences for the reason the rows are."""
    body = """
    <component><section>
      <code code="10164-2" codeSystem="2.16.840.1.113883.6.1"/>
      <title>History</title>
      <text>Story in prose.</text>
    </section></component>
    """
    lines = physician_reading(aggregate([document_ledger(_write(tmp_path, body=body))]))
    assert "Those sections carried no coded entries." in lines


_TWO_AUTHORS = """
    <author><time value="20230510"/><assignedAuthor>
      <id root="{first}"/>
      <assignedPerson><name><given>Quinn</given><family>Authorman</family></name></assignedPerson>
    </assignedAuthor></author>
    <author><time value="20230510"/><assignedAuthor>
      <id root="{second}"/>
      <assignedPerson><name><given>Rey</given><family>Scribener</family></name></assignedPerson>
    </assignedAuthor></author>
"""


def test_a_shared_id_root_cannot_put_a_cause_in_the_verdict(tmp_path: Path) -> None:
    """The review's blocker, pinned: the record CARRIES both authors either
    way, and the only difference between these two documents is whether their
    id roots collide. A collision may move the count from credited to
    uncredited — that is the documented bias — but it must never make the
    sentence assert a loss with a cause, because the "loss" here is the
    instrument's blindness, not the adapter's slot."""
    verdicts = {}
    for label, second_root in (
        ("shared", "feedface-auth-0000-0000-000000000001"),
        ("distinct", "feedface-auth-0000-0000-000000000002"),
    ):
        path = tmp_path / f"{label}.xml"
        path.write_text(
            _DOCUMENT.format(
                body="",
                header=_TWO_AUTHORS.format(
                    first="feedface-auth-0000-0000-000000000001", second=second_root
                ),
            ),
            encoding="utf-8",
        )
        record = parse_document(path)
        assert len(record.practitioners) == 2, "the adapter has a slot, and used it"
        verdicts[label] = physician_reading(aggregate([document_ledger(path, record)]))
    assert any("2 became data" in line for line in verdicts["distinct"])
    assert any("2 not credited as data" in line for line in verdicts["shared"])
    assert any("2 constructs impossible to check" in line for line in verdicts["shared"])
    for word in ("dropped", "no place"):
        assert word not in " ".join(verdicts["shared"]), f"a cause ({word!r}) was asserted"


def test_three_bodies_in_one_document_are_not_three_documents(tmp_path: Path) -> None:
    """A schema-invalid document with three nonXMLBody components — exactly the
    input class this instrument is hardened for — must not be read back as
    three documents two lines under "Across 1 document"."""
    path = tmp_path / "scan.xml"
    body_xml = (
        '<component><nonXMLBody><text mediaType="application/pdf" '
        'representation="B64">JVBERi0xLjQK</text></nonXMLBody></component>' * 3
    )
    path.write_text(
        _DOCUMENT.replace(
            "<component><structuredBody>{body}</structuredBody></component>", body_xml
        ).format(header=""),
        encoding="utf-8",
    )
    lines = physician_reading(aggregate([document_ledger(path)]))
    assert lines[0] == "Across 1 document the source offered no sections."
    body_line = next(line for line in lines if "non-XML body" in line)
    assert body_line.startswith("The whole chart travelled as a scanned or non-XML body 3 times:")
    assert "3 documents" not in " ".join(lines)


# --- what a positive disposition has to be backed by --------------------------
#
# Three ways this ledger awarded credit it had not earned, each found by an
# adversarial probe that mutated a parsed record and re-graded it. The shape of
# every test below is the same: prove the intact case is still CREDITED, then
# take away exactly the evidence the credit was supposed to rest on and prove
# the verdict changes. A guard that only ever says no is not a guard.


_SERVICE_EVENT_HEADER = """
  <documentationOf><serviceEvent classCode="PCPR">
    <id root="feedface-serv-0000-0000-000000000901"/>
    <code code="PCPR" displayName="Synthetic care period"/>
    <effectiveTime><low value="20200101"/><high value="20201231"/></effectiveTime>
  </serviceEvent></documentationOf>
"""


def test_an_empty_parked_payload_is_not_a_preserved_participation(tmp_path: Path) -> None:
    """A namespace key is the cheapest thing in a record to be right about.

    ``parked_under`` asked whether ``ccda:serviceEvent`` existed, so an adapter
    that wrote the key and stored nothing under it scored exactly what one that
    stored the event's facts scored. A regression that cleared the payload was
    reported as preservation. The payload is what is read now.

    No performer on this event on purpose: a nested practitioner would give
    ``links`` an id of its own to credit the whole wrapper by, and the question
    here is what the PARKED evidence proves by itself.
    """
    path = _write(tmp_path, header=_SERVICE_EVENT_HEADER)
    record = parse_document(path)
    construct = "participation:serviceEvent"

    assert record.patient.extensions["ccda:serviceEvent"], "the probe needs a stored payload"
    assert _sole(document_ledger(path, record), construct) is Disposition.NARRATIVE_PRESERVED

    emptied = record.model_copy(deep=True)
    emptied.patient.extensions["ccda:serviceEvent"] = []
    assert _sole(document_ledger(path, emptied), construct) is Disposition.UNSUPPORTED

    removed = record.model_copy(deep=True)
    del removed.patient.extensions["ccda:serviceEvent"]
    assert _sole(document_ledger(path, removed), construct) is Disposition.UNSUPPORTED


def test_one_parked_item_answers_for_one_offered_construct(tmp_path: Path) -> None:
    """Two events against one stored item is one preserved and one lost.

    The claim is spent, like every other pool in this file. A place that could
    be asked without spending would credit the same stored fact twice, which is
    the arithmetic this ledger exists to refuse.
    """
    path = _write(tmp_path, header=_SERVICE_EVENT_HEADER * 2)
    record = parse_document(path)
    row = _row(document_ledger(path, record), "participation:serviceEvent")

    assert row.offered == 2  # type: ignore[attr-defined]
    record.patient.extensions["ccda:serviceEvent"] = record.patient.extensions["ccda:serviceEvent"][
        :1
    ]
    starved = _row(document_ledger(path, record), "participation:serviceEvent")
    assert starved.instances[Disposition.NARRATIVE_PRESERVED] == 1  # type: ignore[attr-defined]
    assert starved.instances[Disposition.UNSUPPORTED] == 1  # type: ignore[attr-defined]


_TWO_MEASUREMENTS = """
  <component><section>
    <code code="30954-2" codeSystem="2.16.840.1.113883.6.1"/>
    <title>Results</title>
    <text>Prose about the panel, which is not a copy of either measurement.</text>
    <entry><organizer classCode="BATTERY" moodCode="EVN">
      <templateId root="2.16.840.1.113883.10.20.22.4.1"/>
      <component><observation classCode="OBS" moodCode="EVN">
        <templateId root="2.16.840.1.113883.10.20.22.4.2"/>
        <id root="feedface-rslt-0000-0000-000000000901"/>
        <code code="2345-7" displayName="Synthetic measurement A"/>
        <value value="1" unit="u"/>
      </observation></component>
      <component><observation classCode="OBS" moodCode="EVN">
        <templateId root="2.16.840.1.113883.10.20.22.4.2"/>
        <id root="feedface-rslt-0000-0000-000000000902"/>
        <code code="2160-0" displayName="Synthetic measurement B"/>
        <value value="2" unit="u"/>
      </observation></component>
    </organizer></entry>
  </section></component>
"""


def test_a_sibling_lost_inside_one_entry_is_not_a_parsed_entry(tmp_path: Path) -> None:
    """One surviving id used to answer for every statement beside it.

    An ``<organizer>`` of two results kept its ``structurally_parsed`` verdict
    when one of the two was dropped, because ``links`` asked whether ANY id
    under the entry reached the record. Organizer entries routinely carry
    several results or vital signs, so a regression could drop any subset and
    the ledger would still certify the wrapper.

    What the entry falls back to is the verbatim copy of its own bytes, so the
    partial loss reads as preserved rather than as unsupported. The column this
    test forbids is the other one: a dropped measurement is not a parse,
    whatever else under the entry survived.
    """
    path = _write(tmp_path, body=_TWO_MEASUREMENTS)
    record = parse_document(path)
    construct = "section:30954-2"
    assert len(record.observations) == 2, "the probe needs both measurements parsed"

    intact = _row(document_ledger(path, record), construct)
    assert intact.entries == {Disposition.STRUCTURALLY_PARSED: 1}  # type: ignore[attr-defined]

    # Either sibling, not just the convenient one: whichever of the two goes,
    # the entry stops being a parsed entry.
    for keep in (slice(0, 1), slice(1, 2)):
        partial = record.model_copy(deep=True)
        partial.observations = record.observations[keep]
        row = _row(document_ledger(path, partial), construct)
        assert row.entries == {Disposition.NARRATIVE_PRESERVED: 1}, (  # type: ignore[attr-defined]
            f"{keep} still read as parsed"
        )

    total = record.model_copy(deep=True)
    total.observations = []
    row = _row(document_ledger(path, total), construct)
    assert row.entries == {Disposition.NARRATIVE_PRESERVED: 1}  # type: ignore[attr-defined]
    # And with the verbatim copy gone too, nothing is left to credit it.
    stripped = _unparked(total.model_copy(deep=True))
    row = _row(document_ledger(path, stripped), construct)
    assert row.entries == {Disposition.UNSUPPORTED: 1}  # type: ignore[attr-defined]


def test_a_sibling_lost_under_a_parked_entry_is_preserved_not_parsed(tmp_path: Path) -> None:
    """The same loss where the entry's own bytes ARE kept: still not parsed.

    A text-less section parks its entries verbatim, so a partially-lost entry
    there has real evidence behind it and reads narrative_preserved rather than
    unsupported. What it must not read is structurally_parsed — that is the
    claim a dropped measurement disproves, whatever else survived.
    """
    body = _TWO_MEASUREMENTS.replace(
        "<text>Prose about the panel, which is not a copy of either measurement.</text>", ""
    )
    path = _write(tmp_path, body=body)
    record = parse_document(path)
    partial = record.model_copy(deep=True)
    partial.observations = record.observations[:1]

    row = _row(document_ledger(path, partial), "section:30954-2")
    assert row.entries == {Disposition.NARRATIVE_PRESERVED: 1}  # type: ignore[attr-defined]


def test_a_statement_the_mapping_folds_in_is_not_required_to_link(tmp_path: Path) -> None:
    """The other half of the same rule, and the reason it is calibrated.

    A Problem Concern Act is what this adapter records a condition by; the
    Problem Observation nested inside it never carries its own provenance and
    never will, because one entry becomes one Condition. Requiring every
    statement to link would report every conforming problem entry as half lost,
    which is the same lie told backwards. Only kinds this document has been
    SEEN to link are required, so the fold-in is not an obligation.
    """
    body = """
  <component><section>
    <code code="11450-4" codeSystem="2.16.840.1.113883.6.1"/>
    <title>Problems</title>
    <text>Prose.</text>
    <entry><act classCode="ACT" moodCode="EVN">
      <templateId root="2.16.840.1.113883.10.20.22.4.3"/>
      <id root="feedface-conc-0000-0000-000000000901"/>
      <code code="CONC" codeSystem="2.16.840.1.113883.5.6"/>
      <statusCode code="active"/>
      <entryRelationship typeCode="SUBJ"><observation classCode="OBS" moodCode="EVN">
        <templateId root="2.16.840.1.113883.10.20.22.4.4"/>
        <id root="feedface-prob-0000-0000-000000000901"/>
        <code code="55607006" displayName="Problem"/>
        <value code="38341003" displayName="Synthetic finding" xsi:type="CD"/>
      </observation></entryRelationship>
    </act></entry>
  </section></component>
"""
    path = _write(tmp_path, body=body)
    record = parse_document(path)
    assert record.conditions, "the probe needs the concern act to have been parsed"
    row = _row(document_ledger(path, record), "section:11450-4")
    assert row.entries == {Disposition.STRUCTURALLY_PARSED: 1}  # type: ignore[attr-defined]


def test_a_parked_entry_is_stored_as_the_document_spells_it(tmp_path: Path) -> None:
    """The verbatim copy is of the FILE, not of the tree the parser worked on.

    ``_inline_narrative_references`` fills each ``<reference>`` element's own
    text in place so the structural parsers can read a coded entry's referenced
    name. It ran before the capture, so the stored "verbatim" entry carried
    narrative the document does not spell at that position — and the ledger,
    re-reading the file, computed different bytes and reported a preserved
    entry as lost. The capture happens first now.
    """
    body = """
  <component><section>
    <code code="11450-4" codeSystem="2.16.840.1.113883.6.1"/>
    <title>Problems</title>
    <text><paragraph ID="prob-1">Referenced narrative name.</paragraph></text>
  </section></component>
  <component><section>
    <code code="48768-6" codeSystem="2.16.840.1.113883.6.1"/>
    <title>Payers</title>
    <entry><observation classCode="OBS" moodCode="EVN">
      <id root="feedface-genr-0000-0000-000000000901"/>
      <code code="75326-9"><originalText><reference value="#prob-1"/></originalText></code>
    </observation></entry>
  </section></component>
"""
    path = _write(tmp_path, body=body)
    record = parse_document(path)

    stored = record.patient.extensions["ccda:entries:48768-6"]
    assert "Referenced narrative name" not in stored[0], "the capture read the hydrated tree"
    assert '<reference value="#prob-1"/>' in stored[0]
    row = _row(document_ledger(path, record), "section:48768-6")
    assert row.entries == {Disposition.NARRATIVE_PRESERVED: 1}  # type: ignore[attr-defined]


def test_two_statement_kinds_are_never_merged_into_one() -> None:
    """A merged kind is somebody else's success calibrating YOUR obligation.

    ``_statement_kind`` is the identity ``links`` calibrates against, so every
    pair it wrongly merges makes a statement an obligation on the strength of
    an unrelated one that linked — the false-alarm direction. Two ways it did:
    the template roots went through the vocabulary check, which collapses every
    non-OID root to one label, so two unrelated vendor templates read as one
    kind; and the element name was dropped whenever a template existed, so an
    ``<act>`` and an ``<observation>`` sharing a template did the same.
    """
    from lxml import etree

    from anastomosis.sources.ccda.ledger import _statement_kind
    from anastomosis.sources.ccda.parser import _PARSER

    def statement(template: str, tag: str = "observation") -> object:
        return etree.fromstring(
            f'<{tag} xmlns="urn:hl7-org:v3"><templateId root="{template}"/></{tag}>'.encode(),
            _PARSER,
        )

    assert _statement_kind(statement("urn:vendor:alpha")) != _statement_kind(  # type: ignore[arg-type]
        statement("urn:vendor:beta")  # type: ignore[arg-type]
    )
    shared = "2.16.840.1.113883.10.20.22.4.2"
    assert _statement_kind(statement(shared)) != _statement_kind(  # type: ignore[arg-type]
        statement(shared, tag="act")  # type: ignore[arg-type]
    )


# --- what the adversarial review found in the fixes above ---------------------


@pytest.mark.parametrize(
    "narrative",
    [
        "",
        "<text/>",
        "<text>   </text>",
        "<text nullFlavor='NI'/>",
        '<text><renderMultiMedia referencedObject="i1"/></text>',
    ],
)
def test_a_section_that_renders_no_text_still_parks_its_entries(
    tmp_path: Path, narrative: str
) -> None:
    """RENDERS no text, not HAS no ``<text>`` element.

    The capture used to ask whether the section's narrative was empty AFTER
    rendering; rewriting it to ask whether the element exists quietly stopped
    preserving four real shapes — an empty element, one holding only
    whitespace, a nullFlavor, and a multimedia-only cell. In each the entries
    are still the only thing the document said, and their bytes stopped
    reaching the record at all, so the export's declared-loss section could not
    name them either.
    """
    body = f"""
  <component><section>
    <code code="18776-5" codeSystem="2.16.840.1.113883.6.1"/>
    <title>Plan of Treatment</title>
    {narrative}
    <entry><observation classCode="OBS" moodCode="EVN">
      <id root="feedface-genr-0000-0000-000000000901"/>
      <code code="75326-9"/><value displayName="A stated fact"/>
    </observation></entry>
  </section></component>
"""
    path = _write(tmp_path, body=body)
    record = parse_document(path)

    assert [k for k in record.patient.extensions if k.startswith("ccda:entries:")]
    row = _row(document_ledger(path, record), "section:18776-5")
    assert row.entries == {Disposition.NARRATIVE_PRESERVED: 1}  # type: ignore[attr-defined]


def test_an_entry_pointing_at_narrative_the_record_kept_is_preserved(
    tmp_path: Path,
) -> None:
    """A ``<reference>`` is the document's own answer, and it is checkable.

    C-CDA's mechanism for "this is my human-readable form" is a reference into
    the section narrative, and it is the one entry-to-narrative link a machine
    can follow. Asking each entry for its OWN bytes and nothing else was too
    strict by exactly this much: a conforming vendor entry that cites a
    narrative cell the record kept was reported as not credited as data.

    The second entry is the control. It states a fact of its own and cites
    nothing, so the same stored narrative says nothing about it and it stays
    uncredited — which is the section-prose credit this whole change removes.
    """
    body = """
  <component><section>
    <code code="47519-4" codeSystem="2.16.840.1.113883.6.1"/>
    <title>Procedures</title>
    <text><paragraph ID="proc-1">Medication reconciliation (procedure) 430193006</paragraph></text>
    <entry><procedure classCode="PROC" moodCode="EVN">
      <id root="feedface-proc-0000-0000-000000000901"/>
      <code code="430193006"><originalText><reference value="#proc-1"/></originalText></code>
    </procedure></entry>
    <entry><procedure classCode="PROC" moodCode="EVN">
      <id root="feedface-proc-0000-0000-000000000902"/>
      <code code="999999999" displayName="Cited by nothing"/>
    </procedure></entry>
  </section></component>
"""
    path = _write(tmp_path, body=body)
    record = _unparked(parse_document(path))
    row = _row(document_ledger(path, record), "section:47519-4")

    assert row.entries == {  # type: ignore[attr-defined]
        Disposition.NARRATIVE_PRESERVED: 1,
        Disposition.UNSUPPORTED: 1,
    }


def test_an_entry_citing_narrative_the_record_lost_is_not_preserved(tmp_path: Path) -> None:
    """The reference has to land in text the record actually kept.

    Same document, with the section's narrative removed from the record after
    parsing. The citation is still in the entry; what it points at is gone, so
    it proves nothing.
    """
    body = """
  <component><section>
    <code code="47519-4" codeSystem="2.16.840.1.113883.6.1"/>
    <title>Procedures</title>
    <text><paragraph ID="proc-1">Medication reconciliation (procedure) 430193006</paragraph></text>
    <entry><procedure classCode="PROC" moodCode="EVN">
      <id root="feedface-proc-0000-0000-000000000901"/>
      <code code="430193006"><originalText><reference value="#proc-1"/></originalText></code>
    </procedure></entry>
  </section></component>
"""
    path = _write(tmp_path, body=body)
    record = _unparked(parse_document(path))
    for key in [k for k in record.patient.extensions if k.startswith("ccda:section:47519-4")]:
        del record.patient.extensions[key]

    row = _row(document_ledger(path, record), "section:47519-4")
    assert row.entries == {Disposition.UNSUPPORTED: 1}  # type: ignore[attr-defined]


def test_a_statement_behind_a_shared_root_is_a_blind_spot_not_a_pass(tmp_path: Path) -> None:
    """The same loss, hidden by an id root something else in the document reuses.

    An obligation is only checkable when the statement carries a root unique to
    it, and one organisation OID stamped on a header author and on an entry is
    ordinary C-CDA. Dropping such a statement from the obligation set let its
    sibling's success answer for it — and the row read structurally_parsed with
    unlinkable=0, a clean bill of health, which is the one thing this ledger's
    stated bias forbids. It is counted as impossible to check instead.
    """
    shared = "feedface-shared-0000-0000-000000000901"
    header = f"""
  <author><time value="20200101"/><assignedAuthor><id root="{shared}"/>
  </assignedAuthor></author>
"""
    body = f"""
  <component><section>
    <code code="30954-2" codeSystem="2.16.840.1.113883.6.1"/>
    <title>Results</title>
    <text>Prose that cites nothing.</text>
    <entry><organizer classCode="BATTERY" moodCode="EVN">
      <component><observation classCode="OBS" moodCode="EVN">
        <templateId root="2.16.840.1.113883.10.20.22.4.2"/>
        <id root="feedface-rslt-0000-0000-000000000901"/>
        <code code="2345-7" displayName="A"/><value value="1" unit="u"/>
      </observation></component>
      <component><observation classCode="OBS" moodCode="EVN">
        <templateId root="2.16.840.1.113883.10.20.22.4.2"/>
        <id root="{shared}"/>
        <code code="2160-0" displayName="B"/><value value="2" unit="u"/>
      </observation></component>
    </organizer></entry>
  </section></component>
"""
    path = _write(tmp_path, body=body, header=header)
    record = parse_document(path)
    partial = record.model_copy(deep=True)
    partial.observations = [
        observation
        for observation in record.observations
        if (observation.provenance.source_id or "").endswith("0901")
    ]

    row = _row(document_ledger(path, partial), "section:30954-2")
    assert Disposition.STRUCTURALLY_PARSED not in row.entries  # type: ignore[attr-defined]
    assert row.unlinkable == 1  # type: ignore[attr-defined]


def test_a_nested_statements_id_does_not_answer_for_the_act_that_holds_it(
    tmp_path: Path,
) -> None:
    """A statement's obligation is its OWN id, not its descendants'.

    ``_own_id_roots`` reads an act's ``<id>`` CHILDREN. Read the descendants
    instead and any carried id under the act answers for the act itself, which
    is the any-of habit reappearing one level down.

    Both entries are concern acts of the same template, so the kind is
    calibrated. The second entry's condition is re-provenanced onto its nested
    REACTION observation — the shape of an adapter that recorded that entry by
    an inner statement — leaving the concern act's own id carried by nothing.
    The two entries' inner statements are different templates on purpose, so
    calibrating one does not make an obligation of the other.

    The first entry stays parsed. The second must not, and must not become so
    on the strength of an id sitting inside it — it reads as preserved, on the
    verbatim copy of its own bytes the parser parks, which is a different column
    from the one this test forbids.
    """
    body = """
  <component><section>
    <code code="11450-4" codeSystem="2.16.840.1.113883.6.1"/>
    <title>Problems</title>
    <text>Prose that cites nothing.</text>
    <entry><act classCode="ACT" moodCode="EVN">
      <templateId root="2.16.840.1.113883.10.20.22.4.3"/>
      <id root="feedface-conc-0000-0000-000000000901"/>
      <code code="CONC" codeSystem="2.16.840.1.113883.5.6"/><statusCode code="active"/>
      <entryRelationship typeCode="SUBJ"><observation classCode="OBS" moodCode="EVN">
        <templateId root="2.16.840.1.113883.10.20.22.4.4"/>
        <id root="feedface-prob-0000-0000-000000000901"/>
        <code code="55607006" displayName="Problem"/>
        <value code="38341003" displayName="First finding" xsi:type="CD"/>
      </observation></entryRelationship>
    </act></entry>
    <entry><act classCode="ACT" moodCode="EVN">
      <templateId root="2.16.840.1.113883.10.20.22.4.3"/>
      <id root="feedface-conc-0000-0000-000000000902"/>
      <code code="CONC" codeSystem="2.16.840.1.113883.5.6"/><statusCode code="active"/>
      <entryRelationship typeCode="SUBJ"><observation classCode="OBS" moodCode="EVN">
        <templateId root="2.16.840.1.113883.10.20.22.4.9"/>
        <id root="feedface-rxn-0000-0000-0000000000902"/>
        <code code="55607006" displayName="Problem"/>
        <value code="26442006" displayName="Second finding" xsi:type="CD"/>
      </observation></entryRelationship>
    </act></entry>
  </section></component>
"""
    path = _write(tmp_path, body=body)
    record = parse_document(path)
    assert len(record.conditions) == 2, "the probe needs both concern acts parsed"

    moved = record.model_copy(deep=True)
    second = next(
        condition
        for condition in moved.conditions
        if (condition.provenance.source_id or "").endswith("0902")
    )
    second.provenance.source_id = "feedface-rxn-0000-0000-0000000000902"

    row = _row(document_ledger(path, moved), "section:11450-4")
    assert row.entries == {  # type: ignore[attr-defined]
        Disposition.STRUCTURALLY_PARSED: 1,
        Disposition.NARRATIVE_PRESERVED: 1,
    }


# --- what the mismatch-repair review found in the repair itself ---------------
#
# The <reference> credit was resolved against every ID in the document and
# tested by asking whether the cell's words occurred anywhere in the section's
# prose. Both halves were wrong, and together they handed back the credit this
# whole branch removes. It is decided by the tree now: a cited cell must be one
# this section's own <text> defines, and one cell answers for one entry.


def _procedures(text: str, entries: str) -> str:
    return f"""
  <component><section>
    <code code="47519-4" codeSystem="2.16.840.1.113883.6.1"/>
    <title>Procedures</title>
    {text}
    {entries}
  </section></component>
"""


def _two_cell_entry(number: int, first: str, second: str) -> str:
    """One entry naming two cells, which is one claim on both of them."""
    return f"""
    <entry><procedure classCode="PROC" moodCode="EVN">
      <id root="feedface-proc-0000-0000-0000000009{number:02d}"/>
      <code code="430193{number:02d}"><originalText>
        <reference value="{first}"/><reference value="{second}"/>
      </originalText></code>
    </procedure></entry>
"""


def _cites(number: int, reference: str, display: str) -> str:
    return f"""
    <entry><procedure classCode="PROC" moodCode="EVN">
      <id root="feedface-proc-0000-0000-00000000090{number}"/>
      <code code="4301930{number}"><originalText>
        <reference value="{reference}"/></originalText></code>
      <text>{display}</text>
    </procedure></entry>
"""


def test_a_sections_whole_prose_is_not_one_of_its_own_narrative_cells(
    tmp_path: Path,
) -> None:
    """The section's ``<text>`` may carry an ID, and citing it is citing prose.

    This is the section-level credit coming back through the front door: put an
    ID on the narrative and every entry beneath can name it, and a containment
    test passes trivially because the text contains itself. A cell is something
    INSIDE the narrative; the narrative is not a cell.
    """
    body = _procedures(
        '<text ID="sect-text">Continue lisinopril and recheck in three months.</text>',
        _cites(1, "#sect-text", "No current problems")
        + _cites(2, "#sect-text", "Colonoscopy due 2031"),
    )
    path = _write(tmp_path, body=body)
    row = _row(document_ledger(path, _unparked(parse_document(path))), "section:47519-4")

    assert row.entries == {Disposition.UNSUPPORTED: 2}  # type: ignore[attr-defined]


def test_a_cell_in_another_section_does_not_answer_here(tmp_path: Path) -> None:
    """Containment decided by the tree, not by one string occurring in another.

    The cited cell lives in another section and reads a word this section's
    prose happens to contain. Resolving anchors document-wide and asking only
    whether the words occur made that a preservation.
    """
    body = """
  <component><section>
    <code code="48768-6" codeSystem="2.16.840.1.113883.6.1"/><title>Payers</title>
    <text><paragraph ID="payer-cell">No</paragraph></text>
  </section></component>
""" + _procedures(
        "<text><paragraph>No procedures were performed.</paragraph></text>",
        _cites(1, "#payer-cell", "Appendectomy"),
    )
    path = _write(tmp_path, body=body)
    row = _row(document_ledger(path, _unparked(parse_document(path))), "section:47519-4")

    assert row.entries == {Disposition.UNSUPPORTED: 1}  # type: ignore[attr-defined]


def test_one_narrative_cell_answers_for_one_entry(tmp_path: Path) -> None:
    """The same arithmetic every other place in this file obeys.

    One cell is one statement of fact. Three entries naming it are not three
    preservations, and the anchor is spent by the first.
    """
    body = _procedures(
        '<text><paragraph ID="proc-1">Medication reconciliation (procedure)</paragraph></text>',
        _cites(1, "#proc-1", "A") + _cites(2, "#proc-1", "B") + _cites(3, "#proc-1", "C"),
    )
    path = _write(tmp_path, body=body)
    row = _row(document_ledger(path, _unparked(parse_document(path))), "section:47519-4")

    assert row.entries == {  # type: ignore[attr-defined]
        Disposition.NARRATIVE_PRESERVED: 1,
        Disposition.UNSUPPORTED: 2,
    }


def test_an_entry_citing_one_real_cell_and_one_dangling_gets_nothing(
    tmp_path: Path,
) -> None:
    """All of them, not any — which is what the docstring said and the code did not.

    Filtering unresolved anchors out before the check meant an entry could name
    a cell the document never defined and still be counted, on the strength of
    its other citation. Half an account is not an account.
    """
    body = _procedures(
        '<text><paragraph ID="proc-1">Medication reconciliation (procedure)</paragraph></text>',
        """
    <entry><procedure classCode="PROC" moodCode="EVN">
      <id root="feedface-proc-0000-0000-000000000901"/>
      <code code="430193006"><originalText><reference value="#proc-1"/></originalText>
        <translation><originalText><reference value="#never-defined"/></originalText></translation>
      </code>
    </procedure></entry>
""",
    )
    path = _write(tmp_path, body=body)
    row = _row(document_ledger(path, _unparked(parse_document(path))), "section:47519-4")

    assert row.entries == {Disposition.UNSUPPORTED: 1}  # type: ignore[attr-defined]


def test_one_entry_naming_one_cell_twice_is_one_citation(tmp_path: Path) -> None:
    """A procedure names its row from ``<originalText>`` and again from ``<text>``.

    That is C-CDA's ordinary spelling, and it is one entry citing one cell —
    not a claim on two copies of it. Requiring a copy per reference made the
    real vendor document's only cited entry read as lost, which is the false
    alarm this credit exists to remove.
    """
    body = _procedures(
        '<text><paragraph ID="proc-1">Medication reconciliation (procedure)</paragraph></text>',
        """
    <entry><procedure classCode="PROC" moodCode="EVN">
      <id root="feedface-proc-0000-0000-000000000901"/>
      <code code="430193006"><originalText>
        <reference value="#proc-1"/></originalText></code>
      <text><reference value="#proc-1"/></text>
    </procedure></entry>
""",
    )
    path = _write(tmp_path, body=body)
    row = _row(document_ledger(path, _unparked(parse_document(path))), "section:47519-4")

    assert row.entries == {Disposition.NARRATIVE_PRESERVED: 1}  # type: ignore[attr-defined]


def test_a_parked_entry_answers_only_for_the_section_that_parked_it(
    tmp_path: Path,
) -> None:
    """Two byte-identical entries, one parked copy, and no theft between them.

    An empty coded entry repeats across sections, and the stored copies were
    pooled by their bytes alone — so a section could claim the copy parked for
    another, and which one got it depended on document order. The parser writes
    the section code into the key; this side reads it.

    Both sections park now, so the theft is asked for by taking Payers' own copy
    away: with the bytes alone as the handle it would answer itself out of the
    copy Plan parked, and both would read preserved.
    """
    entry = """
    <entry><observation classCode="OBS" moodCode="EVN">
      <code code="75326-9" codeSystem="2.16.840.1.113883.6.1"/>
    </observation></entry>
"""
    body = f"""
  <component><section>
    <code code="48768-6" codeSystem="2.16.840.1.113883.6.1"/><title>Payers</title>
    <text>Prose that cites nothing.</text>{entry}
  </section></component>
  <component><section>
    <code code="18776-5" codeSystem="2.16.840.1.113883.6.1"/><title>Plan</title>{entry}
  </section></component>
"""
    path = _write(tmp_path, body=body)
    record = parse_document(path)
    del record.patient.extensions["ccda:entries:48768-6"]
    ledger = document_ledger(path, record)

    assert _row(ledger, "section:48768-6").entries == {Disposition.UNSUPPORTED: 1}  # type: ignore[attr-defined]
    assert _row(ledger, "section:18776-5").entries == {  # type: ignore[attr-defined]
        Disposition.NARRATIVE_PRESERVED: 1
    }


# --- what round three found ---------------------------------------------------
#
# The headline outcome of the anchor rewrite — the one real vendor document's
# cited entry reading preserved — was pinned by nothing: every test here put the
# cited cell on a direct child of <text>, while a real C-CDA puts it in a <td>
# inside <table><tbody><tr>. A one-token mutation flipped that document and the
# whole suite stayed green. These are the shapes that were missing.


def test_a_cell_deep_inside_a_narrative_table_is_still_a_cell(tmp_path: Path) -> None:
    """Where a real C-CDA actually puts its anchors.

    Every other test in this file cites a ``<paragraph ID>`` sitting directly
    under ``<text>``, and so does the corpus generator — so the descendant walk
    could have been a direct-child walk and nothing would have failed, while
    the one vendor-shaped fixture in the tree silently lost its credit.
    """
    body = _procedures(
        """<text><table><tbody><tr>
        <td ID="proc-1">Medication reconciliation (procedure)</td>
        <td ID="proc-code-1">430193006</td>
      </tr></tbody></table></text>""",
        _cites(1, "#proc-1", "Medication reconciliation"),
    )
    path = _write(tmp_path, body=body)
    row = _row(document_ledger(path, _unparked(parse_document(path))), "section:47519-4")

    assert row.entries == {Disposition.NARRATIVE_PRESERVED: 1}  # type: ignore[attr-defined]


def test_the_shipped_vendor_fixture_keeps_its_cited_entry(tmp_path: Path) -> None:
    """The claim this change is verified by, asserted rather than described.

    The Synthea sample's Procedures entry cites a ``<td>`` in its own section's
    narrative table. It is the only document in the tree that exercises the
    citation path end to end, so it gets an assertion of its own instead of
    being a sentence in a commit message.
    """
    del tmp_path
    fixture = (
        Path(__file__).resolve().parents[1] / "fixtures" / "synthea" / "synthea_ccda_sample.xml"
    )
    row = _row(document_ledger(fixture, _unparked(parse_document(fixture))), "section:47519-4")

    assert row.entries == {Disposition.NARRATIVE_PRESERVED: 1}  # type: ignore[attr-defined]


def test_a_cell_that_renders_no_words_preserves_nothing(tmp_path: Path) -> None:
    """A citation has to land on something the record actually kept.

    ``<content ID="x"><renderMultiMedia/></content>`` reaches the stored
    narrative as nothing at all, so an entry citing it is pointing at a hole.
    """
    body = _procedures(
        """<text>The following procedure was performed.
        <content ID="proc-1"><renderMultiMedia referencedObject="MM1"/></content></text>""",
        _cites(1, "#proc-1", "Appendectomy"),
    )
    path = _write(tmp_path, body=body)
    row = _row(document_ledger(path, _unparked(parse_document(path))), "section:47519-4")

    assert row.entries == {Disposition.UNSUPPORTED: 1}  # type: ignore[attr-defined]


def test_a_table_holding_the_prose_is_not_one_of_its_cells(tmp_path: Path) -> None:
    """The whole-prose credit, wearing a container's clothes.

    Excluding the ``<text>`` element by identity closed the direct route and
    left the obvious detour: wrap the prose in a ``<table ID>`` and every entry
    beneath can cite the arrangement instead. A cell is a thing inside the
    table, not the table.
    """
    body = _procedures(
        """<text><table ID="whole"><tbody><tr>
        <td>Continue lisinopril and recheck blood pressure in three months.</td>
      </tr></tbody></table></text>""",
        _cites(1, "#whole", "No current problems") + _cites(2, "#whole", "Colonoscopy due 2031"),
    )
    path = _write(tmp_path, body=body)
    row = _row(document_ledger(path, _unparked(parse_document(path))), "section:47519-4")

    assert row.entries == {Disposition.UNSUPPORTED: 2}  # type: ignore[attr-defined]


def test_one_id_on_two_nodes_is_still_one_cell(tmp_path: Path) -> None:
    """Counting occurrences let a repeated name answer twice.

    A document carrying one ID on two nodes is malformed and real. The parser's
    own resolver keeps one node per name, so counting the nodes made the two
    sides of this mirror disagree about how many cells a name is.
    """
    body = _procedures(
        """<text><paragraph ID="proc-1">Medication reconciliation</paragraph>
        <paragraph ID="proc-1">Medication reconciliation</paragraph></text>""",
        _cites(1, "#proc-1", "A") + _cites(2, "#proc-1", "B"),
    )
    path = _write(tmp_path, body=body)
    row = _row(document_ledger(path, _unparked(parse_document(path))), "section:47519-4")

    assert row.entries == {  # type: ignore[attr-defined]
        Disposition.NARRATIVE_PRESERVED: 1,
        Disposition.UNSUPPORTED: 1,
    }


_SHARED_CODE_ENTRY = """
    <entry><observation classCode="OBS" moodCode="EVN">
      <code code="75326-9" codeSystem="2.16.840.1.113883.6.1"/>
    </observation></entry>
"""


@pytest.mark.parametrize("textless_first", [False, True])
def test_two_sections_sharing_a_code_do_not_share_a_parked_copy(
    tmp_path: Path, textless_first: bool
) -> None:
    """Problems (Active) and Problems (Resolved) are both 11450-4.

    Keying the stored copies by section code narrowed the theft rather than
    ending it: two sections of one code met in the same bucket, and which of
    them got a copy depended on which came first. The bucket holds as many
    copies as there are sections that parked into it — both of these do — and
    each takes exactly one, so the reading is the same either way round. A
    parser that parked for only one of them would leave the other reading
    unsupported in one order and preserved in the other, which is the reading
    nobody could reproduce.
    """
    textless = f"""
  <component><section>
    <code code="11450-4" codeSystem="2.16.840.1.113883.6.1"/>
    <title>Problems (Resolved)</title>{_SHARED_CODE_ENTRY}
  </section></component>
"""
    prose = f"""
  <component><section>
    <code code="11450-4" codeSystem="2.16.840.1.113883.6.1"/>
    <title>Problems (Active)</title>
    <text>Prose that cites nothing.</text>{_SHARED_CODE_ENTRY}
  </section></component>
"""
    path = _write(tmp_path, body=(textless + prose) if textless_first else (prose + textless))
    # Per OCCURRENCE, in document order: the merged row cannot tell which of
    # the two got the copy, and which one got it is the entire question.
    rows = [
        row
        for row in document_ledger(path, parse_document(path)).rows
        if row.construct == "section:11450-4"
    ]
    assert len(rows) == 2
    assert [row.entries for row in rows] == [{Disposition.NARRATIVE_PRESERVED: 1}] * 2, (
        "a section did not get the copy it parked"
    )


def test_a_stamped_loss_ledger_does_not_claim_a_foreign_sections_copy(
    tmp_path: Path,
) -> None:
    """51899-3 is a public LOINC, and this repo's exporter writes one too.

    The parser reads its OWN loss ledger back as prior losses rather than
    parking its entries, so a stamped section has no copy of its own. Meeting
    a foreign 51899-3 in the same bucket, it took the copy that one earned —
    the shared-code theft above from a third direction, and the direction the
    round that added this guard did not pin.
    """
    stamped = f"""
  <component><section>
    <templateId root="{LOSS_NARRATIVE_TEMPLATE_ROOT}"/>
    <code code="51899-3" codeSystem="2.16.840.1.113883.6.1"/>
    <title>{LOSS_NARRATIVE_TITLE}</title>{_SHARED_CODE_ENTRY}
  </section></component>
"""
    foreign = f"""
  <component><section>
    <code code="51899-3" codeSystem="2.16.840.1.113883.6.1"/>
    <title>Vendor Extensions</title>{_SHARED_CODE_ENTRY}
  </section></component>
"""
    path = _write(tmp_path, body=stamped + foreign)
    rows = [
        row
        for row in document_ledger(path, parse_document(path)).rows
        if row.construct == "section:51899-3"
    ]
    assert len(rows) == 2

    assert rows[0].entries == {Disposition.UNSUPPORTED: 1}, (
        "the exporter's own loss ledger took a copy it never parked"
    )
    assert rows[1].entries == {Disposition.NARRATIVE_PRESERVED: 1}, (
        "the foreign section lost its own copy"
    )


def test_a_code_less_section_still_claims_its_own_parked_entries(tmp_path: Path) -> None:
    """The ``unknown`` bucket both sides have to agree on.

    The parser writes ``ccda:entries:unknown`` for a section with no code and
    numbers a repeat ``#2``; this side has to name the same bucket, and neither
    the suffix strip nor the fallback was pinned by anything.
    """
    entry = _SHARED_CODE_ENTRY.replace("75326-9", "75327-9")
    body = f"""
  <component><section><title>No code at all</title>{_SHARED_CODE_ENTRY}</section></component>
  <component><section><title>Also no code</title>{entry}</section></component>
"""
    path = _write(tmp_path, body=body)
    row = _row(document_ledger(path, parse_document(path)), "section:none")

    assert row.entries == {Disposition.NARRATIVE_PRESERVED: 2}  # type: ignore[attr-defined]


def test_a_padded_reference_value_still_names_its_cell(tmp_path: Path) -> None:
    """Both sides strip, or the mirror reports its own drift as loss.

    The parser strips before it resolves a reference, so a ``value=" #id "``
    resolves there; this side must read the same citation rather than see no
    ``#`` and call the entry lost.
    """
    body = _procedures(
        '<text><paragraph ID="proc-1">Medication reconciliation</paragraph></text>',
        _cites(1, " #proc-1 ", "A"),
    )
    path = _write(tmp_path, body=body)
    row = _row(document_ledger(path, _unparked(parse_document(path))), "section:47519-4")

    assert row.entries == {Disposition.NARRATIVE_PRESERVED: 1}  # type: ignore[attr-defined]


# --- what round four found ----------------------------------------------------


def test_a_section_the_parser_never_walked_parks_nothing(tmp_path: Path) -> None:
    """Asking the parser's walk, rather than restating it.

    The first version of this predicate restated the rule — "parent is a
    component under a structuredBody" — and diverged from the parser in both
    directions. A document nesting a second ``<structuredBody>`` inside a
    section has children whose parent chain satisfies that test and which the
    parser's anchored path never reaches: they parked nothing, and took the
    copy earned by the section that did. Whichever came first won, which is
    exactly the order-dependence this was supposed to end.
    """
    entry = """
    <entry><observation classCode="OBS" moodCode="EVN">
      <code code="75326-9" codeSystem="2.16.840.1.113883.6.1"/>
    </observation></entry>
"""
    body = f"""
  <component><section>
    <code code="30954-2" codeSystem="2.16.840.1.113883.6.1"/><title>Outer</title>
    <text>Prose that cites nothing.</text>
    <component><structuredBody><component><section>
      <code code="30954-2" codeSystem="2.16.840.1.113883.6.1"/><title>Buried</title>{entry}
    </section></component></structuredBody></component>
  </section></component>
  <component><section>
    <code code="30954-2" codeSystem="2.16.840.1.113883.6.1"/><title>Parked</title>{entry}
  </section></component>
"""
    path = _write(tmp_path, body=body)
    rows = [
        row
        for row in document_ledger(path, parse_document(path)).rows
        if row.construct == "section:30954-2" and row.entries
    ]
    assert len(rows) == 2
    buried, parked = rows

    assert buried.entries == {Disposition.UNSUPPORTED: 1}, (
        "a section the parser skipped took a copy"
    )
    assert parked.entries == {Disposition.NARRATIVE_PRESERVED: 1}, (
        "the parking section lost its own"
    )


@pytest.mark.parametrize("container", ["table", "thead", "tbody", "tfoot", "list", "caption"])
def test_no_narrative_container_is_one_of_its_own_cells(tmp_path: Path, container: str) -> None:
    """Each name in the list, not just the one that had a test.

    Only ``table`` was pinned, so five of the six could have been deleted with
    the suite still green. An ID on any of them names the whole arrangement —
    or, for a caption, labels it — and an entry citing that is citing the
    section's prose by another route.
    """
    body = _procedures(
        f"""<text><{container} ID="whole"><tbody><tr>
        <td>Continue lisinopril and recheck blood pressure in three months.</td>
      </tr></tbody></{container}></text>""",
        _cites(1, "#whole", "No current problems"),
    )
    path = _write(tmp_path, body=body)
    row = _row(document_ledger(path, _unparked(parse_document(path))), "section:47519-4")

    assert row.entries == {Disposition.UNSUPPORTED: 1}  # type: ignore[attr-defined]


def test_a_table_row_is_one_statement_and_may_be_cited(tmp_path: Path) -> None:
    """``<tr>`` is not a container of everything — it is one row.

    It was on the exclusion list, and that reported loss for narrative the
    record demonstrably holds: the same document, with the ID moved one level
    down to the ``<td>``, read preserved. A row is the granularity of an
    ``<item>``, and the rule is about elements that hold EVERY cell.
    """
    body = _procedures(
        """<text><table><tbody>
        <tr ID="proc-1"><td>Appendectomy</td><td>2019-04-02</td></tr>
      </tbody></table></text>""",
        _cites(1, "#proc-1", "Appendectomy"),
    )
    path = _write(tmp_path, body=body)
    row = _row(document_ledger(path, _unparked(parse_document(path))), "section:47519-4")

    assert row.entries == {Disposition.NARRATIVE_PRESERVED: 1}  # type: ignore[attr-defined]


def test_a_cell_wrapped_in_another_named_cell_is_one_statement(tmp_path: Path) -> None:
    """One word cannot preserve three entries by wearing three labels.

    Keying the pool by name stopped a repeated name answering twice, and left
    nesting open: three ids around one word minted three claims against the
    single word the record holds. Only the innermost name over any given words
    is a cell.
    """
    body = _procedures(
        '<text><content ID="a1"><content ID="a2">'
        '<content ID="a3">Appendectomy</content></content></content></text>',
        _cites(1, "#a1", "A") + _cites(2, "#a2", "B") + _cites(3, "#a3", "C"),
    )
    path = _write(tmp_path, body=body)
    row = _row(document_ledger(path, _unparked(parse_document(path))), "section:47519-4")

    assert row.entries == {  # type: ignore[attr-defined]
        Disposition.NARRATIVE_PRESERVED: 1,
        Disposition.UNSUPPORTED: 2,
    }


#: One outer name and one inner name over the SAME word, with a growing number
#: of unnamed elements between them. The first is the shape the nesting rule
#: was written against; the rest are what a real document looks like, and the
#: table one is the shape that made ``<tr>`` citable in the first place.
_NESTED_CELLS = [
    '<content ID="a1"><content ID="a3">Appendectomy</content></content>',
    '<table><tbody><tr ID="a1"><td>'
    '<content ID="a3">Appendectomy</content></td></tr></tbody></table>',
    '<table><tbody><tr><td ID="a1"><list>'
    '<item ID="a3">Appendectomy</item></list></td></tr></tbody></table>',
    '<content ID="a1"><paragraph><content ID="a3">Appendectomy</content></paragraph></content>',
    # THREE levels. At two, the nearest enclosing cell and the outermost one
    # are the same element and the ancestor walk never runs past its first
    # step, so a rule keeping the wrong end of the chain — or stopping after
    # one step — reads identically. The canonical C-CDA table is three.
    '<table><tbody><tr ID="a1"><td ID="a2">'
    '<content ID="a3">Appendectomy</content></td></tr></tbody></table>',
]


@pytest.mark.parametrize("narrative", _NESTED_CELLS)
def test_a_cell_is_wrapped_at_any_depth_not_only_by_its_parent(
    tmp_path: Path, narrative: str
) -> None:
    """Depth is not a way to buy a second preservation of one word.

    The rule above was written with ``other in node``, which asks lxml whether
    ``other`` is a direct CHILD. A table puts a ``<td>`` between the row and
    the cell — and making a row citable is what the round before this one did —
    so the canonical arrangement minted two preservations against one word.
    A false clean bill, which this ledger's bias forbids above all.
    """
    body = _procedures(
        f"<text>{narrative}</text>",
        _cites(1, "#a1", "A") + _cites(3, "#a3", "C"),
    )
    path = _write(tmp_path, body=body)
    row = _row(document_ledger(path, _unparked(parse_document(path))), "section:47519-4")

    assert row.entries == {  # type: ignore[attr-defined]
        Disposition.NARRATIVE_PRESERVED: 1,
        Disposition.UNSUPPORTED: 1,
    }


@pytest.mark.parametrize("narrative", _NESTED_CELLS)
def test_an_entry_citing_the_cell_that_holds_its_word_is_credited(
    tmp_path: Path, narrative: str
) -> None:
    """The other direction, and the one a real document takes.

    Dropping the outer name from the pool stopped two entries splitting one
    word, and it also stopped ONE entry citing its own row — the ordinary
    arrangement, where no double credit is even possible. The record holds
    the word; the ledger called it unsupported. A name over a word is an
    address for it, not a competing claim on it.
    """
    body = _procedures(f"<text>{narrative}</text>", _cites(1, "#a1", "A"))
    path = _write(tmp_path, body=body)
    record = parse_document(path)
    row = _row(document_ledger(path, record), "section:47519-4")

    assert record.patient.extensions["ccda:section:47519-4"]["text"], (
        "the record does not hold the word, so this is not a false-loss case"
    )
    assert row.entries == {Disposition.NARRATIVE_PRESERVED: 1}  # type: ignore[attr-defined]


def test_one_entry_naming_a_word_by_two_addresses_makes_one_claim(
    tmp_path: Path,
) -> None:
    """A procedure names its row from ``originalText`` and its cell from ``text``.

    Two names, one word, one entry. The all-or-nothing claim must resolve them
    to the single cell they both stand over, not demand two copies of it.
    """
    entry = """
    <entry><procedure classCode="PROC" moodCode="EVN">
      <id root="feedface-proc-0000-0000-000000000901"/>
      <code code="43019301"><originalText>
        <reference value="#a1"/></originalText></code>
      <text><reference value="#a3"/></text>
    </procedure></entry>
"""
    body = _procedures(
        '<text><table><tbody><tr ID="a1"><td>'
        '<content ID="a3">Appendectomy</content></td></tr></tbody></table></text>',
        entry,
    )
    path = _write(tmp_path, body=body)
    row = _row(document_ledger(path, _unparked(parse_document(path))), "section:47519-4")

    assert row.entries == {Disposition.NARRATIVE_PRESERVED: 1}  # type: ignore[attr-defined]


def test_an_entry_claiming_two_cells_spends_both_or_neither(tmp_path: Path) -> None:
    """All of them and not any, and the difference is visible in the totals.

    An entry citing two cells that finds one is an entry half of whose account
    is missing, and it must not keep the half it found: that half is a claim
    the next entry to name it would have been owed. Three cells, three entries
    — one naming a cell that is already taken alongside a free one, and one
    naming that free cell alongside another. Spending greedily lets the first
    take what the second needed, and reads three preserved where the document
    supports two.
    """
    two_cells = """
    <entry><procedure classCode="PROC" moodCode="EVN">
      <id root="feedface-proc-0000-0000-00000000090{n}"/>
      <code code="4301930{n}"><originalText>
        <reference value="#{first}"/><reference value="#{second}"/>
      </originalText></code>
    </procedure></entry>
"""
    body = _procedures(
        '<text><content ID="x1">Colonoscopy</content>'
        '<content ID="x2">Appendectomy</content>'
        '<content ID="x3">Cholecystectomy</content></text>',
        _cites(1, "#x1", "A")
        + two_cells.format(n=2, first="x1", second="x2")
        + two_cells.format(n=3, first="x2", second="x3"),
    )
    path = _write(tmp_path, body=body)
    row = _row(document_ledger(path, _unparked(parse_document(path))), "section:47519-4")

    assert row.entries == {  # type: ignore[attr-defined]
        Disposition.NARRATIVE_PRESERVED: 2,
        Disposition.UNSUPPORTED: 1,
    }


def test_every_level_of_a_nested_arrangement_is_an_address(tmp_path: Path) -> None:
    """Row, cell, content: three names over one word, and one claim.

    Two levels cannot tell a rule that keeps the NEAREST enclosing cell from
    one that keeps the OUTERMOST, nor one that walks the whole chain from one
    that stops after a step — at two levels those are the same answer. Both
    mistakes are live at three, and both mint a second credit for one word.
    """
    narrative = (
        '<table><tbody><tr ID="a1"><td ID="a2">'
        '<content ID="a3">Appendectomy</content></td></tr></tbody></table>'
    )
    body = _procedures(f"<text>{narrative}</text>", _cites(1, "#a2", "A") + _cites(3, "#a3", "C"))
    path = _write(tmp_path, body=body)
    row = _row(document_ledger(path, _unparked(parse_document(path))), "section:47519-4")

    assert row.entries == {  # type: ignore[attr-defined]
        Disposition.NARRATIVE_PRESERVED: 1,
        Disposition.UNSUPPORTED: 1,
    }


def test_two_cells_inside_one_named_cell_are_still_two_statements(
    tmp_path: Path,
) -> None:
    """Which end of the nesting answers, pinned.

    A rule keeping the OUTERMOST name instead of the innermost satisfies every
    other test here — one preserved and one unsupported reads the same either
    way round — and is wrong: two cells over two different words, wrapped in
    one named cell, are two statements the record holds both of.
    """
    body = _procedures(
        '<text><table><tbody><tr><td ID="c1">'
        '<content ID="x1">Colonoscopy</content>'
        '<content ID="x2">Appendectomy</content></td></tr></tbody></table></text>',
        _cites(1, "#x1", "A") + _cites(3, "#x2", "C"),
    )
    path = _write(tmp_path, body=body)
    row = _row(document_ledger(path, _unparked(parse_document(path))), "section:47519-4")

    assert row.entries == {Disposition.NARRATIVE_PRESERVED: 2}  # type: ignore[attr-defined]


@pytest.mark.parametrize("row_first", [True, False])
def test_the_reading_does_not_turn_on_which_entry_is_listed_first(
    tmp_path: Path, row_first: bool
) -> None:
    """One document's content has one reading.

    Served in the order the section lists them, an entry citing a whole row
    takes every cell under it and starves the entries that cite those cells by
    name — so these same three entries over these same two words read two
    preserved or one, decided by nothing but which came first. Both ends are
    tried now, and the answer is the same either way round.
    """
    row = _cites(1, "#row1", "A")
    cells = _cites(2, "#cell-a", "B") + _cites(3, "#cell-b", "C")
    body = _procedures(
        '<text><table><tbody><tr ID="row1">'
        '<td ID="cell-a">Colonoscopy</td>'
        '<td ID="cell-b">Appendectomy</td></tr></tbody></table></text>',
        (row + cells) if row_first else (cells + row),
    )
    path = _write(tmp_path, body=body)
    record = _unparked(parse_document(path))
    ledger_row = _row(document_ledger(path, record), "section:47519-4")

    assert record.patient.extensions["ccda:section:47519-4"]["text"], (
        "the record does not hold the narrative, so this is not the case at issue"
    )
    assert ledger_row.entries == {  # type: ignore[attr-defined]
        Disposition.NARRATIVE_PRESERVED: 2,
        Disposition.UNSUPPORTED: 1,
    }


def test_a_parsed_entry_does_not_take_the_cell_its_sibling_needs(
    tmp_path: Path,
) -> None:
    """A parsed entry's evidence is its object, so it asks the cells for nothing.

    Both entries here name the same cell, and only one of them was taken apart
    into the record. If the parsed one is allowed to claim the cell as well it
    takes the only one there is, and the sibling that has nothing else to show
    reads as lost — a preservation moved from the entry that needed it to the
    entry that did not.
    """
    body = """
  <component><section>
    <code code="11450-4" codeSystem="2.16.840.1.113883.6.1"/>
    <title>Problems</title>
    <text><list><item ID="p1">Essential hypertension</item></list></text>
    <entry>
      <act classCode="ACT" moodCode="EVN">
        <templateId root="2.16.840.1.113883.10.20.22.4.3"/>
        <id root="feedface-prob-0000-0000-000000000001"/>
        <code code="CONC" codeSystem="2.16.840.1.113883.5.6"/>
        <statusCode code="active"/>
        <effectiveTime><low value="20210215"/></effectiveTime>
        <text><reference value="#p1"/></text>
        <entryRelationship typeCode="SUBJ">
          <observation classCode="OBS" moodCode="EVN">
            <templateId root="2.16.840.1.113883.10.20.22.4.4"/>
            <id root="feedface-prob-0000-0000-000000000011"/>
            <code code="55607006" codeSystem="2.16.840.1.113883.6.96"/>
            <statusCode code="completed"/>
            <effectiveTime><low value="20210215"/></effectiveTime>
            <value xsi:type="CD" code="38341003"
                   codeSystem="2.16.840.1.113883.6.96"/>
          </observation>
        </entryRelationship>
      </act>
    </entry>
    <entry>
      <act classCode="ACT" moodCode="EVN">
        <id root="feedface-prob-0000-0000-000000000099"/>
        <code code="CONC" codeSystem="2.16.840.1.113883.5.6"/>
        <text><reference value="#p1"/></text>
      </act>
    </entry>
  </section></component>
"""
    path = _write(tmp_path, body=body)
    record = parse_document(path)
    assert len(record.conditions) == 1, "the first entry was meant to parse"
    row = _row(document_ledger(path, record), "section:11450-4")

    assert row.entries == {  # type: ignore[attr-defined]
        Disposition.STRUCTURALLY_PARSED: 1,
        Disposition.NARRATIVE_PRESERVED: 1,
    }


_CHAINED = {
    "A": ("#x1", "#x2"),
    "B": ("#x2", "#x3"),
    "C": ("#x3", "#x4"),
}


@pytest.mark.parametrize("order", list(itertools.permutations("ABC")))
def test_entries_whose_claims_overlap_in_a_chain_read_the_same_either_way(
    tmp_path: Path, order: tuple[str, ...]
) -> None:
    """Ties are not a licence to fall back on document order.

    Serving the narrowest claim first fixed the case where one entry asked for
    more than the others. It did nothing when they all ask for the SAME
    number: three entries over four cells, each naming two, chained so that
    each overlaps its neighbour — every demand is two, the sort is stable, and
    the section's listing order decided the answer again. The order is decided
    by the content now: how much is asked for, then the names it is asked by.
    """
    body = _procedures(
        "<text>"
        + "".join(f'<content ID="x{i}">Word{i}</content>' for i in range(1, 5))
        + "</text>",
        "".join(
            _two_cell_entry(number, *_CHAINED[key]) for number, key in enumerate(order, start=1)
        ),
    )
    path = _write(tmp_path, body=body)
    row = _row(document_ledger(path, _unparked(parse_document(path))), "section:47519-4")

    assert row.entries == {  # type: ignore[attr-defined]
        Disposition.NARRATIVE_PRESERVED: 2,
        Disposition.UNSUPPORTED: 1,
    }


def test_one_entry_reaching_into_two_rows_does_not_kill_them_both(
    tmp_path: Path,
) -> None:
    """The narrowest claim is not always the one to serve first.

    Two rows of three cells, an entry citing each row, and one entry naming a
    single cell from each. That last claim is the smallest, and serving it
    first takes a cell out of both rows and leaves neither row-citing entry
    able to complete — one preservation where the record holds every word and
    an assignment for two plainly exists. Both ends are tried now, and the
    reading is whichever honours more entries.
    """
    body = _procedures(
        "<text><table><tbody>"
        '<tr ID="r1"><td ID="a1">A1</td><td ID="a2">A2</td><td ID="a3">A3</td></tr>'
        '<tr ID="r2"><td ID="b1">B1</td><td ID="b2">B2</td><td ID="b3">B3</td></tr>'
        "</tbody></table></text>",
        _cites(1, "#r1", "R1") + _cites(2, "#r2", "R2") + _two_cell_entry(3, "#a1", "#b1"),
    )
    path = _write(tmp_path, body=body)
    row = _row(document_ledger(path, _unparked(parse_document(path))), "section:47519-4")

    assert row.entries == {  # type: ignore[attr-defined]
        Disposition.NARRATIVE_PRESERVED: 2,
        Disposition.UNSUPPORTED: 1,
    }


def _random_section(rng: random.Random) -> tuple[object, list[list[str]]]:
    """A small table of rows and cells, and entries citing names from it."""
    from lxml import etree

    # Two rows minimum, and every entry naming at least two cells: the
    # settlement only has anything to decide when claims actually contend, and
    # a generator of one-cell sections would agree with any rule at all.
    names: list[str] = []
    rows = []
    for r in range(rng.randint(2, 3)):
        cells = []
        for c in range(rng.randint(2, 3)):
            cell = f"c{r}{c}"
            names.append(cell)
            cells.append(f'<td ID="{cell}">W{r}{c}</td>')
        row = f"r{r}"
        names.append(row)
        rows.append(f'<tr ID="{row}">{"".join(cells)}</tr>')
    citations = [
        [f"#{rng.choice(names)}" for _ in range(rng.randint(2, 3))]
        for _ in range(rng.randint(3, 5))
    ]
    entries = "".join(
        '<entry><procedure classCode="PROC" moodCode="EVN">'
        f'<id root="feedface-proc-0000-0000-0000000009{i:02d}"/>'
        '<code code="430193"><originalText>'
        + "".join(f'<reference value="{ref}"/>' for ref in refs)
        + "</originalText></code></procedure></entry>"
        for i, refs in enumerate(citations)
    )
    section = etree.fromstring(
        '<section xmlns="urn:hl7-org:v3"><code code="47519-4"/>'
        f"<text><table><tbody>{''.join(rows)}</tbody></table></text>{entries}</section>".encode()
    )
    return section, citations


def _credited(section: object) -> int:
    """How many of this section's entries the ledger credits to narrative."""
    covers = ledger._section_anchors(section)  # type: ignore[arg-type]
    asking = list(section.findall("{urn:hl7-org:v3}entry"))  # type: ignore[attr-defined]
    return len(ledger._narrative_credits(asking, covers))


def _most_honestly_creditable(section: object) -> int:
    """The most entries a disjoint assignment of these cells could ever credit.

    Brute force over every subset, deliberately: it is the SPECIFICATION the
    ledger's rule is measured against, written independently of it, and a
    slower oracle is the point. An entry naming a cell this narrative does not
    define claims nothing and can never be credited.
    """
    covers = ledger._section_anchors(section)  # type: ignore[arg-type]
    demands = []
    for entry in section.findall("{urn:hl7-org:v3}entry"):  # type: ignore[attr-defined]
        cited = ledger._cited_anchors(entry)
        demands.append(
            set()
            if not cited or any(name not in covers for name in cited)
            else {cell for name in cited for cell in covers[name]}
        )
    for size in range(len(demands), 0, -1):
        for pick in itertools.combinations(demands, size):
            used: set[str] = set()
            if all(want and not (want & used) and not used.update(want) for want in pick):
                return size
    return 0


def test_the_ledger_never_credits_more_than_the_cells_could_honour() -> None:
    """The one property the whole instrument rests on, checked against a brute force.

    Settling entries against cells is set packing, and this ledger's rule is a
    heuristic over it — it may credit FEWER entries than an optimal assignment
    would, and says so. What it must never do is credit MORE, because every
    extra credit is a preservation that no assignment of the surviving cells
    supports: an invented one. Reported once as a measurement, this is now
    asked on every run.

    Only the ceiling is asserted. How OFTEN the rule falls short of it is a
    few cases in a thousand, and an assertion about that on a few hundred
    would be a coin toss dressed as a check.
    """
    rng = random.Random(20260902)
    for _ in range(400):
        section, _ = _random_section(rng)
        credited = _credited(section)
        ceiling = _most_honestly_creditable(section)
        assert credited <= ceiling, (
            f"credited {credited} entries where {ceiling} is the most any "
            f"assignment of these cells could honour"
        )


def test_the_order_a_document_writes_its_references_in_changes_nothing() -> None:
    """``<reference>`` children are a set of names, not a sequence.

    The settlement breaks a tie between two entries asking for the same amount
    by the names they ask BY, and it has to compare those names as a set: an
    entry writing ``#b`` before ``#a`` is naming what an entry writing ``#a``
    before ``#b`` names. Comparing the raw lists instead reads the two as
    different claims and reorders the settlement around a difference the
    document does not have.
    """
    rng = random.Random(4711)
    for _ in range(200):
        section, _ = _random_section(rng)
        before = _credited(section)
        for entry in section.findall("{urn:hl7-org:v3}entry"):  # type: ignore[attr-defined]
            for original in entry.iter("{urn:hl7-org:v3}originalText"):
                shuffled = list(original)
                rng.shuffle(shuffled)
                original[:] = shuffled
        assert _credited(section) == before, (
            "the reading moved when the entries' references were reordered"
        )


def test_a_reference_without_a_hash_names_no_cell(tmp_path: Path) -> None:
    """``value="proc-1"`` is not a citation, and the parser agrees.

    ``_inline_narrative_references`` resolves only a ``#``-prefixed value, so
    a reference without one reaches the record carrying nothing. Crediting the
    entry for it would credit it to narrative the record never took.
    """
    body = _procedures(
        '<text><paragraph ID="proc-1">Medication reconciliation</paragraph></text>',
        """
    <entry><procedure classCode="PROC" moodCode="EVN">
      <id root="feedface-proc-0000-0000-000000000901"/>
      <code code="430193006"><originalText>
        <reference value="proc-1"/></originalText></code>
    </procedure></entry>
""",
    )
    path = _write(tmp_path, body=body)
    row = _row(document_ledger(path, _unparked(parse_document(path))), "section:47519-4")

    assert row.entries == {Disposition.UNSUPPORTED: 1}  # type: ignore[attr-defined]


def test_two_cells_that_wrap_nothing_are_two_statements(tmp_path: Path) -> None:
    """The counterweight: the rule must not collapse cells that are separate.

    A rule keeping only what nothing else contains would be satisfied by
    keeping nothing, and by keeping one. Two cells side by side over two
    different words are two preservations, and stay two.
    """
    body = _procedures(
        '<text><content ID="a1">Colonoscopy</content>'
        '<content ID="a3">Appendectomy</content></text>',
        _cites(1, "#a1", "A") + _cites(3, "#a3", "C"),
    )
    path = _write(tmp_path, body=body)
    row = _row(document_ledger(path, _unparked(parse_document(path))), "section:47519-4")

    assert row.entries == {Disposition.NARRATIVE_PRESERVED: 2}  # type: ignore[attr-defined]


def test_a_comment_in_the_narrative_is_not_a_cell(tmp_path: Path) -> None:
    """lxml gives a comment a callable tag, and asking it for a name raises.

    The guard against it had no test, so deleting it did not fail the suite —
    it made the ledger raise ``ValueError: Invalid input tag`` on any document
    whose narrative carries a comment, which is ordinary in exported XML.
    """
    body = _procedures(
        '<text><!-- generated by the exporter --><paragraph ID="proc-1">Med rec</paragraph></text>',
        _cites(1, "#proc-1", "A"),
    )
    path = _write(tmp_path, body=body)
    row = _row(document_ledger(path, _unparked(parse_document(path))), "section:47519-4")

    assert row.entries == {Disposition.NARRATIVE_PRESERVED: 1}  # type: ignore[attr-defined]


def test_a_bare_hash_is_a_citation_that_cannot_resolve(tmp_path: Path) -> None:
    """``#`` names no cell, so it fails its entry like ``#never-defined``.

    Filtering it out instead let an entry carrying it be credited on its other
    citation — and this commit's own argument is that the two are equally
    unresolvable.
    """
    body = _procedures(
        '<text><paragraph ID="proc-1">Med rec</paragraph></text>',
        """
    <entry><procedure classCode="PROC" moodCode="EVN">
      <id root="feedface-proc-0000-0000-000000000901"/>
      <code code="430193006"><originalText><reference value="#proc-1"/></originalText>
        <translation><originalText><reference value="#"/></originalText></translation>
      </code>
    </procedure></entry>
""",
    )
    path = _write(tmp_path, body=body)
    row = _row(document_ledger(path, _unparked(parse_document(path))), "section:47519-4")

    assert row.entries == {Disposition.UNSUPPORTED: 1}  # type: ignore[attr-defined]
