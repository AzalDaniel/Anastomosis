#!/usr/bin/env python3
"""A synthetic C-CDA corpus, generated rather than committed.

``tools/ccda_shape_report.py`` can only be pointed at documents that must never
leave the operator's machine, so the shapes that actually broke this adapter
were only ever visible to someone who could not show them to us. This generator
is the other half: it makes the shape space itself, out of nothing, so the
ingest conservation ledger (``anastomosis.sources.ccda.ledger``) can be run in
CI against thousands of documents that contain no patient.

Every byte it emits is synthetic and follows the repo's fixture conventions —
``feedface-`` ids, the 555 exchange, a never-issued SSN area (>= 900),
``example.com``, and the invented people who already live in ``tests/fixtures``.

**Deterministic.** The shape of document *n* is a pure function of *n*: the
document type cycles, the ten primary flags are a stride through their own
2^10 combinations (so 6,144 documents are every type against every combination,
exactly once), and the five secondary flags are read off the index's low bits so
all 32 of their combinations recur throughout. Taking the first N documents of
the enumeration therefore spans the space rather than sampling one corner of it,
which is what lets CI run a few hundred and a person run all 6,144 from the same
command. The seed varies only the CONTENT — which invented names, how many
entries — so a finding at ``--seed 7 --count 6144`` is reproducible by anyone
with that one line and no corpus at all.

Provenance of the vocabulary, stated because a fixture that quietly invents a
code teaches the wrong thing:

* Section LOINC codes and section template OIDs marked verified below come from
  ``tests/fixtures/ccda/feedface_ccd.xml`` and
  ``deliver/ccda_export/builder.py`` — this repository's own C-CDA reference
  (that fixture's README carries the provenance ledger).
* Document-type LOINC codes are the ones ``docs/PLAN.md`` already names
  (Progress 11506-3, Discharge 18842-5, Consult 34111-5) plus the CCD's
  34133-9 from the fixture. The Referral Note's 57133-1 is the one code here
  with NO in-repo reference; nothing in the measurement depends on it, and it
  is called out rather than blended in.
* The sections this adapter does not parse (chief complaint, payers, goals,
  family history, ...) carry their C-CDA section code and NO templateId,
  because this repository has no verified template OID for them and a
  plausible-looking invented OID is worse than an absent one.
* ``vendor_templates`` documents stamp sections with roots under
  ``2.16.840.1.113883.19``, the arc HL7's own example material uses, so a
  "vendor template we have never seen" is unmistakably not a real one.

Usage:

    python tools/ccda_corpus.py --out DIR                 # write the corpus
    python tools/ccda_corpus.py --ledger --count 6144     # the scale reading
    python tools/ccda_corpus.py --ledger --out report.json

The ledger run generates into a temporary directory, parses every document,
aggregates one corpus ledger, and prints the gap table. Nothing it prints is
patient-derived: the ledger's own emission whitelist is applied to the report
before a byte of it is written.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

V3 = "urn:hl7-org:v3"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
SDTC = "urn:hl7-org:sdtc"
NSMAP = {None: V3, "sdtc": SDTC, "xsi": XSI}

OID_LOINC = "2.16.840.1.113883.6.1"
OID_SNOMED = "2.16.840.1.113883.6.96"
OID_ICD10 = "2.16.840.1.113883.6.90"
OID_RXNORM = "2.16.840.1.113883.6.88"
OID_CVX = "2.16.840.1.113883.12.292"
OID_SSN = "2.16.840.1.113883.4.1"
OID_CPT = "2.16.840.1.113883.6.12"
OID_ACTCLASS = "2.16.840.1.113883.5.6"
OID_ACTCODE = "2.16.840.1.113883.5.4"

TPL_US_REALM_HEADER = ("2.16.840.1.113883.10.20.22.1.1", "2015-08-01")
TPL_CCD = ("2.16.840.1.113883.10.20.22.1.2", "2015-08-01")

#: The example arc, for the templateIds a document declares that we have never
#: seen. Deliberately not a plausible-looking C-CDA root: a reader who greps one
#: of these finds nothing, and concludes it was made up on purpose.
VENDOR_TEMPLATE_ROOT = "2.16.840.1.113883.19"

#: Invented people, borrowed from the fixtures that already carry them.
GIVEN_NAMES = ("Cora", "Synthia", "Ada", "Boris", "Cleo", "Gus", "Quinn")
FAMILY_NAMES = ("Specimen", "Probe", "Fixture", "Sample", "Placeholder")

#: The ten flags enumerated exhaustively against every document type.
PRIMARY_FLAGS: tuple[str, ...] = (
    "note_body",
    "missing_custodian",
    "null_flavors",
    "original_text",
    "utf16",
    "duplicate_sections",
    "vendor_templates",
    "narrative_only",
    "entries_without_narrative",
    "text_reference",
)

#: The five read off the index's low bits, so all 32 of their combinations
#: recur across the enumeration without multiplying its length by another 32.
SECONDARY_FLAGS: tuple[str, ...] = (
    "empty_sections",
    "extra_authors",
    "encompassing_encounter",
    "service_event",
    "header_participations",
)

_COMBINATIONS = 1 << len(PRIMARY_FLAGS)
#: Odd, therefore coprime with a power of two, therefore a stride that visits
#: every flag combination exactly once before repeating. A plain 0,1,2,... walk
#: would give a truncated run only documents with the first flags off, which is
#: the corner of the space where nothing is wrong.
_STRIDE = 613


# --- element helpers ---------------------------------------------------------


def _q(tag: str) -> str:
    return f"{{{V3}}}{tag}"


def _el(parent: etree._Element, tag: str, **attrs: str | None) -> etree._Element:
    node = etree.SubElement(parent, _q(tag))
    for name, value in attrs.items():
        if value is not None:
            node.set(name, value)
    return node


def _text_el(parent: etree._Element, tag: str, text: str, **attrs: str | None) -> etree._Element:
    node = _el(parent, tag, **attrs)
    node.text = text
    return node


def _typed(node: etree._Element, xsi_type: str) -> etree._Element:
    node.set(f"{{{XSI}}}type", xsi_type)
    return node


def _template(parent: etree._Element, template: tuple[str, str] | None) -> None:
    if template is not None:
        _el(parent, "templateId", root=template[0], extension=template[1])


def _person_name(parent: etree._Element, doc: Doc) -> None:
    name = _el(_el(parent, "assignedPerson"), "name")
    _text_el(name, "given", doc.pick(GIVEN_NAMES))
    _text_el(name, "family", doc.pick(FAMILY_NAMES))


# --- one document's shape ----------------------------------------------------


@dataclass(frozen=True)
class DocType:
    """One C-CDA document type: what its header says and what body it grows."""

    key: str
    loinc: str
    display: str
    title: str
    sections: tuple[str, ...]
    #: An Unstructured Document has no structuredBody at all — its entire
    #: clinical content is one embedded artifact — which is the shape that
    #: parses cleanly and yields a chart with nothing on it.
    unstructured: bool = False


@dataclass
class Doc:
    """Per-document state: the shape, the seeded content, and the id counter."""

    index: int
    doc_type: DocType
    flags: frozenset[str]
    rng: random.Random
    #: The narrative anchor entries point at when the document links its coded
    #: statements to their words by reference (set per section).
    narrative_id: str | None = None
    _next: int = field(default=0, init=False)

    def has(self, flag: str) -> bool:
        return flag in self.flags

    def id(self, tag: str) -> str:
        """A document-unique synthetic id.

        Unique because the ledger can only attribute a canonical object to the
        construct it came from when that construct's ``<id root>`` occurs once:
        a corpus that reused a root across two entries would measure its own
        laziness rather than the adapter.
        """
        self._next += 1
        return f"feedface-{tag}-0000-0000-{self._next:012d}"

    def pick(self, options: Sequence[str]) -> str:
        return options[self.rng.randrange(len(options))]


DOC_TYPES: tuple[DocType, ...] = (
    DocType(
        key="ccd",
        loinc="34133-9",
        display="Summarization of Episode Note",
        title="Continuity of Care Document",
        sections=(
            "problems",
            "allergies",
            "medications",
            "immunizations",
            "vitals",
            "results",
            "social",
            "encounters",
            "plan",
            "payers",
            "family_history",
            "goals",
        ),
    ),
    DocType(
        key="progress",
        loinc="11506-3",
        display="Progress note",
        title="Progress Note",
        sections=("problems", "vitals", "assessment", "plan", "chief_complaint"),
    ),
    DocType(
        key="consultation",
        loinc="34111-5",
        display="Consult note",
        title="Consultation Note",
        sections=(
            "reason_for_visit",
            "history_of_present_illness",
            "physical_exam",
            "problems",
            "assessment",
            "past_medical_history",
        ),
    ),
    DocType(
        key="discharge",
        loinc="18842-5",
        display="Discharge summary",
        title="Discharge Summary",
        sections=(
            "hospital_course",
            "discharge_diagnosis",
            "medications",
            "problems",
            "advance_directives",
            "health_concerns",
        ),
    ),
    DocType(
        key="referral",
        loinc="57133-1",
        display="Referral note",
        title="Referral Note",
        sections=("reason_for_visit", "problems", "medications", "allergies", "procedures"),
    ),
    DocType(
        key="unstructured",
        loinc="34133-9",
        display="Summarization of Episode Note",
        title="Scanned Document",
        sections=(),
        unstructured=True,
    ),
)


def shapes(count: int) -> Iterator[tuple[int, DocType, frozenset[str]]]:
    """The corpus's shapes, in a fixed order, ``count`` of them.

    Index-driven rather than seeded: document 3,001 is the same document on
    every machine and in every run, which is what makes "reproduce it with this
    command" an instruction rather than an approximation.
    """
    for index in range(count):
        doc_type = DOC_TYPES[index % len(DOC_TYPES)]
        combination = (index // len(DOC_TYPES) * _STRIDE) % _COMBINATIONS
        flags = {name for bit, name in enumerate(PRIMARY_FLAGS) if combination >> bit & 1}
        flags |= {name for bit, name in enumerate(SECONDARY_FLAGS) if index >> bit & 1}
        yield index, doc_type, frozenset(flags)


def documents(count: int, seed: int) -> Iterator[tuple[str, bytes]]:
    """``(filename, bytes)`` for the whole corpus, in enumeration order."""
    for index, doc_type, flags in shapes(count):
        doc = Doc(
            index=index,
            doc_type=doc_type,
            flags=flags,
            # Reproducibility, not secrecy: this seeds a corpus of invented
            # people, and the whole point is that the same command makes the
            # same one twice.
            rng=random.Random(f"{seed}:{index}"),  # noqa: S311
        )
        yield f"doc_{index:05d}.xml", _render(doc)


# --- header ------------------------------------------------------------------


def _header(root: etree._Element, doc: Doc) -> None:
    _el(root, "realmCode", code="US")
    _el(root, "typeId", root="2.16.840.1.113883.1.3", extension="POCD_HD000040")
    _template(root, TPL_US_REALM_HEADER)
    if doc.doc_type.key == "ccd":
        # The only document-type templateId this repository has a verified root
        # for. The others declare the US Realm Header alone rather than an OID
        # nobody here can check.
        _template(root, TPL_CCD)
    _el(root, "id", root=doc.id("docu"))
    _el(
        root,
        "code",
        code=doc.doc_type.loinc,
        displayName=doc.doc_type.display,
        codeSystem=OID_LOINC,
        codeSystemName="LOINC",
    )
    _text_el(root, "title", doc.doc_type.title)
    # A required element carrying nullFlavor instead of a value is ordinary in
    # real exports, and is what turns "read the attribute" into None everywhere
    # downstream.
    if doc.has("null_flavors"):
        _el(root, "effectiveTime", nullFlavor="UNK")
    else:
        _el(root, "effectiveTime", value="20230510150000-0500")
    _el(root, "confidentialityCode", code="N", codeSystem="2.16.840.1.113883.5.25")
    _el(root, "languageCode", code="en-US")


def _record_target(root: etree._Element, doc: Doc) -> None:
    role = _el(_el(root, "recordTarget"), "patientRole")
    _el(role, "id", root=OID_SSN, extension=f"9{doc.rng.randrange(100):02d}-65-4329")
    _el(role, "id", root=doc.id("pati"))
    addr = _el(role, "addr", use="HP")
    _text_el(addr, "streetAddressLine", "456 Sample Way")
    _text_el(addr, "city", "Springfield")
    _text_el(addr, "state", "WA")
    _text_el(addr, "postalCode", "98102")
    _el(role, "telecom", value="tel:+1(206)555-0177", use="HP")
    _el(role, "telecom", value="mailto:records@example.com")
    _patient(role, doc)


def _patient(role: etree._Element, doc: Doc) -> None:
    patient = _el(role, "patient")
    name = _el(patient, "name", use="L")
    _text_el(name, "given", doc.pick(GIVEN_NAMES))
    _text_el(name, "family", doc.pick(FAMILY_NAMES))
    if doc.has("null_flavors"):
        # nullFlavor="OTH" with the source's own spelling in originalText: the
        # shape that carries a value no code set has a code for.
        gender = _el(patient, "administrativeGenderCode", nullFlavor="OTH")
        _text_el(gender, "originalText", "Declined to state")
    else:
        _el(
            patient,
            "administrativeGenderCode",
            code="F",
            displayName="Female",
            codeSystem="2.16.840.1.113883.5.1",
        )
    _el(patient, "birthTime", value="19790406")
    _el(_el(patient, "languageCommunication"), "languageCode", code="en")


def _author(root: etree._Element, doc: Doc, *, device: bool = False) -> None:
    author = _el(root, "author")
    _el(author, "time", value="20230510150000-0500")
    assigned = _el(author, "assignedAuthor")
    _el(assigned, "id", root=doc.id("auth"))
    if device:
        # An authoring device is a named actor too — the system that generated
        # the document — and has no canonical home at all today.
        node = _el(assigned, "assignedAuthoringDevice")
        _text_el(node, "manufacturerModelName", "Synthetic EHR")
        _text_el(node, "softwareName", "Synthetic EHR Export")
        return
    _person_name(assigned, doc)
    organization = _el(assigned, "representedOrganization")
    _el(organization, "id", root=doc.id("orga"))
    _text_el(organization, "name", "Sample Family Medicine")


def _custodian(root: etree._Element, doc: Doc) -> None:
    organization = _el(
        _el(_el(root, "custodian"), "assignedCustodian"), "representedCustodianOrganization"
    )
    _el(organization, "id", root=doc.id("cust"))
    _text_el(organization, "name", "Sample Family Medicine")
    _el(organization, "telecom", value="tel:+1(206)555-0133", use="WP")


def _assigned_entity(
    parent: etree._Element,
    doc: Doc,
    id_tag: str,
    *,
    entity_tag: str = "assignedEntity",
    signature: bool = False,
) -> None:
    if signature:
        _el(parent, "time", value="20230510150000-0500")
        _el(parent, "signatureCode", code="S")
    entity = _el(parent, entity_tag)
    _el(entity, "id", root=doc.id(id_tag))
    _person_name(entity, doc)


def _participations(root: etree._Element, doc: Doc) -> None:
    """The header participations beyond author and custodian.

    Each names a person or an organization that took part in the care, and each
    is here because a document that HAS them is the case worth measuring — a
    corpus of documents with none would report a clean ledger by omission.
    """
    _assigned_entity(_el(root, "dataEnterer"), doc, "dtae")
    related = _el(_el(root, "informant"), "relatedEntity", classCode="PRS")
    _el(related, "code", code="SPS", displayName="spouse", codeSystem="2.16.840.1.113883.5.111")
    _assigned_entity(_el(root, "informationRecipient"), doc, "irec", entity_tag="intendedRecipient")
    _assigned_entity(_el(root, "legalAuthenticator"), doc, "lgau", signature=True)
    _assigned_entity(_el(root, "authenticator"), doc, "autn", signature=True)
    entity = _el(_el(root, "participant", typeCode="IND"), "associatedEntity", classCode="ECON")
    _el(entity, "id", root=doc.id("assc"))
    _el(entity, "telecom", value="tel:+1(206)555-0155", use="HP")


def _service_event(root: etree._Element, doc: Doc) -> None:
    event = _el(_el(root, "documentationOf"), "serviceEvent", classCode="PCPR")
    _el(event, "id", root=doc.id("serv"))
    _el(event, "effectiveTime", value="20230510")
    entity = _el(_el(event, "performer", typeCode="PRF"), "assignedEntity")
    _el(entity, "id", root=doc.id("perf"))
    _person_name(entity, doc)


def _encompassing_encounter(root: etree._Element, doc: Doc) -> None:
    """The visit the document itself is about.

    A Progress Note routinely says which encounter it documents HERE and
    nowhere else — such a document has no Encounters section — so a reader that
    looks only at 46240-8 sees a note attached to no visit at all.
    """
    encounter = _el(_el(root, "componentOf"), "encompassingEncounter")
    _el(encounter, "id", root=doc.id("encm"))
    _el(
        encounter,
        "code",
        code="99213",
        displayName="Office outpatient visit 15 minutes",
        codeSystem=OID_CPT,
        codeSystemName="CPT",
    )
    _el(encounter, "effectiveTime", value="20230510")
    party = _el(_el(encounter, "responsibleParty"), "assignedEntity")
    _el(party, "id", root=doc.id("resp"))
    _person_name(party, doc)


# --- entries -----------------------------------------------------------------


def _original_text(parent: etree._Element, doc: Doc, text: str) -> None:
    """The statement's own words — inline, by reference, or not at all.

    All three occur in the wild, and the reference form is the one that reads as
    empty to anything that does not resolve it: the words sit in the section's
    narrative, several elements away, behind a ``value="#id"``.
    """
    if doc.narrative_id is not None:
        _el(_el(parent, "originalText"), "reference", value=f"#{doc.narrative_id}")
    elif doc.has("original_text"):
        _text_el(parent, "originalText", text)


def _problem_entry(section: etree._Element, doc: Doc) -> None:
    act = _el(_el(section, "entry"), "act", classCode="ACT", moodCode="EVN")
    _template(act, ("2.16.840.1.113883.10.20.22.4.3", "2015-08-01"))
    _el(act, "id", root=doc.id("prob"))
    _el(act, "code", code="CONC", codeSystem=OID_ACTCLASS)
    _el(act, "statusCode", code="active")
    relationship = _el(act, "entryRelationship", typeCode="SUBJ")
    observation = _el(relationship, "observation", classCode="OBS", moodCode="EVN")
    _template(observation, ("2.16.840.1.113883.10.20.22.4.4", "2015-08-01"))
    _el(observation, "id", root=doc.id("prbo"))
    _el(
        observation,
        "code",
        code="55607006",
        displayName="Problem",
        codeSystem=OID_SNOMED,
        codeSystemName="SNOMED CT",
    )
    _el(observation, "statusCode", code="completed")
    _el(_el(observation, "effectiveTime"), "low", value="20220118")
    value = _typed(
        _el(
            observation,
            "value",
            code="59621000",
            displayName="Essential hypertension",
            codeSystem=OID_SNOMED,
        ),
        "CD",
    )
    _el(
        value,
        "translation",
        code="I10",
        displayName="Essential (primary) hypertension",
        codeSystem=OID_ICD10,
    )
    _original_text(value, doc, "Essential hypertension")


def _allergy_entry(section: etree._Element, doc: Doc) -> None:
    act = _el(_el(section, "entry"), "act", classCode="ACT", moodCode="EVN")
    _template(act, ("2.16.840.1.113883.10.20.22.4.30", "2015-08-01"))
    _el(act, "id", root=doc.id("alrg"))
    _el(act, "code", code="CONC", codeSystem=OID_ACTCLASS)
    _el(_el(act, "effectiveTime"), "low", value="20190302")
    relationship = _el(act, "entryRelationship", typeCode="SUBJ")
    observation = _el(relationship, "observation", classCode="OBS", moodCode="EVN")
    _template(observation, ("2.16.840.1.113883.10.20.22.4.7", "2014-06-09"))
    _el(observation, "id", root=doc.id("alro"))
    _el(observation, "code", code="ASSERTION", codeSystem=OID_ACTCODE)
    _typed(
        _el(
            observation,
            "value",
            code="416098002",
            displayName="Drug allergy",
            codeSystem=OID_SNOMED,
        ),
        "CD",
    )
    participant = _el(observation, "participant", typeCode="CSM")
    role = _el(participant, "participantRole", classCode="MANU")
    entity = _el(role, "playingEntity", classCode="MMAT")
    _el(entity, "code", code="7980", displayName="Penicillin G", codeSystem=OID_RXNORM)


def _medication_entry(section: etree._Element, doc: Doc) -> None:
    entry = _el(section, "entry")
    admin = _el(entry, "substanceAdministration", classCode="SBADM", moodCode="EVN")
    _template(admin, ("2.16.840.1.113883.10.20.22.4.16", "2014-06-09"))
    _el(admin, "id", root=doc.id("medi"))
    _el(admin, "statusCode", code="active")
    period = _typed(_el(admin, "effectiveTime"), "IVL_TS")
    _el(period, "low", value="20220118")
    _el(period, "high", nullFlavor="UNK")
    _el(admin, "doseQuantity", value="1", unit="{tablet}")
    _el(
        admin,
        "routeCode",
        code="C38288",
        displayName="Oral",
        codeSystem="2.16.840.1.113883.3.26.1.1",
    )
    product = _el(_el(admin, "consumable"), "manufacturedProduct", classCode="MANU")
    _template(product, ("2.16.840.1.113883.10.20.22.4.23", "2014-06-09"))
    material = _el(product, "manufacturedMaterial")
    _el(
        material,
        "code",
        code="314076",
        displayName="Lisinopril 10 MG Oral Tablet",
        codeSystem=OID_RXNORM,
        codeSystemName="RxNorm",
    )


def _immunization_entry(section: etree._Element, doc: Doc) -> None:
    entry = _el(section, "entry")
    admin = _el(entry, "substanceAdministration", classCode="SBADM", moodCode="EVN")
    _template(admin, ("2.16.840.1.113883.10.20.22.4.52", "2015-08-01"))
    _el(admin, "id", root=doc.id("immu"))
    _el(admin, "statusCode", code="completed")
    _el(admin, "effectiveTime", value="20221012")
    product = _el(_el(admin, "consumable"), "manufacturedProduct", classCode="MANU")
    _template(product, ("2.16.840.1.113883.10.20.22.4.54", "2014-06-09"))
    material = _el(product, "manufacturedMaterial")
    _el(
        material,
        "code",
        code="140",
        displayName="Influenza, seasonal, injectable",
        codeSystem=OID_CVX,
        codeSystemName="CVX",
    )
    _text_el(material, "lotNumberText", "SYN-0001")


def _organizer_entry(
    section: etree._Element, doc: Doc, readings: Sequence[tuple[str, str, str, str]]
) -> None:
    organizer = _el(_el(section, "entry"), "organizer", classCode="CLUSTER", moodCode="EVN")
    _el(organizer, "id", root=doc.id("orgz"))
    _el(organizer, "statusCode", code="completed")
    _el(organizer, "effectiveTime", value="20230510140000-0500")
    for code, display, reading, unit in readings:
        component = _el(organizer, "component")
        observation = _el(component, "observation", classCode="OBS", moodCode="EVN")
        _el(observation, "id", root=doc.id("meas"))
        _el(observation, "code", code=code, displayName=display, codeSystem=OID_LOINC)
        _el(observation, "statusCode", code="completed")
        _el(observation, "effectiveTime", value="20230510140000-0500")
        _typed(_el(observation, "value", value=reading, unit=unit), "PQ")


def _vitals_entry(section: etree._Element, doc: Doc) -> None:
    _organizer_entry(
        section,
        doc,
        (
            ("8480-6", "Systolic blood pressure", "122", "mm[Hg]"),
            ("8462-4", "Diastolic blood pressure", "78", "mm[Hg]"),
            ("8867-4", "Heart rate", "70", "/min"),
        ),
    )


def _results_entry(section: etree._Element, doc: Doc) -> None:
    _organizer_entry(
        section,
        doc,
        (
            ("2345-7", "Glucose", "92", "mg/dL"),
            ("2160-0", "Creatinine", "0.9", "mg/dL"),
        ),
    )


def _social_entry(section: etree._Element, doc: Doc) -> None:
    observation = _el(_el(section, "entry"), "observation", classCode="OBS", moodCode="EVN")
    _template(observation, ("2.16.840.1.113883.10.20.22.4.78", "2014-06-09"))
    _el(observation, "id", root=doc.id("socl"))
    _el(
        observation,
        "code",
        code="72166-2",
        displayName="Tobacco smoking status",
        codeSystem=OID_LOINC,
    )
    _el(observation, "statusCode", code="completed")
    _el(observation, "effectiveTime", value="20230510")
    _typed(
        _el(
            observation,
            "value",
            code="266919005",
            displayName="Never smoker",
            codeSystem=OID_SNOMED,
        ),
        "CD",
    )


def _encounter_entry(section: etree._Element, doc: Doc) -> None:
    encounter = _el(_el(section, "entry"), "encounter", classCode="ENC", moodCode="EVN")
    _template(encounter, ("2.16.840.1.113883.10.20.22.4.49", "2015-08-01"))
    _el(encounter, "id", root=doc.id("encr"))
    code = _el(
        encounter,
        "code",
        code="99213",
        displayName="Office outpatient visit 15 minutes",
        codeSystem=OID_CPT,
        codeSystemName="CPT",
    )
    _original_text(code, doc, "Office visit")
    _el(encounter, "effectiveTime", value="20230510")
    entity = _el(_el(encounter, "performer"), "assignedEntity")
    _el(entity, "id", root=doc.id("encp"))
    _person_name(entity, doc)


def _note_entry(section: etree._Element, doc: Doc) -> None:
    act = _el(_el(section, "entry"), "act", classCode="ACT", moodCode="EVN")
    _template(act, ("2.16.840.1.113883.10.20.22.4.202", "2016-11-01"))
    _el(act, "id", root=doc.id("note"))
    _el(act, "code", code="34109-9", displayName="Note", codeSystem=OID_LOINC)
    _text_el(act, "text", "Patient returns for routine follow-up and reports feeling well.")
    _el(act, "statusCode", code="completed")
    _el(act, "effectiveTime", value="20230510150000-0500")
    author = _el(act, "author")
    _el(author, "time", value="20230510150000-0500")
    _el(_el(author, "assignedAuthor"), "id", root=doc.id("nota"))


def _generic_entry(section: etree._Element, doc: Doc) -> None:
    """The entry shape this adapter has no dispatch for at all.

    Structurally an ordinary clinical statement — coded, dated, identified — and
    that is the point: nothing about it is malformed, and it still arrives
    nowhere.
    """
    observation = _el(_el(section, "entry"), "observation", classCode="OBS", moodCode="EVN")
    _el(observation, "id", root=doc.id("genr"))
    _el(observation, "code", code="75326-9", displayName="Problem", codeSystem=OID_LOINC)
    _el(observation, "statusCode", code="completed")
    _el(observation, "effectiveTime", value="20230510")
    _typed(
        _el(
            observation,
            "value",
            code="160245001",
            displayName="No current problems",
            codeSystem=OID_SNOMED,
        ),
        "CD",
    )


# --- section vocabulary ------------------------------------------------------


@dataclass(frozen=True)
class SectionSpec:
    """One section: its code, its narrative, and how to grow one entry of it.

    ``template`` is only ever a root this repository has verified. A section
    whose real template OID is not in the tree carries none, which is itself a
    shape the corpus should hold.
    """

    loinc: str
    title: str
    display: str
    narrative: str
    template: tuple[str, str] | None = None
    entry: Callable[[etree._Element, Doc], None] | None = None


SECTIONS: dict[str, SectionSpec] = {
    "problems": SectionSpec(
        loinc="11450-4",
        title="Problems",
        display="Problem List",
        narrative="Essential hypertension, active since 2022.",
        template=("2.16.840.1.113883.10.20.22.2.5.1", "2015-08-01"),
        entry=_problem_entry,
    ),
    "allergies": SectionSpec(
        loinc="48765-2",
        title="Allergies",
        display="Allergies and Adverse Reactions",
        narrative="Penicillin G, hives, moderate.",
        template=("2.16.840.1.113883.10.20.22.2.6.1", "2015-08-01"),
        entry=_allergy_entry,
    ),
    "medications": SectionSpec(
        loinc="10160-0",
        title="Medications",
        display="History of Medication Use",
        narrative="Lisinopril 10 MG oral tablet, one daily.",
        template=("2.16.840.1.113883.10.20.22.2.1.1", "2014-06-09"),
        entry=_medication_entry,
    ),
    "immunizations": SectionSpec(
        loinc="11369-6",
        title="Immunizations",
        display="History of Immunizations",
        narrative="Influenza vaccine administered 2022-10-12.",
        template=("2.16.840.1.113883.10.20.22.2.2.1", "2015-08-01"),
        entry=_immunization_entry,
    ),
    "vitals": SectionSpec(
        loinc="8716-3",
        title="Vital Signs",
        display="Vital Signs",
        narrative="BP 122/78 mm[Hg], HR 70 /min.",
        template=("2.16.840.1.113883.10.20.22.2.4.1", "2015-08-01"),
        entry=_vitals_entry,
    ),
    "results": SectionSpec(
        loinc="30954-2",
        title="Results",
        display="Relevant Diagnostic Tests and/or Laboratory Data",
        narrative="Basic metabolic panel within reference range.",
        template=("2.16.840.1.113883.10.20.22.2.3.1", "2015-08-01"),
        entry=_results_entry,
    ),
    "social": SectionSpec(
        loinc="29762-2",
        title="Social History",
        display="Social History",
        narrative="Never smoker.",
        template=("2.16.840.1.113883.10.20.22.2.17", "2015-08-01"),
        entry=_social_entry,
    ),
    "encounters": SectionSpec(
        loinc="46240-8",
        title="Encounters",
        display="History of Encounters",
        narrative="Office outpatient visit (2023-05-10).",
        template=("2.16.840.1.113883.10.20.22.2.22.1", "2015-08-01"),
        entry=_encounter_entry,
    ),
    "notes": SectionSpec(
        loinc="34109-9",
        title="Notes",
        display="Note",
        narrative="Progress note from the 2023-05-10 office visit.",
        template=("2.16.840.1.113883.10.20.22.2.65", "2016-11-01"),
        entry=_note_entry,
    ),
    "plan": SectionSpec(
        loinc="18776-5",
        title="Plan of Treatment",
        display="Plan of Treatment",
        narrative="Continue lisinopril and recheck blood pressure in three months.",
        template=("2.16.840.1.113883.10.20.22.2.10", "2014-06-09"),
        entry=_generic_entry,
    ),
    # No verified template OID in this repository for the rest: each carries its
    # section code and no templateId, rather than an OID nobody here can check.
    "chief_complaint": SectionSpec(
        loinc="10154-3",
        title="Chief Complaint",
        display="Chief Complaint",
        narrative="Blood pressure follow-up.",
    ),
    "reason_for_visit": SectionSpec(
        loinc="29299-5",
        title="Reason for Visit",
        display="Reason for Visit",
        narrative="Referred for hypertension management.",
    ),
    "history_of_present_illness": SectionSpec(
        loinc="10164-2",
        title="History of Present Illness",
        display="History of Present Illness",
        narrative="Three weeks of well-controlled readings at home.",
    ),
    "physical_exam": SectionSpec(
        loinc="29545-1",
        title="Physical Examination",
        display="Physical Findings",
        narrative="Unremarkable cardiopulmonary examination.",
    ),
    "assessment": SectionSpec(
        loinc="51848-0",
        title="Assessment",
        display="Assessment",
        narrative="Hypertension, controlled on current therapy.",
    ),
    "hospital_course": SectionSpec(
        loinc="8648-8",
        title="Hospital Course",
        display="Hospital Course",
        narrative="Admitted overnight for observation, discharged the following morning.",
    ),
    "discharge_diagnosis": SectionSpec(
        loinc="11535-2",
        title="Discharge Diagnosis",
        display="Hospital Discharge Diagnosis",
        narrative="Hypertensive urgency, resolved.",
        entry=_generic_entry,
    ),
    "past_medical_history": SectionSpec(
        loinc="11348-0",
        title="Past Medical History",
        display="History of Past Illness",
        narrative="Appendectomy 2004.",
        entry=_generic_entry,
    ),
    "family_history": SectionSpec(
        loinc="10157-6",
        title="Family History",
        display="History of Family Member Diseases",
        narrative="Mother: hypertension. Father: type 2 diabetes.",
        entry=_generic_entry,
    ),
    "advance_directives": SectionSpec(
        loinc="42348-3",
        title="Advance Directives",
        display="Advance Directives",
        narrative="Healthcare power of attorney on file.",
        entry=_generic_entry,
    ),
    "payers": SectionSpec(
        loinc="48768-6",
        title="Payers",
        display="Payment Sources",
        narrative="Synthetic Mutual, group 555-0100.",
        entry=_generic_entry,
    ),
    "goals": SectionSpec(
        loinc="61146-7",
        title="Goals",
        display="Goals",
        narrative="Maintain home blood-pressure readings under 130/80.",
        entry=_generic_entry,
    ),
    "health_concerns": SectionSpec(
        loinc="75310-3",
        title="Health Concerns",
        display="Health Concerns",
        narrative="Risk of cardiovascular disease.",
        entry=_generic_entry,
    ),
    "procedures": SectionSpec(
        loinc="47519-4",
        title="Procedures",
        display="History of Procedures",
        narrative="Electrocardiogram performed 2023-05-10.",
        entry=_generic_entry,
    ),
}


# --- body --------------------------------------------------------------------


def _narrative(section: etree._Element, spec: SectionSpec, doc: Doc) -> None:
    """The section's human-readable text, in the two shapes that matter.

    Under ``text_reference`` the narrative is an ID'd ``<content>`` — the
    standard C-CDA way for a coded entry to point at its own words — so a reader
    that does not resolve references sees an entry with nothing in it and a
    paragraph belonging to nobody.
    """
    text = _el(section, "text")
    if doc.narrative_id is not None:
        _text_el(text, "content", spec.narrative, ID=doc.narrative_id)
        return
    _text_el(text, "paragraph", spec.narrative)


def _section(body: etree._Element, spec: SectionSpec, doc: Doc, *, entries: int) -> None:
    doc.narrative_id = doc.id("narr") if doc.has("text_reference") else None
    section = _el(_el(body, "component"), "section")
    _template(section, spec.template)
    if doc.has("vendor_templates"):
        _el(
            section,
            "templateId",
            root=f"{VENDOR_TEMPLATE_ROOT}.{spec.loinc.split('-')[0]}",
            extension="2019-01-01",
        )
    _el(
        section,
        "code",
        code=spec.loinc,
        displayName=spec.display,
        codeSystem=OID_LOINC,
        codeSystemName="LOINC",
    )
    _text_el(section, "title", spec.title)
    if not doc.has("entries_without_narrative"):
        _narrative(section, spec, doc)
    if spec.entry is not None:
        for _ in range(entries):
            spec.entry(section, doc)


def _empty_section(body: etree._Element) -> None:
    """A section with a code and nothing else — declared, then never filled.

    Real exports carry these by the hundred, and a ledger that could not tell
    one from a section whose contents were dropped would call an empty chart a
    lossless migration.
    """
    section = _el(_el(body, "component"), "section")
    _el(
        section,
        "code",
        code="10157-6",
        displayName="History of Family Member Diseases",
        codeSystem=OID_LOINC,
    )


def _narrative_only_section(body: etree._Element) -> None:
    section = _el(_el(body, "component"), "section")
    _el(section, "code", code="51847-2", displayName="Assessment and Plan", codeSystem=OID_LOINC)
    _text_el(section, "title", "Assessment and Plan")
    _text_el(
        _el(section, "text"), "paragraph", "Continue current therapy; recheck in three months."
    )


def _structured_body(root: etree._Element, doc: Doc) -> None:
    body = _el(_el(root, "component"), "structuredBody")
    keys = list(doc.doc_type.sections)
    if doc.has("note_body"):
        keys.append("notes")
    if doc.has("duplicate_sections"):
        # A document legitimately splits one section code across two sections:
        # Problems (Active) and Problems (Resolved) are both 11450-4.
        keys.append(keys[0])
    for key in keys:
        _section(body, SECTIONS[key], doc, entries=doc.rng.randint(1, 3))
    if doc.has("narrative_only"):
        _narrative_only_section(body)
    if doc.has("empty_sections"):
        _empty_section(body)


def _non_xml_body(root: etree._Element) -> None:
    body = _el(_el(root, "component"), "nonXMLBody")
    _text_el(body, "text", "JVBERi0xLjQK", mediaType="application/pdf", representation="B64")


def _render(doc: Doc) -> bytes:
    root = etree.Element(_q("ClinicalDocument"), nsmap=NSMAP)
    _header(root, doc)
    _record_target(root, doc)
    _author(root, doc)
    if doc.has("extra_authors"):
        _author(root, doc)
        _author(root, doc, device=True)
    if not doc.has("missing_custodian"):
        _custodian(root, doc)
    if doc.has("header_participations"):
        _participations(root, doc)
    if doc.has("service_event"):
        _service_event(root, doc)
    if doc.has("encompassing_encounter"):
        _encompassing_encounter(root, doc)
    if doc.doc_type.unstructured:
        _non_xml_body(root)
    else:
        _structured_body(root, doc)
    encoding = "utf-16" if doc.has("utf16") else "utf-8"
    return etree.tostring(root, encoding=encoding, xml_declaration=True, pretty_print=True)


def write_corpus(out: Path, count: int, seed: int) -> int:
    out.mkdir(parents=True, exist_ok=True)
    written = 0
    for name, xml in documents(count, seed):
        (out / name).write_bytes(xml)
        written += 1
    return written


# --- the reading -------------------------------------------------------------


def run_ledger(count: int, seed: int) -> dict[str, object]:
    """Generate, parse, and account for a whole corpus in one pass.

    Each document lives in a temporary directory for exactly as long as it takes
    to read it: thousands of files are a corpus, not an artifact, and a corpus
    committed to a repository is a corpus nobody regenerates.
    """
    from anastomosis.sources.ccda.ledger import aggregate, document_ledger

    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        ledgers = []
        for name, xml in documents(count, seed):
            path = directory / name
            path.write_bytes(xml)
            ledgers.append(document_ledger(path))
            path.unlink()
        return aggregate(ledgers).as_report()


def _grouped(report: dict[str, object]) -> dict[str, list[int]]:
    """Construct rows summed across their template variants.

    The JSON keeps section code and templateId paired — that pairing is what a
    corpus is read for — but a human table wants one line per construct, and a
    reader comparing 61 rows against 37 concepts loses the finding in the
    bookkeeping.
    """
    totals: dict[str, list[int]] = {}
    constructs = report["constructs"]
    assert isinstance(constructs, list)
    for entry in constructs:
        instances = entry["instances"]
        row = totals.setdefault(str(entry["construct"]), [0, 0, 0, 0, 0, 0])
        row[0] += int(entry["offered"])
        row[1] += int(instances.get("structurally_parsed", 0))
        row[2] += int(instances.get("narrative_preserved", 0))
        row[3] += int(instances.get("unsupported", 0))
        row[4] += int(instances.get("source_empty", 0))
        row[5] = max(row[5], int(entry["present_in_documents"]))
    return totals


def _gap_table(report: dict[str, object]) -> list[str]:
    """The corpus reading as a markdown table, worst first.

    Sorted by what was lost rather than by name: a table ordered alphabetically
    buries the finding among the constructs that were fine, and this table
    exists to be read by whoever is deciding what to fix.
    """
    lines = [
        "| Construct | Offered | Parsed | Narrative only | Unsupported | Empty | Docs |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    rows = sorted(_grouped(report).items(), key=lambda item: (-item[1][3], -item[1][0], item[0]))
    for construct, (offered, parsed, narrative, unsupported, empty, docs) in rows:
        lines.append(
            f"| `{construct}` | {offered} | {parsed} | {narrative} | "
            f"{unsupported} | {empty} | {docs} |"
        )
    return lines


def _entry_table(report: dict[str, object]) -> list[str]:
    """What became of the ``<entry>`` elements, section by section.

    A second table rather than six more columns on the first: a section and its
    entries can disagree — a section whose narrative was kept while every coded
    entry inside it was dropped is the most common way a chart looks preserved
    and is not — and a reader has to be able to see the two answers side by side
    without reading across fourteen columns.
    """
    totals: dict[str, Counter[str]] = {}
    constructs = report["constructs"]
    assert isinstance(constructs, list)
    for entry in constructs:
        if entry["entries"]:
            totals.setdefault(str(entry["construct"]), Counter()).update(entry["entries"])
    lines = [
        "| Section | Entries | Parsed | Narrative only | Unsupported | Empty |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for construct, counts in sorted(totals.items(), key=lambda item: -sum(item[1].values())):
        lines.append(
            f"| `{construct}` | {sum(counts.values())} | "
            f"{counts['structurally_parsed']} | {counts['narrative_preserved']} | "
            f"{counts['unsupported']} | {counts['source_empty']} |"
        )
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a synthetic C-CDA corpus.")
    parser.add_argument("--count", type=int, default=6144, help="documents to generate")
    parser.add_argument("--seed", type=int, default=7, help="content seed (shape is index-driven)")
    parser.add_argument("--out", type=Path, help="corpus directory (or report JSON with --ledger)")
    parser.add_argument(
        "--ledger", action="store_true", help="parse the corpus and print the gap table"
    )
    args = parser.parse_args(argv)

    if not args.ledger:
        out = args.out or Path("ccda_corpus")
        print(
            f"wrote {write_corpus(out, args.count, args.seed)} synthetic C-CDA documents to {out}"
        )
        return 0

    report = run_ledger(args.count, args.seed)
    print(f"documents  : {report['documents']}")
    print(f"constructs : {report['constructs_offered']}")
    print(f"entries    : {report['entries_offered']}")
    print()
    print("\n".join(_gap_table(report)))
    print()
    print("\n".join(_entry_table(report)))
    if args.out is not None:
        args.out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
