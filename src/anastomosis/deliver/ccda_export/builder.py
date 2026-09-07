"""PatientRecord → C-CDA R2.1 / CCD XML (the inverse of ``sources/ccda``); the
parser is the spec for every xpath, LOINC and template id here (RULES.md 55).

**Not schema-valid C-CDA** — omits header participations the parser ignores
and uses non-OID identifier roots, so it fails ``CDA.xsd``/Schematron.

Nothing drops silently: anything no structured emitter consumes serializes
into the stamped loss-narrative section; the rest is :data:`DECLARED_LOSSES`.
See ``docs/CCDA_EXPORT.md`` for the scope table.
"""

from __future__ import annotations

import base64
import logging
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, TypeVar
from uuid import NAMESPACE_URL, uuid5

from lxml import etree

from anastomosis.core.ccda_codes import (
    ARTIFACT_INTEGRITY_ALGORITHM,
    ARTIFACT_TEMPLATE_ROOT,
    EXT_PRIOR_LOSS_NARRATIVE,
    EXT_SECTION_ENTRIES,
    LOINC_ALLERGIES,
    LOINC_ENCOUNTERS,
    LOINC_EXTENSIONS,
    LOINC_IMMUNIZATIONS,
    LOINC_MEDICATIONS,
    LOINC_NOTES,
    LOINC_PROBLEMS,
    LOINC_RESULTS,
    LOINC_SOCIAL,
    LOINC_VITALS,
    LOSS_NARRATIVE_GENERATION_ROOT,
    LOSS_NARRATIVE_TEMPLATE_ROOT,
    LOSS_NARRATIVE_TITLE,
    OID_ICD10,
    OID_RXNORM,
    OID_SNOMED,
    OID_SSN,
    SDTC,
    SECTION_CODE_UNKNOWN,
    TPL_SEVERITY,
    V3,
    XSI,
    first_rooted_id,
    organizer_component_source_id,
)
from anastomosis.core.conservation import Conservation
from anastomosis.core.logutil import safe_log_id
from anastomosis.core.model import (
    EXT_INLINE_CONTENT,
    AllergyCategory,
    AllergyIntolerance,
    Condition,
    ContactKind,
    ContactPoint,
    DocumentArtifact,
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
from anastomosis.core.model.base import AnastBase
from anastomosis.core.model.patient import Address

__all__ = ["DECLARED_LOSSES", "CcdMeasurement", "DeliveredArtifact", "build_ccd", "measure_ccd"]

logger = logging.getLogger(__name__)

# --- the writer's own namespaces, OIDs and template ids ----------------------
#
# Not mirrored anywhere: the parser reads none of these. What both halves DO
# have to agree on lives in core.ccda_codes, imported above.

NSMAP = {None: V3, "sdtc": SDTC, "xsi": XSI}

OID_LOINC = "2.16.840.1.113883.6.1"
OID_CVX = "2.16.840.1.113883.12.292"
OID_GENDER = "2.16.840.1.113883.5.1"
OID_MARITAL = "2.16.840.1.113883.5.2"
OID_CONFIDENTIALITY = "2.16.840.1.113883.5.25"
OID_ACTCLASS = "2.16.840.1.113883.5.6"
OID_ACTCODE = "2.16.840.1.113883.5.4"  # HL7 ActCode (ASSERTION, SEV)

# The writer's own version stamp on the section it emits. NOT mirrored: the
# parser recognises the root and ignores the extension, so this is the one
# loss-narrative constant that is genuinely one-sided.
LOSS_NARRATIVE_TEMPLATE_VERSION = "1"

# Header template ids. Declarative only — the parser does not dispatch on them.
# (TPL_SEVERITY, which it DOES key on, is shared via core.ccda_codes.)
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
# Keyed on the lowercased source string. The real Practice Fusion Gender column
# holds "M"/"F" — the reference generator translates {'M': 'Male', 'F': 'Female'}
# off it — so an exact-match table over "Female"/"Male" matched no real patient.
_SEX_CODE = {"f": "F", "female": "F", "m": "M", "male": "M"}

# Extension keys this format round-trips through native structured slots (each
# is emitted by the model's own structured entry and read back onto that model
# by the parser), so they must NOT be re-emitted into the declared-loss
# extensions section. This is the ONLY extension exemption: a key not listed
# here is narrated, whatever its namespace.
_NATIVE_EXT_KEYS = frozenset({"ccda:route", "ccda:dose", "ccda:allergen_code", "ccda:negationInd"})

# The one extension whose value is not narrated because it is DELIVERED as
# BYTES: an artifact that came inline with its record (a C-CDA Unstructured
# Document's scan lives inside the XML, not beside it) rides here as base64
# until a writer puts it on disk. Two writers do: the run writes it into the
# attachments directory beside the charts, and :func:`deliver_ccda` writes it
# again beside the C-CDA it hands the receiving EHR, referenced from the
# document by :func:`_delivered_documents`.
#
# It stays out of the narrative because narrating it would preserve nothing
# more — the bytes are on disk twice, witnessed by ``documents[].sha256`` —
# while inlining tens of megabytes of base64 into the document and, since a
# re-ingest recovers the narrative and the next export re-narrates it, growing
# it without bound generation over generation. Declared in DECLARED_LOSSES.
_DELIVERED_NOT_NARRATED = frozenset({EXT_INLINE_CONTENT})

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
# from re-ingest as patient.extensions[EXT_PRIOR_LOSS_NARRATIVE] (see the module
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
        "like any vendor extension, because this exporter re-emits none of "
        "them — except the ccda:entries: family below, which it does"
    ),
    "patient.extensions:ccda:entries:<code>": (
        "a section's own <entry> elements, which a C-CDA ingest parked verbatim "
        "because prose about a section is not a copy of the entries beneath it, "
        "are DELIVERED, not narrated: _carry_preserved re-emits them as the "
        "entries of the section carrying that code — a carrier section when this "
        "exporter emits none for it — so they leave as the entries they arrived "
        "as, and a re-ingest parks the same bytes again. Narrating them instead "
        "would serialise XML into path = value lines no emitter consumes, which "
        "a re-ingest parks and the next export narrates again, without bound"
    ),
    "documents[]:artifact bytes": (
        "an artifact's BYTES are delivered as a file, never inlined into the "
        "document: the run writes them into the attachments directory beside "
        "the charts, and deliver_ccda writes them again into the delivery "
        "directory beside the CCD, where an <observationMedia> entry names the "
        "file, its media type and its SHA-256 so a re-ingest resolves it back "
        "to the same artifact. build_ccd alone writes no files, so a caller "
        "that delivers nothing gets a document with no artifact entries and "
        "documents[].path/.mime_type/.sha256 narrated instead — nothing is "
        "dropped either way, but only the DELIVERER conserves the bytes"
    ),
    "*:narrative-only recovery": (
        "every other populated field with no structured CDA slot (native fields "
        "and vendor extensions alike) is written to the 51899-3 narrative and "
        f"recovered as patient.extensions[{EXT_PRIOR_LOSS_NARRATIVE!r}] as "
        "discrete entries, NOT back onto its original typed model"
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
    # _delivered_documents, and ONLY for an artifact this delivery wrote a file
    # for (see _consumed_fields): the <observationMedia> entry carries the media
    # type on @mediaType and the digest on the ED's @integrityCheck, and the
    # parser reads both back. ``path`` is deliberately absent — the reference
    # names the file THIS tool wrote, under its own PHI-free name, so the
    # source's own path is still a source field with no CDA slot and narrates.
    "documents": frozenset({"mime_type", "sha256"}),
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

#: What ``DECLARED_LOSSES["*.provenance"]`` has always promised, at the depth it
#: says: ANY. Ingest provenance is recreated at parse time and carries a wall
#: clock, so narrating one nested inside an extension payload made the document
#: differ between two exports of the same record — the determinism contract
#: broken by the one field the contract already named. ``_STRUCTURAL_SKIP``
#: applied only at the top level of a model, and extension payloads (a source's
#: own model dumps among them) are walked by a serializer that had no notion of
#: it at all.
_STRUCTURAL_SKIP_ANYWHERE = frozenset({"provenance"})

# A fixed namespace for deterministic document ids derived from the patient id.
_DOC_NS = uuid5(NAMESPACE_URL, "anastomosis:ccda-export:document")

#: What the caller wrote beside the document, by artifact id. Empty means this
#: build delivers no files — see :func:`_delivered_documents`.
_Delivered = Mapping[str, "DeliveredArtifact"]


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
    """``<tag attr=value/>``, or ``<tag nullFlavor="NI"/>`` when absent — the
    sentinel boundary the parser reads back as ``None``, never "".
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
    elif (sex_code := _SEX_CODE.get(patient.sex.strip().lower())) is not None:
        _el(
            person,
            "administrativeGenderCode",
            code=sex_code,
            displayName=patient.sex,  # verbatim: the round trip reads this first
            codeSystem=OID_GENDER,
        )
    else:
        # Not an AdministrativeGender concept, so say so. "UN" is the real code
        # for Undifferentiated — a clinical claim about the patient, not a shrug
        # at an unrecognised string, and the receiver cannot tell the difference.
        _text_el(
            _el(person, "administrativeGenderCode", nullFlavor="OTH"),
            "originalText",
            patient.sex,
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
    """Open a ``<component><section>`` on ``loinc``; return the ``<section>``
    for entries. ``template_id``/``section_id`` are used only by the loss
    narrative, the one section this tool must recognize as its own.
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


# --- the source's own entries, delivered rather than narrated ----------------


#: The title a carrier section gets. It must not be ``LOSS_NARRATIVE_TITLE``:
#: the parser recognises this tool's own loss ledger by that title as well as by
#: the stamp, and a carrier wearing it would have its entries read as ledger
#: paragraphs — which is to say, not read at all.
PRESERVED_ENTRIES_TITLE = "Preserved Source Entries"

#: The same hardened posture ``sources/ccda`` reads a document under. These
#: bytes came from a parsed document and are being turned back into elements,
#: not fetched — no entities, no network, no DTD.
_ENTRY_PARSER = etree.XMLParser(
    resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False
)

_Stated = TypeVar("_Stated", bound=AnastBase)


@dataclass(frozen=True)
class _Preserved:
    """The source's own ``<entry>`` bytes, and the objects they already
    state. Delivered verbatim, never narrated (would regrow ~15 KB per
    generation); :meth:`own` says which objects still need a structured
    entry of their own.
    """

    entries: dict[str, list[etree._Element]]
    stated: dict[str, frozenset[str | None]]

    def own(self, loinc: str, objects: list[_Stated]) -> list[_Stated]:
        """``objects`` minus the ones already stated, matched by
        provenance source id (``None`` matches an id-less preserved
        entry); an object from elsewhere keeps its entry.
        """
        stated = self.stated.get(loinc, frozenset())
        return [obj for obj in objects if _source_id(obj) not in stated]


def _source_id(obj: AnastBase) -> str | None:
    """The source id an object's provenance names, or ``None`` when it has none."""
    return obj.provenance.source_id if obj.provenance is not None else None


def _derived_component_ids(entry: etree._Element) -> set[str]:
    """The organizer-derived id for each component observation with no id
    of its own, via the same :func:`first_rooted_id` the parser derives
    from, so both sides land on the same string by construction (#378).
    """
    derived: set[str] = set()
    for organizer in entry.iter(f"{{{V3}}}organizer"):
        organizer_id = first_rooted_id(organizer)
        if organizer_id is None:
            continue
        root, extension = organizer_id
        components = organizer.findall(f"{{{V3}}}component/{{{V3}}}observation")
        for index, component in enumerate(components):
            if first_rooted_id(component) is None:
                derived.add(organizer_component_source_id(root, extension, index))
    return derived


def _stated_ids(entry: etree._Element) -> set[str | None]:
    """Every ``<id root>`` this entry carries at any depth, plus each id-less
    component's organizer-derived id, or ``{None}`` when neither finds one —
    the shape :meth:`_Preserved.own` compares against.
    """
    roots: set[str | None] = {
        root for node in entry.iter(f"{{{V3}}}id") if (root := node.get("root")) is not None
    }
    roots |= _derived_component_ids(entry)
    return roots or {None}


def _preserved_entries(value: Any) -> list[etree._Element] | None:
    """The ``<entry>`` elements parked in ``value``, or ``None`` for a shape
    this exporter cannot re-emit — the caller then narrates it instead
    (:func:`_walk_extensions`), so a key is never skipped by both. PHI: a
    parse failure answers ``None``, never a message quoting the bytes.
    """
    if not isinstance(value, list) or not value:
        return None
    parsed: list[etree._Element] = []
    for item in value:
        if not isinstance(item, str):
            return None
        try:
            node = etree.fromstring(item.encode(), _ENTRY_PARSER)
        except etree.XMLSyntaxError:
            return None
        if node.tag != f"{{{V3}}}entry":
            return None
        parsed.append(node)
    return parsed


def _preserved(record: PatientRecord) -> _Preserved:
    """Every preserved entry the record carries, by section code — repeated
    codes (``#2``, ``#3``) collapse into the one this exporter writes.
    """
    entries: dict[str, list[etree._Element]] = {}
    stated: dict[str, set[str | None]] = {}
    prefix = f"{EXT_SECTION_ENTRIES}:"
    for key, value in record.patient.extensions.items():
        parsed = _preserved_entries(value) if key.startswith(prefix) else None
        if parsed is None:
            continue
        code = key[len(prefix) :].partition("#")[0]
        entries.setdefault(code, []).extend(parsed)
        for node in parsed:
            stated.setdefault(code, set()).update(_stated_ids(node))
    return _Preserved(entries, {code: frozenset(ids) for code, ids in stated.items()})


def _sections_by_code(body: etree._Element) -> dict[str, etree._Element]:
    """The first section this document carries for each section code."""
    found: dict[str, etree._Element] = {}
    for section in body.iterfind(f"{{{V3}}}component/{{{V3}}}section"):
        code = section.find(f"{{{V3}}}code")
        name = SECTION_CODE_UNKNOWN if code is None else (code.get("code") or SECTION_CODE_UNKNOWN)
        found.setdefault(name, section)
    return found


def _carrier_section(body: etree._Element, code: str) -> etree._Element:
    """A section carrying preserved entries for a code this exporter emits
    no section of its own for — the ordinary case, not a guard on a
    strange one. States only the code (no ``codeSystem``) and the
    preserved-entries title.
    """
    section = _el(_el(body, "component"), "section")
    if code != SECTION_CODE_UNKNOWN:
        _el(section, "code", code=code)
    _text_el(section, "title", PRESERVED_ENTRIES_TITLE)
    return section


def _carry_preserved(body: etree._Element, preserved: _Preserved) -> None:
    """Append every preserved entry to the section carrying its code. Runs
    before :func:`_extensions_section`, so a third party's unstamped
    51899-3 entries land in a carrier of their own, never inside this
    tool's stamped ledger.
    """
    sections = _sections_by_code(body)
    for code, entries in preserved.entries.items():
        section = sections.get(code)
        if section is None:
            section = _carrier_section(body, code)
        section.extend(entries)


# --- problems ----------------------------------------------------------------


def _problems(body: etree._Element, conditions: list[Condition], preserved: _Preserved) -> None:
    section = _section(body, LOINC_PROBLEMS, "Problems", "Problem List")
    # The narrative lists every condition; the entries are only the ones the
    # section's own preserved entries do not already state (see _Preserved).
    _narrative(section, [_condition_line(c) for c in conditions])
    for condition in preserved.own(LOINC_PROBLEMS, conditions):
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


def _allergies(
    body: etree._Element, allergies: list[AllergyIntolerance], preserved: _Preserved
) -> None:
    section = _section(body, LOINC_ALLERGIES, "Allergies", "Allergies and Adverse Reactions")
    _narrative(section, [a.substance or "Allergy" for a in allergies])
    for allergy in preserved.own(LOINC_ALLERGIES, allergies):
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


def _medications(
    body: etree._Element, medications: list[MedicationStatement], preserved: _Preserved
) -> None:
    section = _section(body, LOINC_MEDICATIONS, "Medications", "History of Medication Use")
    _narrative(section, [m.display_name or "Medication" for m in medications])
    for med in preserved.own(LOINC_MEDICATIONS, medications):
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


def _immunizations(
    body: etree._Element, immunizations: list[Immunization], preserved: _Preserved
) -> None:
    section = _section(body, LOINC_IMMUNIZATIONS, "Immunizations", "History of Immunizations")
    _narrative(section, [i.vaccine or "Immunization" for i in immunizations])
    for imm in preserved.own(LOINC_IMMUNIZATIONS, immunizations):
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
    preserved: _Preserved,
) -> None:
    section = _section(body, loinc, title, display_name)
    _narrative(section, [_measurement_line(o) for o in observations])
    own = preserved.own(loinc, observations)
    if not own:
        return
    organizer = _el(_el(section, "entry"), "organizer", classCode=organizer_class, moodCode="EVN")
    _el(organizer, "statusCode", code="completed")
    # An organizer-level effectiveTime gives the parser a fallback timestamp.
    effs = [o.effective_at for o in own if o.effective_at is not None]
    if effs:
        _el(organizer, "effectiveTime", value=_ts_datetime(effs[0]))
    for obs in own:
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
    """Whether ``obs`` is the 72166-2 (Tobacco use) concept the parser
    structurally recovers, keyed on the explicit LOINC code or the
    canonical display; stamping that code on any other social observation
    would relabel a charted value as a false tobacco statement (BLOCKER 1)."""
    return obs.code == LOINC_SMOKING_STATUS or (
        obs.code is None and obs.display == _SMOKING_STATUS_DISPLAY
    )


