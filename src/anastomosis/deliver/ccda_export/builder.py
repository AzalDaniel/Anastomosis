"""PatientRecord → C-CDA R2.1 / CCD XML (the inverse of ``sources/ccda``).

This is the export side of the C-CDA round trip. Its single hard contract is
that the document it emits is read back by **this repository's own**
``sources/ccda/parser.py`` into the same canonical clinical content
(``parse(build_ccd(record)) ≈ record``). Every xpath, LOINC section code,
template id, and ``xsi:type`` here is chosen to match exactly what that parser
traverses — the parser is the spec, never the other way around.

Scope honesty
-------------
The output targets two things and only two things:

* **Round-trip fidelity** with anastomosis's own parser (the test suite proves
  it section by section), and
* the **structural shape the parser consumes** — a well-formed HL7 v3
  ``ClinicalDocument`` carrying the US-Realm header, the CCD template ids, and
  the entry shapes ``sources/ccda/parser.py`` traverses.

It does **NOT** target — and does not achieve — full HL7 **CDA R2 XSD schema
validity**: the document omits mandatory CDA header participations the parser
ignores (``author``, ``custodian``) and uses non-OID ``root`` values for source
identifiers (e.g. a vendor system name or a ``urn:anastomosis:id:*`` MRN root),
so it does **not** validate against the HL7 ``CDA.xsd`` schema set. It likewise
does not target C-CDA R2.1 **Schematron** template conformance or ONC
certification (``codeSystemName`` everywhere, value-set binding strength,
narrative ``<reference>`` linkage). XSD-structural and Schematron conformance are
a later, separate effort via external validator tooling (see ``docs/PLAN.md``
M6); today the deliverable is the own-parser round trip, nothing broader.

Declared losses (the no-silent-drop rule, made explicit)
--------------------------------------------------------
A canonical ``PatientRecord`` carries far more than standard CDA has structured
slots for. The losslessness invariant forbids dropping any of it *silently*. The
exporter handles this in two tiers:

1. **The loss narrative (recoverable).** Every populated source field that no
   structured emitter consumes — native canonical fields with no CDA slot
   (``Encounter.chief_complaint``, ``Patient.gender_identity``,
   ``Immunization.expires`` …), record-level lists the parser cannot produce
   (``prescriptions``, ``coverages``, ``family_history`` …), and every
   ``extensions`` key other than the four this format round-trips natively onto
   their models (:data:`_NATIVE_EXT_KEYS`) — including the ``ccda:section:*``
   narratives an earlier CDA ingest captured, which no emitter here re-derives
   — is serialized as deterministic ``path = value`` lines
   into a namespaced ``<text>`` block on a dedicated extensions section (LOINC
   ``51899-3``), one ``<paragraph>`` per entry. The section is stamped with
   :data:`LOSS_NARRATIVE_TEMPLATE_ROOT` so a later ingest can tell THIS tool's
   loss ledger from a third party's 51899-3 section; ``sources/ccda`` reads a
   stamped section back into ``patient.extensions["ccda:prior_loss_narrative"]``
   as discrete entries, so the data is **visible in the document and recoverable
   from re-ingest** — just as narrative text, NOT back onto its original typed
   models. The set of fields each structured emitter *does* consume is declared
   in :data:`_EXPORTED_FIELDS`, kept adjacent to each emitter so drift is caught
   in review; everything outside that allowlist flows to the narrative
   automatically (no per-field whack-a-mole).

   **Generations.** Export → ingest → export is a loop a migration legitimately
   runs more than once, and the ledger must not grow without bound around it.
   Re-ingesting a stamped section as ONE ``ccda:section:51899-3`` narrative made
   generation N's ledger swallow generation N-1's whole text as a single line —
   an ever-growing blob that drowned the real entries. Instead the entries come
   back discrete and are re-emitted as a carry-forward appendix, deduplicated
   against this generation's own entries by :func:`_carried_forward`: identical
   entries collapse, distinct ones all survive at their multiplicity, and the
   document carries exactly one 51899-3 section stamped with its generation
   number. The ledger therefore reaches a fixed point at generation 2.

2. **Truly unrecoverable losses (:data:`DECLARED_LOSSES`).** A small mapping of
   field-path patterns to reasons, covering only what cannot even ride the
   narrative: the SOAP note ``kind`` split (subjective/objective/assessment/plan
   collapse into one ``narrative`` section on re-ingest), and the structural
   plumbing the narrative deliberately omits (per-object ``id``/``provenance``,
   regenerated or non-deterministic on ingest).

Determinism: same record in → byte-identical bytes out (stable element order,
no wall-clock or random ids; the loss narrative is sorted; ``document_id``
defaults to a uuid5 over the patient id, so a record with no explicit id still
produces a fixed document). Non-deterministic fields (``provenance.ingested_at``,
``uuid4`` ids) are excluded from the narrative by :data:`_STRUCTURAL_SKIP`.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import UTC, date, datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from lxml import etree

from anastomosis.core.logutil import safe_log_id
from anastomosis.core.model import (
    AllergyCategory,
    AllergyIntolerance,
    Condition,
    ContactKind,
    ContactPoint,
    Encounter,
    Identifier,
    IdentifierKind,
    Immunization,
    MedicationStatement,
    Observation,
    ObservationCategory,
    Patient,
    PatientRecord,
)
from anastomosis.core.model.patient import Address

__all__ = ["DECLARED_LOSSES", "build_ccd"]

logger = logging.getLogger(__name__)

# --- namespaces / OIDs (must mirror sources/ccda/parser.py exactly) ----------

V3 = "urn:hl7-org:v3"
SDTC = "urn:hl7-org:sdtc"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
NSMAP = {None: V3, "sdtc": SDTC, "xsi": XSI}

OID_SSN = "2.16.840.1.113883.4.1"
OID_SNOMED = "2.16.840.1.113883.6.96"
OID_ICD10 = "2.16.840.1.113883.6.90"
OID_RXNORM = "2.16.840.1.113883.6.88"
OID_LOINC = "2.16.840.1.113883.6.1"
OID_CPT = "2.16.840.1.113883.6.12"
OID_CVX = "2.16.840.1.113883.12.292"
OID_GENDER = "2.16.840.1.113883.5.1"
OID_MARITAL = "2.16.840.1.113883.5.2"
OID_CONFIDENTIALITY = "2.16.840.1.113883.5.25"
OID_ACTCLASS = "2.16.840.1.113883.5.6"
OID_ACTCODE = "2.16.840.1.113883.5.4"  # HL7 ActCode (ASSERTION, SEV)

# Section LOINC codes — the values sources/ccda/parser.py dispatches on.
LOINC_PROBLEMS = "11450-4"
LOINC_ALLERGIES = "48765-2"
LOINC_MEDICATIONS = "10160-0"
LOINC_IMMUNIZATIONS = "11369-6"
LOINC_VITALS = "8716-3"
LOINC_RESULTS = "30954-2"
LOINC_SOCIAL = "29762-2"
LOINC_ENCOUNTERS = "46240-8"
LOINC_NOTES = "34109-9"
# The parser does not structurally parse this section; it captures the entries
# of a section STAMPED as ours into patient.extensions["ccda:prior_loss_narrative"]
# and any other 51899-3 section into ccda:section:51899-3 like ordinary foreign
# narrative. We use it as the declared home for source fields CDA has no
# structured slot for.
LOINC_EXTENSIONS = "51899-3"

# The stamp that makes THIS tool's loss ledger self-identifying, so a re-ingest
# can carry its entries forward instead of re-narrating the whole block as one
# ever-growing line — and so a third party's 51899-3 section (unstamped) stays
# ordinary foreign narrative. Non-OID roots are this exporter's existing
# convention for anastomosis-private identifiers (see _patient_ids); the module
# docstring's scope note covers why XSD-OID discipline does not apply here.
# These four must mirror sources/ccda/parser.py exactly.
LOSS_NARRATIVE_TEMPLATE_ROOT = "urn:anastomosis:ccda:loss-narrative"
LOSS_NARRATIVE_TEMPLATE_VERSION = "1"
LOSS_NARRATIVE_GENERATION_ROOT = "urn:anastomosis:ccda:loss-narrative:generation"
# The pre-stamp marker: documents exported before the templateId existed carry
# only this title, and the parser still recognizes them by it.
LOSS_NARRATIVE_TITLE = "Anastomosis Preserved Source Fields"
# Where a re-ingest parks a stamped section's entries (mirrors the parser).
EXT_PRIOR_LOSS_NARRATIVE = "ccda:prior_loss_narrative"

# Template ids the parser keys on (allergy severity, social tobacco).
TPL_SEVERITY = "2.16.840.1.113883.10.20.22.4.8"
TPL_US_REALM_HEADER = "2.16.840.1.113883.10.20.22.1.1"
TPL_CCD = "2.16.840.1.113883.10.20.22.1.2"

# canonical AllergyCategory → the SNOMED class code the parser maps back.
_CATEGORY_CODE: dict[AllergyCategory, tuple[str, str]] = {
    AllergyCategory.DRUG: ("416098002", "Drug allergy"),
    AllergyCategory.FOOD: ("414285001", "Food allergy"),
    AllergyCategory.ENVIRONMENT: ("426232007", "Environmental allergy"),
}

# canonical ContactKind → C-CDA telecom @use (parser maps @use back to kind;
# PHONE_HOME maps from HP, so HP is the deterministic choice for it).
_PHONE_USE: dict[ContactKind, str] = {
    ContactKind.PHONE_HOME: "HP",
    ContactKind.PHONE_MOBILE: "MC",
    ContactKind.PHONE_WORK: "WP",
}

# canonical sex display → administrativeGenderCode @code (parser reads
# @displayName first, so the displayName we emit is what actually round-trips).
_SEX_CODE = {"Female": "F", "Male": "M"}

# Extension keys this format round-trips through native structured slots (each
# is emitted by the model's own structured entry and read back onto that model
# by the parser), so they must NOT be re-emitted into the declared-loss
# extensions section. This is the ONLY extension exemption: a key not listed
# here is narrated, whatever its namespace.
_NATIVE_EXT_KEYS = frozenset({"ccda:route", "ccda:dose", "ccda:allergen_code", "ccda:negationInd"})

# The canonical display the parser stamps on the one social-history observation
# it structurally recovers (the smoking-status concept, LOINC 72166-2). PF and
# the C-CDA parser both produce this exact display with ``code is None``; it is
# the only social observation that may be emitted as the structured 72166-2
# entry. Every other social observation (Occupation, Industry, Education, …)
# rides the loss narrative — emitting it under the tobacco code would relabel a
# charted value into a clinically false statement (the corruption BLOCKER 1
# names). See :func:`_is_smoking_status`.
LOINC_SMOKING_STATUS = "72166-2"
_SMOKING_STATUS_DISPLAY = "Tobacco use"

# DECLARED_LOSSES — the losses that cannot even ride the 51899-3 loss narrative
# (NIT 4: structured as {field-path pattern: reason}). Everything else that no
# structured emitter consumes is serialized into that narrative and recovered
# from re-ingest as patient.extensions['ccda:section:51899-3'] (see the module
# docstring and :func:`_collect_lost_fields`). Keep this minimal: a field that
# could be written to the narrative does NOT belong here.
DECLARED_LOSSES: dict[str, str] = {
    "*.NoteSection.kind": (
        "SOAP kind split (subjective/objective/assessment/plan) collapses into a "
        "single narrative section on re-ingest; the section bodies survive "
        "(labelled) but the per-kind structure does not"
    ),
    "*.id": (
        "per-object canonical id is identity plumbing — the parser regenerates "
        "ids deterministically on re-ingest, so the source id is not preserved "
        "(excluded from the loss narrative by _STRUCTURAL_SKIP)"
    ),
    "*.provenance": (
        "ingest provenance (source_system/file/id, ingested_at) is non-clinical, "
        "non-deterministic metadata recreated at parse time; not narrated"
    ),
    "extensions:ccda:route|dose|allergen_code|negationInd": (
        "these four round-trip NATIVELY onto their own models (see "
        "_NATIVE_EXT_KEYS), so they are not narrated — they are not a loss. "
        "patient.extensions['ccda:prior_loss_narrative'] is likewise not "
        "narrated and not a loss: it IS a previous generation's loss ledger, "
        "re-emitted entry-by-entry as the deduplicated carry-forward appendix "
        "(_carried_forward). Every OTHER ccda:* key (a captured section "
        "narrative, the source document's id/effectiveTime/title) is narrated "
        "like any vendor extension, because this exporter re-emits none of them"
    ),
    "*:narrative-only recovery": (
        "every other populated field with no structured CDA slot (native fields "
        "and vendor extensions alike) is written to the 51899-3 narrative and "
        "recovered as patient.extensions['ccda:section:51899-3'], NOT back onto "
        "its original typed model"
    ),
}

# Per-emitter EXPORTED-field allowlists, keyed by PatientRecord attribute. Each
# tuple names the **leaf field paths** the structured emitter for that collection
# actually consumes, relative to the collection's model and using ``[]`` for a
# list index (so ``addresses[].line1`` names a sub-field of a nested model while
# ``addresses[].line2`` — which the emitter does NOT write — is deliberately
# absent). Anything populated but NOT listed here flows to the loss narrative.
# Kept adjacent to the emitters (cross-referenced in each emitter's section) so
# that adding a field to an emitter without updating its allowlist — or vice
# versa — is visible in review. A PatientRecord attribute absent from this map
# has NO structured emitter at all: its entire contents go to the narrative
# (prescriptions, coverages, family_history, …).
_EXPORTED_FIELDS: dict[str, frozenset[str]] = {
    # _record_target / _patient_demographics. Note: Address.line2 is NOT emitted
    # (no CDA slot the parser reads it back from) → it rides the narrative.
    "patient": frozenset(
        {
            "given_name",
            "middle_name",
            "family_name",
            "suffix",
            "birth_date",
            "sex",
            "race[]",
            "ethnicity[]",
            "language",
            "marital_status",
            # identifiers[].kind is deliberately absent: only SSN and SOURCE_GUID
            # re-ingest as themselves. MRN/PRN/OTHER come back as SOURCE_GUID
            # (their kind rides the id root as urn:anastomosis:id:<kind>), so the
            # kind has to narrate to survive on its own field.
            "identifiers[].value",
            "identifiers[].system",
            "telecom[].kind",
            "telecom[].value",
            "addresses[].line1",
            "addresses[].city",
            "addresses[].state",
            "addresses[].postal_code",
        }
    ),
    # _problems / _condition_value
    "conditions": frozenset({"snomed", "icd10", "display", "onset", "stopped", "active"}),
    # _allergies (substance/category/reactions/severity/onset + ccda:allergen_code ext)
    "allergies": frozenset({"substance", "category", "reactions[]", "severity", "onset", "active"}),
    # _medications / _med_route_dose / _med_consumable (+ ccda:route, ccda:dose ext)
    "medications": frozenset({"display_name", "rxnorm", "start", "stop", "active"}),
    # _immunizations (+ ccda:negationInd ext). ``comment`` is NOT consumed: only
    # the refusal flag round-trips (re-derived as the literal "Refused"); a
    # free-text comment has no slot and rides the narrative.
    "immunizations": frozenset({"vaccine", "administered_on", "lot_number"}),
    # _measurements (vitals + results) and _social_history share the Observation
    # fields they consume; the social tobacco entry consumes a subset.
    "observations": frozenset({"category", "code", "display", "value", "unit", "effective_at"}),
    # _encounters + _notes: the structured Encounters section consumes type+date;
    # the Notes section consumes the note BODY and kind label (sections[].text /
    # sections[].kind) + note_type + date. Section title/html and every other
    # encounter field (chief_complaint, provider_id, signed_*, addenda, …) are
    # the silent-loss BLOCKER 2 names — they flow to the narrative.
    "encounters": frozenset(
        {
            "date_of_service",
            "encounter_type",
            "note_type",
            "sections[].text",
            "sections[].kind",
        }
    ),
}

# Structural / non-deterministic plumbing the loss narrative deliberately omits
# (covered by DECLARED_LOSSES patterns instead): per-object id (uuid4 default,
# regenerated on ingest) and provenance (carries wall-clock ingested_at). The
# ``extensions`` field is NOT dropped here — :func:`_walk_model` routes it
# through :func:`_walk_extensions`, which exempts only the natively round-tripped
# ``ccda:*`` keys. It is listed so a model that somehow surfaces ``extensions``
# outside that route still never lands as a raw dict in the narrative.
_STRUCTURAL_SKIP = frozenset({"id", "provenance", "extensions"})

# A fixed namespace for deterministic document ids derived from the patient id.
_DOC_NS = uuid5(NAMESPACE_URL, "anastomosis:ccda-export:document")


# --- element construction helpers --------------------------------------------


def _el(parent: etree._Element | None, tag: str, **attrs: str | None) -> etree._Element:
    """Create a v3 element ``tag`` under ``parent`` with non-None ``attrs``.

    ``xsi:type`` is passed as the key ``xsi_type`` (Python keywords forbid the
    colon). Attribute insertion order is fixed by call order — determinism.
    """
    node = etree.SubElement(parent, f"{{{V3}}}{tag}") if parent is not None else _root_el(tag)
    for name, value in attrs.items():
        if value is None:
            continue
        if name == "xsi_type":
            node.set(f"{{{XSI}}}type", value)
        else:
            node.set(name, value)
    return node


def _root_el(tag: str) -> etree._Element:
    # lxml accepts a None key for the default namespace (so children render
    # unprefixed in the v3 namespace the parser expects); the lxml stubs type
    # the key as str, hence the targeted ignore on a use that is correct at run
    # time and exercised by the well-formedness test.
    return etree.Element(f"{{{V3}}}{tag}", nsmap=NSMAP)  # type: ignore[arg-type]


def _text_el(parent: etree._Element, tag: str, text: str | None) -> etree._Element | None:
    """An element whose body is ``text``; skipped entirely when ``text`` is None
    (sentinel discipline — never emit an empty placeholder element)."""
    if text is None:
        return None
    node = _el(parent, tag)
    node.text = text
    return node


def _nullable(parent: etree._Element, tag: str, value: str | None, attr: str = "value") -> None:
    """Emit ``<tag attr=value/>`` or ``<tag nullFlavor="NI"/>`` when absent.

    This is the sentinel boundary on export: a missing optional becomes an
    explicit nullFlavor, which the parser reads back as ``None`` (never "" or a
    placeholder), preserving the round trip's None-stays-None guarantee.
    """
    if value is None:
        _el(parent, tag, nullFlavor="NI")
    else:
        _el(parent, tag, **{attr: value})


# --- timestamp formatting (CDA TS) -------------------------------------------


def _ts_datetime(value: datetime) -> str:
    """A CDA ``TS`` with offset, e.g. ``20230510140000-0500``.

    parse_dt reads ``%Y%m%d%H%M%S%z``; we emit exactly that. Naive datetimes
    are treated as UTC (the source-database convention timeutil documents).
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.strftime("%Y%m%d%H%M%S%z")


