"""US Core R4 resources → canonical :class:`PatientRecord` objects.

This maps *standard* FHIR R4 / US Core resources — the shape a certified EHR's
Bulk-Data ``$export`` or ``Patient/$everything`` produces — into the canonical
model. It is deliberately NOT the inverse of this project's own exporter
(:func:`anastomosis.core.fhir.ingest.from_bundle`), which reads the
``urn:anastomosis:*`` round-trip extensions; arbitrary vendors do not emit
those. Everything here reads the public US Core codings (LOINC, ICD-10-CM,
SNOMED CT, RxNorm, CVX) and US Core extensions.

Design rules:

* **Lossless.** A resource field the mapper does not lift into a typed slot is
  preserved verbatim under a ``fhir_r4:`` namespaced key in the owning object's
  ``extensions``; whole resource types with no canonical home (e.g. Procedure)
  are preserved under the record's ``extensions``. Nothing from the source is
  silently dropped.
* **Deterministic.** No clocks, no randomness, no set iteration — output order
  follows the input order, so the same bundle always yields byte-identical
  records.
* **Defensive reads.** Vendor exports vary; every accessor tolerates a missing
  or differently-shaped field rather than raising, so one malformed resource
  cannot abort a whole patient. (The adapter raises only when a bundle has no
  Patient at all — the loud, structural failure.)
"""

from __future__ import annotations

import base64
from collections import defaultdict
from collections.abc import Iterator
from datetime import date, datetime
from typing import Any