def _social_history(
    body: etree._Element, observations: list[Observation], preserved: _Preserved
) -> None:
    """Social History (LOINC_SOCIAL, template 2.16.840.1.113883.10.20.22.4.78):
    only a smoking-status observation gets the structured 72166-2 entry;
    every other social observation rides the loss narrative instead."""
    section = _section(body, LOINC_SOCIAL, "Social History", "Social History")
    _narrative(section, [_measurement_line(o) for o in observations])
    for obs in preserved.own(LOINC_SOCIAL, observations):
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


def _encounter_code(node: etree._Element, encounter_type: str | None) -> None:
    """The encounter's type as ``originalText`` under ``nullFlavor="OTH"`` —
    never a coded value, since no source field supplies a real CPT code and
    a coded assertion would be trusted over a mismatched type on import. No
    type at all is ``nullFlavor="NI"``.
    """
    if encounter_type is None:
        # Reached only by an encounter with neither type nor note content
        # (_structured_encounters); OTH would still assert a real value.
        _el(node, "code", nullFlavor="NI")
        return
    _text_el(_el(node, "code", nullFlavor="OTH"), "originalText", encounter_type)


def _encounters(body: etree._Element, encounters: list[Encounter], preserved: _Preserved) -> None:
    section = _section(body, LOINC_ENCOUNTERS, "Encounters", "History of Encounters")
    _narrative(section, [e.encounter_type or "Encounter" for e in encounters])
    for enc in preserved.own(LOINC_ENCOUNTERS, encounters):
        node = _el(_el(section, "entry"), "encounter", classCode="ENC", moodCode="EVN")
        _el(node, "templateId", root="2.16.840.1.113883.10.20.22.4.49", extension="2015-08-01")
        # id @root drives the deterministic encounter id on re-ingest.
        _el(node, "id", root=enc.id)
        _encounter_code(node, enc.encounter_type)
        _nullable(
            node,
            "effectiveTime",
            _ts_date(enc.date_of_service) if enc.date_of_service else None,
        )