def _ts_date(value: date) -> str:
    """A CDA date-only ``TS``, e.g. ``20230510`` (parse_date reads ``%Y%m%d``)."""
    return value.strftime("%Y%m%d")


# --- header ------------------------------------------------------------------


def _header(doc: etree._Element, patient: Patient, document_id: str, effective: datetime) -> None:
    _el(doc, "realmCode", code="US")
    _el(doc, "typeId", root="2.16.840.1.113883.1.3", extension="POCD_HD000040")
    _el(doc, "templateId", root=TPL_US_REALM_HEADER, extension="2015-08-01")
    _el(doc, "templateId", root=TPL_CCD, extension="2015-08-01")
    _el(doc, "id", root=document_id)
    _el(
        doc,
        "code",
        code="34133-9",
        displayName="Summarization of Episode Note",
        codeSystem=OID_LOINC,
        codeSystemName="LOINC",
    )
    _text_el(doc, "title", "Continuity of Care Document")
    _el(doc, "effectiveTime", value=_ts_datetime(effective))
    _el(doc, "confidentialityCode", code="N", codeSystem=OID_CONFIDENTIALITY)
    _el(doc, "languageCode", code="en-US")
    _record_target(doc, patient)


def _record_target(doc: etree._Element, patient: Patient) -> None:
    role = _el(_el(doc, "recordTarget"), "patientRole")
    _patient_ids(role, patient.identifiers)
    _addresses(role, patient.addresses)
    _telecom(role, patient.telecom)
    _patient_demographics(role, patient)


