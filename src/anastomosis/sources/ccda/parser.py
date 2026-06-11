"""C-CDA R2.1 / CCD XML → canonical PatientRecord.

The lossless rule, applied to a CDA document: every section the adapter knows
how to take apart becomes discrete canonical models; **every section it does
not** has its title and normalized narrative captured into
``patient.extensions["ccda:section:<loinc>"]`` so nothing on the chart is ever
silently dropped. Document-level metadata rides ``patient.extensions`` too.

Parsing is defensive by design: a missing optional element maps to ``None``, a
``nullFlavor`` on an element means "absent", but a file that is not a
``ClinicalDocument`` at all raises :exc:`ValueError` — a loud failure, never a
silent skip (the source-adapter contract).

Element names here are limited to the verified C-CDA R2.1 reference; nothing
is invented. See ``tests/fixtures/ccda/README.md`` for the provenance ledger.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from lxml import etree

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
    NoteSection,
    Observation,
    ObservationCategory,
    Patient,
    PatientRecord,
    Provenance,
    SectionKind,
)
from anastomosis.core.model.patient import Address
from anastomosis.core.textutil import format_phone
from anastomosis.core.timeutil import parse_date, parse_dt

__all__ = ["SOURCE", "parse_document"]

SOURCE = "ccda"

# --- namespaces / OIDs (verified C-CDA R2.1 reference) -----------------------

V3 = "urn:hl7-org:v3"
SDTC = "urn:hl7-org:sdtc"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
NS = {"v3": V3, "sdtc": SDTC, "xsi": XSI}

OID_SSN = "2.16.840.1.113883.4.1"
OID_SNOMED = "2.16.840.1.113883.6.96"
OID_ICD10 = "2.16.840.1.113883.6.90"
OID_RXNORM = "2.16.840.1.113883.6.88"

# Section LOINC codes this adapter structurally parses; anything else is
# captured as narrative (the losslessness rule below).
LOINC_PROBLEMS = "11450-4"
LOINC_ALLERGIES = "48765-2"
LOINC_MEDICATIONS = "10160-0"
LOINC_IMMUNIZATIONS = "11369-6"
LOINC_VITALS = "8716-3"
LOINC_RESULTS = "30954-2"
LOINC_SOCIAL = "29762-2"
LOINC_ENCOUNTERS = "46240-8"
LOINC_NOTES = "34109-9"

# Allergy substance-class SNOMED codes → canonical category.
_ALLERGY_CATEGORY = {
    "416098002": AllergyCategory.DRUG,
    "414285001": AllergyCategory.FOOD,
    "426232007": AllergyCategory.ENVIRONMENT,
}

# C-CDA telecom @use → canonical phone kind.
_PHONE_USE = {
    "HP": ContactKind.PHONE_HOME,
    "HV": ContactKind.PHONE_HOME,
    "MC": ContactKind.PHONE_MOBILE,
    "WP": ContactKind.PHONE_WORK,
}

# administrativeGenderCode @code → display (when @displayName is absent).
_SEX_BY_CODE = {"F": "Female", "M": "Male", "UN": None}

# A GUID-shaped string: the synthetic-fixture prefix OR the canonical
# 8-4-4-4-12 hex form a real EHR would emit. Either is trusted as an
# already-stable id; everything else gets a deterministic uuid5.
_GUID_RE = re.compile(
    r"^(?:feedface-|00000000-)[0-9a-fA-F-]+$|"
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
    re.IGNORECASE,
)
_WS_RE = re.compile(r"\s+")

# --- small element helpers ---------------------------------------------------

_Element = etree._Element


def _q(tag: str) -> str:
    """A Clark-notation qualified name in the default v3 namespace."""
    return f"{{{V3}}}{tag}"


def _find(node: _Element | None, path: str) -> _Element | None:
    return None if node is None else node.find(path, NS)


def _findall(node: _Element | None, path: str) -> list[_Element]:
    return [] if node is None else node.findall(path, NS)


def _attr(node: _Element | None, name: str) -> str | None:
    """An attribute value, treating ``nullFlavor`` on the element as absent."""
    if node is None or node.get("nullFlavor") is not None:
        return None
    value = node.get(name)
    if value is None:
        return None
    text = value.strip()
    return text or None


def _val_attr(node: _Element | None, path: str, name: str) -> str | None:
    """``@name`` of the single child at ``path`` (nullFlavor-aware)."""
    return _attr(_find(node, path), name)


def _ts(node: _Element | None, path: str) -> Any:
    """``@value`` of the element at ``path``, parsed as an aware datetime."""
    return parse_dt(_val_attr(node, path, "value"))


def _ts_date(node: _Element | None, path: str) -> Any:
    """``@value`` of the element at ``path``, parsed as a calendar date."""
    return parse_date(_val_attr(node, path, "value"))


def _text_content(node: _Element | None) -> str | None:
    """Normalized visible text of an element subtree (whitespace collapsed)."""
    if node is None:
        return None
    parts = [t if isinstance(t, str) else t.decode() for t in node.itertext()]
    text = _WS_RE.sub(" ", "".join(parts)).strip()
    return text or None


def _prov(source_file: str, source_id: str | None) -> Provenance:
    return Provenance(source_system=SOURCE, source_file=source_file, source_id=source_id)


# --- demographics ------------------------------------------------------------


def _identifiers(patient_role: _Element) -> list[Identifier]:
    out: list[Identifier] = []
    for id_node in _findall(patient_role, "v3:id"):
        root = _attr(id_node, "root")
        extension = _attr(id_node, "extension")
        if root == OID_SSN and extension:
            out.append(Identifier(kind=IdentifierKind.SSN, value=extension))
        elif extension:
            out.append(Identifier(kind=IdentifierKind.SOURCE_GUID, value=extension, system=root))
        elif root:
            out.append(Identifier(kind=IdentifierKind.SOURCE_GUID, value=root))
    return out


def _telecom(patient_role: _Element) -> list[ContactPoint]:
    out: list[ContactPoint] = []
    for node in _findall(patient_role, "v3:telecom"):
        raw = _attr(node, "value")
        if raw is None:
            continue
        if raw.startswith("mailto:"):
            out.append(ContactPoint(kind=ContactKind.EMAIL, value=raw.removeprefix("mailto:")))
        elif raw.startswith("tel:"):
            phone = format_phone(raw.removeprefix("tel:"))
            if phone:
                kind = _PHONE_USE.get(_attr(node, "use") or "", ContactKind.PHONE_OTHER)
                out.append(ContactPoint(kind=kind, value=phone))
    return out


def _addresses(patient_role: _Element) -> list[Address]:
    out: list[Address] = []
    for node in _findall(patient_role, "v3:addr"):
        address = Address(
            line1=_text_content(_find(node, "v3:streetAddressLine")),
            city=_text_content(_find(node, "v3:city")),
            state=_text_content(_find(node, "v3:state")),
            postal_code=_text_content(_find(node, "v3:postalCode")),
        )
        if any(address.model_dump().values()):
            out.append(address)
    return out


def _race(patient: _Element) -> list[str]:
    out: list[str] = []
    for tag in (_q("raceCode"), f"{{{SDTC}}}raceCode"):
        for node in patient.findall(tag):
            display = _attr(node, "displayName")
            if display and display not in out:
                out.append(display)
    return out


def _ethnicity(patient: _Element) -> list[str]:
    out: list[str] = []
    for node in _findall(patient, "v3:ethnicGroupCode"):
        display = _attr(node, "displayName")
        if display and display not in out:
            out.append(display)
    return out


def _patient(clinical_doc: _Element, source_file: str, doc_meta: dict[str, Any]) -> Patient:
    patient_role = _find(clinical_doc, "v3:recordTarget/v3:patientRole")
    if patient_role is None:
        raise ValueError("C-CDA recordTarget/patientRole is missing")
    patient = _find(patient_role, "v3:patient")
    if patient is None:
        raise ValueError("C-CDA patientRole/patient is missing")

    name = _find(patient, "v3:name")
    givens = [g for n in _findall(name, "v3:given") if (g := _text_content(n))]
    gender = _find(patient, "v3:administrativeGenderCode")
    sex = _attr(gender, "displayName")
    if sex is None and gender is not None:
        sex = _SEX_BY_CODE.get(_attr(gender, "code") or "")

    source_id = next(
        (i.value for i in _identifiers(patient_role) if i.kind == IdentifierKind.SSN), None
    )
    return Patient(
        given_name=givens[0] if givens else None,
        middle_name=" ".join(givens[1:]) or None if len(givens) > 1 else None,
        family_name=_text_content(_find(name, "v3:family")),
        suffix=_text_content(_find(name, "v3:suffix")),
        birth_date=_ts_date(patient, "v3:birthTime"),
        sex=sex,
        marital_status=_val_attr(patient, "v3:maritalStatusCode", "displayName"),
        race=_race(patient),
        ethnicity=_ethnicity(patient),
        language=_val_attr(patient, "v3:languageCommunication/v3:languageCode", "code"),
        identifiers=_identifiers(patient_role),
        telecom=_telecom(patient_role),
        addresses=_addresses(patient_role),
        extensions=doc_meta,
        provenance=_prov(source_file, source_id),
    )


# --- section dispatch --------------------------------------------------------


def _section_code(section: _Element) -> str | None:
    return _val_attr(section, "v3:code", "code")


def _sections(clinical_doc: _Element) -> list[_Element]:
    return _findall(clinical_doc, "v3:component/v3:structuredBody/v3:component/v3:section")


def _entries(section: _Element) -> list[_Element]:
    return _findall(section, "v3:entry")


# --- problems ----------------------------------------------------------------


def _conditions(section: _Element, patient_id: str, source_file: str) -> list[Condition]:
    out: list[Condition] = []
    for entry in _entries(section):
        act = _find(entry, "v3:act")
        if act is None:
            continue
        active = _val_attr(act, "v3:statusCode", "code") == "active"
        obs = _find(act, "v3:entryRelationship/v3:observation")
        if obs is None:
            continue
        value = _find(obs, "v3:value")
        snomed = icd10 = None
        if value is not None and _attr(value, "codeSystem") == OID_SNOMED:
            snomed = _attr(value, "code")
        translation = _find(value, "v3:translation")
        if translation is not None and _attr(translation, "codeSystem") == OID_ICD10:
            icd10 = _attr(translation, "code")
        display = _attr(value, "displayName") or _text_content(_find(value, "v3:originalText"))
        out.append(
            Condition(
                patient_id=patient_id,
                snomed=snomed,
                icd10=icd10,
                display=display,
                onset=_ts_date(obs, "v3:effectiveTime/v3:low"),
                stopped=_ts_date(obs, "v3:effectiveTime/v3:high"),
                active=active,
                provenance=_prov(source_file, _val_attr(act, "v3:id", "root")),
            )
        )
    return out


# --- allergies ---------------------------------------------------------------


def _allergies(section: _Element, patient_id: str, source_file: str) -> list[AllergyIntolerance]:
    out: list[AllergyIntolerance] = []
    for entry in _entries(section):
        obs = _find(entry, "v3:act/v3:entryRelationship/v3:observation")
        if obs is None:
            continue
        value_code = _val_attr(obs, "v3:value", "code")
        category = _ALLERGY_CATEGORY.get(value_code or "", AllergyCategory.OTHER)

        allergen = _find(obs, "v3:participant/v3:participantRole/v3:playingEntity/v3:code")
        substance = _attr(allergen, "displayName")
        extensions: dict[str, Any] = {}
        if (allergen_code := _attr(allergen, "code")) is not None:
            extensions["ccda:allergen_code"] = allergen_code

        reactions: list[str] = []
        severity: str | None = None
        for rel in _findall(obs, "v3:entryRelationship"):
            inner = _find(rel, "v3:observation")
            if inner is None:
                continue
            template = _val_attr(inner, "v3:templateId", "root")
            display = _val_attr(inner, "v3:value", "displayName")
            if rel.get("typeCode") == "MFST" and display:
                reactions.append(display)
            elif template == "2.16.840.1.113883.10.20.22.4.8":  # Severity Observation
                severity = display

        out.append(
            AllergyIntolerance(
                patient_id=patient_id,
                substance=substance,
                category=category,
                reactions=reactions,
                severity=severity,
                onset=_ts_date(_find(entry, "v3:act"), "v3:effectiveTime/v3:low"),
                extensions=extensions,
                provenance=_prov(source_file, _val_attr(obs, "v3:id", "root")),
            )
        )
    return out


# --- medications -------------------------------------------------------------


def _medications(section: _Element, patient_id: str, source_file: str) -> list[MedicationStatement]:
    out: list[MedicationStatement] = []
    for entry in _entries(section):
        admin = _find(entry, "v3:substanceAdministration")
        if admin is None:
            continue
        material = _find(
            admin, "v3:consumable/v3:manufacturedProduct/v3:manufacturedMaterial/v3:code"
        )
        period = _find(admin, "v3:effectiveTime")
        extensions: dict[str, Any] = {}
        if (dose := _val_attr(admin, "v3:doseQuantity", "value")) is not None:
            unit = _val_attr(admin, "v3:doseQuantity", "unit")
            extensions["ccda:dose"] = f"{dose} {unit}" if unit else dose
        if (route := _val_attr(admin, "v3:routeCode", "displayName")) is not None:
            extensions["ccda:route"] = route

        is_rxnorm = _attr(material, "codeSystem") == OID_RXNORM
        out.append(
            MedicationStatement(
                patient_id=patient_id,
                display_name=_attr(material, "displayName"),
                rxnorm=_attr(material, "code") if is_rxnorm else None,
                start=_ts_date(period, "v3:low"),
                stop=_ts_date(period, "v3:high"),  # nullFlavor=UNK → None
                active=_val_attr(admin, "v3:statusCode", "code") == "active",
                extensions=extensions,
                provenance=_prov(source_file, _val_attr(admin, "v3:id", "root")),
            )
        )
    return out


# --- immunizations -----------------------------------------------------------


def _immunizations(section: _Element, patient_id: str, source_file: str) -> list[Immunization]:
    out: list[Immunization] = []
    for entry in _entries(section):
        admin = _find(entry, "v3:substanceAdministration")
        if admin is None:
            continue
        material = _find(admin, "v3:consumable/v3:manufacturedProduct/v3:manufacturedMaterial")
        code = _find(material, "v3:code")
        refused = admin.get("negationInd") == "true"
        extensions: dict[str, Any] = {}
        if refused:
            extensions["ccda:negationInd"] = "true"

        out.append(
            Immunization(
                patient_id=patient_id,
                vaccine=_attr(code, "displayName"),
                administered_on=_ts_date(admin, "v3:effectiveTime"),
                lot_number=_text_content(_find(material, "v3:lotNumberText")),
                comment="Refused" if refused else None,
                extensions=extensions,
                provenance=_prov(source_file, _val_attr(admin, "v3:id", "root")),
            )
        )
    return out


# --- vitals + results --------------------------------------------------------


def _measurements(
    section: _Element,
    patient_id: str,
    category: ObservationCategory,
    organizer_path: str,
    source_file: str,
) -> list[Observation]:
    out: list[Observation] = []
    for entry in _entries(section):
        organizer = _find(entry, organizer_path)
        if organizer is None:
            continue
        for component in _findall(organizer, "v3:component/v3:observation"):
            code = _find(component, "v3:code")
            value = _find(component, "v3:value")
            out.append(
                Observation(
                    patient_id=patient_id,
                    category=category,
                    code=_attr(code, "code"),
                    display=_attr(code, "displayName"),
                    value=_attr(value, "value"),
                    unit=_attr(value, "unit"),
                    effective_at=_ts(component, "v3:effectiveTime")
                    or _ts(organizer, "v3:effectiveTime"),
                    provenance=_prov(source_file, _val_attr(component, "v3:id", "root")),
                )
            )
    return out


# --- social history ----------------------------------------------------------


def _social_history(section: _Element, patient_id: str, source_file: str) -> list[Observation]:
    out: list[Observation] = []
    for entry in _entries(section):
        obs = _find(entry, "v3:observation")
        if obs is None or _val_attr(obs, "v3:code", "code") != "72166-2":
            continue
        out.append(
            Observation(
                patient_id=patient_id,
                category=ObservationCategory.SOCIAL_HISTORY,
                display="Tobacco use",
                value=_val_attr(obs, "v3:value", "displayName"),
                effective_at=_ts(obs, "v3:effectiveTime"),
                provenance=_prov(source_file, _val_attr(obs, "v3:id", "root")),
            )
        )
    return out


# --- encounters + notes ------------------------------------------------------


def _encounter_id(root: str | None, source_file: str, index: int) -> str:
    """Stable encounter id.

    Prefers the source's id-root when it looks like a real GUID (the
    synthetic-fixture shape, or any 8-4-4-4-12 hex pattern a vendor would
    emit). Otherwise derives a deterministic UUID from the file name and
    the encounter's positional index in the document — so re-parsing the
    same CCD yields the same encounter ids, which is what the engine's
    idempotent-skip invariant rides on.
    """
    if root and _GUID_RE.match(root):
        return root
    return str(uuid5(NAMESPACE_URL, f"anastomosis:ccda:{source_file}:encounter:{index}"))


def _encounters(section: _Element, patient_id: str, source_file: str) -> list[Encounter]:
    out: list[Encounter] = []
    for index, entry in enumerate(_entries(section)):
        enc = _find(entry, "v3:encounter")
        if enc is None:
            continue
        code = _find(enc, "v3:code")
        encounter_type = _attr(code, "displayName")
        out.append(
            Encounter(
                id=_encounter_id(_val_attr(enc, "v3:id", "root"), source_file, index),
                patient_id=patient_id,
                date_of_service=_ts_date(enc, "v3:effectiveTime")
                or _ts_date(enc, "v3:effectiveTime/v3:low"),
                encounter_type=encounter_type,
                note_type=encounter_type,
                provenance=_prov(source_file, _val_attr(enc, "v3:id", "root")),
            )
        )
    return out


def _note_encounters(section: _Element, patient_id: str, source_file: str) -> list[Encounter]:
    out: list[Encounter] = []
    for index, entry in enumerate(_entries(section)):
        act = _find(entry, "v3:act")
        if act is None:
            continue
        text = _text_content(_find(act, "v3:text"))
        out.append(
            Encounter(
                id=_encounter_id(_val_attr(act, "v3:id", "root"), f"{source_file}:note", index),
                patient_id=patient_id,
                date_of_service=_ts_date(act, "v3:author/v3:time"),
                note_type=_val_attr(act, "v3:code", "displayName"),
                sections=[NoteSection(kind=SectionKind.NARRATIVE, text=text, html=None)],
                provenance=_prov(source_file, _val_attr(act, "v3:id", "root")),
            )
        )
    return out


# --- top-level assembly ------------------------------------------------------


# Hardened XML parser: third-party clinical documents must never resolve
# external entities (XXE), fetch external DTDs over the network (SSRF), or
# expand into unbounded trees (billion-laughs / quadratic blowup). These
# flags are the OWASP-recommended posture for any XML ingest the
# application does not author itself.
_PARSER = etree.XMLParser(
    resolve_entities=False,
    no_network=True,
    load_dtd=False,
    huge_tree=False,
)


def parse_document(path: Path) -> PatientRecord:
    """Parse one C-CDA / CCD XML file into a :class:`PatientRecord`.

    Raises :exc:`ValueError` if the file is not a CDA ``ClinicalDocument`` —
    a loud failure, per the source-adapter contract.
    """
    tree = etree.parse(str(path), _PARSER)
    root = tree.getroot()
    if etree.QName(root).localname != "ClinicalDocument" or root.tag != _q("ClinicalDocument"):
        raise ValueError(f"{path.name}: not a C-CDA ClinicalDocument (root <{root.tag}>)")

    source_file = path.name
    doc_meta: dict[str, Any] = {}
    if (doc_id := _val_attr(root, "v3:id", "root")) is not None:
        doc_meta["ccda:documentId"] = doc_id
    if (effective := _val_attr(root, "v3:effectiveTime", "value")) is not None:
        doc_meta["ccda:effectiveTime"] = effective
    if (title := _text_content(_find(root, "v3:title"))) is not None:
        doc_meta["ccda:title"] = title

    patient = _patient(root, source_file, doc_meta)
    pid = patient.id
    record = PatientRecord(
        patient=patient, provenance=_prov(source_file, doc_meta.get("ccda:documentId"))
    )

    for section in _sections(root):
        loinc = _section_code(section)
        if loinc == LOINC_PROBLEMS:
            record.conditions += _conditions(section, pid, source_file)
        elif loinc == LOINC_ALLERGIES:
            record.allergies += _allergies(section, pid, source_file)
        elif loinc == LOINC_MEDICATIONS:
            record.medications += _medications(section, pid, source_file)
        elif loinc == LOINC_IMMUNIZATIONS:
            record.immunizations += _immunizations(section, pid, source_file)
        elif loinc == LOINC_VITALS:
            record.observations += _measurements(
                section, pid, ObservationCategory.VITAL_SIGNS, "v3:organizer", source_file
            )
        elif loinc == LOINC_RESULTS:
            record.observations += _measurements(
                section, pid, ObservationCategory.LABORATORY, "v3:organizer", source_file
            )
        elif loinc == LOINC_SOCIAL:
            record.observations += _social_history(section, pid, source_file)
        elif loinc == LOINC_ENCOUNTERS:
            record.encounters += _encounters(section, pid, source_file)
        elif loinc == LOINC_NOTES:
            record.encounters += _note_encounters(section, pid, source_file)
        else:
            # Losslessness: an unparsed section's narrative is never dropped.
            # Mutating the model's extensions dict in place persists it on the
            # patient (it is the validated dict object, not a fresh copy).
            key = f"ccda:section:{loinc}" if loinc else "ccda:section:unknown"
            record.patient.extensions[key] = {
                "title": _text_content(_find(section, "v3:title")),
                "text": _text_content(_find(section, "v3:text")),
            }

    return record