# --- notes -------------------------------------------------------------------


def _notes(
    body: etree._Element, encounters: list[Encounter], preserved: _Preserved
) -> etree._Element:
    """Notes (LOINC_NOTES, code 34109-9): one act per encounter with
    narrative content; SOAP sections concatenate into one labelled body
    (declared loss). Returns the section so :func:`_delivered_documents`
    can hang artifacts off the same one, never a second Notes section.
    """
    section = _section(body, LOINC_NOTES, "Notes", "Note")
    with_notes = [e for e in encounters if e.has_note_content]
    _narrative(section, [e.note_type or "Note" for e in with_notes])
    for enc in preserved.own(LOINC_NOTES, with_notes):
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
    return section


@dataclass(frozen=True)
class DeliveredArtifact:
    """One source artifact a delivery wrote beside the document (the
    deliverer owns the filesystem and names it — never derived here
    independently, which is #373 from the other side). ``sha256`` is the
    digest of what is actually on disk.
    """

    name: str
    sha256: str


def _delivered_documents(
    section: etree._Element, documents: list[DocumentArtifact], delivered: _Delivered
) -> None:
    """Reference each delivered artifact from the Notes section as
    ``<observationMedia>``/ED (base CDA R2, #373): bytes stay off-document,
    the digest travels on ``@integrityCheck``. An artifact absent from
    ``delivered`` gets no entry — narrated instead (:func:`_consumed_fields`).
    """
    for index, doc in enumerate(documents, start=1):
        landed = delivered.get(doc.id)
        if landed is None:
            continue
        anchor = f"anast-artifact-{index}"
        _render_multimedia(section, anchor, landed.name)
        media = _el(
            _el(section, "entry"),
            "observationMedia",
            classCode="OBS",
            moodCode="EVN",
            ID=anchor,
        )
        _el(media, "templateId", root=ARTIFACT_TEMPLATE_ROOT)
        # The id root, not root+extension: two artifacts sharing one root
        # would both be unattributable to the ledger, which keys by root alone.
        _el(media, "id", root=doc.id)
        value = _el(
            media,
            "value",
            xsi_type="ED",
            mediaType=doc.mime_type,
            integrityCheckAlgorithm=ARTIFACT_INTEGRITY_ALGORITHM,
            integrityCheck=_integrity_check(landed.sha256),
        )
        _el(value, "reference", value=landed.name)