def _patient_ids(role: etree._Element, identifiers: list[Identifier]) -> None:
    """Emit patient ids. SSN gets the canonical SSN OID/extension shape the
    parser recognizes; any other id rides as root+extension (SOURCE_GUID)."""
    emitted = False
    for ident in identifiers:
        if ident.kind == IdentifierKind.SSN:
            _el(role, "id", root=OID_SSN, extension=ident.value)
            emitted = True
        elif ident.kind == IdentifierKind.SOURCE_GUID:
            _el(role, "id", root=ident.system or _DOC_NS.hex, extension=ident.value)
            emitted = True
        else:
            # PRN/MRN/OTHER have no standard CDA patient-id slot the parser
            # reads back into a typed kind; carry them as root+extension so the
            # value is not dropped (recovered as a SOURCE_GUID on re-ingest).
            _el(role, "id", root=f"urn:anastomosis:id:{ident.kind.value}", extension=ident.value)
            emitted = True
    if not emitted:
        _el(role, "id", nullFlavor="NI")


def _addresses(role: etree._Element, addresses: list[Address]) -> None:
    if not addresses:
        _el(role, "addr", nullFlavor="NI")
        return
    for address in addresses:
        addr = _el(role, "addr", use="HP")
        _text_el(addr, "streetAddressLine", address.line1)
        _text_el(addr, "city", address.city)
        _text_el(addr, "state", address.state)
        _text_el(addr, "postalCode", address.postal_code)


