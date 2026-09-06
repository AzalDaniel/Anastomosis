"""C-CDA R2.1 / CCD XML → canonical PatientRecord.

Every section becomes discrete models where it can, and narrative
verbatim otherwise (rules 55, 59); the header's participations keep
their document role (rule 60); a stamped 51899-3 loss ledger merges by
generation (rule 61). See ``tests/fixtures/ccda/README.md`` for the
element provenance.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date
from functools import cache
from pathlib import Path
from typing import Any, TypedDict
from uuid import NAMESPACE_URL, uuid5

from lxml import etree

from anastomosis.core.ccda_codes import (
    ARTIFACT_INTEGRITY_ALGORITHM,
    ARTIFACT_TEMPLATE_ROOT,
    EXT_PRIOR_LOSS_NARRATIVE,
    EXT_SECTION_ENTRIES,
    GUID_RE,
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
    identity_from_ii,
    organizer_component_source_id,
)
from anastomosis.core.hashutil import hash_and_size
from anastomosis.core.model import (
    EXT_INLINE_CONTENT,
    AllergyCategory,
    AllergyIntolerance,
    Condition,
    ContactKind,
    ContactPoint,
    DocumentArtifact,
    Encounter,
    Facility,
    Identifier,
    IdentifierKind,
    Immunization,
    MedicationStatement,
    NoteSection,
    Observation,
    ObservationCategory,
    Patient,
    PatientRecord,
    Practitioner,
    Provenance,
    SectionKind,
)
from anastomosis.core.model.patient import Address
from anastomosis.core.textutil import format_phone, media_type_suffix
from anastomosis.core.timeutil import is_zero_sentinel, parse_date, parse_dt
from anastomosis.sources.base import SourceDataError

__all__ = [
    "MAX_ARTIFACT_BYTES",
    "SOURCE",
    "UnstructuredBodyMissingError",
    "UnstructuredBodyTooLargeError",
    "parse_document",
]

SOURCE = "ccda"

# --- namespaces / OIDs (verified C-CDA R2.1 reference) -----------------------

NS = {"v3": V3, "sdtc": SDTC, "xsi": XSI}


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

# The GUID predicate lives beside the identity rule it serves (#412).
_GUID_RE = GUID_RE
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
    """``@value`` of the element at ``path``, parsed as an aware datetime.
    The zero-sentinel check (rule 67) happens HERE, before ``parse_dt``
    sees it — ``parse_dt`` is shared with every row-based adapter, where a
    bare "0" is a stated value, not absence.
    """
    raw = _val_attr(node, path, "value")
    return None if is_zero_sentinel(raw) else parse_dt(raw)


def _ts_date(node: _Element | None, path: str) -> Any:
    """``@value`` of the element at ``path``, parsed as a calendar date.
    Same zero-sentinel guard as :func:`_ts`: ``parse_date`` calls
    ``parse_dt`` and would raise on a zero run just as loudly.
    """
    raw = _val_attr(node, path, "value")
    return None if is_zero_sentinel(raw) else parse_date(raw)


def _text_content(node: _Element | None) -> str | None:
    """Normalized visible text of an element subtree (whitespace collapsed)."""
    if node is None:
        return None
    parts = [t if isinstance(t, str) else t.decode() for t in node.itertext()]
    text = _WS_RE.sub(" ", "".join(parts)).strip()
    return text or None


def _collapse(raw: str | bytes | None) -> str | None:
    """Whitespace-collapsed text, or None when there is nothing to keep."""
    if raw is None:
        return None
    text = _WS_RE.sub(" ", raw if isinstance(raw, str) else raw.decode()).strip()
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


def _patient_id(patient_role: _Element, source_file: str) -> str:
    """Stable canonical patient id: the identifier the SOURCE states,
    honoured whole per rule 20 (the pair, each half quoted per
    :func:`~anastomosis.core.ccda_codes.identity_from_ii`). Absent any id,
    the file name stands in.
    """
    fallback = f"{source_file}:patient"
    for ident in _identifiers(patient_role):
        if ident.kind != IdentifierKind.SOURCE_GUID:
            continue
        # `_identifiers` records the root as `system` when an extension exists,
        # and as `value` when it stands alone — so the pair is (system, value)
        # or (value, None), never both readings at once.
        pair = (ident.system, ident.value) if ident.system else (ident.value, None)
        return identity_from_ii("patient", pair, fallback, bare_root_names_the_instance=True)
    return identity_from_ii("patient", None, fallback, bare_root_names_the_instance=True)


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


def _addresses(node_with_addresses: _Element) -> list[Address]:
    """Every ``<addr>`` on an element — a patientRole, or any role that has one."""
    out: list[Address] = []
    for node in _findall(node_with_addresses, "v3:addr"):
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
        # nullFlavor="OTH" carries the source spelling in originalText — that is
        # where an unmapped gender string lives, so read it before the code.
        sex = _text_content(_find(gender, "v3:originalText")) or _SEX_BY_CODE.get(
            _attr(gender, "code") or ""
        )

    source_id = next(
        (i.value for i in _identifiers(patient_role) if i.kind == IdentifierKind.SSN), None
    )
    return Patient(
        id=_patient_id(patient_role, source_file),
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


def _artifact_media(entry: _Element) -> _Element | None:
    """``entry``'s ``<observationMedia>`` when THIS toolkit's export wrote
    it — one reading shared by :func:`_delivered_artifacts` and
    :func:`_capture_entries`, so the two cannot drift. Decided by the
    stamp (:data:`ARTIFACT_TEMPLATE_ROOT`), never the element name.
    """
    media = _find(entry, "v3:observationMedia")
    if media is None:
        return None
    stamped = any(t.get("root") == ARTIFACT_TEMPLATE_ROOT for t in _findall(media, "v3:templateId"))
    return media if stamped else None


def entry_verbatim(entry: _Element) -> str:
    """One ``<entry>``, exactly as the document spells it — the shared
    vocabulary between what :func:`_capture_entries` stores and what the
    ledger asks for, so the mirror cannot drift. ``with_tail=False``: the
    whitespace after ``</entry>`` belongs to the section, not the entry.
    """
    return etree.tostring(entry, encoding="unicode", with_tail=False)


def free_key(extensions: dict[str, Any], key: str) -> str:
    """``key``, or its first free ``#2``, ``#3``, … variant, in document
    order — a document may repeat a section code or carry several
    code-less sections, and one stored section must never replace
    another. Public: the pipeline's record fold parks a clashing
    extension the same way.
    """
    if key not in extensions:
        return key
    occurrence = 2
    while f"{key}#{occurrence}" in extensions:
        occurrence += 1
    return f"{key}#{occurrence}"


def _capture_narrative(record: PatientRecord, section: _Element, loinc: str | None) -> None:
    """Preserve one section's title and narrative under
    ``ccda:section:<loinc>`` (rule 59), for EVERY section whether or not
    it structurally parsed. No title and no text adds no key. Mutates the
    model's extensions dict in place.
    """
    title = _text_content(_find(section, "v3:title"))
    text = _text_content(_find(section, "v3:text"))
    extensions = record.patient.extensions
    if title is None and text is None:
        return
    key = free_key(extensions, f"ccda:section:{loinc or SECTION_CODE_UNKNOWN}")
    extensions[key] = {"title": title, "text": text}


def _capture_entries(root: _Element) -> dict[_Element, list[str]]:
    """Every section's entries, exactly as spelled, in one pass over the
    untouched tree (rule 59) — EVERY section, whatever it renders (#314).
    Excludes this toolkit's own stamped ``<observationMedia>``: re-derived
    from the typed object, not the source's statement.
    """
    captured: dict[_Element, list[str]] = {}
    for section in _sections(root):
        entries = [entry for entry in _entries(section) if _artifact_media(entry) is None]
        if entries:
            captured[section] = [entry_verbatim(entry) for entry in entries]
    return captured


def _store_entries(
    extensions: dict[str, Any],
    captured: dict[_Element, list[str]],
    section: _Element,
    loinc: str | None,
) -> None:
    """Park one section's captured entries under
    ``ccda:entries:<loinc>``, read by the ingest ledger's entry pool and
    ``deliver/ccda_export``. A section with no code parks under
    :data:`SECTION_CODE_UNKNOWN`.
    """
    entries = captured.get(section)
    if not entries:
        return
    key = free_key(extensions, f"{EXT_SECTION_ENTRIES}:{loinc or SECTION_CODE_UNKNOWN}")
    extensions[key] = entries


def _is_own_loss_narrative(section: _Element, loinc: str | None) -> bool:
    """Whether ``section`` is a loss ledger THIS repo's C-CDA exporter wrote.

    Matched on the section code plus the exporter's own stamp — never on the
    code alone: 51899-3 is a public LOINC any vendor may use, and a third
    party's section must keep round-tripping as ordinary foreign narrative.
    """
    if loinc != LOINC_EXTENSIONS:
        return False
    if any(
        _attr(node, "root") == LOSS_NARRATIVE_TEMPLATE_ROOT
        for node in _findall(section, "v3:templateId")
    ):
        return True
    return _text_content(_find(section, "v3:title")) == LOSS_NARRATIVE_TITLE


def _loss_generation(section: _Element) -> int | None:
    """The export generation stamped on a loss ledger, or ``None`` when absent
    or unreadable (sentinel discipline — the counter is provenance, not clinical
    content, so an unreadable one restarts the count rather than raising)."""
    for node in _findall(section, "v3:id"):
        if _attr(node, "root") != LOSS_NARRATIVE_GENERATION_ROOT:
            continue
        raw = _attr(node, "extension")
        if raw is None:
            continue
        try:
            return int(raw)
        except ValueError:
            # `str.isdigit()` was the guard here, and it is true for characters
            # int() refuses — a superscript "²" among them — so a crafted stamp
            # raised out of parse_document and aborted the WHOLE ingest instead
            # of this one counter. Asking int() directly is the same question
            # without the gap between the two.
            return None
    return None


def _narrative_entries(text_node: _Element | None) -> list[str]:
    """Every discrete piece of a section's narrative, in document order —
    one entry per child element (a ``<paragraph>``, but equally a
    ``<table>`` or ``<list>``), plus any loose text between them.
    """
    if text_node is None:
        return []
    entries: list[str] = []
    if lead := _collapse(text_node.text):
        entries.append(lead)
    for child in text_node:
        # A comment or processing instruction is not an element: lxml gives it a
        # callable tag, and itertext() raises ValueError on one. Without this a
        # comment inside <text> aborts the whole parse.
        if callable(child.tag):
            continue
        if body := _text_content(child):
            entries.append(body)
        if tail := _collapse(child.tail):
            entries.append(tail)
    return entries


def _capture_loss_narrative(record: PatientRecord, section: _Element) -> None:
    """Preserve OUR OWN loss ledger under
    ``ccda:prior_loss_narrative``, as discrete per-paragraph entries so a
    re-export can dedupe them (rule 61) rather than swallowing one
    ever-growing blob. An unreadable (non-per-paragraph) ledger is kept
    whole as one entry.
    """
    entries = _narrative_entries(_find(section, "v3:text"))
    if not entries:
        return
    merge_loss_narrative(record.patient.extensions, _loss_generation(section), entries)


def is_loss_ledger(value: Any) -> bool:
    """Whether ``value`` has the ``{generation, entries}`` shape this
    module writes under :data:`EXT_PRIOR_LOSS_NARRATIVE`. Public: both
    :func:`merge_loss_narrative` and the pipeline's fold must tell a real
    carried-forward ledger from an ordinary dict before folding into it.
    """
    return (
        isinstance(value, dict)
        and isinstance(value.get("entries"), list)
        and isinstance(value.get("generation"), int | None)
    )


def merge_loss_narrative(
    extensions: dict[str, Any], generation: int | None, entries: list[str]
) -> None:
    """Fold one stamped loss ledger into whatever ``extensions`` already
    holds: entries concatenate, highest generation wins (rule 61).
    Checked via :func:`is_loss_ledger`, never bare ``isinstance(dict)``,
    since a merged chart can already hold a non-ledger dict at this key.
    """
    # Annotated: `dict.get` without a default types `Any | None`, and
    # `is_loss_ledger` isn't a TypeGuard, so mypy needs the hint explicitly.
    prior: Any = extensions.get(EXT_PRIOR_LOSS_NARRATIVE)
    if is_loss_ledger(prior):
        prior_generation = prior["generation"]
        extensions[EXT_PRIOR_LOSS_NARRATIVE] = {
            "generation": (
                generation if prior_generation is None else max(prior_generation, generation or 0)
            ),
            "entries": [*prior["entries"], *entries],
        }
        return
    extensions[EXT_PRIOR_LOSS_NARRATIVE] = {"generation": generation, "entries": entries}


# --- problems ----------------------------------------------------------------


def _fact_id(kind: str, element: _Element | None, source_file: str, position: str) -> str:
    """A clinical fact's canonical id, on the one identifier rule (#412, #405).

    Every clinical object used to take ``AnastBase.id``'s ``uuid4`` default,
    which no adapter set — so two loads of one document produced two different
    ids for the same problem, and the FHIR bundle they were written into could
    not be byte-compared for drift (#405). A fact's identity is not this run's
    bookkeeping any more than a patient's is: it is the ``<id>`` the source
    stated, and where the source stated none, its position in the document.

    ``bare_root_names_the_instance=False``, the encounter answer rather than
    the patient one, and for the encounter reason: a section lists MANY
    problems, allergies and results, so a bare vendor OID shared between them
    is the assigning authority, not the fact. Unlike an organization there is
    no conflict guard here that would catch a wrong fold, so the ambiguous
    case takes the position it was written in rather than a merge nobody
    would be told about.
    """
    return identity_from_ii(
        kind,
        first_rooted_id(element) if element is not None else None,
        f"{source_file}:{kind}:{position}",
        bare_root_names_the_instance=False,
    )


def _conditions(section: _Element, patient_id: str, source_file: str) -> list[Condition]:
    out: list[Condition] = []
    for index, entry in enumerate(_entries(section)):
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
                id=_fact_id("condition", act, source_file, str(index)),
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
    for index, entry in enumerate(_entries(section)):
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
            elif template == TPL_SEVERITY:
                severity = display

        out.append(
            AllergyIntolerance(
                id=_fact_id("allergy", obs, source_file, str(index)),
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
    for index, entry in enumerate(_entries(section)):
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
                id=_fact_id("medication", admin, source_file, str(index)),
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
    for index, entry in enumerate(_entries(section)):
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
                id=_fact_id("immunization", admin, source_file, str(index)),
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


def _observation_value(value: _Element | None) -> tuple[str | None, str | None]:
    """``(value, unit)`` from any C-CDA result form, not just ``xsi:type="PQ"``.

    Reading only the PQ shape (``@value``/``@unit``) left every qualitative
    result empty while still producing an Observation carrying its LOINC code —
    a finalized result that says nothing, which a receiver reads as "no result"
    rather than as the Negative or Trace the document actually recorded.
    """
    if value is None:
        return None, None
    if (quantity := _attr(value, "value")) is not None:  # PQ, INT, REAL
        return quantity, _attr(value, "unit")
    if (coded := _attr(value, "displayName")) is not None:  # CD
        return coded, None
    if (interval := _interval_value(value)) is not None:  # IVL_PQ
        return interval
    # ST / ED and anything else that carries its result as element text.
    return _text_content(value), None


def _interval_value(value: _Element) -> tuple[str | None, str | None] | None:
    """``(reading, unit)`` for an IVL_PQ, or ``None`` when it is not one.

    A range with both ends reads ``low-high``; one open end reads as the end it
    has, because "under 5" is still a result and dropping the number to avoid
    picking a spelling for the missing half would lose it. ``None`` means the
    element carried no bound at all, which is the caller's cue to keep looking
    rather than to record an empty range.
    """
    low, high = _find(value, "v3:low"), _find(value, "v3:high")
    lo, hi = (_attr(end, "value") for end in (low, high))
    if not (lo or hi):
        return None
    reading = f"{lo}-{hi}" if lo and hi else (lo or hi)
    return reading, _attr(low, "unit") or _attr(high, "unit")


def _measurements(
    section: _Element,
    patient_id: str,
    category: ObservationCategory,
    organizer_path: str,
    source_file: str,
) -> list[Observation]:
    out: list[Observation] = []
    for entry_index, entry in enumerate(_entries(section)):
        organizer = _find(entry, organizer_path)
        if organizer is None:
            continue
        organizer_id = first_rooted_id(organizer)
        components = _findall(organizer, "v3:component/v3:observation")
        for index, component in enumerate(components):
            code = _find(component, "v3:code")
            reading, unit = _observation_value(_find(component, "v3:value"))
            # A component with no id of its own is still the organizer's
            # statement, not a statement with no provenance at all — see
            # organizer_component_source_id. ``first_rooted_id`` reads every
            # ``<id>`` child in document order, not only the first, so a
            # component whose first ``<id>`` is nullFlavor and whose second
            # is rooted is read as owning that second id — the same reading
            # the builder's stated-id walk already gives it.
            component_id = first_rooted_id(component)
            source_id = component_id[0] if component_id is not None else None
            if source_id is None and organizer_id is not None:
                root, extension = organizer_id
                source_id = organizer_component_source_id(root, extension, index)
            out.append(
                Observation(
                    id=_fact_id("observation", component, source_file, f"{entry_index}:{index}"),
                    patient_id=patient_id,
                    category=category,
                    code=_attr(code, "code"),
                    display=_attr(code, "displayName"),
                    value=reading,
                    unit=unit,
                    effective_at=_ts(component, "v3:effectiveTime")
                    or _ts(organizer, "v3:effectiveTime"),
                    provenance=_prov(source_file, source_id),
                )
            )
    return out


# --- social history ----------------------------------------------------------


def _social_history(section: _Element, patient_id: str, source_file: str) -> list[Observation]:
    out: list[Observation] = []
    for index, entry in enumerate(_entries(section)):
        obs = _find(entry, "v3:observation")
        if obs is None or _val_attr(obs, "v3:code", "code") != "72166-2":
            continue
        out.append(
            Observation(
                id=_fact_id("observation", obs, source_file, str(index)),
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


def _encounter_id(id_pair: tuple[str, str | None] | None, source_file: str, index: int) -> str:
    """Stable encounter id: the identifier the source states, when it states one.

    HL7 v3's ``II`` datatype is the PAIR ``(root, extension)``: a root names a
    namespace and an extension names the instance within it. Two encounters
    stating the same pair are the same encounter by the datatype's own
    definition — so ANY root paired with a non-blank extension is trusted as
    identity, hashed deterministically over both halves.
    A GUID root standing ALONE (no extension, the synthetic-fixture shape or
    any 8-4-4-4-12 hex pattern a vendor would emit) is trusted too, verbatim,
    because a GUID needs no assigning authority to be unique.

    An OID root standing alone is different: it usually names the vendor's
    assigning authority, shared by every encounter that vendor ever writes,
    not a visit — so it is not treated as identity. Those encounters (and any
    with no usable id at all) fall through to a deterministic UUID derived
    from the file name and the encounter's positional index in the document
    — so re-parsing the same CCD still yields the same encounter ids, which is
    what the engine's idempotent-skip invariant rides on.

    An extension that is blank or all-whitespace is not an extension —
    ``id_pair`` never carries one (see :func:`~anastomosis.core.ccda_codes.
    first_rooted_id`, which normalizes a blank extension to ``None`` before
    this function ever sees it) — so a root with only a blank extension is
    read as root-only, same as a bare root.
    """
    return identity_from_ii(
        "encounter",
        id_pair,
        f"{source_file}:encounter:{index}",
        bare_root_names_the_instance=False,
    )


def _encounters(section: _Element, patient_id: str, actors: _Actors) -> list[Encounter]:
    source_file = actors.source_file
    out: list[Encounter] = []
    for index, entry in enumerate(_entries(section)):
        enc = _find(entry, "v3:encounter")
        if enc is None:
            continue
        code = _find(enc, "v3:code")
        # displayName first — a genuinely coded encounter from another system
        # names itself there. A nullFlavor code has no displayName to read (_attr
        # treats the whole element as absent), and its type is in originalText.
        original_text = _text_content(_find(code, "v3:originalText"))
        encounter_type = _attr(code, "displayName") or original_text
        out.append(
            Encounter(
                id=_encounter_id(first_rooted_id(enc), source_file, index),
                patient_id=patient_id,
                date_of_service=_ts_date(enc, "v3:effectiveTime")
                or _ts_date(enc, "v3:effectiveTime/v3:low"),
                encounter_type=encounter_type,
                provider_id=_entry_performer(enc, actors),
                provenance=_prov(source_file, _val_attr(enc, "v3:id", "root")),
            )
        )
    return out


def _entry_performer(enc: _Element, actors: _Actors) -> str | None:
    """Who delivered the care this entry records.

    An encounter that names no performer cannot say which clinician the visit
    should be attributed to, which is half of what a migrated chart is FOR.
    """
    performer = _find(enc, "v3:performer")
    entity = _find(performer, _ASSIGNED_ENTITY.path)
    if performer is None or entity is None:
        return None
    return actors.add_person(performer, "performer", entity, _ASSIGNED_ENTITY)


def _note_encounters(section: _Element, patient_id: str, actors: _Actors) -> list[Encounter]:
    source_file = actors.source_file
    out: list[Encounter] = []
    for index, entry in enumerate(_entries(section)):
        act = _find(entry, "v3:act")
        if act is None:
            continue
        text = _text_content(_find(act, "v3:text"))
        # The note's own author, already read out of the document-wide author
        # pass: this is the answer to "who wrote this note", and it is the one a
        # reader of the note needs rather than the header's.
        author = _find(act, "v3:author")
        out.append(
            Encounter(
                id=_encounter_id(first_rooted_id(act), f"{source_file}:note", index),
                patient_id=patient_id,
                date_of_service=_ts_date(act, "v3:author/v3:time"),
                note_type=_val_attr(act, "v3:code", "displayName"),
                provider_id=None if author is None else actors.authors.get(author),
                sections=[NoteSection(kind=SectionKind.NARRATIVE, text=text, html=None)],
                provenance=_prov(source_file, _val_attr(act, "v3:id", "root")),
            )
        )
    return out


_ENCOUNTER_LIST_FIELDS = ("sections", "addenda", "diagnosis_ids")
_ENCOUNTER_IDENTITY_FIELDS = ("id", "patient_id", "provenance")


def _folds_together(seen: Encounter, incoming: Encounter) -> bool:
    """Whether two encounters under one id are halves of one visit.

    They are only if nothing they BOTH state disagrees. Two entries describing
    genuinely different visits — different dates, different types — happen in
    ordinary C-CDA and must stay two objects, so the archive refuses rather than
    writing one page over the other. Folding those would invent a hybrid visit
    that happened on neither day, which is the misfiling this project exists to
    prevent (see tests/unit/test_duplicate_encounter_ids.py).
    """
    for name in type(seen).model_fields:
        if name in _ENCOUNTER_IDENTITY_FIELDS or name in _ENCOUNTER_LIST_FIELDS:
            continue
        if name == "extensions":
            continue
        mine, theirs = getattr(seen, name), getattr(incoming, name)
        if mine is not None and theirs is not None and mine != theirs:
            return False
    return True


def fold_encounters_sharing_an_id(encounters: list[Encounter]) -> list[Encounter]:
    """One ``<id root>`` is one visit when the halves agree.

    A C-CDA may describe the same encounter twice: once as an entry in the
    46240-8 Encounters section and again as the Note Activity documenting it in
    34109-9. Both legitimately carry the same ``<id root>``, and this parser
    appended an Encounter for each, so a record round-tripped through our own
    exporter came back with every visit doubled and two objects sharing one id.
    Downstream that is fatal rather than untidy: ArchiveDeliverer refuses the
    patient with DeliveredNameCollision, blaming a source that did nothing wrong.

    Complementary halves fold — first non-None wins per scalar, lists
    concatenate, order preserved. Contradictory ones do not: they stay separate
    so the collision still surfaces.

    Public because the same two halves also arrive in two DOCUMENTS: an export
    holding one patient's visit summary and its note names that visit twice
    across two files, and the pipeline's fold unions their encounters. The rule
    is a property of the canonical Encounter, not of one traversal, so both
    callers run this one — but the reach across documents is only as wide as
    :func:`_encounter_id` makes it: the cross-document fold reaches encounters
    whose ``<id>`` states an identity — a GUID root standing alone, or ANY
    root paired with a non-blank extension (HL7 v3's ``II`` is the pair, not
    the root alone). An OID root standing alone names an assigning authority
    rather than a visit, so it still gets one id PER DOCUMENT and never folds
    here, across two documents, no matter how it agrees with itself (#393).
    """
    folded: dict[str, Encounter] = {}
    order: list[str] = []
    kept_apart: list[Encounter] = []
    for encounter in encounters:
        seen = folded.get(encounter.id)
        if seen is None:
            folded[encounter.id] = encounter
            order.append(encounter.id)
            continue
        if not _folds_together(seen, encounter):
            kept_apart.append(encounter)
            continue
        folded[encounter.id] = seen.model_copy(update=_folded_fields(seen, encounter))
    return [folded[key] for key in order] + kept_apart


def _folded_fields(seen: Encounter, incoming: Encounter) -> dict[str, object]:
    """The fields to copy from ``incoming`` onto ``seen``, by kind.

    Three rules, and which one applies is a property of the field rather than
    of the visit: lists concatenate in the order the halves were read,
    extensions merge with the later half winning a shared key, and every other
    scalar is filled in only where the first half left a gap. Identity fields
    are excluded — the halves already agree on those, which is what let them
    fold at all.
    """
    update: dict[str, object] = {}
    for name in _ENCOUNTER_LIST_FIELDS:
        update[name] = [*(getattr(seen, name) or []), *(getattr(incoming, name) or [])]
    if extensions := incoming.extensions:
        update["extensions"] = {**(seen.extensions or {}), **extensions}
    for name in _folded_scalar_fields(type(incoming)):
        value = getattr(incoming, name)
        if getattr(seen, name) is None and value is not None:
            update[name] = value
    return update


@cache
def _folded_scalar_fields(model: type[Encounter]) -> tuple[str, ...]:
    """Every field folded by the fill-a-gap rule: not identity, list or extensions.

    Derived from the model rather than listed here, so a field added to
    Encounter is folded by default instead of being silently skipped by a
    hand-written tuple nobody remembered to extend.
    """
    handled = {*_ENCOUNTER_IDENTITY_FIELDS, *_ENCOUNTER_LIST_FIELDS, "extensions"}
    return tuple(name for name in model.model_fields if name not in handled)


# --- participations: who wrote it, who did it, and where ---------------------

# The NPI arc. An id under this root names the provider nationally and has its
# own slot on Practitioner; every other id on a role is that role's own
# identifier.
OID_NPI = "2.16.840.1.113883.4.6"


@dataclass(frozen=True)
class _Role:
    """One CDA role element, and where it keeps the person and the organization.

    CDA spells this differently under every participation — an
    ``assignedEntity`` plays an ``assignedPerson`` and represents an
    organization, an ``associatedEntity`` plays an ``associatedPerson`` and is
    scoped by one — so the spellings are written out here rather than derived
    from the participation's name. Deriving them would be a guess, and a guess
    at this seam reads a document's spouse as its author.

    ``person`` is a tuple because a document may play a person under another
    role's spelling; the element actually found is recorded on the practitioner,
    so the record says what the document said rather than what was expected.
    """

    path: str
    person: tuple[str, ...]
    organization: str | None


_ASSIGNED_ENTITY = _Role("v3:assignedEntity", ("v3:assignedPerson",), "v3:representedOrganization")
_RELATED_ENTITY = _Role("v3:relatedEntity", ("v3:relatedPerson",), None)
#: ``informationRecipient`` is the conforming spelling and is tried first;
#: ``assignedPerson`` is VENDOR TOLERANCE, kept deliberately. No C-CDA R2.1
#: document may play a person under that name here — an ``intendedRecipient``
#: plays an ``informationRecipient`` — but exporters that reuse their
#: ``assignedEntity`` writer for this one participation do emit it, and the
#: posture of this adapter is to read what exists and refuse only what it would
#: lose. Reading it costs one path and loses nothing: ``_person_element``
#: returns the element name the document actually used and it lands on the
#: practitioner as ``ccda:entity``, so a record built from the non-standard form
#: still says which form it was built from. Removing the path would turn a
#: recipient this adapter can name into a document that parsed clean and carried
#: nobody, which is the failure this whole module exists to close (#312).
#: This is tolerance on the READ side only: the corpus generator emits the
#: conforming shape (#327), so the ledger's numbers are evidence about documents
#: that could exist, and the tolerant path is exercised by a fixture that says
#: in its own name that it is vendor divergence.
_INTENDED_RECIPIENT = _Role(
    "v3:intendedRecipient",
    ("v3:informationRecipient", "v3:assignedPerson"),
    "v3:receivedOrganization",
)
_ASSOCIATED_ENTITY = _Role(
    "v3:associatedEntity", ("v3:associatedPerson",), "v3:scopingOrganization"
)
_ASSIGNED_AUTHOR = _Role("v3:assignedAuthor", ("v3:assignedPerson",), "v3:representedOrganization")

#: The participations besides ``author``: the ElementPath each is found at, and
#: the role element(s) CDA allows beneath it. One row per participation NAME, and
#: that name is what lands on the canonical object: a legalAuthenticator is not
#: an authenticator and neither is an informant, so a record that folded them
#: together could no longer say who signed the chart and who merely supplied the
#: history.
#:
#: Scope is per-row and argued, not uniform — the same argument the ingest
#: ledger's own table makes, so the two agree about what they are counting.
#: ``informant`` is read wherever it appears, because a clinical statement may
#: name who supplied it and the header's informant does not answer for that one.
#: Everything else is a DIRECT child of ``ClinicalDocument``: a ``<participant>``
#: nested inside an allergy entry is the allergen substance and is parsed as one
#: already, and a walk that reached it would make a practitioner out of a drug.
_PARTICIPATIONS: tuple[tuple[str, str, tuple[_Role, ...]], ...] = (
    ("dataEnterer", "v3:dataEnterer", (_ASSIGNED_ENTITY,)),
    ("informant", ".//v3:informant", (_ASSIGNED_ENTITY, _RELATED_ENTITY)),
    ("informationRecipient", "v3:informationRecipient", (_INTENDED_RECIPIENT,)),
    ("legalAuthenticator", "v3:legalAuthenticator", (_ASSIGNED_ENTITY,)),
    ("authenticator", "v3:authenticator", (_ASSIGNED_ENTITY,)),
    ("participant", "v3:participant", (_ASSOCIATED_ENTITY,)),
)

#: Marks the Encounter that came from ``componentOf/encompassingEncounter``.
#: Read back by :func:`_visit_candidates`, which has to tell the document's own
#: frame from an entry inside it.
EXT_ENCOMPASSING = "ccda:componentOf"


@dataclass
class _Actors:
    """The people and places one document names, gathered as they are read.

    Facilities are keyed and practitioners are not, and the asymmetry is the
    point. One organization is routinely named by several participations — in
    most exports the author's practice IS the custodian — and two Facility
    objects under one id is the collision ``ArchiveDeliverer`` refuses a whole
    patient for. One person is equally often named twice, but as two different
    answers: the clinician who wrote the note and the one who legally signed it
    are separate facts even when they are the same human, so each participation
    keeps its own Practitioner carrying its own role.

    ``authors`` maps each ``<author>`` element to the practitioner it produced,
    so a Note Activity can point at the author already read out of it instead of
    a second pass creating that person twice.
    """

    source_file: str
    practitioners: list[Practitioner] = field(default_factory=list)
    facilities: dict[str, Facility] = field(default_factory=dict)
    authors: dict[_Element, str] = field(default_factory=dict)

    def add_person(
        self, participation: _Element, participation_name: str, entity: _Element, role: _Role
    ) -> str | None:
        """Record the person a participation names; return the practitioner id.

        ``None`` when the role element names nobody. CDA requires the wrapper
        even when it is empty — this repo's own exporter writes a bare
        ``<assignedAuthor/>`` to satisfy the schema — and a Practitioner built
        from one would be an actor no document ever named.
        """
        if not _names_an_actor(entity, role):
            return None
        practitioner = _person_practitioner(
            participation,
            participation_name,
            entity,
            role,
            self.source_file,
            len(self.practitioners),
        )
        self._link_organization(practitioner, entity, role)
        self.practitioners.append(practitioner)
        return practitioner.id

    def add_device(self, author: _Element, entity: _Element) -> str:
        """Record the SYSTEM an author names; return the practitioner id."""
        practitioner = _device_practitioner(
            author, entity, self.source_file, len(self.practitioners)
        )
        self._link_organization(practitioner, entity, _ASSIGNED_AUTHOR)
        self.practitioners.append(practitioner)
        return practitioner.id

    def add_facility(self, nodes: Sequence[_Element]) -> str | None:
        """Record an organization; return the facility id it was filed under.

        ``None`` when the elements describe no place at all, for the same reason
        an empty role names nobody.
        """
        if not _describes_a_place(nodes):
            return None
        facility = _facility(nodes, self.source_file, len(self.facilities))
        seen = self.facilities.get(facility.id)
        self.facilities[facility.id] = facility if seen is None else _fill_gaps(seen, facility)
        return facility.id

    def _link_organization(self, practitioner: Practitioner, entity: _Element, role: _Role) -> None:
        """Register the organization a role is scoped by, and remember the link.

        The link rides `extensions` as the ORGANIZATION'S OWN id root rather
        than the canonical facility id: it is a fact the document stated, and it
        stays resolvable through the facility's provenance without inventing a
        field the model does not have.
        """
        if role.organization is None:
            return
        organization = _find(entity, role.organization)
        if organization is None:
            return
        self.add_facility((organization,))
        identifier = _find(organization, "v3:id")
        key = f"ccda:{role.organization.removeprefix('v3:')}"
        if (root := _attr(identifier, "root")) is not None:
            practitioner.extensions[key] = root
        # CDA II identity is the pair: extension is unique only inside root.
        # Keep the established root-valued key for compatibility and retain the
        # other half explicitly so a downstream resolver cannot join the actor
        # to a different organization under the same assigning authority.
        if (extension := _attr(identifier, "extension")) is not None:
            practitioner.extensions[f"{key}Extension"] = extension


#: What a role element can state about who took part. An informant is routinely
#: a coded relationship and nothing else ("spouse"), and a header participant an
#: id and a phone number, so a person element is not the only evidence there is.
_ACTOR_EVIDENCE = ("v3:id", "v3:code", "v3:telecom", "v3:addr", "v3:assignedAuthoringDevice")


def _names_an_actor(entity: _Element, role: _Role) -> bool:
    """Whether a role element states anything at all about who took part.

    Only a bare wrapper is nobody. CDA requires the wrapper even when it is
    empty — this repo's own exporter writes a lone ``<assignedAuthor/>`` to
    satisfy the schema — and a Practitioner built from one would be an actor no
    document ever named.
    """
    if _person_element(entity, role)[0] is not None:
        return True
    return any(_find(entity, path) is not None for path in _ACTOR_EVIDENCE)


def _describes_a_place(nodes: Sequence[_Element]) -> bool:
    """Whether organization elements state anything about a place."""
    return any(
        _first(nodes, path) is not None for path in ("v3:id", "v3:name", "v3:addr", "v3:telecom")
    )


def _participant_id(source_file: str, participation: str, index: int) -> str:
    """Stable practitioner id, derived rather than taken from the source root.

    One ``<id root>`` legitimately names two participations — the clinician who
    wrote the note and then signed it carries the same ``assignedEntity`` id
    twice — and those are two answers the record has to keep apart, so using the
    root verbatim would put two objects under one id. ``provenance.source_id``
    still names the root the document carried, which is what makes the parse
    attributable back to the element it came from.
    """
    return str(uuid5(NAMESPACE_URL, f"anastomosis:ccda:{source_file}:{participation}:{index}"))


def _facility_id(root: str | None, extension: str | None, source_file: str, index: int) -> str:
    """Stable facility id, on the one identifier rule (#412).

    A bare root DOES name the organization here, as it does for a patient and
    unlike an encounter — and the reason is worth stating, because the earlier
    docstring's bare claim that this "mirrors ``_patient_id``" is what made the
    rule look accidental. An organization is deliberately re-stated inside one
    document: the author's practice IS the custodian in most exports, and the
    same clinic reappears as a performer and a location. Those namings must
    fold, or one clinic becomes several and each holds a fragment of the
    address. An encounter is the opposite — a document lists many DISTINCT
    visits, so folding them on a shared vendor root would merge the lot.

    What makes folding safe here rather than a guess is that the ambiguous case
    is caught rather than absorbed: two genuinely different organizations
    reusing one root raise "organization identifier is reused with conflicting
    facility fields" (below) instead of silently blending into one facility.
    Identity folds; disagreement refuses. An encounter has no such guard, which
    is exactly why it takes the positional fallback instead.
    """
    return identity_from_ii(
        "organization",
        (root, extension) if root else None,
        f"{source_file}:organization:{index}",
        bare_root_names_the_instance=True,
    )


def _first(nodes: Sequence[_Element], path: str) -> _Element | None:
    """The first match for ``path`` across ``nodes``, in the order given."""
    for node in nodes:
        found = _find(node, path)
        if found is not None:
            return found
    return None


def _every(nodes: Sequence[_Element], path: str) -> list[_Element]:
    """Every match for ``path`` across ``nodes``, in the order given."""
    return [found for node in nodes for found in _findall(node, path)]


def _name_parts(name: _Element | None) -> tuple[str | None, str | None, str | None]:
    """``(given, family, display)`` from a CDA ``<name>``.

    ``display`` is filled only when the document did NOT split the name: a name
    written as element text still says who this is, and dropping it for having
    no parts would lose the answer the whole record is here to carry.
    """
    given = next((g for node in _findall(name, "v3:given") if (g := _text_content(node))), None)
    family = _text_content(_find(name, "v3:family"))
    if given is not None or family is not None:
        return given, family, None
    return None, None, _text_content(name)


def _name_residue(name: _Element | None) -> dict[str, Any]:
    """Name parts Practitioner has no field for.

    A credential is deliberately NOT derived from a suffix: "MD" is one and
    "Jr." is not, and guessing which would print a credential the document never
    claimed.
    """
    out: dict[str, Any] = {}
    for path, key in (("v3:prefix", "ccda:prefix"), ("v3:suffix", "ccda:suffix")):
        if (value := _text_content(_find(name, path))) is not None:
            out[key] = value
    return out


def _person_element(entity: _Element, role: _Role) -> tuple[_Element | None, str | None]:
    """The person a role plays, and the element name the document used for it."""
    for path in role.person:
        found = _find(entity, path)
        if found is not None:
            return found, path.removeprefix("v3:")
    return None, None


def _npi(entity: _Element) -> str | None:
    return next(
        (
            extension
            for node in _findall(entity, "v3:id")
            if _attr(node, "root") == OID_NPI and (extension := _attr(node, "extension"))
        ),
        None,
    )


def _role_source_id(entity: _Element) -> str | None:
    """The id root a role is identified BY, or ``None`` when it carries none.

    The NPI arc is a code system, not an instance identifier: every provider in
    the country shares that root, so crediting a parse to it would attribute one
    document's object by a value that says nothing about this document. A role
    with only an NPI gets no source id — recorded as unattributable rather than
    attributed to the wrong thing.
    """
    return next(
        (
            root
            for node in _findall(entity, "v3:id")
            if (root := _attr(node, "root")) and root != OID_NPI
        ),
        None,
    )


def _residual_ids(entity: _Element, consumed: str | None) -> list[dict[str, str]]:
    """Identifiers the mapping did not consume, so a second one is not dropped."""
    out: list[dict[str, str]] = []
    for node in _findall(entity, "v3:id"):
        root, extension = _attr(node, "root"), _attr(node, "extension")
        if root == OID_NPI:
            continue
        # ``source_id`` consumes only a root. When an II also has an extension,
        # that extension remains clinical provenance and must survive here;
        # otherwise every provider under one assigning authority becomes
        # indistinguishable after ingest.
        if root is not None and root == consumed and extension is None:
            continue
        if pair := {
            key: value for key, value in (("root", root), ("extension", extension)) if value
        }:
            out.append(pair)
    return out


def _contact_residue(node: _Element) -> dict[str, Any]:
    """A role's own address and telecom, which Practitioner has no slot for.

    Kept rather than dropped: the phone number on a note's author is how a
    receiving practice reaches the clinician who wrote it.
    """
    out: dict[str, Any] = {}
    if values := [value for n in _findall(node, "v3:telecom") if (value := _attr(n, "value"))]:
        out["ccda:telecom"] = values
    if addresses := [address.model_dump(exclude_none=True) for address in _addresses(node)]:
        out["ccda:addr"] = addresses
    return out


def _role_extensions(
    participation: _Element,
    participation_name: str,
    entity: _Element,
    role: _Role,
    entity_name: str | None,
) -> dict[str, Any]:
    """Everything a participation states that no Practitioner field holds.

    Two of these are load-bearing rather than incidental. ``ccda:participation``
    is the document's own word for what this actor did, and it is the difference
    between "who signed this chart" and "who told us about it".
    ``ccda:role`` is the CDA ROLE CLASS, and CDA draws a line with it that this
    record has to keep: an ``assignedEntity`` is somebody in a healthcare-
    provider role, a ``relatedEntity`` or ``associatedEntity`` is somebody in a
    personal relationship with the patient. It is recorded separately from
    ``ccda:entity`` — which names the person or device element played — because a
    role routinely names an actor with no person element at all (an emergency
    contact given as an id and a phone number), and a downstream reader that had
    to fall back on the participation's NAME would be guessing.
    """
    out: dict[str, Any] = {
        "ccda:participation": participation_name,
        "ccda:role": role.path.removeprefix("v3:"),
    }
    if entity_name is not None:
        out["ccda:entity"] = entity_name
    for node, attribute, key in (
        (participation, "typeCode", "ccda:typeCode"),
        (entity, "classCode", "ccda:classCode"),
    ):
        if (value := node.get(attribute)) is not None:
            out[key] = value
    for path, attribute, key in (
        ("v3:time", "value", "ccda:time"),
        ("v3:signatureCode", "code", "ccda:signatureCode"),
        ("v3:functionCode", "displayName", "ccda:functionCode"),
    ):
        if (value := _val_attr(participation, path, attribute)) is not None:
            out[key] = value
    code = _find(entity, "v3:code")
    if (display := _attr(code, "displayName") or _attr(code, "code")) is not None:
        out["ccda:code"] = display
    return out


def _person_practitioner(
    participation: _Element,
    participation_name: str,
    entity: _Element,
    role: _Role,
    source_file: str,
    index: int,
) -> Practitioner:
    """The human a participation names, carrying the role it named them in."""
    person, entity_name = _person_element(entity, role)
    name = _find(person, "v3:name")
    given, family, display = _name_parts(name)
    source_id = _role_source_id(entity)
    extensions = _role_extensions(participation, participation_name, entity, role, entity_name)
    extensions |= _name_residue(name)
    extensions |= _contact_residue(entity)
    if residual := _residual_ids(entity, source_id):
        extensions["ccda:id"] = residual
    return Practitioner(
        id=_participant_id(source_file, participation_name, index),
        given_name=given,
        family_name=family,
        display_name=display,
        npi=_npi(entity),
        extensions=extensions,
        provenance=_prov(source_file, source_id),
    )


def _device_practitioner(
    author: _Element, entity: _Element, source_file: str, index: int
) -> Practitioner:
    """The system that generated a document, kept apart from the people who write one.

    A human author and an authoring device are different answers to "who wrote
    this", and a record that cannot tell them apart is worse than one admitting
    it does not know: an automated summary attributed to a clinician is a
    statement nobody made. The device keeps ``ccda:entity`` naming the element
    CDA gave it, so the distinction survives on the object rather than in the
    reader's memory of where it came from.
    """
    device = _find(entity, "v3:assignedAuthoringDevice")
    model = _text_content(_find(device, "v3:manufacturerModelName"))
    software = _text_content(_find(device, "v3:softwareName"))
    source_id = _role_source_id(entity)
    extensions = _role_extensions(
        author, "author", entity, _ASSIGNED_AUTHOR, "assignedAuthoringDevice"
    )
    extensions |= _contact_residue(entity)
    for value, key in ((model, "ccda:manufacturerModelName"), (software, "ccda:softwareName")):
        if value is not None:
            extensions[key] = value
    if residual := _residual_ids(entity, source_id):
        extensions["ccda:id"] = residual
    return Practitioner(
        id=_participant_id(source_file, "author", index),
        display_name=software or model,
        extensions=extensions,
        provenance=_prov(source_file, source_id),
    )


def _facility_contacts(telecoms: list[_Element]) -> tuple[str | None, str | None, list[str]]:
    """``(phone, fax, everything else)`` from a set of ``<telecom>`` elements.

    First of each kind wins and the rest ride `extensions` rather than
    overwriting it, because a practice with two lines has two lines.
    """
    phone = fax = None
    residue: list[str] = []
    for node in telecoms:
        raw = _attr(node, "value")
        if raw is None:
            continue
        if raw.startswith("tel:") and phone is None:
            phone = format_phone(raw.removeprefix("tel:"))
        elif raw.startswith("fax:") and fax is None:
            fax = format_phone(raw.removeprefix("fax:"))
        else:
            residue.append(raw)
    return phone, fax, residue


def _facility(nodes: Sequence[_Element], source_file: str, index: int) -> Facility:
    """A practice location from the organization element(s) that describe it.

    Several elements rather than one because C-CDA splits a place across them:
    an ``encompassingEncounter``'s ``healthCareFacility`` carries the id, the
    ``location`` beneath it carries the name and address, and the
    ``serviceProviderOrganization`` beside it carries the organization's. Each
    field is taken from the first element that states it, in the order CDA
    nests them, so a document that fills either half is read the same way.
    """
    identifier = _first(nodes, "v3:id")
    root = _attr(identifier, "root")
    extension = _attr(identifier, "extension")
    address = _first(nodes, "v3:addr")
    lines = [
        line for node in _findall(address, "v3:streetAddressLine") if (line := _text_content(node))
    ]
    phone, fax, residue = _facility_contacts(_every(nodes, "v3:telecom"))
    extensions: dict[str, Any] = {}
    if extension is not None:
        extensions["ccda:id"] = [
            {key: value for key, value in (("root", root), ("extension", extension)) if value}
        ]
    if residue:
        extensions["ccda:telecom"] = residue
    if len(lines) > 2:
        extensions["ccda:streetAddressLine"] = lines[2:]
    return Facility(
        id=_facility_id(root, extension, source_file, index),
        name=_text_content(_first(nodes, "v3:name")),
        address_line1=lines[0] if lines else None,
        address_line2=lines[1] if len(lines) > 1 else None,
        city=_text_content(_find(address, "v3:city")),
        state=_text_content(_find(address, "v3:state")),
        postal_code=_text_content(_find(address, "v3:postalCode")),
        phone=phone,
        fax=fax,
        extensions=extensions,
        provenance=_prov(source_file, root),
    )


_FACILITY_SKIP = frozenset({"id", "provenance", "extensions"})


def _merge_facility_extensions(seen: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Merge lossless tails without choosing a winner for conflicting facts."""
    merged = dict(seen)
    for key, value in incoming.items():
        previous = merged.get(key)
        if previous is None:
            merged[key] = value
        elif previous == value:
            continue
        elif isinstance(previous, list) and isinstance(value, list):
            merged[key] = [*previous, *(item for item in value if item not in previous)]
        else:
            raise ValueError("C-CDA organization identifier has conflicting extension facts")
    return merged


def _mergeable(seen: Facility, incoming: Facility) -> Iterator[tuple[str, Any, Any]]:
    """Each field the merge may consider, with both namings' values beside it.

    Identity, provenance and the lossless tail are excluded: the first two are
    what established these are the same organization, and the third merges by
    its own rule rather than field by field.
    """
    for name in type(seen).model_fields:
        if name not in _FACILITY_SKIP:
            yield name, getattr(seen, name), getattr(incoming, name)


def _stated(seen: Facility, incoming: Facility) -> Iterator[tuple[str, Any, Any]]:
    """Fields both namings filled in — the only ones that can disagree."""
    return (
        (name, old, new)
        for name, old, new in _mergeable(seen, incoming)
        if old is not None and new is not None
    )


def _unstated(seen: Facility, incoming: Facility) -> Iterator[tuple[str, Any, Any]]:
    """Fields the first naming left empty and the second can fill."""
    return (
        (name, old, new)
        for name, old, new in _mergeable(seen, incoming)
        if old is None and new is not None
    )


def _fill_gaps(seen: Facility, incoming: Facility) -> Facility:
    """One organization named twice is one facility; the second naming fills gaps.

    Neither naming is more authoritative than the other — the author's header
    and the custodian's block describe the same practice — so nothing already
    stated is overwritten and only what was missing is filled. That is the same
    rule two halves of one encounter fold by, for the same reason: writing one
    over the other would silently pick a winner.
    """
    conflicts = [name for name, old, new in _stated(seen, incoming) if old != new]
    if conflicts:
        # Field names/counts only: neither organization value reaches the error.
        raise ValueError(
            "C-CDA organization identifier is reused with conflicting facility fields "
            f"({len(conflicts)} fields)"
        )
    update: dict[str, object] = {name: new for name, _old, new in _unstated(seen, incoming)}
    if incoming.extensions:
        update["extensions"] = _merge_facility_extensions(seen.extensions, incoming.extensions)
    return seen.model_copy(update=update) if update else seen


def _authors(root: _Element, actors: _Actors) -> None:
    """Every ``<author>`` in the document, header and note alike.

    Document-wide rather than header-only because the clinician who wrote a note
    is as much the answer to "who wrote this" as the one in the header — and for
    a reader of that note, it is the ONLY answer that helps. An
    ``assignedAuthoringDevice`` takes the other branch: it is a system, not a
    person, and the two must not arrive looking alike.
    """
    for author in root.iter(_q("author")):
        entity = _find(author, _ASSIGNED_AUTHOR.path)
        if entity is None:
            continue
        if _find(entity, "v3:assignedAuthoringDevice") is not None:
            actors.authors[author] = actors.add_device(author, entity)
            continue
        if (actor := actors.add_person(author, "author", entity, _ASSIGNED_AUTHOR)) is not None:
            actors.authors[author] = actor


def _named_participations(root: _Element, actors: _Actors) -> None:
    """The named actors besides the authors, each keeping the role it carried."""
    for name, path, roles in _PARTICIPATIONS:
        for participation in _findall(root, path):
            for role in roles:
                entity = _find(participation, role.path)
                if entity is not None:
                    actors.add_person(participation, name, entity, role)


def _custodians(root: _Element, actors: _Actors) -> None:
    """The organization holding the record.

    A custodian names no person at all — the element under it is an
    organization — so it becomes a facility and nothing pretends a human
    custodied the chart.
    """
    for custodian in _findall(root, "v3:custodian"):
        organization = _find(custodian, "v3:assignedCustodian/v3:representedCustodianOrganization")
        if organization is not None:
            actors.add_facility((organization,))


def _service_events(root: _Element, actors: _Actors, record: PatientRecord) -> None:
    """The episode of care the document is about, and who delivered it.

    The performer becomes a Practitioner — that is the answer to "who provided
    this care". The serviceEvent itself does NOT become an Encounter: its
    ``effectiveTime`` is the care-provision PERIOD, routinely years wide on a
    CCD, and charting its low bound as a date of service would invent a visit on
    a day nothing happened. So its own facts are preserved under
    ``patient.extensions`` instead, where they are recoverable without claiming
    to be a visit.
    """
    events: list[dict[str, Any]] = []
    for event in _findall(root, "v3:documentationOf/v3:serviceEvent"):
        for performer in _findall(event, "v3:performer"):
            entity = _find(performer, _ASSIGNED_ENTITY.path)
            if entity is not None:
                actors.add_person(performer, "performer", entity, _ASSIGNED_ENTITY)
        if facts := _service_event_facts(event):
            events.append(facts)
    if events:
        record.patient.extensions["ccda:serviceEvent"] = events


def _service_event_facts(event: _Element) -> dict[str, Any]:
    """What a service event states about itself, as plain data.

    Only the values the document actually carried: an absent key and a key
    holding ``None`` say different things, and this is the one copy of these
    facts the record keeps.
    """
    period = _find(event, "v3:effectiveTime")
    facts = {
        "id": _val_attr(event, "v3:id", "root"),
        "classCode": event.get("classCode"),
        "code": _val_attr(event, "v3:code", "code"),
        "display": _val_attr(event, "v3:code", "displayName"),
        "low": _attr(period, "value") or _val_attr(period, "v3:low", "value"),
        "high": _val_attr(period, "v3:high", "value"),
    }
    return {key: value for key, value in facts.items() if value is not None}


def _encompassing_encounters(root: _Element, patient_id: str, actors: _Actors) -> list[Encounter]:
    """The visit the document itself is about.

    A Progress Note routinely says which encounter it documents HERE and nowhere
    else — such a document has no Encounters section at all — so a reader that
    looks only at 46240-8 sees a note attached to no visit, which is the shape
    an unattributed chart arrives in.
    """
    source_file = actors.source_file
    out: list[Encounter] = []
    for index, enc in enumerate(_findall(root, "v3:componentOf/v3:encompassingEncounter")):
        code = _find(enc, "v3:code")
        root_id = _val_attr(enc, "v3:id", "root")
        _encounter_participants(enc, actors)
        out.append(
            Encounter(
                id=_encounter_id(first_rooted_id(enc), f"{source_file}:encompassing", index),
                patient_id=patient_id,
                date_of_service=_ts_date(enc, "v3:effectiveTime")
                or _ts_date(enc, "v3:effectiveTime/v3:low"),
                encounter_type=_attr(code, "displayName")
                or _text_content(_find(code, "v3:originalText")),
                provider_id=_responsible_party(enc, actors),
                facility_id=_encounter_facility(enc, actors),
                extensions={EXT_ENCOMPASSING: "encompassingEncounter"},
                provenance=_prov(source_file, root_id),
            )
        )
    return out


def _responsible_party(enc: _Element, actors: _Actors) -> str | None:
    """The clinician the visit was under — the encounter's own provider."""
    responsible = _find(enc, "v3:responsibleParty")
    party = _find(responsible, _ASSIGNED_ENTITY.path)
    if responsible is None or party is None:
        return None
    return actors.add_person(responsible, "responsibleParty", party, _ASSIGNED_ENTITY)


def _encounter_participants(enc: _Element, actors: _Actors) -> None:
    """Everyone else the visit names — the attender, the admitter, the referrer.

    Encounter has one provider slot and these are not it, so they are recorded
    as the participations they are rather than competing for that field.
    """
    for participant in _findall(enc, "v3:encounterParticipant"):
        entity = _find(participant, _ASSIGNED_ENTITY.path)
        if entity is not None:
            actors.add_person(participant, "encounterParticipant", entity, _ASSIGNED_ENTITY)


def _encounter_facility(enc: _Element, actors: _Actors) -> str | None:
    """Where the visit happened, from the encounter's own location."""
    facility = _find(enc, "v3:location/v3:healthCareFacility")
    if facility is None:
        return None
    nodes = [facility]
    nodes += [
        node
        for path in ("v3:location", "v3:serviceProviderOrganization")
        if (node := _find(facility, path)) is not None
    ]
    return actors.add_facility(tuple(nodes))


def _participations(
    root: _Element, patient_id: str, actors: _Actors, record: PatientRecord
) -> None:
    """Everyone and everywhere the document names, into the record.

    The C-CDA header is where a chart says who wrote it, who signed it, who
    holds it and which visit it belongs to, and none of it was being read: 2,103
    audited documents parsed without error and produced not one practitioner and
    not one facility between them. A note that arrives with no author has lost
    the answer to "who wrote this" while still looking complete, which is the
    worst shape a surviving record can take.
    """
    _authors(root, actors)
    _named_participations(root, actors)
    _custodians(root, actors)
    _service_events(root, actors, record)
    record.encounters += _encompassing_encounters(root, patient_id, actors)


# --- the unstructured body ---------------------------------------------------

#: The largest artifact this adapter carries out of one document, decoded.
#:
#: An Unstructured Document's body is a whole scanned chart, and a base64 one is
#: resident twice on the way in — as the source's characters and as the bytes it
#: decodes to. So the ceiling is DECLARED here rather than discovered at
#: whatever size this machine happens to die at, which is a limit nobody can
#: read, reproduce, or raise. 32 MiB admits the scans real exports carry (a few
#: hundred colour pages) and refuses the pathological one loudly, because the
#: alternative — carrying the first 32 MiB of a clinical document — is a chart
#: that looks complete and is not.
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024

#: What a body that declares no ``@mediaType`` is recorded as: "some bytes, no
#: idea what kind" — the same answer ``pf_tebra`` gives a row that states no
#: type. Never a guess at the real one: an artifact announced as a PDF it may
#: not be is worse than one that admits the document said nothing.
UNDECLARED_MEDIA_TYPE = "application/octet-stream"

#: Where the ``<nonXMLBody>``'s own declarations ride, so an attribute this
#: adapter has no field for is preserved rather than read and discarded.
EXT_NON_XML_BODY = "ccda:nonXMLBody"

#: What happened to this chart, in words, for whoever reads the record — the
#: export's loss narrative, the preserved-fields section, a physician opening
#: the bundle. Constant text: it describes the SHAPE of an Unstructured
#: Document, and nothing in it comes off the document it describes.
UNSTRUCTURED_BODY_NOTE = (
    "This document's entire clinical content is one embedded artifact — a scan, "
    "a fax, or another non-XML file — carried as an attachment beside the chart "
    "and named by this document's path. The source held no coded clinical data: "
    "no problems, medications, allergies, immunizations, results or notes were "
    "available to migrate as data, so the attachment is the chart."
)

_EMBEDDED = "embedded artifact"
_REFERENCED = "referenced artifact"
_DELIVERED = "delivered document"


class UnstructuredBodyMissingError(SourceDataError):
    """An Unstructured Document's whole content is a file the export lacks.

    Nothing else is on that chart, so carrying the document anyway would hand
    the operator a patient with a name and no record — the silent total loss
    this adapter exists to make impossible. The run refuses instead.
    """


class UnstructuredBodyTooLargeError(SourceDataError):
    """An artifact over :data:`MAX_ARTIFACT_BYTES`.

    Refused whole rather than truncated: half a scanned discharge summary is
    not a smaller version of that summary, it is a document whose second half
    silently does not exist.
    """


def _within_ceiling(size: int, what: str) -> None:
    """Refuse an artifact this adapter will not carry, in bytes and limits only."""
    if size > MAX_ARTIFACT_BYTES:
        raise UnstructuredBodyTooLargeError(
            f"a C-CDA Unstructured Document's {what} is {size} bytes, over this "
            f"adapter's declared {MAX_ARTIFACT_BYTES}-byte ceiling; refusing to carry it "
            "rather than truncating a clinical document to fit. Nothing else is on that "
            "chart, so the run stops instead of delivering the patient without it."
        )


def _beside_document(reference: str, document: Path) -> Path | None:
    """The file ``reference`` names beside ``document``, or ``None`` for none.

    The value is a third party's word about the filesystem, so it is resolved
    and then checked back against the directory it was resolved in: a ``../`` in
    someone else's document must not make this adapter read a file the operator
    never pointed at.

    A value that is not a relative filename at all — an absolute path, a URL, a
    ``#`` fragment — resolves to nothing beside the document and fails the same
    check, which is the right answer for all three: this parser reads the export
    the operator pointed at and fetches nothing.

    Returns rather than raises because two constructs resolve references this
    way and a missing file means a different thing to each: a ``nonXMLBody``'s
    is the whole chart, a delivered artifact's is one document on it. Each
    caller says its own sentence.
    """
    directory = document.parent.resolve()
    resolved = (directory / reference).resolve()
    if not resolved.is_relative_to(directory) or not resolved.is_file():
        return None
    return resolved


def _resolved_reference(reference: str, document: Path) -> Path:
    """The file a ``nonXMLBody`` reference names, beside the document itself.

    PHI: the refusal names no filename. A C-CDA export names its files after the
    patient, so the reference value is a patient-derived string and the message
    says the SHAPE of what is missing instead.
    """
    resolved = _beside_document(reference, document)
    if resolved is None:
        raise UnstructuredBodyMissingError(
            "a C-CDA Unstructured Document's entire content is a referenced file that is "
            "not beside it in the export, so this patient's chart did not resolve to "
            "anything. The run refuses rather than migrating a patient with correct "
            "demographics and nothing on their chart. The file is not named here because "
            "a C-CDA export names its files after the patient: look for the document whose "
            "<nonXMLBody><text><reference> points at a file the export does not hold."
        )
    return resolved


def _embedded_bytes(text: _Element) -> bytes:
    """The artifact a ``nonXMLBody`` carries inside itself.

    ``representation="B64"`` is what a scan arrives as; anything else means the
    element's own characters ARE the content (CDA's default for ED), which is
    how a plain-text body comes across. Base64 that does not decode raises —
    the adapter names the document by position and refuses the run — because
    the alternative is a patient whose chart quietly became zero bytes.

    The ceiling is checked against the encoded length BEFORE decoding: four
    base64 characters are three bytes, so the estimate is exact to within the
    padding, and a body that cannot be carried must not be materialized to find
    that out.
    """
    # Joined verbatim rather than through `_text_content`: that helper
    # collapses whitespace, which is right for narrative and destroys a
    # plain-text body's own line breaks.
    raw = "".join(t if isinstance(t, str) else t.decode() for t in text.itertext())
    if (text.get("representation") or "").upper() != "B64":
        plain = raw.encode("utf-8")
        _within_ceiling(len(plain), _EMBEDDED)
        return plain
    packed = "".join(raw.split())
    _within_ceiling(len(packed) // 4 * 3, _EMBEDDED)
    content = base64.b64decode(packed, validate=True)
    _within_ceiling(len(content), _EMBEDDED)
    return content


def _non_xml_body_extensions(body: _Element, text: _Element) -> dict[str, Any]:
    """What the ``<nonXMLBody>`` declared that no ``DocumentArtifact`` field holds.

    ``@mediaType`` is the one attribute consumed into a field (``mime_type``)
    and so is not repeated here; a body that declared none is recorded as
    having declared none, because "the document said octet-stream" and "the
    document said nothing" are different facts about a chart.
    """
    declared: dict[str, Any] = {
        "note": UNSTRUCTURED_BODY_NOTE,
        "representation": text.get("representation"),
        "reference": _val_attr(text, "v3:reference", "value"),
        "languageCode": _val_attr(body, "v3:languageCode", "code"),
        "confidentialityCode": _val_attr(body, "v3:confidentialityCode", "code"),
        "classCode": body.get("classCode"),
    }
    kept = {key: value for key, value in declared.items() if value is not None}
    if text.get("mediaType") is None:
        kept["mediaType_declared"] = False
    return kept


def _artifact_content(
    text: _Element, document: Path, extensions: dict[str, Any], media_type: str | None, index: int
) -> tuple[str, str]:
    """``(delivered path, sha256)`` for the body, filling ``extensions`` as needed.

    A referenced body already exists as a file in the export, and keeps the name
    the export gave it — exactly like a ``pf_tebra`` attachment, so the pipeline
    copies it with no new machinery. An EMBEDDED body has no file anywhere, so
    its bytes travel on the artifact (:data:`EXT_INLINE_CONTENT`) and delivery
    writes them under a name derived from the artifact's own id, which is
    deterministic and carries nothing off the patient.
    """
    if (reference := _val_attr(text, "v3:reference", "value")) is not None:
        digest, size = hash_and_size(_resolved_reference(reference, document))
        _within_ceiling(size, _REFERENCED)
        return reference, digest
    content = _embedded_bytes(text)
    extensions[EXT_INLINE_CONTENT] = base64.b64encode(content).decode("ascii")
    return f"{_artifact_name(document, index)}{media_type_suffix(media_type)}", _digest(content)


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _artifact_name(document: Path, index: int) -> str:
    """The artifact's id, and so the stem of an embedded one's delivered filename.

    Derived the way every other id this parser mints is (:func:`_participant_id`,
    :func:`_facility_id`): a uuid5 over the source file, the construct and the
    position, so two runs over the same export deliver the same name and the
    name itself carries nothing readable off the patient. The position is in it
    for the same reason it is in a participant's: a document stating the
    construct twice is two artifacts, and one id would deliver the second over
    the first.
    """
    return str(uuid5(NAMESPACE_URL, f"anastomosis:ccda:{document.name}:nonXMLBody:{index}"))


def _unstructured_documents(
    root: _Element, document: Path, patient_id: str, doc_meta: dict[str, Any]
) -> list[DocumentArtifact]:
    """The chart of an Unstructured Document, as the artifact it is.

    A C-CDA Unstructured Document carries its whole clinical content as an
    embedded or referenced file under ``<nonXMLBody>`` instead of coded
    sections. The section walk cannot see it, so such a document parsed cleanly
    and produced a patient with an empty chart, and nothing in the run said so:
    1,024 of them in a 6,144-document ledger run, every one reported a success.

    Carried rather than refused. Refusing loses the patient outright; carrying
    preserves exactly what the source had. A record holding demographics plus
    one attached artifact is not a degraded chart — for a practice whose records
    were scanned, it is a faithful one.

    Returns a list, so the caller needs no branch and a document that states the
    construct more than once (CDA allows one body, an export is not obliged to
    obey) carries every one rather than the first.
    """
    return [
        artifact
        for index, body in enumerate(_findall(root, "v3:component/v3:nonXMLBody"))
        if (artifact := _artifact(body, index, document, patient_id, doc_meta)) is not None
    ]


def _artifact(
    body: _Element, index: int, document: Path, patient_id: str, doc_meta: dict[str, Any]
) -> DocumentArtifact | None:
    """One ``<nonXMLBody>`` as a :class:`DocumentArtifact`, or ``None``.

    ``None`` for a body stating no ``<text>`` at all, which is the honest answer
    for a construct that offered nothing — and the answer the ledger reads back
    as ``source_empty`` rather than as a loss.
    """
    text = _find(body, "v3:text")
    if text is None:
        return None
    media_type = text.get("mediaType")
    # Namespaced, so what the body declared stays attributable to the construct
    # it came off rather than becoming loose keys on an artifact.
    extensions: dict[str, Any] = {EXT_NON_XML_BODY: _non_xml_body_extensions(body, text)}
    path, digest = _artifact_content(text, document, extensions, media_type, index)
    return DocumentArtifact(
        id=_artifact_name(document, index),
        patient_id=patient_id,
        path=path,
        sha256=digest,
        # As DECLARED. A type this adapter normalized, corrected or guessed would
        # be this tool telling a receiving system what a scan is, which is a
        # claim only the document holding it can make.
        mime_type=media_type or UNDECLARED_MEDIA_TYPE,
        title=doc_meta.get("ccda:title"),
        extensions=extensions,
        # The document's own id: an unstructured body carries no <id> of its own
        # (CDA gives NonXMLBody none), and the document IS the artifact.
        provenance=_prov(document.name, doc_meta.get("ccda:documentId")),
    )


# --- documents this toolkit delivered beside a C-CDA -------------------------


class DeliveredArtifactError(SourceDataError):
    """A document this toolkit's own C-CDA export named is missing or is not it.

    The export writes each of a record's source documents into the delivery
    directory and names it from the CCD with its media type and its SHA-256
    (#373). A stamped entry whose file is gone, or whose bytes hash to
    something else, means the directory was split up, truncated or edited
    between the export and here — so the chart in front of the operator refers
    to a document nobody can produce. Refused rather than carried: a chart
    naming a scan that is not the scan is worse than one that stops.
    """


def _delivered_artifacts(
    section: _Element, document: Path, patient_id: str
) -> list[DocumentArtifact]:
    """The documents this toolkit delivered beside ``document``, as artifacts.

    The inverse of ``deliver.ccda_export.builder._delivered_documents``. Only
    an ``<observationMedia>`` stamped with :data:`ARTIFACT_TEMPLATE_ROOT` is
    read this way: a third party's multimedia carries no such stamp and stays
    what it has always been — narrative and entries preserved, nothing claimed
    about it.

    Reading it back is what makes the C-CDA deliverable a round trip rather
    than a one-way door. Before #373 a re-ingest of the delivered directory
    produced the patient with an empty chart, which is precisely the reading
    that let the loss ship.
    """
    return [
        _delivered_artifact(media, document, patient_id)
        for entry in _entries(section)
        if (media := _artifact_media(entry)) is not None
    ]


def _delivered_artifact(media: _Element, document: Path, patient_id: str) -> DocumentArtifact:
    """One stamped ``<observationMedia>`` as the artifact it names.

    Every field this reads is one the exporter wrote, so anything missing is a
    document that was edited after it was written, and that is a refusal rather
    than a best guess: an artifact recovered under a made-up id would be filed
    as a different document from the one the chart means.
    """
    value = _find(media, "v3:value")
    artifact_id = _val_attr(media, "v3:id", "root")
    reference = _val_attr(value, "v3:reference", "value")
    if value is None or artifact_id is None or reference is None:
        raise DeliveredArtifactError(
            "a document entry written by this toolkit's own C-CDA export is missing its "
            "id, its media value or the reference naming the file; refusing to guess which "
            "document a chart means"
        )
    resolved = _beside_document(reference, document)
    if resolved is None:
        raise DeliveredArtifactError(
            "a C-CDA delivered by this toolkit names a document that is not beside it. The "
            "delivery directory holds the CCD and its documents together; refusing to carry "
            "a chart whose documents did not travel with it. The file is not named here "
            "because a delivery is read by whoever it was handed to"
        )
    digest, size = hash_and_size(resolved)
    _within_ceiling(size, _DELIVERED)
    _verify_integrity(value, digest)
    return DocumentArtifact(
        id=artifact_id,
        patient_id=patient_id,
        path=reference,
        sha256=digest,
        # As DECLARED, exactly as for a nonXMLBody: a type this adapter
        # normalized would be this tool telling a receiving system what a scan
        # is, which is a claim only the document holding it can make.
        mime_type=value.get("mediaType") or UNDECLARED_MEDIA_TYPE,
        # Deliberately not the document's <title>: that titles the CCD, not the
        # scan inside it. The source's own document title rides the export's
        # loss narrative with the rest of the fields no CDA slot holds.
        title=None,
        provenance=_prov(document.name, artifact_id),
    )


def _verify_integrity(value: _Element, digest: str) -> None:
    """Check the delivered file against the digest the ED witnesses.

    The HL7 v3 ED datatype carries ``@integrityCheck`` (the digest, base64) and
    ``@integrityCheckAlgorithm``; the export writes SHA-256 into both. A file
    that hashes to something else is not a slightly different file, it is a
    different document, so it stops the run here rather than being carried as
    the one the chart names.

    An ED declaring no check at all is not refused — the datatype allows it,
    and a document without one is simply unwitnessed. What is refused is a
    check this reader cannot evaluate: an algorithm it does not implement would
    otherwise pass silently by not being compared.
    """
    declared = value.get("integrityCheck")
    if declared is None:
        return
    algorithm = value.get("integrityCheckAlgorithm") or ARTIFACT_INTEGRITY_ALGORITHM
    if algorithm != ARTIFACT_INTEGRITY_ALGORITHM:
        raise DeliveredArtifactError(
            f"a delivered document witnesses its bytes with {algorithm!r}, which this "
            f"adapter does not compute; refusing to carry a document it cannot check "
            f"rather than reporting an unchecked one as verified"
        )
    try:
        witnessed = base64.b64decode(declared, validate=True).hex()
    except (binascii.Error, ValueError) as exc:
        raise DeliveredArtifactError(
            f"a delivered document's integrity check did not decode ({type(exc).__name__}); "
            "refusing to carry a document whose witness is unreadable"
        ) from None
    if witnessed != digest:
        raise DeliveredArtifactError(
            "a delivered document's bytes do not match the SHA-256 the C-CDA naming it "
            "witnesses; the delivery was edited or truncated after it was written, and "
            "the chart in front of you refers to a document nobody can produce"
        )


# --- top-level assembly ------------------------------------------------------


class _HardenedXMLKwargs(TypedDict):
    """The keyword shape of :data:`_HARDENED_XML_KWARGS` — a ``TypedDict`` so
    unpacking it with ``**`` at a call site still type-checks under strict
    mypy, the way a plain ``dict[str, bool]`` cannot against ``XMLParser`` or
    ``iterparse``'s own keyword-only signatures."""

    resolve_entities: bool
    no_network: bool
    load_dtd: bool
    huge_tree: bool


# Hardened XML parser: third-party clinical documents must never resolve
# external entities (XXE), fetch external DTDs over the network (SSRF), or
# expand into unbounded trees (billion-laughs / quadratic blowup). These
# flags are the OWASP-recommended posture for any XML ingest the
# application does not author itself.
#
# Kept as a dict, not only as the ``_PARSER`` built from it, because
# ``etree.iterparse`` — the cheap "does this look like CDA" sniff in
# ``sources/ccda/__init__.py`` (#384 round two) — has no ``parser=`` argument
# to hand a built ``XMLParser`` to; it takes these same flags directly. A
# document sniffed under weaker settings than the one it is then read under
# would defeat the point of hardening the read at all.
_HARDENED_XML_KWARGS: _HardenedXMLKwargs = {
    "resolve_entities": False,
    "no_network": True,
    "load_dtd": False,
    "huge_tree": False,
}
_PARSER = etree.XMLParser(**_HARDENED_XML_KWARGS)


def _inline_narrative_references(root: _Element) -> None:
    """Give every ``<reference value="#id"/>`` the narrative text it points at.

    Linking a coded entry to its human-readable narrative by reference is THE
    standard C-CDA mechanism, and a ``<reference>`` element carries no text of
    its own. So a problem whose ``<originalText>`` is a reference came through
    unnamed, and a Note Activity whose ``<text>`` is a reference came through
    empty — while the words sat a few elements away in the same document.

    Resolved once per document by filling in each reference's own text, so every
    reader downstream (originalText, entry text, note bodies) sees the words
    without knowing references exist. A reference naming an id the document does
    not define is left alone rather than guessed at.
    """
    targets = {node.get("ID"): node for node in root.iter() if node.get("ID")}
    if not targets:
        return
    for reference in root.iter(_q("reference")):
        value = (reference.get("value") or "").strip()
        if not value.startswith("#"):
            continue
        # A reference carrying its own fallback text keeps it; an id the document
        # does not define resolves to None, which leaves the reference as it was.
        if not reference.text:
            reference.text = _text_content(targets.get(value[1:]))


# Categories a visit may claim. A measurement is taken AT a moment, so a visit
# on its date says something about where it belongs. Social history does not
# work that way — smoking status is a standing fact about the patient, not a
# reading from one afternoon — so it stays record-level, which is also where
# the packs read it from.
_VISIT_MEASUREMENTS = frozenset({ObservationCategory.VITAL_SIGNS, ObservationCategory.LABORATORY})


def _visit_candidates(record: PatientRecord) -> dict[date, list[Encounter]]:
    """The visits a measurement may be charted at, indexed by calendar day.

    Only an encounter carrying BOTH a date and a type is a candidate: a
    note-only encounter documents a visit, it is not one, and counting it would
    make every documented visit ambiguous with itself.

    A ``componentOf/encompassingEncounter`` is the document's FRAME rather than
    an entry inside it, so it supplies a candidate only for a day no entry
    claims. When a document states the visit both ways — in its header and again
    in the Encounters section — those are one visit stated twice, and admitting
    both would make the day ambiguous and strand the measurements taken on it.
    """
    entries: dict[date, list[Encounter]] = {}
    frames: dict[date, list[Encounter]] = {}
    for encounter in record.encounters:
        if encounter.date_of_service is None or encounter.encounter_type is None:
            continue
        index = frames if EXT_ENCOMPASSING in encounter.extensions else entries
        index.setdefault(encounter.date_of_service, []).append(encounter)
    for day, framed in frames.items():
        entries.setdefault(day, framed)
    return entries


def _link_measurements_to_encounters(record: PatientRecord) -> None:
    """Attach a measurement to the visit it was taken at, when exactly one visit
    claims that calendar day.

    C-CDA does not require a structural link from a Vital Signs or Results
    observation back to an Encounter activity, and the documents we see carry
    none — no ``componentOf/encompassingEncounter``, no ``entryRelationship``
    naming one — so the document's own timestamps are the only evidence there
    is. Every other source adapter fills ``encounter_id`` in; this one left it
    empty, so every observation on a chart grouped under the patient-level
    ``None`` key and both SOAP packs, which index strictly by encounter id,
    rendered no vitals at all. The QA check written to catch precisely that
    reads the same empty index, so it passed on an empty loop while the values
    were missing from the page — a silent loss with its own guard blinded by
    the same root cause.

    ONE encounter on the observation's date is evidence; two are not, and the
    measurement then stays record-level rather than being charted at a visit it
    may not belong to. Note-only encounters are not candidates: a Note Activity
    documents a visit, it is not one, and counting it would make every
    documented visit ambiguous with itself. An ``encounter_id`` a source already
    stated is never overwritten.

    Runs after the id fold, so a visit described twice — once in Encounters,
    again as the note documenting it — is one candidate here rather than two.
    """
    by_date = _visit_candidates(record)
    for observation in record.observations:
        if observation.encounter_id is not None or observation.effective_at is None:
            continue
        if observation.category not in _VISIT_MEASUREMENTS:
            continue
        # Both sides are the calendar date the document wrote: `parse_dt` keeps
        # the source's own offset and `parse_date` is that same instant's
        # `.date()`, so neither has been shifted across midnight on the way here.
        same_day = by_date.get(observation.effective_at.date(), [])
        if len(same_day) == 1:
            observation.encounter_id = same_day[0].id


# --- zero-sentinel timestamps -------------------------------------------

# Every TS shape a _ts/_ts_date call in this module reads, named the way
# _count_zero_sentinels walks it: a bare tag read straight off an entry
# (birthTime, effectiveTime, author/time), or a two-level parent-then-child
# for a TS read off an already-resolved parent — an IVL_TS's own low/high,
# where the medications reader resolves the effectiveTime node once and then
# reads its low and high children off that node, rather than re-stating the
# whole path each time. The shape read is still effectiveTime/low, regardless
# of which already-found node the call started from.
# test_every_timestamp_path_the_parser_reads_is_named_in_TS_PATHS is the
# anti-drift guard: a _ts/_ts_date call reading a path not named here (or a
# name here nothing reads any more) fails it, so the two cannot separate
# silently.
TS_PATHS = ("birthTime", "effectiveTime", "effectiveTime/low", "effectiveTime/high", "author/time")

EXT_TS_NO_INSTANT = "ccda:timestamp_named_no_instant"


def _count_zero_sentinels(root: _Element) -> dict[str, int]:
    """How many TS elements the document itself names as a run of zeros, by shape.

    Walked over the whole document independently of any `_ts`/`_ts_date` call
    site — this is what the DOCUMENT states, not what one field extractor
    happened to read, so the count does not depend on which caller's local
    variable reached the node first. A node carrying `nullFlavor` is skipped:
    that is an explicit "absent", not a zero run, and already reads as `None`
    with no help from `is_zero_sentinel`.
    """
    counts: dict[str, int] = {}
    for shape in TS_PATHS:
        xpath = ".//v3:" + "/v3:".join(shape.split("/"))
        n = sum(
            1
            for node in _findall(root, xpath)
            if node.get("nullFlavor") is None and is_zero_sentinel(node.get("value"))
        )
        if n:
            counts[shape] = n
    return counts


def _record_zero_sentinels(record: PatientRecord, root: _Element) -> None:
    """Credit a run-of-zeros TS the parser read as absent, on the record itself.

    `_ts`/`_ts_date` treat a "0" (of any length) as absent rather than raising
    `parse_dt`'s own ValueError, so the medication (or condition, encounter,
    ...) it belongs to survives with no start instead of aborting the whole
    document. That is a real loss — a start date the source named nothing
    usable for — and losslessness means it rides the record rather than
    vanishing at the parse boundary. Sets the key only when the count is
    non-empty, so an ordinary document carries no trace of a check that found
    nothing.
    """
    if counts := _count_zero_sentinels(root):
        record.patient.extensions[EXT_TS_NO_INSTANT] = counts


def parse_document(path: Path) -> PatientRecord:
    """Parse one C-CDA / CCD XML file into a :class:`PatientRecord`.

    Raises :exc:`ValueError` if the file is not a CDA ``ClinicalDocument`` —
    a loud failure, per the source-adapter contract.
    """
    tree = etree.parse(str(path), _PARSER)
    root = tree.getroot()
    if etree.QName(root).localname != "ClinicalDocument" or root.tag != _q("ClinicalDocument"):
        raise ValueError(f"{path.name}: not a C-CDA ClinicalDocument (root <{root.tag}>)")

    # Verbatim FIRST, hydration second. `_inline_narrative_references` fills a
    # <reference> element's own text in place, so an entry serialised after it
    # runs carries narrative this document does not spell at that position —
    # which made the "verbatim" copy a copy of the parser's tree rather than of
    # the file, and broke the byte-exact question the ledger asks of it. The
    # copy is taken from the document as parsed, and nothing else touches it.
    entries_by_section = _capture_entries(root)

    _inline_narrative_references(root)

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
    actors = _Actors(source_file=source_file)
    _participations(root, pid, actors, record)
    # Before the section walk, and unconditionally: an Unstructured Document has
    # no sections to walk, which is exactly why one used to leave here with a
    # patient and an empty chart.
    record.documents += _unstructured_documents(root, path, pid, doc_meta)

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
            record.encounters += _encounters(section, pid, actors)
        elif loinc == LOINC_NOTES:
            record.encounters += _note_encounters(section, pid, actors)
            # The documents a C-CDA this toolkit delivered carries beside it.
            # Read in the Notes section because that is where the export hangs
            # them: a scanned chart IS the note for the visit it documents, and
            # a second 34109-9 section would tell a reader the document has two.
            record.documents += _delivered_artifacts(section, path, pid)
        # Losslessness: the narrative and the entries are captured for every
        # section, not only the unparsed ones. The structural parsers above `continue` past an entry
        # whose shape they do not support, so a known section can yield nothing
        # while its <text> still holds the clinical statement; the duplication
        # for a fully-parsed section is the cheap side of that trade. Our own
        # loss ledger is the one exception — captured entry-by-entry so a repeat
        # export cannot nest it inside the next one.
        if _is_own_loss_narrative(section, loinc):
            _capture_loss_narrative(record, section)
        else:
            _capture_narrative(record, section, loinc)
            _store_entries(record.patient.extensions, entries_by_section, section, loinc)

    record.practitioners = actors.practitioners
    record.facilities = list(actors.facilities.values())
    _record_zero_sentinels(record, root)
    record.encounters = fold_encounters_sharing_an_id(record.encounters)
    _link_measurements_to_encounters(record)
    return record