def _render_multimedia(section: etree._Element, anchor: str, name: str) -> None:
    """Link the narrative to one media object via ``<renderMultiMedia>`` —
    the prose names the delivered file (this tool's pseudonymous name),
    never the source's own filename, which stays in the loss narrative.
    """
    text = _find_or_add_text(section)
    paragraph = _el(text, "paragraph")
    paragraph.text = f"Attached document: {name}"
    _el(paragraph, "renderMultiMedia", referencedObject=anchor)


def _find_or_add_text(section: etree._Element) -> etree._Element:
    """The section's ``<text>``, created if :func:`_narrative` did not."""
    existing = section.find(f"{{{V3}}}text")
    return _el(section, "text") if existing is None else existing


def _integrity_check(digest: str) -> str:
    """A hex sha256 as the ED ``@integrityCheck`` BIN: base64 of the raw
    digest bytes, not the hex spelling. Raises on a digest that will not
    decode — the deliverer only ever passes one it just measured.
    """
    return base64.b64encode(bytes.fromhex(digest)).decode("ascii")


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


def _extensions_section(body: etree._Element, record: PatientRecord, delivered: _Delivered) -> None:
    """The loss ledger as narrative on one stamped 51899-3 section
    (LOINC_EXTENSIONS). Round trip N carries the prior ledger forward
    deduplicated (:func:`_carried_forward`); a third party's own unstamped
    51899-3 entries carry in a section of their own (:func:`_carry_preserved`).
    """
    current = _collect_lost_fields(record, delivered)
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
    :data:`EXT_PRIOR_LOSS_NARRATIVE`, or ``None`` for a shape this exporter
    did not write — the caller then narrates it like any other extension.
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
    """A loss entry with its PATH's per-object indices erased; the value is
    untouched. A line with no ``" = "`` separator is its own key.
    """
    path, sep, value = line.partition(" = ")
    if not sep:
        return line
    return f"{_INDEX_RE.sub('[]', path)} = {value}"