def _telecom(role: etree._Element, telecom: list[ContactPoint]) -> None:
    if not telecom:
        _el(role, "telecom", nullFlavor="NI")
        return
    for contact in telecom:
        if contact.kind == ContactKind.EMAIL:
            _el(role, "telecom", value=f"mailto:{contact.value}")
        else:
            use = _PHONE_USE.get(contact.kind)
            _el(role, "telecom", value=f"tel:{contact.value}", use=use)


def _patient_demographics(role: etree._Element, patient: Patient) -> None:
    person = _el(role, "patient")
    name = _el(person, "name", use="L")
    _text_el(name, "given", patient.given_name)
    # middle_name re-ingests as the 2nd..nth given joined by space; emit each
    # whitespace-split token as its own <given> so the parser's
    # " ".join(givens[1:]) recovers the same string.
    for token in (patient.middle_name or "").split():
        _text_el(name, "given", token)
    _text_el(name, "family", patient.family_name)
    _text_el(name, "suffix", patient.suffix)

    if patient.sex is None:
        _el(person, "administrativeGenderCode", nullFlavor="NI")
    else:
        _el(
            person,
            "administrativeGenderCode",
            code=_SEX_CODE.get(patient.sex, "UN"),
            displayName=patient.sex,
            codeSystem=OID_GENDER,
        )
    _nullable(person, "birthTime", _ts_date(patient.birth_date) if patient.birth_date else None)

    if patient.marital_status is not None:
        _el(
            person,
            "maritalStatusCode",
            displayName=patient.marital_status,
            codeSystem=OID_MARITAL,
        )
    for race in patient.race:
        _el(person, "raceCode", displayName=race, codeSystem="2.16.840.1.113883.6.238")
    for ethnicity in patient.ethnicity:
        _el(person, "ethnicGroupCode", displayName=ethnicity, codeSystem="2.16.840.1.113883.6.238")
    if patient.language is not None:
        lang = _el(person, "languageCommunication")
        _el(lang, "languageCode", code=patient.language)


# --- section scaffold --------------------------------------------------------


def _section(
    body: etree._Element,
    loinc: str,
    title: str,
    display_name: str,
    *,
    template_id: tuple[str, str] | None = None,
    section_id: tuple[str, str] | None = None,
) -> etree._Element:
    """Open a ``<component><section>`` with the LOINC code the parser dispatches
    on and a title, returning the ``<section>`` for entries to attach to.

    ``template_id``/``section_id`` are ``(root, extension)`` pairs emitted BEFORE
    ``<code>`` (CDA element order). Only the loss narrative uses them — that is
    the one section this tool must be able to recognize as its own on re-ingest.
    """
    section = _el(_el(body, "component"), "section")
    if template_id is not None:
        _el(section, "templateId", root=template_id[0], extension=template_id[1])
    if section_id is not None:
        _el(section, "id", root=section_id[0], extension=section_id[1])
    _el(
        section,
        "code",
        code=loinc,
        displayName=display_name,
        codeSystem=OID_LOINC,
        codeSystemName="LOINC",
    )
    _text_el(section, "title", title)
    return section


def _narrative(section: etree._Element, lines: list[str]) -> None:
    """A human-readable ``<text>`` block (CDA requires one per section; the
    parser only reads it for unparsed sections, but it keeps the doc valid)."""
    text = _el(section, "text")
    for line in lines:
        _text_el(text, "paragraph", line)


# --- problems ----------------------------------------------------------------


def _problems(body: etree._Element, conditions: list[Condition]) -> None:
    section = _section(body, LOINC_PROBLEMS, "Problems", "Problem List")
    _narrative(section, [_condition_line(c) for c in conditions])
    for condition in conditions:
        act = _el(_el(section, "entry"), "act", classCode="ACT", moodCode="EVN")
        _el(act, "templateId", root="2.16.840.1.113883.10.20.22.4.3", extension="2015-08-01")
        _el(act, "code", code="CONC", codeSystem=OID_ACTCLASS)
        # The parser reads `active` from THIS act's statusCode.
        _el(act, "statusCode", code="active" if condition.active else "completed")
        rel = _el(act, "entryRelationship", typeCode="SUBJ")
        obs = _el(rel, "observation", classCode="OBS", moodCode="EVN")
        _el(obs, "templateId", root="2.16.840.1.113883.10.20.22.4.4", extension="2015-08-01")
        _el(obs, "code", code="55607006", displayName="Problem", codeSystem=OID_SNOMED)
        _el(obs, "statusCode", code="completed")
        eff = _el(obs, "effectiveTime")
        _nullable(eff, "low", _ts_date(condition.onset) if condition.onset else None)
        if condition.stopped is not None:
            _el(eff, "high", value=_ts_date(condition.stopped))
        _condition_value(obs, condition)


def _condition_value(obs: etree._Element, condition: Condition) -> None:
    value = _el(
        obs,
        "value",
        xsi_type="CD",
        code=condition.snomed,
        displayName=condition.display,
        codeSystem=OID_SNOMED if condition.snomed else None,
        codeSystemName="SNOMED CT" if condition.snomed else None,
    )
    if condition.snomed is None and condition.display is not None:
        # No coded value: the parser falls back to value/originalText for display.
        _text_el(value, "originalText", condition.display)
    if condition.icd10 is not None:
        _el(
            value,
            "translation",
            code=condition.icd10,
            codeSystem=OID_ICD10,
            codeSystemName="ICD-10-CM",
        )


def _condition_line(condition: Condition) -> str:
    state = "active" if condition.active else "resolved"
    return f"{condition.display or 'Problem'} ({state})"


# --- allergies ---------------------------------------------------------------


