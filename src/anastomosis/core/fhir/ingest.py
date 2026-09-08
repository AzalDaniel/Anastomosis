"""FHIR R4 Bundle → PatientRecord (exact inverse of export)."""

from __future__ import annotations

import base64
import json
from datetime import date, datetime
from typing import Any

from lxml import html as lxml_html

from anastomosis.core.model import (
    Addendum,
    Address,
    AllergyIntolerance,
    Condition,
    ContactKind,
    ContactPoint,
    Coverage,
    DocumentArtifact,
    Encounter,
    Facility,
    FamilyMemberHistory,
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
    Prescription,
    SectionKind,
)
from anastomosis.core.timeutil import iso_date, iso_datetime

from .fields import (
    ACTOR,
    ALLERGY,
    ARTIFACT,
    CONDITION,
    COVERAGE,
    ENCOUNTER,
    EXTRAS_NS,
    FAMILY_HISTORY,
    IDENTIFIER_SYSTEMS,
    IMMUNIZATION,
    MEDICATION,
    OBSERVATION,
    PATIENT,
    PRESCRIPTION,
    RECORD_EXTRAS,
    TELECOM,
    from_extensions,
    split_extensions,
)

__all__ = ["from_bundle"]

_KIND_BY_SYSTEM = {system: kind for kind, system in IDENTIFIER_SYSTEMS.items()}
_TELECOM_BY_SHAPE = {shape: kind for kind, shape in TELECOM.items()}


def _unref(ref: dict[str, str] | None) -> str | None:
    if not ref or "reference" not in ref:
        return None
    # Recover the id from either URN scheme to_bundle emits: urn:uuid: for a UUID
    # id (the standard, server-resolvable form), urn:anastomosis: otherwise.
    return ref["reference"].removeprefix("urn:uuid:").removeprefix("urn:anastomosis:")


def _pref(kwargs: dict[str, Any], key: str, fhir_value: Any, placeholder: str) -> None:
    """The tail's exact value wins; otherwise the FHIR value, unless it is the
    required-field placeholder export fabricates for a missing value."""
    kwargs.setdefault(key, None if fhir_value == placeholder else fhir_value)


def _dt(value: Any) -> datetime | None:
    return iso_datetime(value)


def _d(value: Any) -> date | None:
    return iso_date(value)