def _carried_forward(prior: list[str], current: list[str]) -> list[str]:
    """The prior generation's entries not already restated — a multiset
    difference keyed on :func:`_entry_key` (indices erased), so a
    regenerated id never regrows the ledger. Prior order is preserved.
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
    """Leaf paths consumed for one observation: vitals/labs via the
    measurements sections, the lone tobacco social observation via
    72166-2 — every other observation is unconsumed and narrates in full
    (the BLOCKER-1-safe counterpart to :func:`_is_smoking_status`)."""
    category = item.get("category")
    if category in (ObservationCategory.VITAL_SIGNS.value, ObservationCategory.LABORATORY.value):
        return _EXPORTED_FIELDS["observations"]
    if category == ObservationCategory.SOCIAL_HISTORY.value and (
        item.get("code") == LOINC_SMOKING_STATUS
        or (item.get("code") is None and item.get("display") == _SMOKING_STATUS_DISPLAY)
    ):
        return _EXPORTED_FIELDS["observations"]
    return frozenset()


def _encounter_consumed(item: dict[str, Any]) -> frozenset[str]:
    """Leaf paths consumed for one encounter: the Encounters section's
    fields when it has a type (:func:`_structured_encounters`), the Notes
    section's when it has note content (:func:`_notes`) — an encounter can
    clear neither gate, same shape as :func:`_observation_consumed`.
    """
    consumed: set[str] = set()
    if item.get("encounter_type") is not None:
        consumed |= {"date_of_service", "encounter_type"}
    sections = item.get("sections") or []
    if any((section.get("text") or "").strip() for section in sections):
        consumed |= {"note_type", "sections[].text", "sections[].kind"}
    return frozenset(consumed)


# Per-collection hook returning the consumed-field set for one item dump.
# Constant except where a gate decides per item (observations by category,
# encounters by which of the two sections will actually take them).
_CONSUMED: dict[str, object] = dict(_EXPORTED_FIELDS)
_CONSUMED["observations"] = _observation_consumed
_CONSUMED["encounters"] = _encounter_consumed


def _consumed_fields(attr: str, item: dict[str, Any], delivered: _Delivered) -> frozenset[str]:
    if attr == "documents":
        # Consumes nothing for an artifact this delivery wrote no file for:
        # with no <observationMedia> entry, its fields would drop to nowhere.
        return _EXPORTED_FIELDS[attr] if item.get("id") in delivered else frozenset()
    hook = _CONSUMED.get(attr)
    if hook is None:
        return frozenset()  # no structured emitter for this collection at all
    if callable(hook):
        return hook(item)  # type: ignore[no-any-return]
    return hook  # type: ignore[return-value]


def _collect_lost_fields(record: PatientRecord, delivered: _Delivered) -> list[str]:
    """Every populated field with no native CDA round trip, as sorted
    ``path = value`` lines: the record's dump minus :data:`_EXPORTED_FIELDS`
    and :data:`_STRUCTURAL_SKIP`. PHI: this builds the document, not a log.
    """
    dump = record.model_dump(mode="json")
    lines: list[str] = []
    for attr in sorted(dump):
        value = dump[attr]
        if attr == "patient":
            lines += _walk_model("patient", value, _consumed_fields("patient", value, delivered))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if not isinstance(item, dict):
                    continue
                consumed = _consumed_fields(attr, item, delivered)
                lines += _walk_model(f"{attr}[{index}]", item, consumed)
        elif isinstance(value, dict):
            # The record's OWN dict attrs (extensions, provenance) — walked
            # from a synthetic "record" root so extensions still route
            # through _walk_extensions and provenance still drops.
            lines += _walk_value("record", "", {attr: value}, frozenset())
    return sorted(lines)


def _walk_model(path: str, item: dict[str, Any], consumed: frozenset[str]) -> list[str]:
    """Serialize one model dump's unconsumed, populated leaves as path
    lines — a leaf narrates unless its relative path (dotted, ``[]`` for
    list indices) is in ``consumed``, so a partially-emitted nested model
    still leaks nothing.
    """
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
    """Serialize an ``extensions`` dump, exempting only what this exporter
    provably re-emits: :data:`_NATIVE_EXT_KEYS`, a recognized
    ``ccda:prior_loss_narrative``, and a re-emittable ``ccda:entries:<code>``.
    Every other key narrates, ingest-side metadata included."""
    lines: list[str] = []
    for key in sorted(extensions):
        if key in _NATIVE_EXT_KEYS or key in _DELIVERED_NOT_NARRATED:
            continue
        if path == "patient" and _delivered_as_entries(key, extensions[key]):
            continue
        if (
            path == "patient"
            and key == EXT_PRIOR_LOSS_NARRATIVE
            and _prior_narrative(extensions[key]) is not None
        ):
            continue
        lines += _serialize(f"{path}.extensions.{key}", extensions[key])
    return lines


def _delivered_as_entries(key: str, value: Any) -> bool:
    """Whether this extension key leaves as ``<entry>`` elements rather
    than narrative — the same test :func:`_preserved` uses to collect
    them, so no key is ever skipped by both sides.
    """
    return key.startswith(f"{EXT_SECTION_ENTRIES}:") and _preserved_entries(value) is not None


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
            if key in _STRUCTURAL_SKIP_ANYWHERE:
                continue
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


@dataclass(frozen=True)
class CcdMeasurement:
    """How big this document is, and how much is preservation — on a real
    export the 51899-3 section was 97% of it, 33x the clinical payload;
    an operator should know that shape before the destination refuses it.
    Counts only, of the serialized bytes as they sit in the document.
    """

    total_bytes: int
    preserved_bytes: int

    @property
    def preserved_share(self) -> float:
        """0.0-1.0. Zero for an empty document rather than a division error."""
        return self.preserved_bytes / self.total_bytes if self.total_bytes else 0.0


def measure_ccd(xml: bytes) -> CcdMeasurement:
    """Measure a built CCD: total size, and the size of every 51899-3
    section (this tool's own ledger and any third party's carrier) — read
    back from the emitted bytes, not an intermediate the serializer might
    still change.
    """
    root = etree.fromstring(xml)
    preserved = 0
    for section in root.iter(f"{{{V3}}}section"):
        code = section.find(f"{{{V3}}}code")
        if code is not None and code.get("code") == LOINC_EXTENSIONS:
            preserved += len(etree.tostring(section))
    return CcdMeasurement(total_bytes=len(xml), preserved_bytes=preserved)


def build_ccd(
    record: PatientRecord,
    *,
    document_id: str | None = None,
    delivered: _Delivered | None = None,
) -> bytes:
    """Export a :class:`PatientRecord` to CCD XML bytes (UTF-8), deterministic
    for a given record. ``delivered`` names artifact files the caller
    already wrote; with none, artifact fields narrate instead — honest, but
    only :func:`~anastomosis.deliver.ccda_export.deliver_ccda` conserves (#373).
    """
    doc_id = document_id or str(uuid5(_DOC_NS, record.patient.id))
    carried: _Delivered = {} if delivered is None else delivered
    # Deterministic effectiveTime: derived from the record, never wall-clock.
    effective = _document_effective(record)

    doc = _root_el("ClinicalDocument")
    _header(doc, record.patient, doc_id, effective)

    preserved = _preserved(record)
    body = _el(_el(doc, "component"), "structuredBody")
    _problems(body, record.conditions, preserved)
    _allergies(body, record.allergies, preserved)
    _medications(body, record.medications, preserved)
    _immunizations(body, record.immunizations, preserved)
    _measurements(
        body,
        LOINC_VITALS,
        "Vital Signs",
        "Vital Signs",
        "CLUSTER",
        [o for o in record.observations if o.category == ObservationCategory.VITAL_SIGNS],
        preserved,
    )
    _measurements(
        body,
        LOINC_RESULTS,
        "Results",
        "Relevant Diagnostic Tests and/or Laboratory Data",
        "BATTERY",
        [o for o in record.observations if o.category == ObservationCategory.LABORATORY],
        preserved,
    )
    _social_history(
        body,
        [o for o in record.observations if o.category == ObservationCategory.SOCIAL_HISTORY],
        preserved,
    )
    _encounters(body, _structured_encounters(record.encounters), preserved)
    _delivered_documents(_notes(body, record.encounters, preserved), record.documents, carried)
    _carry_preserved(body, preserved)
    _extensions_section(body, record, carried)
    _assert_encounters_reach_a_section(body, record)

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
    """Encounters belonging in the structured Encounters section: typed, or
    neither typed nor noted — every encounter the Notes section does not
    already stand for (an encounter with both reaches both sections; the
    parser reads them separately, so that is not a duplicate).
    :func:`_assert_encounters_reach_a_section` checks the partition holds."""
    return [e for e in encounters if e.encounter_type is not None or not e.has_note_content]


def _section_id_roots(body: etree._Element, loinc: str) -> set[str]:
    """Every ``<id root>`` under the first section carrying ``loinc``, at
    any depth — empty if no such section. Reads the emitted tree, like
    :func:`measure_ccd`, since a unit that reached no artifact leaves
    nothing for an emitter's own bookkeeping to see."""
    section = _sections_by_code(body).get(loinc)
    if section is None:
        return set()
    return {root for node in section.iter(f"{{{V3}}}id") if (root := node.get("root")) is not None}


def _assert_encounters_reach_a_section(body: etree._Element, record: PatientRecord) -> None:
    """Every encounter must reach the Encounters section, the Notes
    section, or both, keyed on either ``enc.id`` or its preserved
    ``_source_id`` (a preserved entry re-emits the latter, not the
    former) — checking one key alone misreads a preserved encounter as
    unaccounted. :class:`Conservation` raises on any encounter in neither."""
    encounters_ids = _section_id_roots(body, LOINC_ENCOUNTERS)
    notes_ids = _section_id_roots(body, LOINC_NOTES)
    both = encounters_only = notes_only = 0
    for enc in record.encounters:
        keys = {enc.id, _source_id(enc)} - {None}
        in_encounters = bool(keys & encounters_ids)
        in_notes = bool(keys & notes_ids)
        both += in_encounters and in_notes
        encounters_only += in_encounters and not in_notes
        notes_only += in_notes and not in_encounters
    Conservation(
        stage="canonical -> ccda",
        unit="encounter",
        offered=len(record.encounters),
        dispositions={
            "both_sections": both,
            "encounters_section": encounters_only,
            "notes_section": notes_only,
        },
    ).check()