def _allergies(body: etree._Element, allergies: list[AllergyIntolerance]) -> None:
    section = _section(body, LOINC_ALLERGIES, "Allergies", "Allergies and Adverse Reactions")
    _narrative(section, [a.substance or "Allergy" for a in allergies])
    for allergy in allergies:
        act = _el(_el(section, "entry"), "act", classCode="ACT", moodCode="EVN")
        _el(act, "templateId", root="2.16.840.1.113883.10.20.22.4.30", extension="2015-08-01")
        _el(act, "code", code="CONC", codeSystem=OID_ACTCLASS)
        _el(act, "statusCode", code="active" if allergy.active else "completed")
        eff = _el(act, "effectiveTime")
        _nullable(eff, "low", _ts_date(allergy.onset) if allergy.onset else None)
        rel = _el(act, "entryRelationship", typeCode="SUBJ")
        obs = _el(rel, "observation", classCode="OBS", moodCode="EVN")
        _el(obs, "templateId", root="2.16.840.1.113883.10.20.22.4.7", extension="2014-06-09")
        _el(obs, "code", code="ASSERTION", codeSystem=OID_ACTCODE)
        _el(obs, "statusCode", code="completed")
        code, display = _CATEGORY_CODE.get(allergy.category, ("419199007", "Allergy"))
        _el(obs, "value", xsi_type="CD", code=code, displayName=display, codeSystem=OID_SNOMED)
        _allergen(obs, allergy)
        _reactions(obs, allergy.reactions)
        if allergy.severity is not None:
            _severity(obs, allergy.severity)


def _allergen(obs: etree._Element, allergy: AllergyIntolerance) -> None:
    participant = _el(obs, "participant", typeCode="CSM")
    role = _el(participant, "participantRole", classCode="MANU")
    entity = _el(role, "playingEntity", classCode="MMAT")
    _el(
        entity,
        "code",
        code=allergy.extensions.get("ccda:allergen_code"),
        displayName=allergy.substance,
    )


def _reactions(obs: etree._Element, reactions: list[str]) -> None:
    for reaction in reactions:
        rel = _el(obs, "entryRelationship", typeCode="MFST", inversionInd="true")
        inner = _el(rel, "observation", classCode="OBS", moodCode="EVN")
        _el(inner, "templateId", root="2.16.840.1.113883.10.20.22.4.9", extension="2014-06-09")
        _el(inner, "code", code="ASSERTION", codeSystem=OID_ACTCODE)
        _el(inner, "statusCode", code="completed")
        _el(inner, "value", xsi_type="CD", displayName=reaction, codeSystem=OID_SNOMED)


def _severity(obs: etree._Element, severity: str) -> None:
    rel = _el(obs, "entryRelationship", typeCode="SUBJ", inversionInd="true")
    inner = _el(rel, "observation", classCode="OBS", moodCode="EVN")
    _el(inner, "templateId", root=TPL_SEVERITY, extension="2014-06-09")
    _el(inner, "code", code="SEV", displayName="Severity Observation", codeSystem=OID_ACTCODE)
    _el(inner, "statusCode", code="completed")
    _el(inner, "value", xsi_type="CD", displayName=severity, codeSystem=OID_SNOMED)


# --- medications -------------------------------------------------------------


def _medications(body: etree._Element, medications: list[MedicationStatement]) -> None:
    section = _section(body, LOINC_MEDICATIONS, "Medications", "History of Medication Use")
    _narrative(section, [m.display_name or "Medication" for m in medications])
    for med in medications:
        entry = _el(section, "entry")
        admin = _el(entry, "substanceAdministration", classCode="SBADM", moodCode="EVN")
        _el(admin, "templateId", root="2.16.840.1.113883.10.20.22.4.16", extension="2014-06-09")
        _el(admin, "statusCode", code="active" if med.active else "completed")
        period = _el(admin, "effectiveTime", xsi_type="IVL_TS")
        _nullable(period, "low", _ts_date(med.start) if med.start else None)
        if med.stop is not None:
            _el(period, "high", value=_ts_date(med.stop))
        else:
            # The parser reads `stop` from high; UNK nullFlavor → None on re-ingest.
            _el(period, "high", nullFlavor="UNK")
        _med_route_dose(admin, med)
        _med_consumable(admin, med)


def _med_route_dose(admin: etree._Element, med: MedicationStatement) -> None:
    route = med.extensions.get("ccda:route")
    if route is not None:
        _el(admin, "routeCode", displayName=route, codeSystem="2.16.840.1.113883.3.26.1.1")
    dose = med.extensions.get("ccda:dose")
    if dose is not None:
        # ccda:dose round-trips as "value unit" or "value"; split back so the
        # parser reconstructs the same string from @value (+ @unit).
        value, _, unit = str(dose).partition(" ")
        _el(admin, "doseQuantity", value=value, unit=unit or None)


def _med_consumable(admin: etree._Element, med: MedicationStatement) -> None:
    product = _el(_el(admin, "consumable"), "manufacturedProduct", classCode="MANU")
    _el(product, "templateId", root="2.16.840.1.113883.10.20.22.4.23", extension="2014-06-09")
    material = _el(product, "manufacturedMaterial")
    _el(
        material,
        "code",
        code=med.rxnorm,
        displayName=med.display_name,
        codeSystem=OID_RXNORM if med.rxnorm else None,
        codeSystemName="RxNorm" if med.rxnorm else None,
    )


# --- immunizations -----------------------------------------------------------


def _immunizations(body: etree._Element, immunizations: list[Immunization]) -> None:
    section = _section(body, LOINC_IMMUNIZATIONS, "Immunizations", "History of Immunizations")
    _narrative(section, [i.vaccine or "Immunization" for i in immunizations])
    for imm in immunizations:
        refused = imm.extensions.get("ccda:negationInd") == "true"
        admin = _el(
            _el(section, "entry"),
            "substanceAdministration",
            classCode="SBADM",
            moodCode="EVN",
            negationInd="true" if refused else "false",
        )
        _el(admin, "templateId", root="2.16.840.1.113883.10.20.22.4.52", extension="2015-08-01")
        _el(admin, "statusCode", code="completed")
        _nullable(
            admin,
            "effectiveTime",
            _ts_date(imm.administered_on) if imm.administered_on else None,
        )
        product = _el(_el(admin, "consumable"), "manufacturedProduct", classCode="MANU")
        _el(product, "templateId", root="2.16.840.1.113883.10.20.22.4.54", extension="2014-06-09")
        material = _el(product, "manufacturedMaterial")
        _el(material, "code", displayName=imm.vaccine, codeSystem=OID_CVX, codeSystemName="CVX")
        _text_el(material, "lotNumberText", imm.lot_number)


# --- vitals + results --------------------------------------------------------


def _measurements(
    body: etree._Element,
    loinc: str,
    title: str,
    display_name: str,
    organizer_class: str,
    observations: list[Observation],
) -> None:
    section = _section(body, loinc, title, display_name)
    _narrative(section, [_measurement_line(o) for o in observations])
    if not observations:
        return
    organizer = _el(_el(section, "entry"), "organizer", classCode=organizer_class, moodCode="EVN")
    _el(organizer, "statusCode", code="completed")
    # An organizer-level effectiveTime gives the parser a fallback timestamp.
    effs = [o.effective_at for o in observations if o.effective_at is not None]
    if effs:
        _el(organizer, "effectiveTime", value=_ts_datetime(effs[0]))
    for obs in observations:
        component = _el(organizer, "component")
        node = _el(component, "observation", classCode="OBS", moodCode="EVN")
        _el(node, "code", code=obs.code, displayName=obs.display, codeSystem=OID_LOINC)
        _el(node, "statusCode", code="completed")
        if obs.effective_at is not None:
            _el(node, "effectiveTime", value=_ts_datetime(obs.effective_at))
        _el(node, "value", xsi_type="PQ", value=obs.value, unit=obs.unit)


