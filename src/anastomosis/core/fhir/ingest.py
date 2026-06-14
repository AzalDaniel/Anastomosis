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
    AdvanceDirective,
    AllergyCategory,
    AllergyIntolerance,
    AnastBase,
    Condition,
    ContactKind,
    ContactPoint,
    Coverage,
    DocumentArtifact,
    Encounter,
    Facility,
    FamilyMemberHistory,
    Goal,
    Guarantor,
    HealthConcern,
    Identifier,
    IdentifierKind,
    Immunization,
    ImplantableDevice,
    LabOrder,
    MedicationStatement,
    NoteSection,
    Observation,
    ObservationCategory,
    PastMedicalHistory,
    Patient,
    PatientContact,
    PatientRecord,
    Practitioner,
    Prescription,
    PrescriptionTransaction,
    SectionKind,
)

from .export import EXT_NS, EXTRAS_NS, FIELD_NS, IDENTIFIER_SYSTEMS, TELECOM

__all__ = ["from_bundle"]

_KIND_BY_SYSTEM = {system: kind for kind, system in IDENTIFIER_SYSTEMS.items()}
_TELECOM_BY_SHAPE = {shape: kind for kind, shape in TELECOM.items()}

_EXTRA_MODELS: dict[str, type[AnastBase]] = {
    "past_medical_history": PastMedicalHistory,
    "advance_directives": AdvanceDirective,
    "health_concerns": HealthConcern,
    "goals": Goal,
    "devices": ImplantableDevice,
    "lab_orders": LabOrder,
}


def _unref(ref: dict[str, str] | None) -> str | None:
    if not ref or "reference" not in ref:
        return None
    return ref["reference"].removeprefix("urn:anastomosis:")