from anastomosis.core.model import (
    AllergyCategory,
    AllergyIntolerance,
    Condition,
    ContactKind,
    ContactPoint,
    Coverage,
    DocumentArtifact,
    Encounter,
    Facility,
    FamilyMemberHistory,
    Goal,
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
from anastomosis.core.textutil import html_to_text

__all__ = ["records_from_resources"]

SOURCE_SYSTEM = "fhir-r4"
_EXT = "fhir_r4:"  # extension-key namespace for preserved-but-unmapped fields

# Code systems, with the spelling variants real exports use (SNOMED ships both
# the ``www.`` and bare hosts; we accept either rather than miss a code).
_LOINC = ("http://loinc.org",)
_ICD10 = ("http://hl7.org/fhir/sid/icd-10-cm",)
_SNOMED = ("http://snomed.info/sct", "http://www.snomed.info/sct")
_RXNORM = ("http://www.nlm.nih.gov/research/umls/rxnorm", "http://rxnorm.info/sct")
_CVX = ("http://hl7.org/fhir/sid/cvx",)
_SSN = ("http://hl7.org/fhir/sid/us-ssn",)
_NPI = "http://hl7.org/fhir/sid/us-npi"

# Smoking-status LOINC (US Core social-history) — categorizes the observation
# even when a vendor omits the FHIR category coding.
_SMOKING_LOINC = "72166-2"

# FHIR Observation category code → canonical category. Anything else → OTHER.
_OBS_CATEGORY = {
    "vital-signs": ObservationCategory.VITAL_SIGNS,
    "social-history": ObservationCategory.SOCIAL_HISTORY,
    "laboratory": ObservationCategory.LABORATORY,
    "screening": ObservationCategory.SCREENING,
}

# FHIR AllergyIntolerance.category → canonical AllergyCategory.
_ALLERGY_CATEGORY = {
    "medication": AllergyCategory.DRUG,
    "food": AllergyCategory.FOOD,
    "environment": AllergyCategory.ENVIRONMENT,
}

# US Core MRN identifier type code (v2-0203) → canonical MRN.
_MRN_TYPE = "MR"

# Patient-scoped resource types the mapper lifts into typed slots. Patient,
# Practitioner, Location, and Organization are handled as shared/reference
# resources separately; every other type is preserved losslessly.
_HANDLED = frozenset(
    {
        "Encounter",
        "Observation",
        "Condition",
        "MedicationRequest",
        "MedicationStatement",
        "AllergyIntolerance",
        "Immunization",
        "Coverage",
        "Goal",
        "FamilyMemberHistory",
        "DocumentReference",
    }
)


# --- primitive accessors ------------------------------------------------------


def _ref_id(ref: Any) -> str | None:
    """The bare id from a FHIR reference dict (``{"reference": "Patient/x"}``).

    Strips a ``ResourceType/`` prefix, a ``urn:uuid:`` prefix, or a full URL,
    leaving the logical id used to join resources within the bundle.
    """
    if not isinstance(ref, dict):
        return None
    value = ref.get("reference")
    if not value:
        return None
    text = str(value)
    if text.startswith("urn:uuid:"):
        return text[len("urn:uuid:") :]
    return text.rsplit("/", 1)[-1] if "/" in text else text


def _patient_ref(resource: dict[str, Any]) -> str | None:
    """The patient this resource hangs off, across the US Core reference fields."""
    for field in ("patient", "subject", "beneficiary"):
        rid = _ref_id(resource.get(field))
        if rid is not None:
            return rid
    return None


def _codings(concept: Any) -> list[dict[str, Any]]:
    if not isinstance(concept, dict):
        return []
    return [c for c in concept.get("coding", []) if isinstance(c, dict)]


def _code_in(concept: Any, systems: tuple[str, ...]) -> str | None:
    """The first ``code`` whose ``system`` is one of ``systems`` (None if absent)."""
    for coding in _codings(concept):
        if coding.get("system") in systems and coding.get("code"):
            return str(coding["code"])
    return None


def _concept_text(concept: Any) -> str | None:
    """Human label of a CodeableConcept: ``text`` first, else a coding display."""
    if not isinstance(concept, dict):
        return None
    if concept.get("text"):
        return str(concept["text"])
    for coding in _codings(concept):
        if coding.get("display"):
            return str(coding["display"])
    return None


def _status_active(resource: dict[str, Any], field: str) -> bool:
    """Whether a clinical-status CodeableConcept (``clinicalStatus``) reads active."""
    for coding in _codings(resource.get(field)):
        if coding.get("code") == "active":
            return True
    return False


def _num_str(value: Any) -> str | None:
    """A FHIR numeric as a clean display string (integral floats lose the ``.0``)."""
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _date(value: Any) -> date | None:
    """Parse a FHIR ``date``/``dateTime`` to a calendar date (partials padded)."""
    if not value:
        return None
    text = str(value).split("T", 1)[0]
    parts = text.split("-")
    if len(parts) == 1 and parts[0].isdigit():
        text = f"{parts[0]}-01-01"
    elif len(parts) == 2:
        text = f"{parts[0]}-{parts[1]}-01"
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _datetime(value: Any) -> datetime | None:
    """Parse a FHIR ``dateTime`` (date-only widens to midnight; never raises)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        day = _date(value)
        return datetime.fromisoformat(day.isoformat()) if day else None


def _prov(source_file: str | None, source_id: str | None) -> Provenance:
    return Provenance(source_system=SOURCE_SYSTEM, source_file=source_file, source_id=source_id)


# Structural keys never kept as residual: resourceType/id (structural) and the
# patient-reference fields (already captured as patient_id).
_STRUCTURAL = frozenset({"resourceType", "id", "subject", "patient", "beneficiary"})


def _residual(resource: dict[str, Any], consumed: frozenset[str]) -> dict[str, Any]:
    """Every top-level resource element the builder did not consume, namespaced.

    The per-field half of the lossless guarantee (mirrors pf_tebra's ``_ext``):
    a FHIR element the mapper does not lift into a typed slot is preserved
    verbatim under ``fhir_r4:<element>`` rather than dropped. This matters most
    for status/verification fields a vendor may set — ``Condition.
    verificationStatus``, ``Observation.status``, ``AllergyIntolerance.
    criticality`` — whose loss would silently *reverse* a record's clinical
    meaning (a refuted diagnosis migrating as active, a retracted value as real).
    """
    skip = consumed | _STRUCTURAL
    return {f"{_EXT}{key}": value for key, value in resource.items() if key not in skip}


# --- resource → canonical -----------------------------------------------------


def _race_ethnicity(resource: dict[str, Any], suffix: str) -> list[str]:
    """The US Core race/ethnicity ``text`` (or ombCategory displays) as a list."""
    url = f"http://hl7.org/fhir/us/core/StructureDefinition/us-core-{suffix}"
    for ext in resource.get("extension", []):
        if not isinstance(ext, dict) or ext.get("url") != url:
            continue
        values: list[str] = []
        text_value: str | None = None
        for sub in ext.get("extension", []):
            if sub.get("url") == "text" and sub.get("valueString"):
                text_value = str(sub["valueString"])
            elif sub.get("url") == "ombCategory":
                display = (sub.get("valueCoding") or {}).get("display")
                if display:
                    values.append(str(display))
        if text_value:
            return [text_value]
        return values
    return []


def _patient(resource: dict[str, Any], source_file: str | None) -> Patient:
    name = next((n for n in resource.get("name", []) if isinstance(n, dict)), {})
    given = [g for g in name.get("given", []) if g]
    communication = resource.get("communication", [])
    language = None
    if communication:
        lang = communication[0].get("language", {})
        language = lang.get("text") or _concept_text(lang)
    identifiers: list[Identifier] = []
    for ident in resource.get("identifier", []):
        if not isinstance(ident, dict) or not ident.get("value"):
            continue
        id_kind = IdentifierKind.OTHER
        if ident.get("system") in _SSN:
            id_kind = IdentifierKind.SSN
        elif any(c.get("code") == _MRN_TYPE for c in _codings(ident.get("type"))):
            id_kind = IdentifierKind.MRN
        identifiers.append(
            Identifier(
                kind=id_kind,
                value=str(ident["value"]),
                system=(ident.get("assigner") or {}).get("display") or ident.get("system"),
            )
        )
    telecom: list[ContactPoint] = []
    for tel in resource.get("telecom", []):
        if not isinstance(tel, dict) or not tel.get("value"):
            continue
        system, use = tel.get("system"), tel.get("use")
        if system == "email":
            tel_kind = ContactKind.EMAIL
        elif use == "home":
            tel_kind = ContactKind.PHONE_HOME
        elif use == "mobile":
            tel_kind = ContactKind.PHONE_MOBILE
        elif use == "work":
            tel_kind = ContactKind.PHONE_WORK
        else:
            tel_kind = ContactKind.PHONE_OTHER
        telecom.append(ContactPoint(kind=tel_kind, value=str(tel["value"])))
    addresses = [_address(a) for a in resource.get("address", []) if isinstance(a, dict)]
    return Patient(
        id=resource["id"],
        given_name=given[0] if given else None,
        middle_name=given[1] if len(given) > 1 else None,
        family_name=name.get("family"),
        suffix=(name.get("suffix") or [None])[0],
        birth_date=_date(resource.get("birthDate")),
        sex=resource.get("gender"),
        race=_race_ethnicity(resource, "race"),
        ethnicity=_race_ethnicity(resource, "ethnicity"),
        language=language,
        marital_status=_concept_text(resource.get("maritalStatus")),
        identifiers=identifiers,
        telecom=telecom,
        addresses=addresses,
        extensions=_residual(
            resource,
            frozenset(
                {
                    "name",
                    "birthDate",
                    "gender",
                    "maritalStatus",
                    "communication",
                    "identifier",
                    "telecom",
                    "address",
                    "extension",
                }
            ),
        ),
        provenance=_prov(source_file, resource["id"]),
    )


def _address(a: dict[str, Any]) -> Any:
    from anastomosis.core.model import Address

    lines = [ln for ln in a.get("line", []) if ln]
    return Address(
        line1=lines[0] if lines else None,
        line2=lines[1] if len(lines) > 1 else None,
        city=a.get("city"),
        state=a.get("state"),
        postal_code=a.get("postalCode"),
    )


def _practitioner(resource: dict[str, Any], source_file: str | None) -> Practitioner:
    name = next((n for n in resource.get("name", []) if isinstance(n, dict)), {})
    given = [g for g in name.get("given", []) if g]
    npi = next(
        (i.get("value") for i in resource.get("identifier", []) if i.get("system") == _NPI), None
    )
    return Practitioner(
        id=resource["id"],
        given_name=given[0] if given else None,
        family_name=name.get("family"),
        display_name=name.get("text"),
        npi=str(npi) if npi else None,
        extensions=_residual(resource, frozenset({"name", "identifier"})),
        provenance=_prov(source_file, resource["id"]),
    )


def _facility(resource: dict[str, Any], source_file: str | None) -> Facility:
    """A canonical Facility from a Location or Organization resource."""
    address = resource.get("address")
    if isinstance(address, list):  # Organization.address is a list; Location's is single
        address = address[0] if address else {}
    address = address or {}
    lines = [ln for ln in address.get("line", []) if ln]
    telecom = {t.get("system"): t.get("value") for t in resource.get("telecom", [])}
    return Facility(
        id=resource["id"],
        name=resource.get("name"),
        address_line1=lines[0] if lines else None,
        address_line2=lines[1] if len(lines) > 1 else None,
        city=address.get("city"),
        state=address.get("state"),
        postal_code=address.get("postalCode"),
        phone=telecom.get("phone"),
        fax=telecom.get("fax"),
        extensions=_residual(resource, frozenset({"name", "address", "telecom"})),
        provenance=_prov(source_file, resource["id"]),
    )


def _encounter(
    resource: dict[str, Any],
    notes: dict[str, list[NoteSection]],
    source_file: str | None,
) -> Encounter:
    period = resource.get("period") or {}
    types = resource.get("type", [])
    reasons = resource.get("reasonCode", [])
    participants = resource.get("participant", [])
    locations = resource.get("location", [])
    return Encounter(
        id=resource["id"],
        patient_id=_patient_ref(resource) or "",
        date_of_service=_date(period.get("start")),
        chief_complaint=_concept_text(reasons[0]) if reasons else None,
        encounter_type=_concept_text(resource.get("class"))
        or (resource.get("class") or {}).get("code"),
        note_type=_concept_text(types[0]) if types else None,
        provider_id=_ref_id(participants[0].get("individual")) if participants else None,
        facility_id=_ref_id(locations[0].get("location")) if locations else None,
        sections=notes.get(resource["id"], []),
        diagnosis_ids=[
            rid for dx in resource.get("diagnosis", []) if (rid := _ref_id(dx.get("condition")))
        ],
        # status, the full period (end), and any reasonCode/type beyond the
        # first ride along; the typed fields capture the primary elements.
        extensions=_residual(
            resource, frozenset({"class", "participant", "location", "diagnosis"})
        ),
        provenance=_prov(source_file, resource["id"]),
    )


def _observations(resource: dict[str, Any], source_file: str | None) -> list[Observation]:
    """One resource → one or more canonical observations (BP-style panels split).

    A component-bearing Observation with no top-level value (the US Core blood
    pressure shape) expands to one canonical Observation per component, so the
    systolic/diastolic LOINCs land as discrete vitals the packs render.
    """
    category = ObservationCategory.OTHER
    for cat in resource.get("category", []):
        code = (_codings(cat) or [{}])[0].get("code")
        if code in _OBS_CATEGORY:
            category = _OBS_CATEGORY[code]
            break
    code_concept = resource.get("code", {})
    loinc = _code_in(code_concept, _LOINC)
    if category is ObservationCategory.OTHER and loinc == _SMOKING_LOINC:
        category = ObservationCategory.SOCIAL_HISTORY
    encounter_id = _ref_id(resource.get("encounter"))
    patient_id = _patient_ref(resource) or ""
    effective = _datetime(
        resource.get("effectiveDateTime") or (resource.get("effectivePeriod") or {}).get("start")
    )

    def _value_unit(node: dict[str, Any]) -> tuple[str | None, str | None]:
        qty = node.get("valueQuantity")
        if isinstance(qty, dict):
            return _num_str(qty.get("value")), (qty.get("unit") or qty.get("code"))
        concept = node.get("valueCodeableConcept")
        if isinstance(concept, dict):
            return _concept_text(concept), None
        if node.get("valueString") is not None:
            return str(node["valueString"]), None
        if node.get("valueBoolean") is not None:
            return str(node["valueBoolean"]), None
        return None, None

    residual = _residual(
        resource,
        frozenset(
            {
                "category",
                "code",
                "valueQuantity",
                "valueCodeableConcept",
                "valueString",
                "valueBoolean",
                "effectiveDateTime",
                "effectivePeriod",
                "encounter",
                "component",
            }
        ),
    )

    def _make(
        obs_id: str, code: str | None, display: str | None, value: Any, unit: Any
    ) -> Observation:
        return Observation(
            id=obs_id,
            patient_id=patient_id,
            encounter_id=encounter_id,
            category=category,
            code=code,
            display=display,
            value=value,
            unit=unit,
            effective_at=effective,
            extensions=dict(residual),
            provenance=_prov(source_file, resource["id"]),
        )

    fallback_code = loinc or (_codings(code_concept) or [{}])[0].get("code")
    components = [c for c in resource.get("component", []) if isinstance(c, dict)]
    top_value, top_unit = _value_unit(resource)
    out: list[Observation] = []
    # Emit the panel's own value when it has one, AND every component (the US
    # Core BP shape is a value-less panel + systolic/diastolic components; a
    # panel may legitimately carry both). Component ids are index-qualified so
    # two components sharing a LOINC never collide.
    if top_value is not None:
        out.append(
            _make(resource["id"], fallback_code, _concept_text(code_concept), top_value, top_unit)
        )
    for index, comp in enumerate(components):
        comp_loinc = _code_in(comp.get("code"), _LOINC) or loinc
        value, unit = _value_unit(comp)
        out.append(
            _make(
                f"{resource['id']}:{index}:{comp_loinc or 'c'}",
                comp_loinc,
                _concept_text(comp.get("code")),
                value,
                unit,
            )
        )
    if not out:  # neither a value nor components (e.g. a dataAbsentReason obs)
        out.append(_make(resource["id"], fallback_code, _concept_text(code_concept), None, None))
    return out


def _condition(resource: dict[str, Any], source_file: str | None) -> Condition:
    code = resource.get("code", {})
    return Condition(
        id=resource["id"],
        patient_id=_patient_ref(resource) or "",
        icd10=_code_in(code, _ICD10),
        snomed=_code_in(code, _SNOMED),
        display=_concept_text(code),
        onset=_date(resource.get("onsetDateTime")),
        stopped=_date(resource.get("abatementDateTime")),
        recorded_at=_datetime(resource.get("recordedDate")),
        active=_status_active(resource, "clinicalStatus"),
        # verificationStatus (refuted/entered-in-error), category, severity,
        # bodySite, note, etc. are preserved so meaning is never reversed.
        extensions=_residual(
            resource,
            frozenset(
                {"code", "clinicalStatus", "onsetDateTime", "abatementDateTime", "recordedDate"}
            ),
        ),
        provenance=_prov(source_file, resource["id"]),
    )


def _medication(resource: dict[str, Any], source_file: str | None) -> MedicationStatement:
    """A canonical med-list entry from a MedicationRequest or MedicationStatement.

    US Core conveys the active medication list chiefly as MedicationRequest; the
    canonical :class:`MedicationStatement` is the list the chart renders, so both
    map here. The originating FHIR resourceType is recorded in extensions (the
    request/statement distinction), and status/intent/requester/etc. ride along
    via the residual catch-all.
    """
    concept = resource.get("medicationCodeableConcept", {})
    dosage = resource.get("dosageInstruction") or resource.get("dosage") or []
    period = resource.get("effectivePeriod") or {}
    start = _date(period.get("start") or resource.get("authoredOn") or resource.get("dateAsserted"))
    extensions: dict[str, Any] = {
        f"{_EXT}resource_type": resource["resourceType"],
        **_residual(
            resource,
            frozenset(
                {
                    "medicationCodeableConcept",
                    "dosageInstruction",
                    "dosage",
                    "effectivePeriod",
                    "authoredOn",
                    "dateAsserted",
                }
            ),
        ),
    }
    return MedicationStatement(
        id=resource["id"],
        patient_id=_patient_ref(resource) or "",
        display_name=_concept_text(concept),
        rxnorm=_code_in(concept, _RXNORM),
        sig=dosage[0].get("text") if dosage and isinstance(dosage[0], dict) else None,
        start=start,
        stop=_date(period.get("end")),
        active=resource.get("status") in ("active", "completed"),
        extensions=extensions,
        provenance=_prov(source_file, resource["id"]),
    )


def _allergy(resource: dict[str, Any], source_file: str | None) -> AllergyIntolerance:
    categories = resource.get("category") or []
    category = AllergyCategory.OTHER
    for c in categories:
        if c in _ALLERGY_CATEGORY:
            category = _ALLERGY_CATEGORY[c]
            break
    reactions = [
        text
        for r in resource.get("reaction", [])
        for m in r.get("manifestation", [])
        if (text := _concept_text(m))
    ]
    severity = next(
        (r.get("severity") for r in resource.get("reaction", []) if r.get("severity")),
        resource.get("criticality"),
    )
    return AllergyIntolerance(
        id=resource["id"],
        patient_id=_patient_ref(resource) or "",
        substance=_concept_text(resource.get("code")),
        category=category,
        reactions=reactions,
        severity=severity,
        onset=_date(resource.get("onsetDateTime")),
        active=_status_active(resource, "clinicalStatus"),
        # criticality (when a reaction severity shadowed it), verificationStatus,
        # type, recordedDate, note are preserved rather than dropped.
        extensions=_residual(
            resource,
            frozenset({"code", "category", "reaction", "clinicalStatus", "onsetDateTime"}),
        ),
        provenance=_prov(source_file, resource["id"]),
    )


def _immunization(resource: dict[str, Any], source_file: str | None) -> Immunization:
    notes = resource.get("note", [])
    extensions: dict[str, Any] = _residual(
        resource,
        frozenset({"vaccineCode", "occurrenceDateTime", "lotNumber", "expirationDate", "note"}),
    )
    cvx = _code_in(resource.get("vaccineCode"), _CVX)
    if cvx:
        extensions[f"{_EXT}cvx"] = cvx
    return Immunization(
        id=resource["id"],
        patient_id=_patient_ref(resource) or "",
        vaccine=_concept_text(resource.get("vaccineCode")),
        administered_on=_date(resource.get("occurrenceDateTime")),
        lot_number=resource.get("lotNumber"),
        expires=_date(resource.get("expirationDate")),
        comment=notes[0].get("text") if notes and isinstance(notes[0], dict) else None,
        extensions=extensions,
        provenance=_prov(source_file, resource["id"]),
    )


def _coverage(resource: dict[str, Any], source_file: str | None) -> Coverage:
    payors = resource.get("payor") or []
    period = resource.get("period") or {}
    classes = {
        (_codings(c.get("type")) or [{}])[0].get("code"): c for c in resource.get("class", [])
    }
    order = resource.get("order")
    return Coverage(
        id=resource["id"],
        patient_id=_patient_ref(resource) or "",
        payer=(payors[0].get("display") if payors and isinstance(payors[0], dict) else None),
        plan_name=(classes.get("group") or classes.get("plan") or {}).get("name"),
        group_number=(classes.get("group") or {}).get("value"),
        member_id=resource.get("subscriberId"),
        # FHIR order is a positiveInt (1 = primary) → canonical 0-based; guard a
        # non-conformant 0 so it never becomes a nonsense -1.
        order_of_benefits=(order - 1) if isinstance(order, int) and order >= 1 else None,
        start=_date(period.get("start")),
        end=_date(period.get("end")),
        active=resource.get("status") == "active",
        extensions=_residual(
            resource,
            frozenset({"payor", "period", "class", "subscriberId", "order", "status"}),
        ),
        provenance=_prov(source_file, resource["id"]),
    )


def _goal(resource: dict[str, Any], source_file: str | None) -> Goal:
    return Goal(
        id=resource["id"],
        patient_id=_patient_ref(resource) or "",
        description=(resource.get("description") or {}).get("text"),
        effective=_date(resource.get("startDate")),
        active=resource.get("lifecycleStatus") in ("active", "accepted", "in-progress"),
        extensions=_residual(resource, frozenset({"description", "startDate", "lifecycleStatus"})),
        provenance=_prov(source_file, resource["id"]),
    )


def _family_history(resource: dict[str, Any], source_file: str | None) -> FamilyMemberHistory:
    condition = next((c for c in resource.get("condition", []) if isinstance(c, dict)), {})
    extensions: dict[str, Any] = _residual(resource, frozenset({"relationship", "condition"}))
    if condition.get("onsetString"):
        extensions[f"{_EXT}onset_string"] = condition["onsetString"]
    return FamilyMemberHistory(
        id=resource["id"],
        patient_id=_patient_ref(resource) or "",
        diagnosis=_concept_text(condition.get("code")),
        relation=_concept_text(resource.get("relationship")),
        onset_date=_date(condition.get("onsetDateTime")),
        extensions=extensions,
        provenance=_prov(source_file, resource["id"]),
    )


# --- DocumentReference (clinical notes + rendered artifacts) ------------------


def _decode_attachment(attachment: dict[str, Any]) -> str | None:
    data = attachment.get("data")
    if not data:
        return None
    try:
        return base64.b64decode(data).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return None


def _note_section(docref: dict[str, Any]) -> NoteSection | None:
    """A narrative DocumentReference → a NARRATIVE NoteSection carrying TEXT only.

    Both text/html and text/plain attachments are carried as plain text
    (text/html is down-converted via ``html_to_text``) and never as
    ``NoteSection.html``. The packs render ``.html`` with Jinja ``| safe``;
    because this lane ingests arbitrary external EHR exports, external markup is
    deliberately kept out of that trusted slot — the clinical text is preserved,
    the (untrusted) markup is not re-emitted into a rendered chart. Binary
    content (PDF etc.) becomes a DocumentArtifact, not a note.
    """
    title = _concept_text(docref.get("type"))
    for content in docref.get("content", []):
        attachment = content.get("attachment") if isinstance(content, dict) else None
        if not isinstance(attachment, dict):
            continue
        content_type = (attachment.get("contentType") or "").lower()
        if not (content_type.startswith("text/") or "html" in content_type or content_type == ""):
            continue
        decoded = _decode_attachment(attachment)
        if decoded is None:
            continue
        text = ((html_to_text(decoded) if "html" in content_type else decoded) or "").strip()
        if text:
            return NoteSection(
                kind=SectionKind.NARRATIVE,
                title=attachment.get("title") or title,
                text=text,
            )
    return None


def _artifact(docref: dict[str, Any], source_file: str | None) -> DocumentArtifact | None:
    """A non-narrative (binary) DocumentReference → a DocumentArtifact record."""
    for content in docref.get("content", []):
        attachment = content.get("attachment") if isinstance(content, dict) else None
        if not isinstance(attachment, dict):
            continue
        content_type = (attachment.get("contentType") or "").lower()
        if content_type.startswith("text/") or "html" in content_type:
            continue
        # Preserve the docref's other top-level fields (status/docStatus/date/
        # author/securityLabel/…) so a retracted PDF never migrates as live.
        extensions: dict[str, Any] = _residual(docref, frozenset({"content", "context"}))
        if attachment.get("url"):
            extensions[f"{_EXT}url"] = attachment["url"]
        if attachment.get("data"):
            extensions[f"{_EXT}has_inline_data"] = True
        return DocumentArtifact(
            id=docref["id"],
            patient_id=_patient_ref(docref) or "",
            encounter_id=_ref_id((docref.get("context", {}).get("encounter") or [{}])[0]),
            mime_type=content_type or "application/octet-stream",
            title=attachment.get("title") or _concept_text(docref.get("type")),
            extensions=extensions,
            provenance=_prov(source_file, docref["id"]),
        )
    return None


def _note_encounter(
    docref: dict[str, Any], section: NoteSection, patient_id: str, source_file: str | None
) -> Encounter:
    """A synthetic encounter carrying a note whose ``context.encounter`` was
    absent or dangling — so the narrative still renders and is never dropped.

    Common in a ``$export`` slice (the DocumentReference's encounter is omitted
    or points outside the slice). The docref's own ``date`` becomes the date of
    service; its other top-level fields ride the encounter extensions. The id is
    namespaced (``docref:<id>``) so it cannot collide with a real Encounter id.
    """
    return Encounter(
        id=f"docref:{docref['id']}",
        patient_id=patient_id,
        date_of_service=_date(docref.get("date")),
        note_type=section.title,
        sections=[section],
        extensions={
            **_residual(docref, frozenset({"content", "context"})),
            f"{_EXT}synthetic_from": "DocumentReference",
        },
        provenance=_prov(source_file, docref["id"]),
    )


# --- orchestration ------------------------------------------------------------


def records_from_resources(
    resources: list[dict[str, Any]], *, source_file: str | None = None
) -> Iterator[PatientRecord]:
    """Group flat FHIR resources into one :class:`PatientRecord` per Patient.

    Accepts the resources from a Bundle's ``entry[].resource`` or the lines of a
    Bulk-Data ``$export`` NDJSON set (already parsed). Yields one record per
    Patient, in the order the patients appear. Raises :class:`ValueError` only
    when there is no Patient at all (the loud structural failure the lossless
    guarantee requires); a per-resource oddity is tolerated, not fatal.

    A resource that references a patient NOT present in the supplied data (a
    dangling reference, or an out-of-scope ``$export`` slice) is "unanchored".
    When the data describes a single patient, unanchored resources are preserved
    under that record's ``extensions["fhir_r4:unanchored"]`` (nothing is dropped).
    When it describes several patients, an unanchored resource cannot be safely
    attributed, so it is not attached — attaching it to an arbitrary record would
    misattribute one patient's data to another, which is worse than omission.
    """
    patients = [r for r in resources if r.get("resourceType") == "Patient" and r.get("id")]
    if not patients:
        raise ValueError("no Patient resource found in the FHIR data")
    patient_ids = {p["id"] for p in patients}

    practitioners = {
        r["id"]: r for r in resources if r.get("resourceType") == "Practitioner" and r.get("id")
    }
    facilities = {
        r["id"]: r
        for r in resources
        if r.get("resourceType") in ("Location", "Organization") and r.get("id")
    }

    # Group everything patient-scoped up front (single pass). DocumentReferences
    # are kept raw and partitioned per record in _assemble (notes vs artifacts,
    # attached vs synthetic), where the patient's encounter ids are known.
    by_patient: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    docrefs_by_patient: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unanchored: list[dict[str, Any]] = []

    for resource in resources:
        rtype = resource.get("resourceType")
        if rtype in ("Patient", "Practitioner", "Location", "Organization") or not rtype:
            continue
        pid = _patient_ref(resource)
        if pid is None or pid not in patient_ids:
            unanchored.append(resource)  # no/dangling patient ref — preserved below
            continue
        if rtype == "DocumentReference":
            docrefs_by_patient[pid].append(resource)
            continue
        by_patient[pid][rtype].append(resource)

    # Unanchored resources are preserved only when there is exactly one patient
    # to attribute them to (see the docstring); otherwise misattribution risk.
    sole_patient = patients[0]["id"] if len(patients) == 1 else None
    for patient_res in patients:
        pid = patient_res["id"]
        yield _assemble(
            patient_res,
            by_patient.get(pid, {}),
            docrefs_by_patient.get(pid, []),
            practitioners,
            facilities,
            source_file,
            unanchored if pid == sole_patient else [],
        )


def _assemble(
    patient_res: dict[str, Any],
    grp: dict[str, list[dict[str, Any]]],
    docrefs: list[dict[str, Any]],
    practitioners: dict[str, dict[str, Any]],
    facilities: dict[str, dict[str, Any]],
    source_file: str | None,
    unanchored: list[dict[str, Any]],
) -> PatientRecord:
    patient_id = patient_res["id"]
    # Partition this patient's DocumentReferences. A narrative note whose
    # context.encounter resolves attaches to that encounter (its other top-level
    # fields ride note_meta); a note with no/dangling encounter gets a synthetic
    # encounter so the narrative still renders and is never dropped; binary
    # content becomes a DocumentArtifact.
    encounter_ids = {r["id"] for r in grp.get("Encounter", []) if r.get("id")}
    notes_for_enc: dict[str, list[NoteSection]] = defaultdict(list)
    note_meta: dict[str, Any] = {}
    unattached_notes: list[tuple[dict[str, Any], NoteSection]] = []
    artifacts: list[DocumentArtifact] = []
    leftover_docrefs: list[dict[str, Any]] = []
    for docref in docrefs:
        section = _note_section(docref)
        if section is None:
            artifact = _artifact(docref, source_file)
            if artifact is not None:
                artifacts.append(artifact)
            else:
                # Neither renderable narrative nor binary (empty/whitespace/
                # undecodable content): preserve the whole resource so its
                # status/type/date are never silently dropped.
                leftover_docrefs.append(docref)
            continue
        enc_id = _ref_id((docref.get("context", {}).get("encounter") or [{}])[0])
        if enc_id and enc_id in encounter_ids:
            notes_for_enc[enc_id].append(section)
            residual = _residual(docref, frozenset({"content", "context"}))
            if residual:
                note_meta[docref["id"]] = residual
        else:
            unattached_notes.append((docref, section))

    meds = [
        _medication(r, source_file)
        for rtype in ("MedicationRequest", "MedicationStatement")
        for r in grp.get(rtype, [])
    ]
    observations: list[Observation] = []
    for r in grp.get("Observation", []):
        observations.extend(_observations(r, source_file))
    encounters = [_encounter(r, notes_for_enc, source_file) for r in grp.get("Encounter", [])]
    encounters.extend(
        _note_encounter(docref, section, patient_id, source_file)
        for docref, section in unattached_notes
    )

    # Attach only the practitioners/facilities this record's encounters cite
    # (the model keeps them denormalized per record). Order follows first use.
    referenced_practitioners: list[str] = []
    referenced_facilities: list[str] = []
    for enc in encounters:
        if enc.provider_id and enc.provider_id not in referenced_practitioners:
            referenced_practitioners.append(enc.provider_id)
        if enc.facility_id and enc.facility_id not in referenced_facilities:
            referenced_facilities.append(enc.facility_id)

    # Preserve every resource type with no canonical home, verbatim, under the
    # record's extensions (the lossless guarantee — e.g. Procedure, CarePlan).
    record_ext: dict[str, Any] = {}
    for rtype, items in grp.items():
        if rtype in _HANDLED:
            continue
        record_ext[f"{_EXT}{rtype}"] = items
    # Metadata of notes attached to real encounters (NoteSection has no
    # extensions slot), so a retracted note's status etc. is not lost.
    if note_meta:
        record_ext[f"{_EXT}note_meta"] = note_meta
    # DocumentReferences with no renderable content are kept whole (same
    # catch-all as unmapped resource types) — nothing is silently dropped.
    if leftover_docrefs:
        record_ext[f"{_EXT}DocumentReference"] = leftover_docrefs
    if unanchored:
        record_ext[f"{_EXT}unanchored"] = unanchored

    return PatientRecord(
        patient=_patient(patient_res, source_file),
        encounters=encounters,
        observations=observations,
        conditions=[_condition(r, source_file) for r in grp.get("Condition", [])],
        allergies=[_allergy(r, source_file) for r in grp.get("AllergyIntolerance", [])],
        medications=meds,
        immunizations=[_immunization(r, source_file) for r in grp.get("Immunization", [])],
        family_history=[
            _family_history(r, source_file) for r in grp.get("FamilyMemberHistory", [])
        ],
        goals=[_goal(r, source_file) for r in grp.get("Goal", [])],
        coverages=[_coverage(r, source_file) for r in grp.get("Coverage", [])],
        documents=artifacts,
        practitioners=[
            _practitioner(practitioners[pid], source_file)
            for pid in referenced_practitioners
            if pid in practitioners
        ],
        facilities=[
            _facility(facilities[fid], source_file)
            for fid in referenced_facilities
            if fid in facilities
        ],
        extensions=record_ext,
    )