def _measurement_line(obs: Observation) -> str:
    parts = [obs.display or obs.code or "Observation"]
    if obs.value is not None:
        parts.append(f"{obs.value} {obs.unit}".strip() if obs.unit else obs.value)
    return " ".join(parts)


# --- social history ----------------------------------------------------------


def _is_smoking_status(obs: Observation) -> bool:
    """Whether ``obs`` IS the smoking-status concept the parser recovers.

    The parser only structurally re-ingests social observations coded
    ``72166-2`` and always stamps display ``"Tobacco use"`` on them. PF/Tebra
    and the C-CDA parser both produce the tobacco observation as
    ``code is None, display == "Tobacco use"``. So the smoking concept is keyed
    on either the explicit LOINC code or that canonical display — and ONLY such
    observations may be emitted under 72166-2. Stamping that code on a
    non-tobacco observation (Occupation, Industry, …) would relabel a charted
    value into a clinically false tobacco statement (BLOCKER 1)."""
    return obs.code == LOINC_SMOKING_STATUS or (
        obs.code is None and obs.display == _SMOKING_STATUS_DISPLAY
    )


def _social_history(body: etree._Element, observations: list[Observation]) -> None:
    """Emit the structured 72166-2 entry ONLY for smoking-status observations.

    Every other social observation has no structured slot the parser reads and
    is NOT emitted here — it rides the loss narrative (the 51899-3 section), so
    it can never re-ingest under a tobacco label. The full set is still listed
    in this section's human narrative for document readability."""
    section = _section(body, LOINC_SOCIAL, "Social History", "Social History")
    _narrative(section, [_measurement_line(o) for o in observations])
    for obs in observations:
        if not _is_smoking_status(obs):
            continue  # non-tobacco social obs → loss narrative, never 72166-2
        entry = _el(section, "entry")
        node = _el(entry, "observation", classCode="OBS", moodCode="EVN")
        _el(node, "templateId", root="2.16.840.1.113883.10.20.22.4.78", extension="2014-06-09")
        _el(
            node,
            "code",
            code=LOINC_SMOKING_STATUS,
            displayName="Tobacco smoking status",
            codeSystem=OID_LOINC,
        )
        _el(node, "statusCode", code="completed")
        if obs.effective_at is not None:
            _el(node, "effectiveTime", value=_ts_datetime(obs.effective_at))
        _el(node, "value", xsi_type="CD", displayName=obs.value, codeSystem=OID_SNOMED)


# --- encounters --------------------------------------------------------------


def _encounters(body: etree._Element, encounters: list[Encounter]) -> None:
    section = _section(body, LOINC_ENCOUNTERS, "Encounters", "History of Encounters")
    _narrative(section, [e.encounter_type or "Encounter" for e in encounters])
    for enc in encounters:
        node = _el(_el(section, "entry"), "encounter", classCode="ENC", moodCode="EVN")
        _el(node, "templateId", root="2.16.840.1.113883.10.20.22.4.49", extension="2015-08-01")
        # id @root drives the deterministic encounter id on re-ingest.
        _el(node, "id", root=enc.id)
        _el(node, "code", code="99999", displayName=enc.encounter_type, codeSystem=OID_CPT)
        _nullable(
            node,
            "effectiveTime",
            _ts_date(enc.date_of_service) if enc.date_of_service else None,
        )


# --- notes -------------------------------------------------------------------


def _notes(body: etree._Element, encounters: list[Encounter]) -> None:
    """Notes section: one act per encounter that carries narrative content.

    The parser models each note act as a single narrative section. SOAP
    sections are concatenated into one labelled body (declared loss: the
    subjective/objective/assessment/plan split does not survive)."""
    section = _section(body, LOINC_NOTES, "Notes", "Note")
    with_notes = [e for e in encounters if e.has_note_content]
    _narrative(section, [e.note_type or "Note" for e in with_notes])
    for enc in with_notes:
        act = _el(_el(section, "entry"), "act", classCode="ACT", moodCode="EVN")
        _el(act, "templateId", root="2.16.840.1.113883.10.20.22.4.202", extension="2016-11-01")
        _el(act, "id", root=enc.id)
        _el(act, "code", code="34109-9", displayName=enc.note_type, codeSystem=OID_LOINC)
        _text_el(act, "text", _note_body(enc))
        _el(act, "statusCode", code="completed")
        # Notes re-ingest date_of_service from author/time; emit it there.
        author = _el(act, "author")
        if enc.date_of_service is not None:
            _el(author, "time", value=_ts_datetime(_midnight_utc(enc.date_of_service)))
        else:
            _el(author, "time", nullFlavor="NI")
        _el(author, "assignedAuthor")  # CDA requires the wrapper; parser ignores it


def _note_body(enc: Encounter) -> str | None:
    """Concatenate note sections into one narrative body (labelled per kind for
    non-narrative sections so a SOAP chart stays readable)."""
    pieces: list[str] = []
    for section in enc.sections:
        body = (section.text or "").strip()
        if not body:
            continue
        if section.kind.value == "narrative":
            pieces.append(body)
        else:
            label = (section.title or section.kind.value).strip().upper()
            pieces.append(f"{label}: {body}")
    return "\n\n".join(pieces) or None


def _midnight_utc(value: date) -> datetime:
    return datetime(value.year, value.month, value.day, tzinfo=UTC)


# --- declared-loss extensions section ----------------------------------------


def _extensions_section(body: etree._Element, record: PatientRecord) -> None:
    """Emit the loss ledger as narrative on a single, stamped 51899-3 section.

    This is the no-silent-drop mechanism, systematic rather than whack-a-mole:
    every populated source field with no structured CDA slot — native canonical
    fields, record-level lists the parser cannot produce, and vendor extension
    namespaces alike — is serialized here as deterministic ``path = value``
    lines, one per ``<paragraph>``. A re-ingest reads a stamped section's entries
    back into patient.extensions["ccda:prior_loss_narrative"], so the data is
    preserved in the document and recoverable on re-ingest, just not back on its
    original typed models (a declared, audited loss).

    Round trip N of the same chart appends that prior ledger here as a
    deduplicated carry-forward (:func:`_carried_forward`) rather than as one
    swallowed blob, and stamps the generation, so the section stays a readable
    ledger instead of growing without bound. Exactly ONE 51899-3 section is ever
    emitted.
    """
    current = _collect_lost_fields(record)
    prior = _prior_narrative(record.patient.extensions.get(EXT_PRIOR_LOSS_NARRATIVE))
    generation, prior_entries = prior if prior is not None else (None, [])
    lines = current + _carried_forward(prior_entries, current)
    if not lines:
        return
    section = _section(
        body,
        LOINC_EXTENSIONS,
        LOSS_NARRATIVE_TITLE,
        "Note",
        template_id=(LOSS_NARRATIVE_TEMPLATE_ROOT, LOSS_NARRATIVE_TEMPLATE_VERSION),
        # A ledger with no readable generation restarts the count at 1; the
        # counter is provenance, never clinical content, so a reset is not a loss.
        section_id=(LOSS_NARRATIVE_GENERATION_ROOT, str((generation or 0) + 1)),
    )
    _narrative(section, lines)


