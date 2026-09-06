"""Every construct is proved twice: what the adapter parses asserts
``structurally_parsed``, what it drops asserts ``unsupported`` -- so the
ledger cannot pass by reciting the parser's own beliefs. Synthetic
throughout (``feedface-`` ids, 555 exchange)."""

from __future__ import annotations

import dataclasses
import itertools
import random
import re
from collections import Counter
from pathlib import Path

import pytest

from anastomosis.core.ccda_codes import (
    EXT_PRIOR_LOSS_NARRATIVE,
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
    """``record`` with the verbatim entry-copy extensions removed, so citation
    tests must resolve ``<reference>`` targets from real parsed data rather
    than the parked mirror (RULES.md 59)."""
    for key in [k for k in record.patient.extensions if k.startswith("ccda:entries:")]:
        del record.patient.extensions[key]
    return record


def _row(ledger: object, construct: str) -> object:
    """The one merged row for ``construct``, read through ``aggregate`` since a
    document ledger holds one row per section OCCURRENCE, not per construct."""
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
    """Constructs CDA's schema gives no ``<id>`` (Device author, informant) are
    credited on stated content instead, never guessed."""
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
    """Content evidence must match exactly -- no case-fold, no trimmed padding."""
    path = _write(tmp_path, header=_DEVICE_AUTHOR)
    record = parse_document(path)
    record.practitioners[0].extensions["ccda:softwareName"] = recorded
    ledger = document_ledger(path, record)
    assert _sole(ledger, "participation:assignedAuthoringDevice") is Disposition.UNSUPPORTED
    assert _row(ledger, "participation:assignedAuthoringDevice").unlinkable == 1  # type: ignore[attr-defined]


def test_two_identical_actors_and_one_object_credit_one_parse(tmp_path: Path) -> None:
    path = _write(tmp_path, header=_INFORMANT * 2)
    record = parse_document(path)
    assert len(record.practitioners) == 2
    del record.practitioners[1]
    row = _row(document_ledger(path, record), "participation:informant")
    assert row.counted(Disposition.STRUCTURALLY_PARSED) == 1  # type: ignore[attr-defined]
    assert row.counted(Disposition.UNSUPPORTED) == 1  # type: ignore[attr-defined]
    assert row.unlinkable == 1  # type: ignore[attr-defined]


def test_a_shared_id_root_is_still_refused_where_content_would_match(tmp_path: Path) -> None:
    """Content evidence never overrides a root-sharing refusal (RULES.md 58): an
    actor that carries an id is judged by it even where content would also
    match."""
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
    """``ID_LESS_CONSTRUCTS`` is a fact about CDA's schema, never inferred at
    runtime from one document's missing id."""
    assert set(ID_LESS_CONSTRUCTS) == {"assignedAuthoringDevice", "informant"}
    assert set(ID_LESS_CONSTRUCTS) <= set(PARTICIPATION_PATHS)


def test_a_content_credited_reading_still_names_nobody(tmp_path: Path) -> None:
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
    """A scanned referral whose whole clinical content is one embedded PDF: no
    coded sections, just the ``nonXMLBody`` artifact."""
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


# --- the places themselves (#329) --------------------------------------------


def test_a_fact_answers_as_often_as_it_is_asked() -> None:
    """An id is true, not owed. Ten constructs carrying it all link."""
    facts = ledger._Facts(frozenset({"2.16.840.1.113883.19"}))
    assert [facts.holds("2.16.840.1.113883.19") for _ in range(10)] == [True] * 10
    assert [facts.any_of({"2.16.840.1.113883.19"}) for _ in range(10)] == [True] * 10


def test_a_fact_has_no_way_to_be_spent() -> None:
    """Frozen dataclass over a ``frozenset``: no method removes a member, so a
    fact cannot be spent by accident."""
    facts = ledger._Facts(frozenset({"a"}))
    with pytest.raises(dataclasses.FrozenInstanceError):
        facts.held = frozenset()  # type: ignore[misc]
    assert not hasattr(facts, "take")
    assert not any(name for name in vars(type(facts)) if name in {"take", "spend", "pop", "remove"})


def test_a_pool_yields_no_more_than_it_holds_however_it_is_asked() -> None:
    """Both pools decrement before answering, so a credit can never exceed what
    was offered."""
    narrative = ledger._KeyedPool(Counter({("Allergies", "none known"): 2}))
    draws = [narrative.take(("Allergies", "none known")) for _ in range(5)]
    assert draws == [True, True, False, False, False]

    one = Practitioner(family_name="Reyes")
    objects = ledger._MatchedPool([one, Practitioner(family_name="Okafor")])
    hits = [objects.take(lambda obj: obj is one) for _ in range(4)]
    assert hits == [True, False, False, False]


def test_a_pool_cannot_be_consulted_without_spending_it() -> None:
    """Taking is the only query surface of every pool; there is no way to check
    a claim without spending it."""
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
    path = _write(tmp_path, body=_TEXTLESS_PROBLEMS)
    record = parse_document(path)
    del record.patient.extensions["ccda:entries:11450-4"]
    row = _row(document_ledger(path, record), "section:11450-4")
    assert row.entries == {Disposition.UNSUPPORTED: 2}  # type: ignore[attr-defined]
    assert _sole(document_ledger(path, record), "section:11450-4") is Disposition.UNSUPPORTED


def test_one_stored_copy_credits_one_entry_not_two(tmp_path: Path) -> None:
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


# --- the reading a physician gets (#315) --------------------------------------


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
    """A shared id root may move a construct from credited to uncredited, but the
    verdict must never assert a cause ("dropped", "no place") -- the loss
    here is the instrument's blind spot, not a stated adapter failure."""
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


# --- what a positive disposition has to be backed by -------------------------


_SERVICE_EVENT_HEADER = """
  <documentationOf><serviceEvent classCode="PCPR">
    <id root="feedface-serv-0000-0000-000000000901"/>
    <code code="PCPR" displayName="Synthetic care period"/>
    <effectiveTime><low value="20200101"/><high value="20201231"/></effectiveTime>
  </serviceEvent></documentationOf>
"""


def test_an_empty_parked_payload_is_not_a_preserved_participation(tmp_path: Path) -> None:
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
    """Only statement kinds this document has been SEEN to link are required to;
    a kind that never links here is not an obligation."""
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
    """Captured before the tree's in-place rewrite, not after (RULES.md 59)."""
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
    """Renders no text, not merely lacks a ``<text>`` element -- an empty
    element, whitespace only, ``nullFlavor``, and multimedia-only text all
    count as none."""
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
# A cited cell must be one this section's own <text> defines; one cell answers
# for one entry.


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
    """A section's whole ``<text>`` is not itself a cell; a cell is something
    INSIDE the narrative."""
    body = _procedures(
        '<text ID="sect-text">Continue lisinopril and recheck in three months.</text>',
        _cites(1, "#sect-text", "No current problems")
        + _cites(2, "#sect-text", "Colonoscopy due 2031"),
    )
    path = _write(tmp_path, body=body)
    row = _row(document_ledger(path, _unparked(parse_document(path))), "section:47519-4")

    assert row.entries == {Disposition.UNSUPPORTED: 2}  # type: ignore[attr-defined]


def test_a_cell_in_another_section_does_not_answer_here(tmp_path: Path) -> None:
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


# --- narrative anchors nested inside real C-CDA table markup -----------------


def test_a_cell_deep_inside_a_narrative_table_is_still_a_cell(tmp_path: Path) -> None:
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


def _stamped_ledger(title_suffix: str, *paragraphs: str) -> str:
    """One 51899-3 section stamped as this exporter's own loss ledger."""
    body = "".join(f"<paragraph>{p}</paragraph>" for p in paragraphs)
    return f"""
  <component><section>
    <templateId root="{LOSS_NARRATIVE_TEMPLATE_ROOT}"/>
    <code code="51899-3" codeSystem="2.16.840.1.113883.6.1"/>
    <title>{LOSS_NARRATIVE_TITLE}{title_suffix}</title>
    <text>{body}</text>
  </section></component>
"""


def _out_of_the_walk(kind: str, ledger: str) -> str:
    """``ledger``, placed where the parser's section walk does not reach --
    nested under another ``<structuredBody>``, or with no ``<component>``
    wrapper at all."""
    if kind == "nested":
        return f"""
  <component><section>
    <code code="30954-2" codeSystem="2.16.840.1.113883.6.1"/>
    <title>Results</title>
    <text>Ordinary results prose.</text>
    <component><structuredBody>{ledger}</structuredBody></component>
  </section></component>
"""
    return ledger.replace("<component><section>", "<section>").replace(
        "</section></component>", "</section>"
    )


@pytest.mark.parametrize("burial", ["nested", "no component"])
@pytest.mark.parametrize(
    "buried_lines", [("coverage: 1 dropped", "buried: 1 dropped"), ("coverage: 1 dropped",)]
)
@pytest.mark.parametrize("readable_first", [False, True])
def test_a_loss_ledger_the_parser_never_reached_is_not_credited(
    tmp_path: Path, burial: str, buried_lines: tuple[str, ...], readable_first: bool
) -> None:
    readable = _stamped_ledger("", "coverage: 1 dropped", "and: 1 dropped")
    buried = _out_of_the_walk(burial, _stamped_ledger(" (buried)", *buried_lines))
    path = _write(tmp_path, body=readable + buried if readable_first else buried + readable)
    record = parse_document(path)
    assert "buried" not in repr(record.patient.extensions), (
        "the parser reached the buried section after all, so this is not the case at issue"
    )
    rows = [row for row in document_ledger(path, record).rows if row.construct == "section:51899-3"]
    assert len(rows) == 2

    # Rows come in document order, so the readable one is first or second by
    # the same parameter that placed it.
    read_row, buried_row = rows if readable_first else rows[::-1]
    assert buried_row.instances == {Disposition.UNSUPPORTED: 1}, (
        "a ledger the parser never reached was reported preserved"
    )
    assert read_row.instances == {Disposition.NARRATIVE_PRESERVED: 1}, (
        "the ledger the parser did read lost its own credit to the buried one"
    )


def test_a_ledger_that_said_nothing_is_not_credited_for_saying_nothing(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, body=_stamped_ledger(""))
    rows = [
        row
        for row in document_ledger(path, parse_document(path)).rows
        if row.construct == "section:51899-3"
    ]
    assert [row.instances for row in rows] == [{Disposition.UNSUPPORTED: 1}]


def test_a_ledger_line_pointing_into_the_narrative_is_still_its_own_line(
    tmp_path: Path,
) -> None:
    problems = """
  <component><section>
    <code code="11450-4" codeSystem="2.16.840.1.113883.6.1"/>
    <title>Problems</title>
    <text><paragraph ID="p1">coverage: 1 dropped</paragraph></text>
  </section></component>
"""
    ledger = _stamped_ledger("", "coverage: 1 dropped", 'see <reference value="#p1"/>')
    path = _write(tmp_path, body=problems + ledger)
    record = parse_document(path)

    # What was STORED is asserted first and on purpose. A fix that made the two
    # sides agree by capturing BEFORE the parser resolves the reference passes
    # the verdict assertion below and quietly stores "see" — and a line that is
    # only a reference stores as nothing at all, so the carried-forward appendix
    # loses it. Agreement bought by deleting content is the wrong trade, and
    # only this assertion can tell the two fixes apart.
    assert record.patient.extensions[EXT_PRIOR_LOSS_NARRATIVE]["entries"] == [
        "coverage: 1 dropped",
        "see coverage: 1 dropped",
    ]

    rows = [row for row in document_ledger(path, record).rows if row.construct == "section:51899-3"]
    assert [row.instances for row in rows] == [{Disposition.NARRATIVE_PRESERVED: 1}]


def test_a_ledger_line_that_is_only_a_reference_is_kept_whole(tmp_path: Path) -> None:
    problems = """
  <component><section>
    <code code="11450-4" codeSystem="2.16.840.1.113883.6.1"/>
    <title>Problems</title>
    <text><paragraph ID="p1">prior.coverage.plan = HMO</paragraph></text>
  </section></component>
"""
    ledger = _stamped_ledger("", "a = 1", '<reference value="#p1"/>')
    path = _write(tmp_path, body=problems + ledger)
    record = parse_document(path)

    assert record.patient.extensions[EXT_PRIOR_LOSS_NARRATIVE]["entries"] == [
        "a = 1",
        "prior.coverage.plan = HMO",
    ], "the line the appendix exists to carry was dropped on the way in"

    rows = [row for row in document_ledger(path, record).rows if row.construct == "section:51899-3"]
    assert [row.instances for row in rows] == [{Disposition.NARRATIVE_PRESERVED: 1}]


def test_an_ordinary_sections_prose_that_points_elsewhere_is_not_reported_lost(
    tmp_path: Path,
) -> None:
    body = """
  <component><section>
    <code code="11450-4" codeSystem="2.16.840.1.113883.6.1"/>
    <title>Problems</title>
    <text><paragraph ID="p1">asthma</paragraph></text>
  </section></component>
  <component><section>
    <code code="10160-0" codeSystem="2.16.840.1.113883.6.1"/>
    <title>Medications</title>
    <text><paragraph>see <reference value="#p1"/></paragraph></text>
  </section></component>
"""
    path = _write(tmp_path, body=body)
    record = parse_document(path)
    assert record.patient.extensions["ccda:section:10160-0"]["text"] == "see asthma", (
        "the parser stopped resolving the reference, so this tests nothing"
    )

    rows = [row for row in document_ledger(path, record).rows if row.construct == "section:10160-0"]
    assert [row.instances for row in rows] == [{Disposition.NARRATIVE_PRESERVED: 1}]


def test_a_cell_that_is_only_a_pointer_still_answers_for_the_entry_citing_it(
    tmp_path: Path,
) -> None:
    body = """
  <component><section>
    <code code="11450-4" codeSystem="2.16.840.1.113883.6.1"/>
    <title>Problems</title>
    <text><paragraph ID="p1">asthma, mild persistent</paragraph></text>
  </section></component>
  <component><section>
    <code code="42349-1" codeSystem="2.16.840.1.113883.6.1"/>
    <title>Reason for Referral</title>
    <text>
      <paragraph ID="c0">seen for the below</paragraph>
      <paragraph ID="c1"><reference value="#p1"/></paragraph>
    </text>
    <entry><observation classCode="OBS" moodCode="EVN">
      <templateId root="1.2.3.4.5.6.7.8.9"/>
      <code code="99999-9" codeSystem="2.16.840.1.113883.6.1"/>
      <text><reference value="#c1"/></text>
    </observation></entry>
  </section></component>
"""
    path = _write(tmp_path, body=body)
    record = parse_document(path)
    assert "asthma" in record.patient.extensions["ccda:section:42349-1"]["text"], (
        "the parser stopped resolving the reference, so this tests nothing"
    )
    rows = [row for row in document_ledger(path, record).rows if row.construct == "section:42349-1"]
    assert [row.instances for row in rows] == [{Disposition.NARRATIVE_PRESERVED: 1}]
    assert [row.entries for row in rows] == [{Disposition.NARRATIVE_PRESERVED: 1}], (
        "the row says the narrative survived; the entry citing a cell of it says it did not"
    )


def test_the_hydrated_twin_of_one_document_is_not_kept_after_the_next(tmp_path: Path) -> None:
    from anastomosis.sources.ccda import ledger as module

    module._hydrated_sections.cache_clear()
    for n in range(3):
        directory = tmp_path / str(n)
        directory.mkdir()
        document_ledger(_write(directory, body=_stamped_ledger("", "a = 1")))
    assert module._hydrated_sections.cache_info().currsize <= 1


def test_a_loss_ledger_key_that_is_not_a_list_of_strings_is_read_not_raised(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, body=_stamped_ledger("", "a = 1"))
    for entries in ([{"k": 1}, "a = 1"], "a = 1", 7):
        record = parse_document(path)
        record.patient.extensions[EXT_PRIOR_LOSS_NARRATIVE] = {
            "generation": None,
            "entries": entries,
        }
        rows = [
            row for row in document_ledger(path, record).rows if row.construct == "section:51899-3"
        ]
        assert len(rows) == 1, f"the reading did not survive entries={type(entries).__name__}"


@pytest.mark.parametrize("second_entry", ["allergy severity: 2 dropped", "coverage: 1 dropped"])
def test_two_loss_ledgers_the_parser_does_read_are_both_credited(
    tmp_path: Path, second_entry: str
) -> None:
    body = _stamped_ledger(" (first)", "coverage: 1 dropped") + _stamped_ledger(
        " (second)", second_entry
    )
    path = _write(tmp_path, body=body)
    rows = [
        row
        for row in document_ledger(path, parse_document(path)).rows
        if row.construct == "section:51899-3"
    ]
    assert len(rows) == 2
    assert [row.instances for row in rows] == [
        {Disposition.NARRATIVE_PRESERVED: 1},
        {Disposition.NARRATIVE_PRESERVED: 1},
    ]


def test_a_line_the_record_kept_only_once_does_not_answer_for_two(
    tmp_path: Path,
) -> None:
    line = "coverage: 1 dropped"
    path = _write(tmp_path, body=_stamped_ledger("", line, line))
    record = parse_document(path)
    store = record.patient.extensions[EXT_PRIOR_LOSS_NARRATIVE]
    assert store["entries"] == [line, line], "the parser stopped keeping both copies"

    store["entries"] = [line]
    rows = [row for row in document_ledger(path, record).rows if row.construct == "section:51899-3"]
    assert [row.instances for row in rows] == [{Disposition.UNSUPPORTED: 1}], (
        "one kept line answered for two offered ones"
    )


def test_a_stamped_loss_ledger_does_not_claim_a_foreign_sections_copy(
    tmp_path: Path,
) -> None:
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
    entry = _SHARED_CODE_ENTRY.replace("75326-9", "75327-9")
    body = f"""
  <component><section><title>No code at all</title>{_SHARED_CODE_ENTRY}</section></component>
  <component><section><title>Also no code</title>{entry}</section></component>
"""
    path = _write(tmp_path, body=body)
    row = _row(document_ledger(path, parse_document(path)), "section:none")

    assert row.entries == {Disposition.NARRATIVE_PRESERVED: 2}  # type: ignore[attr-defined]


def test_a_padded_reference_value_still_names_its_cell(tmp_path: Path) -> None:
    body = _procedures(
        '<text><paragraph ID="proc-1">Medication reconciliation</paragraph></text>',
        _cites(1, " #proc-1 ", "A"),
    )
    path = _write(tmp_path, body=body)
    row = _row(document_ledger(path, _unparked(parse_document(path))), "section:47519-4")

    assert row.entries == {Disposition.NARRATIVE_PRESERVED: 1}  # type: ignore[attr-defined]


# --- what round four found ----------------------------------------------------


def test_a_section_the_parser_never_walked_parks_nothing(tmp_path: Path) -> None:
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
    """The most entries any disjoint assignment of cited cells could credit --
    the brute-force specification the ledger's own heuristic is measured
    against."""
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
    """The ledger may credit fewer entries than an optimal assignment, never more
    (RULES.md 57), checked here against brute force."""
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
    body = _procedures(
        '<text><content ID="a1">Colonoscopy</content>'
        '<content ID="a3">Appendectomy</content></text>',
        _cites(1, "#a1", "A") + _cites(3, "#a3", "C"),
    )
    path = _write(tmp_path, body=body)
    row = _row(document_ledger(path, _unparked(parse_document(path))), "section:47519-4")

    assert row.entries == {Disposition.NARRATIVE_PRESERVED: 2}  # type: ignore[attr-defined]


def test_a_comment_in_the_narrative_is_not_a_cell(tmp_path: Path) -> None:
    body = _procedures(
        '<text><!-- generated by the exporter --><paragraph ID="proc-1">Med rec</paragraph></text>',
        _cites(1, "#proc-1", "A"),
    )
    path = _write(tmp_path, body=body)
    row = _row(document_ledger(path, _unparked(parse_document(path))), "section:47519-4")

    assert row.entries == {Disposition.NARRATIVE_PRESERVED: 1}  # type: ignore[attr-defined]


def test_a_bare_hash_is_a_citation_that_cannot_resolve(tmp_path: Path) -> None:
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