def _exts(resource: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a resource's extension list back into (source_ext, fields)."""
    source: dict[str, Any] = {}
    fields: dict[str, Any] = {}
    for ext in resource.get("extension", []):
        if ext["url"] == EXT_NS:
            source = json.loads(ext["valueString"])
        elif ext["url"].startswith(FIELD_NS):
            fields[ext["url"].removeprefix(FIELD_NS)] = json.loads(ext["valueString"])
    return source, fields


def _pref(fields: dict[str, Any], key: str, fhir_value: Any, placeholder: str) -> Any:
    """Exact field-ext wins; otherwise the FHIR value unless it is the
    required-field placeholder export fabricates for a missing value."""
    if key in fields:
        return fields[key]
    return None if fhir_value == placeholder else fhir_value


def _dt(value: Any) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _d(value: Any) -> date | None:
    return date.fromisoformat(value) if value else None


def _by_type(bundle: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in bundle.get("entry", []):
        resource = entry["resource"]
        grouped.setdefault(resource["resourceType"], []).append(resource)
    return grouped


def _patient(resource: dict[str, Any]) -> Patient:
    source, fields = _exts(resource)
    name = (resource.get("name") or [{}])[0]
    given = name.get("given", [])
    address_list = [
        Address(
            line1=(a.get("line") or [None])[0],
            line2=(a.get("line") or [None, None])[1] if len(a.get("line", [])) > 1 else None,
            city=a.get("city"),
            state=a.get("state"),
            postal_code=a.get("postalCode"),
        )
        for a in resource.get("address", [])
    ]
    communication = resource.get("communication", [])
    return Patient(
        id=resource["id"],
        given_name=None if "middle_name" in fields else (given[0] if given else None),
        middle_name=fields.get("middle_name", given[1] if len(given) > 1 else None),
        family_name=name.get("family"),
        suffix=(name.get("suffix") or [None])[0],
        birth_date=_d(resource.get("birthDate")),
        sex=fields.get("sex"),
        gender_identity=fields.get("gender_identity"),
        sexual_orientation=fields.get("sexual_orientation"),
        race=fields.get("race", []),
        ethnicity=fields.get("ethnicity", []),
        language=communication[0]["language"]["text"] if communication else None,
        marital_status=(resource.get("maritalStatus") or {}).get("text"),
        mothers_maiden_name=fields.get("mothers_maiden_name"),
        contact_preference=fields.get("contact_preference"),
        status=fields.get("status"),
        notes=fields.get("notes"),
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
        addresses=(
            [Address.model_validate(a) for a in fields["addresses"]]
            if "addresses" in fields
            else address_list
        ),
        contacts=[PatientContact.model_validate(c) for c in fields.get("contacts", [])],
        guarantor=(
            Guarantor.model_validate(fields["guarantor"]) if fields.get("guarantor") else None
        ),
        extensions=source,
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
    source, fields = _exts(resource)
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
        encounter_type=fields.get("encounter_type"),
        note_type=types[0]["text"] if types else None,
        provider_id=_unref(participants[0]["individual"]) if participants else None,
        facility_id=_unref(locations[0]["location"]) if locations else None,
        signed_by_id=fields.get("signed_by_id"),
        signed_at=_dt(fields.get("signed_at")),
        last_modified_at=_dt(fields.get("last_modified_at")),
        sections=sections,
        addenda=addenda,
        diagnosis_ids=[
            ref for dx in resource.get("diagnosis", []) if (ref := _unref(dx.get("condition")))
        ],
        extensions=source,
    )


def _observation(resource: dict[str, Any]) -> Observation:
    source, fields = _exts(resource)
    categories = resource.get("category", [])
    category = "other"
    if categories and categories[0].get("coding"):
        category = categories[0]["coding"][0].get("code", "other")
    code = resource.get("code", {})
    coding = (code.get("coding") or [{}])[0]
    return Observation(
        id=resource["id"],
        patient_id=_unref(resource.get("subject")) or "",
        encounter_id=_unref(resource.get("encounter")),
        category=ObservationCategory(category),
        code=coding.get("code"),
        display=coding.get("display") or fields.get("display") or code.get("text"),
        value=fields.get("value", resource.get("valueString")),
        unit=fields.get("unit"),
        effective_at=_dt(resource.get("effectiveDateTime")),
        recorded_at=_dt(fields.get("recorded_at")),
        extensions=source,
    )


def _condition(resource: dict[str, Any]) -> Condition:
    source, fields = _exts(resource)
    by_system = {c.get("system"): c.get("code") for c in resource.get("code", {}).get("coding", [])}
    status = resource["clinicalStatus"]["coding"][0]["code"]
    return Condition(
        id=resource["id"],
        patient_id=_unref(resource.get("subject")) or "",
        icd10=by_system.get("http://hl7.org/fhir/sid/icd-10-cm"),
        snomed=by_system.get("http://www.snomed.info/sct"),
        display=resource.get("code", {}).get("text"),
        acuity=fields.get("acuity"),
        onset=_d(resource.get("onsetDateTime")),
        stopped=_d(resource.get("abatementDateTime")),
        recorded_at=_dt(resource.get("recordedDate")),
        active=status == "active",
        extensions=source,
    )


def _allergy(resource: dict[str, Any]) -> AllergyIntolerance:
    source, fields = _exts(resource)
    reactions = fields.get(
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
        category=AllergyCategory(fields.get("category", "other")),
        reactions=reactions,
        severity=fields.get("severity"),
        onset=_d(resource.get("onsetDateTime")),
        active=resource["clinicalStatus"]["coding"][0]["code"] == "active",
        extensions=source,
    )


def _medication(resource: dict[str, Any]) -> MedicationStatement:
    source, fields = _exts(resource)
    period = resource.get("effectivePeriod", {})
    dosage = resource.get("dosage", [])
    return MedicationStatement(
        id=resource["id"],
        patient_id=_unref(resource.get("subject")) or "",
        generic_name=fields.get("generic_name"),
        brand_name=fields.get("brand_name"),
        strength=fields.get("strength"),
        route=fields.get("route"),
        dose_form=fields.get("dose_form"),
        display_name=_pref(
            fields, "display_name", resource["medicationCodeableConcept"]["text"], "Unknown"
        ),
        sig=dosage[0]["text"] if dosage else None,
        associated_dx=fields.get("associated_dx"),
        rxnorm=fields.get("rxnorm"),
        start=_d(period.get("start")),
        stop=_d(period.get("end")),
        last_modified_at=_dt(fields.get("last_modified_at")),
        active=resource["status"] == "active",
        prescription_ids=fields.get("prescription_ids", []),
        extensions=source,
    )


def _prescription(resource: dict[str, Any]) -> Prescription:
    source, fields = _exts(resource)
    return Prescription(
        id=resource["id"],
        patient_id=_unref(resource.get("subject")) or "",
        medication_id=fields.get("medication_id"),
        prescriber_id=_unref(resource.get("requester")),
        prefix=fields.get("prefix"),
        status_label=fields.get("status_label"),
        display_date=_dt(fields.get("display_date")),
        sig=(resource.get("dosageInstruction") or [{}])[0].get("text"),
        refills=fields.get("refills"),
        quantity=fields.get("quantity"),
        transactions=[
            PrescriptionTransaction.model_validate(t) for t in fields.get("transactions", [])
        ],
        extensions=source,
    )


def _immunization(resource: dict[str, Any]) -> Immunization:
    source, fields = _exts(resource)
    notes = resource.get("note", [])
    return Immunization(
        id=resource["id"],
        patient_id=_unref(resource.get("patient")) or "",
        vaccine=_pref(fields, "vaccine", resource["vaccineCode"]["text"], "Unknown"),
        administered_on=_d(resource.get("occurrenceDateTime")),
        source=fields.get("source"),
        lot_number=resource.get("lotNumber"),
        expires=_d(resource.get("expirationDate")),
        comment=notes[0]["text"] if notes else None,
        extensions=source,
    )


def _family_history(resource: dict[str, Any]) -> FamilyMemberHistory:
    source, fields = _exts(resource)
    condition = (resource.get("condition") or [{}])[0]
    return FamilyMemberHistory(
        id=resource["id"],
        patient_id=_unref(resource.get("patient")) or "",
        diagnosis=_pref(fields, "diagnosis", condition.get("code", {}).get("text"), "Unknown"),
        relation=_pref(fields, "relation", resource["relationship"]["text"], "unknown"),
        onset_date=_d(fields.get("onset_date", condition.get("onsetString"))),
        extensions=source,
    )


def _coverage(resource: dict[str, Any]) -> Coverage:
    source, fields = _exts(resource)
    period = resource.get("period", {})
    fhir_order = resource.get("order")
    return Coverage(
        id=resource["id"],
        patient_id=_unref(resource.get("beneficiary")) or "",
        payer=_pref(fields, "payer", resource["payor"][0].get("display"), "Unknown"),
        plan_name=fields.get("plan_name"),
        plan_type=fields.get("plan_type"),
        coverage_type=fields.get("coverage_type"),
        member_id=resource.get("subscriberId"),
        group_number=fields.get("group_number"),
        order_of_benefits=fields.get(
            "order_of_benefits", None if fhir_order is None else fhir_order - 1
        ),
        priority_label=fields.get("priority_label"),
        employer=fields.get("employer"),
        relationship_to_insured=fields.get("relationship_to_insured"),
        payment_type=fields.get("payment_type"),
        copay=fields.get("copay"),
        start=_d(period.get("start")),
        end=_d(period.get("end")),
        active=resource["status"] == "active",
        status_label=fields.get("status_label"),
        extensions=source,
    )


def _practitioner(resource: dict[str, Any]) -> Practitioner:
    source, fields = _exts(resource)
    name = (resource.get("name") or [{}])[0]
    identifiers = resource.get("identifier", [])
    return Practitioner(
        id=resource["id"],
        given_name=(name.get("given") or [None])[0],
        family_name=name.get("family"),
        display_name=name.get("text"),
        credential=fields.get("credential"),
        npi=identifiers[0]["value"] if identifiers else None,
        extensions=source,
    )


def _facility(resource: dict[str, Any]) -> Facility:
    source, _ = _exts(resource)
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
    source, fields = _exts(resource)
    attachment = resource["content"][0]["attachment"]
    return DocumentArtifact(
        id=resource["id"],
        patient_id=_unref(resource.get("subject")) or "",
        encounter_id=fields.get("encounter_id"),
        path=fields.get("path"),
        sha256=fields.get("sha256"),
        mime_type=attachment.get("contentType", "application/octet-stream"),
        title=attachment.get("title"),
        page_count=fields.get("page_count"),
        pack_name=fields.get("pack_name"),
        generated_at=_dt(fields.get("generated_at")),
        extensions=source,
    )


def from_bundle(bundle: dict[str, Any]) -> PatientRecord:
    """Rebuild a PatientRecord from a Bundle produced by :func:`to_bundle`."""
    grouped = _by_type(bundle)
    if not grouped.get("Patient"):
        raise ValueError("bundle contains no Patient resource")

    notes: dict[str, dict[str, str]] = {}
    artifacts: list[DocumentArtifact] = []
    for docref in grouped.get("DocumentReference", []):
        _, fields = _exts(docref)
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
        practitioners=[_practitioner(r) for r in grouped.get("Practitioner", [])],
        facilities=[_facility(r) for r in grouped.get("Location", [])],
    )
    for name, items in extras.items():
        model = _EXTRA_MODELS.get(name)
        if model is not None:
            setattr(record, name, [model.model_validate(item) for item in items])
    meta = extras.get("__record__")
    if meta:
        record.id = meta["id"]
        record.extensions = meta["extensions"]
    return record