def _prior_narrative(value: Any) -> tuple[int | None, list[str]] | None:
    """The ``(generation, entries)`` a re-ingest parked under
    :data:`EXT_PRIOR_LOSS_NARRATIVE`, or ``None`` when ``value`` is not the shape
    this exporter writes.

    ``None`` is the honest answer for an unrecognized shape: the caller then
    leaves the key to :func:`_walk_extensions`, which narrates it like any other
    vendor extension. A key this exporter cannot re-emit must never be BOTH
    skipped here and exempted there — that is the silent drop.
    """
    if not isinstance(value, dict):
        return None
    entries = value.get("entries")
    if not isinstance(entries, list) or not all(isinstance(entry, str) for entry in entries):
        return None
    generation = value.get("generation")
    if generation is not None and not isinstance(generation, int):
        return None
    return generation, list(entries)


# A path's per-object index: `medications[<uuid4>]`, `addenda[0]`, `race[]`.
_INDEX_RE = re.compile(r"\[[^\]]*\]")


def _entry_key(line: str) -> str:
    """A loss entry with the per-object indices erased from its PATH only.

    The value is left untouched (it may legitimately contain brackets), and a
    line with no ``" = "`` separator is its own key. Paths never contain
    ``" = "``, so the first separator is the right split.
    """
    path, sep, value = line.partition(" = ")
    if not sep:
        return line
    return f"{_INDEX_RE.sub('[]', path)} = {value}"


def _carried_forward(prior: list[str], current: list[str]) -> list[str]:
    """The prior generation's entries this generation does not already state.

    Dedupe is a MULTISET difference keyed on :func:`_entry_key` — the entry with
    per-object indices erased. A canonical id is a DECLARED loss, regenerated on
    every re-ingest, so ``medications[<old id>].patient_id = X`` and
    ``medications[<new id>].patient_id = X`` are the same statement wearing two
    dead ids; leaving both in would grow the ledger by one line per object per
    generation. Multiset (not set) difference is what keeps that safe: two
    distinct objects that genuinely share a field value contribute two entries
    and only as many as the current narrative restates are dropped, so an entry
    describing a REAL loss is never collapsed away — the ledger's per-entry count
    is monotone across generations and settles at generation 2.

    Prior document order is preserved, so output stays deterministic.
    """
    restated = Counter(_entry_key(line) for line in current)
    out: list[str] = []
    for line in prior:
        key = _entry_key(line)
        if restated[key]:
            restated[key] -= 1
            continue
        out.append(line)
    return out


def _observation_consumed(item: dict[str, Any]) -> frozenset[str]:
    """Leaf paths the structured emitters consume for ONE observation dump.

    Vitals/labs round-trip through the measurements sections; the lone tobacco
    social observation round-trips through 72166-2. Every other observation
    (non-tobacco social, screening, other) is not structurally emitted at all,
    so NOTHING is consumed and its whole field set flows to the narrative —
    this is the BLOCKER-1-safe counterpart to :func:`_is_smoking_status`."""
    category = item.get("category")
    if category in (ObservationCategory.VITAL_SIGNS.value, ObservationCategory.LABORATORY.value):
        return _EXPORTED_FIELDS["observations"]
    if category == ObservationCategory.SOCIAL_HISTORY.value and (
        item.get("code") == LOINC_SMOKING_STATUS
        or (item.get("code") is None and item.get("display") == _SMOKING_STATUS_DISPLAY)
    ):
        return _EXPORTED_FIELDS["observations"]
    return frozenset()


# Per-collection hook returning the consumed-field set for one item dump.
# Constant for every collection except observations (category-dependent).
_CONSUMED: dict[str, object] = dict(_EXPORTED_FIELDS)
_CONSUMED["observations"] = _observation_consumed


def _consumed_fields(attr: str, item: dict[str, Any]) -> frozenset[str]:
    hook = _CONSUMED.get(attr)
    if hook is None:
        return frozenset()  # no structured emitter for this collection at all
    if callable(hook):
        return hook(item)  # type: ignore[no-any-return]
    return hook  # type: ignore[return-value]


def _collect_lost_fields(record: PatientRecord) -> list[str]:
    """Every populated source field with no native CDA round trip, as sorted
    ``path = value`` text lines.

    The collector walks the record's pydantic dump (None/empty pruned),
    subtracts the per-emitter allowlist (:data:`_EXPORTED_FIELDS`) and the
    structural plumbing (:data:`_STRUCTURAL_SKIP`), and serializes the remainder
    — native fields, nested sub-fields, record-level unmappable lists, and
    ``extensions`` alike, both the patient's and the RECORD's own (extensions
    via :func:`_walk_extensions`, which exempts the natively round-tripped
    ``ccda:*`` keys).

    Determinism: ``mode="json"`` gives stable scalar forms (dates as ISO
    strings); output lines are sorted. PHI: this builds the document body, not
    log output — nothing here is logged. Values are clinical content already
    destined for the document.
    """
    dump = record.model_dump(mode="json")
    lines: list[str] = []
    for attr in sorted(dump):
        value = dump[attr]
        if attr == "patient":
            lines += _walk_model("patient", value, _consumed_fields("patient", value))
        elif isinstance(value, list):
            for item in value:
                if not isinstance(item, dict):
                    continue
                lines += _walk_model(
                    f"{attr}[{_model_index(item)}]", item, _consumed_fields(attr, item)
                )
        elif isinstance(value, dict):
            # The record's OWN dict attrs — `extensions` (vendor namespaces the
            # sources hang off the record, e.g. pf_tebra:unmapped:<table>) and
            # `provenance`. Walking them from a synthetic "record" root routes
            # extensions through _walk_extensions exactly as the patient's are,
            # and drops provenance via _STRUCTURAL_SKIP. Without this branch a
            # record-level dict fell through the loop entirely and never reached
            # the narrative.
            lines += _walk_value("record", "", {attr: value}, frozenset())
        # scalar top-level attrs (none exist today) would fall through silently;
        # the record is a fixed set of model/list/dict fields, so there are none.
    return sorted(lines)


def _model_index(item: dict[str, Any]) -> str:
    """A stable per-item index for path lines: the canonical id where present
    (ids are GUID-shaped for top-level models), else a positional fallback."""
    ident = item.get("id")
    return str(ident) if ident else "0"