def _by_type(bundle: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in bundle.get("entry", []):
        resource = entry["resource"]
        grouped.setdefault(resource["resourceType"], []).append(resource)
    return grouped


def _patient(resource: dict[str, Any]) -> Patient:
    source, fields = split_extensions(resource)
    kwargs = from_extensions(fields, PATIENT)
    name = (resource.get("name") or [{}])[0]
    given = name.get("given", [])
    communication = resource.get("communication", [])
    # Reading half of export's middle-name and address rules: the tail holds
    # either only when the FHIR shape would read it back into a neighbour.
    kwargs.setdefault("middle_name", given[1] if len(given) > 1 else None)
    kwargs.setdefault(
        "addresses",
        [
            Address(
                line1=(a.get("line") or [None])[0],
                line2=(a.get("line") or [None, None])[1] if len(a.get("line", [])) > 1 else None,
                city=a.get("city"),
                state=a.get("state"),
                postal_code=a.get("postalCode"),
            )
            for a in resource.get("address", [])
        ],
    )
    return Patient(
        id=resource["id"],
        given_name=None if "middle_name" in fields else (given[0] if given else None),
        family_name=name.get("family"),
        suffix=(name.get("suffix") or [None])[0],
        birth_date=_d(resource.get("birthDate")),
        language=communication[0]["language"]["text"] if communication else None,
        marital_status=(resource.get("maritalStatus") or {}).get("text"),
        identifiers=[
            Identifier(
                kind=IdentifierKind(_KIND_BY_SYSTEM.get(i.get("system", ""), "other")),
                value=i["value"],
                system=(i.get("assigner") or {}).get("display"),
            )
            for i in resource.get("identifier", [])
        ],
        telecom=[
            ContactPoint(
                kind=ContactKind(
                    _TELECOM_BY_SHAPE.get((t.get("system"), t.get("use")), "phone_other")
                ),
                value=t["value"],
            )
            for t in resource.get("telecom", [])
        ],
        extensions=source,
        **kwargs,
    )


def _note_sections(html_text: str) -> tuple[list[NoteSection], list[Addendum]]:
    from anastomosis.core.textutil import html_to_text

    sections: list[NoteSection] = []
    addenda: list[Addendum] = []
    fragment = lxml_html.fragment_fromstring(html_text, create_parent="div")
    for node in fragment.findall("section"):
        kind = node.get("data-kind", "narrative")
        if kind == "addendum":
            addenda.append(
                Addendum(
                    text=node.text_content() or None,
                    status=node.get("data-status"),
                    author_name=node.get("data-author"),
                    author_credential=node.get("data-credential"),
                    source=node.get("data-source"),
                    at=_dt(node.get("data-at")),
                )
            )
            continue
        inner = (node.text or "") + "".join(
            lxml_html.tostring(child, encoding="unicode") for child in node
        )
        html_value = inner or None
        sections.append(
            NoteSection(
                kind=SectionKind(kind),
                title=node.get("data-title"),
                html=html_value,
                text=node.get("data-text") or html_to_text(html_value),
            )
        )
    return sections, addenda


def _encounter(resource: dict[str, Any], notes: dict[str, dict[str, str]]) -> Encounter:
    source, fields = split_extensions(resource)
    sections: list[NoteSection] = []
    addenda: list[Addendum] = []
    note = notes.get(resource["id"])
    if note and "application/json" in note:
        exact = json.loads(note["application/json"])
        sections = [NoteSection.model_validate(s) for s in exact["sections"]]
        addenda = [Addendum.model_validate(a) for a in exact["addenda"]]
    elif note and "text/html" in note:
        sections, addenda = _note_sections(note["text/html"])
    types = resource.get("type", [])
    reasons = resource.get("reasonCode", [])
    participants = resource.get("participant", [])
    locations = resource.get("location", [])
    return Encounter(
        id=resource["id"],
        patient_id=_unref(resource.get("subject")) or "",
        date_of_service=_d((resource.get("period") or {}).get("start")),
        chief_complaint=reasons[0]["text"] if reasons else None,
        note_type=types[0]["text"] if types else None,
        provider_id=_unref(participants[0]["individual"]) if participants else None,
        facility_id=_unref(locations[0]["location"]) if locations else None,
        sections=sections,
        addenda=addenda,
        diagnosis_ids=[
            ref for dx in resource.get("diagnosis", []) if (ref := _unref(dx.get("condition")))
        ],
        extensions=source,
        **from_extensions(fields, ENCOUNTER),
    )


def _observation(resource: dict[str, Any]) -> Observation:
    source, fields = split_extensions(resource)
    kwargs = from_extensions(fields, OBSERVATION)
    categories = resource.get("category", [])
    category = "other"
    if categories and categories[0].get("coding"):
        category = categories[0]["coding"][0].get("code", "other")
    code = resource.get("code", {})
    coding = (code.get("coding") or [{}])[0]
    kwargs["display"] = coding.get("display") or kwargs.get("display") or code.get("text")
    kwargs.setdefault("value", resource.get("valueString"))
    return Observation(
        id=resource["id"],
        patient_id=_unref(resource.get("subject")) or "",
        encounter_id=_unref(resource.get("encounter")),
        category=ObservationCategory(category),
        code=coding.get("code"),
        effective_at=_dt(resource.get("effectiveDateTime")),
        extensions=source,
        **kwargs,
    )


def _condition(resource: dict[str, Any]) -> Condition:
    source, fields = split_extensions(resource)
    by_system = {c.get("system"): c.get("code") for c in resource.get("code", {}).get("coding", [])}
    status = resource["clinicalStatus"]["coding"][0]["code"]
    return Condition(
        id=resource["id"],
        patient_id=_unref(resource.get("subject")) or "",
        icd10=by_system.get("http://hl7.org/fhir/sid/icd-10-cm"),
        snomed=by_system.get("http://www.snomed.info/sct"),
        display=resource.get("code", {}).get("text"),
        onset=_d(resource.get("onsetDateTime")),
        stopped=_d(resource.get("abatementDateTime")),
        recorded_at=_dt(resource.get("recordedDate")),
        active=status == "active",
        extensions=source,
        **from_extensions(fields, CONDITION),
    )


def _allergy(resource: dict[str, Any]) -> AllergyIntolerance:
    source, fields = split_extensions(resource)
    kwargs = from_extensions(fields, ALLERGY)
    kwargs.setdefault(
        "reactions",
        [
            m["text"]
            for r in resource.get("reaction", [])
            for m in r.get("manifestation", [])
            if m.get("text")
        ],
    )
    return AllergyIntolerance(
        id=resource["id"],
        patient_id=_unref(resource.get("patient")) or "",
        substance=resource.get("code", {}).get("text"),
        onset=_d(resource.get("onsetDateTime")),
        active=resource["clinicalStatus"]["coding"][0]["code"] == "active",
        extensions=source,
        **kwargs,
    )


def _medication(resource: dict[str, Any]) -> MedicationStatement:
    source, fields = split_extensions(resource)
    kwargs = from_extensions(fields, MEDICATION)
    _pref(kwargs, "display_name", resource["medicationCodeableConcept"]["text"], "Unknown")
    period = resource.get("effectivePeriod", {})
    dosage = resource.get("dosage", [])
    return MedicationStatement(
        id=resource["id"],
        patient_id=_unref(resource.get("subject")) or "",
        sig=dosage[0]["text"] if dosage else None,
        start=_d(period.get("start")),
        stop=_d(period.get("end")),
        active=resource["status"] == "active",
        extensions=source,
        **kwargs,
    )


def _prescription(resource: dict[str, Any]) -> Prescription:
    source, fields = split_extensions(resource)
    return Prescription(
        id=resource["id"],
        patient_id=_unref(resource.get("subject")) or "",
        prescriber_id=_unref(resource.get("requester")),
        sig=(resource.get("dosageInstruction") or [{}])[0].get("text"),
        extensions=source,
        **from_extensions(fields, PRESCRIPTION),
    )


def _immunization(resource: dict[str, Any]) -> Immunization:
    source, fields = split_extensions(resource)
    kwargs = from_extensions(fields, IMMUNIZATION)
    _pref(kwargs, "vaccine", resource["vaccineCode"]["text"], "Unknown")
    notes = resource.get("note", [])
    return Immunization(
        id=resource["id"],
        patient_id=_unref(resource.get("patient")) or "",
        administered_on=_d(resource.get("occurrenceDateTime")),
        lot_number=resource.get("lotNumber"),
        expires=_d(resource.get("expirationDate")),
        comment=notes[0]["text"] if notes else None,
        extensions=source,
        **kwargs,
    )


def _family_history(resource: dict[str, Any]) -> FamilyMemberHistory:
    source, fields = split_extensions(resource)
    kwargs = from_extensions(fields, FAMILY_HISTORY)
    condition = (resource.get("condition") or [{}])[0]
    _pref(kwargs, "diagnosis", condition.get("code", {}).get("text"), "Unknown")
    _pref(kwargs, "relation", resource["relationship"]["text"], "unknown")
    kwargs.setdefault("onset_date", _d(condition.get("onsetString")))
    return FamilyMemberHistory(
        id=resource["id"],
        patient_id=_unref(resource.get("patient")) or "",
        extensions=source,
        **kwargs,
    )


def _coverage(resource: dict[str, Any]) -> Coverage:
    source, fields = split_extensions(resource)
    kwargs = from_extensions(fields, COVERAGE)
    _pref(kwargs, "payer", resource["payor"][0].get("display"), "Unknown")
    fhir_order = resource.get("order")
    kwargs.setdefault("order_of_benefits", None if fhir_order is None else fhir_order - 1)
    period = resource.get("period", {})
    return Coverage(
        id=resource["id"],
        patient_id=_unref(resource.get("beneficiary")) or "",
        member_id=resource.get("subscriberId"),
        start=_d(period.get("start")),
        end=_d(period.get("end")),
        active=resource["status"] == "active",
        extensions=source,
        **kwargs,
    )


#: The three FHIR types :func:`~anastomosis.core.fhir.export.to_bundle` turns
#: a canonical Practitioner into; all three read back, or two thirds of the
#: record's people would be lost on the way back.
_ACTOR_TYPES = frozenset({"Practitioner", "RelatedPerson", "Device"})


def _actors(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """Every resource from a canonical Practitioner, in bundle order — not
    grouped by type, which would return all clinicians, then all relatives,
    then all devices, not the record that was written.
    """
    return [
        entry["resource"]
        for entry in bundle.get("entry", [])
        if entry["resource"]["resourceType"] in _ACTOR_TYPES
    ]


def _practitioner(resource: dict[str, Any]) -> Practitioner:
    source, fields = split_extensions(resource)
    name = (resource.get("name") or [{}])[0]
    # A Device names itself in `deviceName` rather than `name`; it is the same
    # canonical field on the way back.
    devices = resource.get("deviceName") or [{}]
    identifiers = resource.get("identifier", [])
    return Practitioner(
        id=resource["id"],
        given_name=(name.get("given") or [None])[0],
        family_name=name.get("family"),
        display_name=name.get("text") or devices[0].get("name"),
        npi=identifiers[0]["value"] if identifiers else None,
        extensions=source,
        **from_extensions(fields, ACTOR),
    )


def _facility(resource: dict[str, Any]) -> Facility:
    source, _ = split_extensions(resource)
    address = resource.get("address", {})
    lines = address.get("line", [])
    telecom = {t["system"]: t["value"] for t in resource.get("telecom", [])}
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
        extensions=source,
    )


def _artifact(resource: dict[str, Any]) -> DocumentArtifact:
    source, fields = split_extensions(resource)
    kwargs = from_extensions(fields, ARTIFACT)
    attachment = resource["content"][0]["attachment"]
    kwargs.setdefault("mime_type", attachment.get("contentType", "application/octet-stream"))
    return DocumentArtifact(
        id=resource["id"],
        patient_id=_unref(resource.get("subject")) or "",
        title=attachment.get("title"),
        extensions=source,
        **kwargs,
    )


def from_bundle(bundle: dict[str, Any]) -> PatientRecord:
    """Rebuild a PatientRecord from a Bundle produced by :func:`to_bundle`."""
    grouped = _by_type(bundle)
    if not grouped.get("Patient"):
        raise ValueError("bundle contains no Patient resource")

    notes: dict[str, dict[str, str]] = {}
    artifacts: list[DocumentArtifact] = []
    for docref in grouped.get("DocumentReference", []):
        _, fields = split_extensions(docref)
        if fields.get("artifact"):
            artifacts.append(_artifact(docref))
            continue
        encounter_ref = _unref(docref["context"]["encounter"][0])
        if encounter_ref:
            notes[encounter_ref] = {
                content["attachment"]["contentType"]: base64.b64decode(
                    content["attachment"]["data"]
                ).decode()
                for content in docref["content"]
                if content["attachment"].get("data")
            }

    patient_resource = grouped["Patient"][0]
    patient = _patient(patient_resource)
    extras_raw = next(
        (
            ext["valueString"]
            for ext in patient_resource.get("extension", [])
            if ext["url"] == EXTRAS_NS
        ),
        None,
    )
    extras: dict[str, Any] = json.loads(extras_raw) if extras_raw else {}

    record = PatientRecord(
        patient=patient,
        encounters=[_encounter(r, notes) for r in grouped.get("Encounter", [])],
        observations=[_observation(r) for r in grouped.get("Observation", [])],
        conditions=[_condition(r) for r in grouped.get("Condition", [])],
        allergies=[_allergy(r) for r in grouped.get("AllergyIntolerance", [])],
        medications=[_medication(r) for r in grouped.get("MedicationStatement", [])],
        prescriptions=[_prescription(r) for r in grouped.get("MedicationRequest", [])],
        immunizations=[_immunization(r) for r in grouped.get("Immunization", [])],
        family_history=[_family_history(r) for r in grouped.get("FamilyMemberHistory", [])],
        coverages=[_coverage(r) for r in grouped.get("Coverage", [])],
        documents=artifacts,
        practitioners=[_practitioner(r) for r in _actors(bundle)],
        facilities=[_facility(r) for r in grouped.get("Location", [])],
    )
    for name, items in extras.items():
        model = RECORD_EXTRAS.get(name)
        if model is not None:
            setattr(record, name, [model.model_validate(item) for item in items])
    meta = extras.get("__record__")
    if meta:
        # Only extensions restored: a record id here (pre-#405 bundles) is
        # the exporting run's own bookkeeping, never source-stated (RULES.md 8).
        record.extensions = meta["extensions"]
    return record
