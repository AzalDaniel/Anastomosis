"""C-CDA R2.1 / CCD XML → canonical PatientRecord.

The lossless rule, applied to a CDA document: every section the adapter knows
how to take apart becomes discrete canonical models, and **every section** —
structurally parsed or not — has its title and normalized narrative captured
into ``patient.extensions["ccda:section:<loinc>"]`` so nothing on the chart is
ever silently dropped (a known section whose entries the parser cannot take
apart would otherwise yield nothing at all). A document repeating a section
code — split Problems (Active)/(Resolved) is ordinary C-CDA — keeps each
occurrence at its own key (``…:<loinc>#2``, ``#3``, … in document order), so a
second section can never overwrite the first. Document-level metadata rides
``patient.extensions`` too.

One section is captured differently: a 51899-3 section carrying this repo's own
export stamp is the loss ledger ``deliver/ccda_export`` wrote, and its entries
land discretely under ``patient.extensions["ccda:prior_loss_narrative"]`` so a
re-export can carry them forward deduplicated. Captured as an ordinary narrative
blob instead, generation N of an export → ingest → export loop swallowed
generation N-1's whole ledger as a single line and the document grew without
bound. An UNSTAMPED 51899-3 section is a third party's and keeps round-tripping
as ordinary foreign narrative.

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

from anastomosis.core.ccda_codes import (
    EXT_PRIOR_LOSS_NARRATIVE,
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
    TPL_SEVERITY,
    V3,
    XSI,
)
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
    """Stable canonical patient id, mirroring :func:`_encounter_id` (re-parsing
    the same document must yield the same id — nothing downstream may see it
    change). A clean source GUID is used verbatim; any other source
    identifier is hashed into a deterministic uuid5; absent any id, the file
    name is.
    """
    for ident in _identifiers(patient_role):
        if ident.kind == IdentifierKind.SOURCE_GUID:
            if _GUID_RE.match(ident.value):
                return ident.value
            return str(uuid5(NAMESPACE_URL, f"anastomosis:ccda:patient:{ident.value}"))
    return str(uuid5(NAMESPACE_URL, f"anastomosis:ccda:{source_file}:patient"))


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


def _capture_narrative(record: PatientRecord, section: _Element, loinc: str | None) -> None:
    """Preserve one section's title and narrative under ``ccda:section:<loinc>``.

    Runs for EVERY section, structurally parsed or not: a structural parser
    skips an entry whose shape it does not support, and the narrative is then
    the only copy of what that entry said. A section with neither a title nor
    narrative text adds no key (sentinel discipline — absent stays absent).
    Mutating the model's extensions dict in place persists it on the patient (it
    is the validated dict object, not a fresh copy).

    Documents legitimately repeat a section code (Problems (Active) and Problems
    (Resolved) are both 11450-4) and may carry several code-less sections, so a
    key already taken is suffixed ``#2``, ``#3``, … in DOCUMENT order rather than
    overwritten — one narrative must never silently replace another. The first
    occurrence keeps the bare key, so a document with one section per code reads
    exactly as it always has.
    """
    title = _text_content(_find(section, "v3:title"))
    text = _text_content(_find(section, "v3:text"))
    if title is None and text is None:
        return
    key = f"ccda:section:{loinc}" if loinc else "ccda:section:unknown"
    extensions = record.patient.extensions
    if key in extensions:
        occurrence = 2
        while f"{key}#{occurrence}" in extensions:
            occurrence += 1
        key = f"{key}#{occurrence}"
    extensions[key] = {"title": title, "text": text}


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
    """Every discrete piece of a section's narrative, in document order.

    One entry per child element — a ``<paragraph>`` as our exporter writes them,
    but equally a ``<table>`` or a ``<list>`` some other system rendered — plus
    any loose text between them.

    Paragraphs alone used to be collected, with the whole ``<text>`` kept only
    when there were NONE. A section holding one paragraph and one table
    therefore lost the table: it reached neither the carried-forward ledger nor
    the ordinary foreign-narrative key, and did not survive re-export. This is
    the section where fields with no structured slot are parked so nothing is
    dropped, so dropping part of it defeated the mechanism at the one point it
    exists to hold.
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
    """Preserve OUR OWN loss ledger under ``ccda:prior_loss_narrative``.

    Captured as discrete entries (one per ``<paragraph>``, as the exporter wrote
    them) rather than as one ``ccda:section:51899-3`` narrative blob, so the next
    export can carry the entries forward deduplicated instead of swallowing the
    whole block as a single ever-growing line. A ledger whose narrative is not
    per-paragraph (hand-edited, or a shape from before this contract) is kept
    whole as one entry — unreadable structure must not cost the content.

    A document merged from two exports can carry two stamped ledgers; their
    entries CONCATENATE into the one key (highest generation wins) so the
    exporter dedupes a single carry-forward ledger and neither one is
    overwritten.
    """
    entries = _narrative_entries(_find(section, "v3:text"))
    if not entries:
        return
    generation = _loss_generation(section)
    prior = record.patient.extensions.get(EXT_PRIOR_LOSS_NARRATIVE)
    if isinstance(prior, dict):
        prior["entries"] = [*prior["entries"], *entries]
        prior_generation = prior["generation"]
        prior["generation"] = (
            generation if prior_generation is None else max(prior_generation, generation or 0)
        )
        return
    record.patient.extensions[EXT_PRIOR_LOSS_NARRATIVE] = {
        "generation": generation,
        "entries": entries,
    }


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
            elif template == TPL_SEVERITY:
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
    low, high = _find(value, "v3:low"), _find(value, "v3:high")  # IVL_PQ
    bounds = [_attr(end, "value") for end in (low, high)]
    if any(bounds):
        lo, hi = bounds
        return ("-".join(part for part in bounds if part) if lo and hi else (lo or hi)), (
            _attr(low, "unit") or _attr(high, "unit")
        )
    # ST / ED and anything else that carries its result as element text.
    return _text_content(value), None


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
            reading, unit = _observation_value(_find(component, "v3:value"))
            out.append(
                Observation(
                    patient_id=patient_id,
                    category=category,
                    code=_attr(code, "code"),
                    display=_attr(code, "displayName"),
                    value=reading,
                    unit=unit,
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


def parse_document(path: Path) -> PatientRecord:
    """Parse one C-CDA / CCD XML file into a :class:`PatientRecord`.

    Raises :exc:`ValueError` if the file is not a CDA ``ClinicalDocument`` —
    a loud failure, per the source-adapter contract.
    """
    tree = etree.parse(str(path), _PARSER)
    root = tree.getroot()
    if etree.QName(root).localname != "ClinicalDocument" or root.tag != _q("ClinicalDocument"):
        raise ValueError(f"{path.name}: not a C-CDA ClinicalDocument (root <{root.tag}>)")

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
        # Losslessness: the narrative is captured for every section, not only the
        # unparsed ones. The structural parsers above `continue` past an entry
        # whose shape they do not support, so a known section can yield nothing
        # while its <text> still holds the clinical statement; the duplication
        # for a fully-parsed section is the cheap side of that trade. Our own
        # loss ledger is the one exception — captured entry-by-entry so a repeat
        # export cannot nest it inside the next one.
        if _is_own_loss_narrative(section, loinc):
            _capture_loss_narrative(record, section)
        else:
            _capture_narrative(record, section, loinc)

    return record