def _walk_model(path: str, item: dict[str, Any], consumed: frozenset[str]) -> list[str]:
    """Serialize one model dump's UNconsumed, populated leaves as path lines.

    A leaf is emitted unless its **relative path** (dotted, with ``[]`` for list
    indices, e.g. ``addresses[].line2`` or ``sections[].title``) is in
    ``consumed`` — so an emitter that writes only part of a nested model leaks
    nothing: the unconsumed sub-fields still narrate. ``id``/``provenance`` are
    structural plumbing (:data:`_STRUCTURAL_SKIP`, declared losses).
    ``extensions`` is walked with the native-key exemption (``ccda:*`` keys
    round-trip on their models; header-metadata keys are re-derived), every
    other vendor key narrates. This is the single, generic loss path: native
    fields, nested sub-fields, and extensions all flow through here, so a new
    model field, sub-field, or collection cannot silently vanish."""
    return _walk_value(path, "", item, consumed)


def _walk_value(path: str, rel: str, value: Any, consumed: frozenset[str]) -> list[str]:
    """Recurse a json-native ``value``, tracking the display ``path`` and the
    allowlist-relative ``rel`` path in parallel."""
    if value is None:
        return []
    if isinstance(value, dict):
        lines: list[str] = []
        for key in sorted(value):
            if rel == "" and key == "extensions":
                lines += _walk_extensions(path, value[key])
                continue
            if rel == "" and key in _STRUCTURAL_SKIP:
                continue
            child_rel = key if rel == "" else f"{rel}.{key}"
            if child_rel in consumed:
                continue
            lines += _walk_value(f"{path}.{key}", child_rel, value[key], consumed)
        return lines
    if isinstance(value, list):
        child_rel = f"{rel}[]"
        if child_rel in consumed:  # the whole list is consumed (e.g. race[])
            return []
        lines = []
        for index, element in enumerate(value):
            lines += _walk_value(f"{path}[{index}]", child_rel, element, consumed)
        return lines
    text = str(value)
    if text == "":
        return []
    return [f"{path} = {text}"]


def _walk_extensions(path: str, extensions: dict[str, Any]) -> list[str]:
    """Serialize an ``extensions`` dump, exempting ONLY the keys this exporter
    provably re-emits into structured slots the parser reads back onto the same
    model (:data:`_NATIVE_EXT_KEYS`).

    The patient's ``ccda:prior_loss_narrative`` is exempt too, but only when
    :func:`_prior_narrative` recognizes its shape — :func:`_extensions_section`
    re-emits those entries one by one as the carry-forward appendix, and
    narrating them here as well would restore the swallowed-blob growth this key
    exists to end. The exemption is scoped to the patient (the only model a
    re-ingest writes it onto) and to a shape the exporter can actually re-emit,
    so an unrecognized value still narrates rather than vanishing.

    Every other key narrates — a vendor namespace, and equally the ``ccda:*``
    keys an earlier ingest of a CDA document left behind. Those are NOT
    re-derived: a captured section narrative (``ccda:section:<loinc>``, the only
    copy of an entry no structural parser could take apart) has no emitter at
    all, and the header this exporter writes carries its own title, its own
    deterministic document id and its own effectiveTime — so exempting the
    ingest-side metadata keys would drop the source document's values. Anything
    not re-emitted must ride the loss narrative."""
    lines: list[str] = []
    for key in sorted(extensions):
        if key in _NATIVE_EXT_KEYS:
            continue
        if (
            path == "patient"
            and key == EXT_PRIOR_LOSS_NARRATIVE
            and _prior_narrative(extensions[key]) is not None
        ):
            continue
        lines += _serialize(f"{path}.extensions.{key}", extensions[key])
    return lines


def _serialize(path: str, value: Any) -> list[str]:
    """Flatten a JSON-native value into deterministic ``path = value`` lines,
    pruning None and empty containers (sentinel discipline: absent stays absent,
    never an empty placeholder line). Used for extension values, which carry no
    allowlist."""
    if value is None:
        return []
    if isinstance(value, dict):
        out: list[str] = []
        for key in sorted(value):
            out += _serialize(f"{path}.{key}", value[key])
        return out
    if isinstance(value, list):
        out = []
        for index, element in enumerate(value):
            out += _serialize(f"{path}[{index}]", element)
        return out
    text = str(value)
    if text == "":
        return []
    return [f"{path} = {text}"]


# --- top-level assembly ------------------------------------------------------


def build_ccd(record: PatientRecord, *, document_id: str | None = None) -> bytes:
    """Export a :class:`PatientRecord` to CCD XML bytes (UTF-8).

    The document round-trips through :mod:`anastomosis.sources.ccda` back to the
    same canonical clinical content. ``document_id`` defaults to a uuid5 over the
    patient id, so output is deterministic and byte-identical for a given record.
    See the module docstring for scope and the declared-loss list.
    """
    doc_id = document_id or str(uuid5(_DOC_NS, record.patient.id))
    # Deterministic effectiveTime: derived from the record, never wall-clock.
    effective = _document_effective(record)

    doc = _root_el("ClinicalDocument")
    _header(doc, record.patient, doc_id, effective)

    body = _el(_el(doc, "component"), "structuredBody")
    _problems(body, record.conditions)
    _allergies(body, record.allergies)
    _medications(body, record.medications)
    _immunizations(body, record.immunizations)
    _measurements(
        body,
        LOINC_VITALS,
        "Vital Signs",
        "Vital Signs",
        "CLUSTER",
        [o for o in record.observations if o.category == ObservationCategory.VITAL_SIGNS],
    )
    _measurements(
        body,
        LOINC_RESULTS,
        "Results",
        "Relevant Diagnostic Tests and/or Laboratory Data",
        "BATTERY",
        [o for o in record.observations if o.category == ObservationCategory.LABORATORY],
    )
    _social_history(
        body,
        [o for o in record.observations if o.category == ObservationCategory.SOCIAL_HISTORY],
    )
    _encounters(body, _structured_encounters(record.encounters))
    _notes(body, record.encounters)
    _extensions_section(body, record)

    logger.info(
        "built CCD for patient %s: %d conditions, %d meds, %d allergies, %d encounters",
        safe_log_id(record.patient.id),
        len(record.conditions),
        len(record.medications),
        len(record.allergies),
        len(record.encounters),
    )
    return etree.tostring(doc, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def _document_effective(record: PatientRecord) -> datetime:
    """A deterministic document effectiveTime: the latest dated encounter at
    midnight UTC, else a fixed epoch. Never wall-clock (determinism + DTZ)."""
    dates = [e.date_of_service for e in record.encounters if e.date_of_service is not None]
    if dates:
        return _midnight_utc(max(dates))
    return datetime(2000, 1, 1, tzinfo=UTC)


def _structured_encounters(encounters: list[Encounter]) -> list[Encounter]:
    """Encounters that belong in the structured Encounters section.

    An encounter carrying only a note (no encounter_type) is represented solely
    by the Notes section; one with an encounter_type is a structured encounter.
    Both can be true at once — the parser reads them from different sections."""
    return [e for e in encounters if e.encounter_type is not None]
