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

import re
from pathlib import Path

import pytest

from anastomosis.core.conservation import ConservationError
from anastomosis.core.model import Practitioner, Provenance
from anastomosis.sources.ccda.ledger import (
    Disposition,
    aggregate,
    assert_emittable,
    document_ledger,
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


@pytest.mark.parametrize("construct", ["participation:author", "participation:custodian"])
def test_a_participation_with_no_slot_is_named_unsupported(
    fixture_ledger: object, construct: str
) -> None:
    """The 2,103-document finding, reproduced on one document: the author and
    the custodian are right there in the header, and no canonical object came
    from either."""
    assert _sole(fixture_ledger, construct) is Disposition.UNSUPPORTED


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

    Same document, same ledger; the only change is a record that carries a
    Practitioner whose provenance names the author's id. If the verdict moved,
    the ledger is reading the record — which is what makes it able to grade the
    fix that has not been written yet.
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
    assert _sole(document_ledger(path), "participation:author") is Disposition.UNSUPPORTED

    record = parse_document(path)
    record.practitioners.append(
        Practitioner(
            given_name="Quinn",
            family_name="Authorman",
            provenance=Provenance(
                source_system="ccda",
                source_file=path.name,
                source_id="feedface-auth-0000-0000-000000000009",
            ),
        )
    )
    assert (
        _sole(document_ledger(path, record), "participation:author")
        is Disposition.STRUCTURALLY_PARSED
    )


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
    """A section with entries and no ``<text>`` stores ``{"title": ..., "text":
    None}``. The word "Payers" is not a recovery of the coverage that was in the
    entries, and counting those entries as narrative-preserved on the strength
    of a stored title is the flattering arithmetic this ledger refuses."""
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
    ledger = document_ledger(_write(tmp_path, body=body))
    assert _row(ledger, "section:48768-6").entries == {Disposition.UNSUPPORTED: 1}  # type: ignore[attr-defined]
    # The section itself did keep something — its title — and says so.
    assert _sole(ledger, "section:48768-6") is Disposition.NARRATIVE_PRESERVED


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


def test_an_unstructured_document_is_a_body_nobody_read(tmp_path: Path) -> None:
    """The shape that parses perfectly and yields a chart with nothing on it:
    a scanned referral whose whole clinical content is one embedded artifact."""
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
    assert _sole(ledger, "body:nonXMLBody") is Disposition.UNSUPPORTED
    record = parse_document(path)
    assert not record.documents and not record.encounters and not record.conditions


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

    monkeypatch.setattr(module, "_entry_dispositions", lambda *args: ({}, 0), raising=True)
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
