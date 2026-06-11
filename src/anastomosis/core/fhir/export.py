"""PatientRecord → FHIR R4 Bundle (type=collection), plain JSON dicts."""

from __future__ import annotations

import base64
import json
import math
from html import escape
from typing import Any

from anastomosis.core.model import (
    AllergyIntolerance,
    Condition,
    Coverage,
    DocumentArtifact,
    Encounter,
    Facility,
    FamilyMemberHistory,
    Immunization,
    MedicationStatement,
    Observation,
    Patient,
    PatientRecord,
    Practitioner,
    Prescription,
)
from anastomosis.core.model.base import AnastBase

__all__ = ["EXT_NS", "FIELD_NS", "to_bundle"]

EXT_NS = "urn:anastomosis:ext"
FIELD_NS = "urn:anastomosis:field:"
EXTRAS_NS = "urn:anastomosis:record-extras"

LOINC = "http://loinc.org"
ICD10 = "http://hl7.org/fhir/sid/icd-10-cm"
SNOMED = "http://www.snomed.info/sct"
SSN = "http://hl7.org/fhir/sid/us-ssn"
NPI = "http://hl7.org/fhir/sid/us-npi"
OBS_CATEGORY = "http://terminology.hl7.org/CodeSystem/observation-category"

IDENTIFIER_SYSTEMS = {
    "ssn": SSN,
    "mrn": "urn:anastomosis:id:mrn",
    "prn": "urn:anastomosis:id:prn",
    "source_guid": "urn:anastomosis:source-guid",
    "other": "urn:anastomosis:id:other",
}
TELECOM = {
    "phone_home": ("phone", "home"),
    "phone_mobile": ("phone", "mobile"),
    "phone_work": ("phone", "work"),
    "phone_other": ("phone", None),
    "email": ("email", None),
}
_FHIR_GENDERS = {"male", "female", "other", "unknown"}
_FHIR_ALLERGY_CATEGORY = {"drug": "medication", "food": "food", "environment": "environment"}
_FHIR_SEVERITIES = {"mild", "moderate", "severe"}


def _ref(resource_id: str) -> dict[str, str]:
    return {"reference": f"urn:anastomosis:{resource_id}"}


def _exts(model: AnastBase, fields: dict[str, Any]) -> list[dict[str, str]]:
    """The lossless tail: source extensions + canonical fields FHIR can't hold."""
    out: list[dict[str, str]] = []
    if model.extensions:
        out.append({"url": EXT_NS, "valueString": json.dumps(model.extensions, sort_keys=True)})
    for name, value in fields.items():
        if value is None or value == [] or value == {}:
            continue
        if True:
            out.append({"url": FIELD_NS + name, "valueString": json.dumps(value, default=str)})
    return out


def _prune(resource: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in resource.items() if v not in (None, "", [], {})}


def _date(value: Any) -> str | None:
    return value.isoformat() if value else None


# --- resources ---------------------------------------------------------------


def _patient(p: Patient, record: PatientRecord) -> dict[str, Any]:
    extras: dict[str, Any] = {
        name: [m.model_dump(mode="json") for m in getattr(record, name)]
        for name in (
            "past_medical_history",
            "advance_directives",
            "health_concerns",
            "goals",
            "devices",
            "lab_orders",
        )
        if getattr(record, name)
    }
    extras["__record__"] = {"id": record.id, "extensions": record.extensions}
    extension = _exts(
        p,
        {
            "sex": p.sex,
            "gender_identity": p.gender_identity,
            "sexual_orientation": p.sexual_orientation,
            "race": p.race,
            "ethnicity": p.ethnicity,
            "mothers_maiden_name": p.mothers_maiden_name,
            "middle_name": p.middle_name if (p.middle_name and not p.given_name) else None,
            "addresses": (
                [a.model_dump(mode="json") for a in p.addresses]
                if any(a.line2 and not a.line1 for a in p.addresses)
                else None
            ),
            "contact_preference": p.contact_preference,
            "status": p.status,
            "notes": p.notes,
            "contacts": [c.model_dump(mode="json") for c in p.contacts],
            "guarantor": p.guarantor.model_dump(mode="json") if p.guarantor else None,
        },
    )
    if extras:  # always true: __record__ is always present
        extension.append({"url": EXTRAS_NS, "valueString": json.dumps(extras, default=str)})
    gender = (p.sex or "").lower()
    return _prune(
        {
            "resourceType": "Patient",
            "id": p.id,
            "extension": extension,
            "identifier": [
                _prune(
                    {
                        "system": IDENTIFIER_SYSTEMS[i.kind.value],
                        "value": i.value,
                        "assigner": {"display": i.system} if i.system else None,
                    }
                )
                for i in p.identifiers
            ],
            "name": (
                [
                    _prune(
                        {
                            "given": [n for n in (p.given_name, p.middle_name) if n],
                            "family": p.family_name,
                            "suffix": [p.suffix] if p.suffix else [],
                        }
                    )
                ]
                if (p.given_name or p.middle_name or p.family_name or p.suffix)
                else []
            ),
            "gender": gender if gender in _FHIR_GENDERS else None,
            "birthDate": _date(p.birth_date),
            "telecom": [
                _prune(
                    {
                        "system": TELECOM[t.kind.value][0],
                        "use": TELECOM[t.kind.value][1],
                        "value": t.value,
                    }
                )
                for t in p.telecom
            ],
            "address": [
                _prune(
                    {
                        "line": [line for line in (a.line1, a.line2) if line],
                        "city": a.city,
                        "state": a.state,
                        "postalCode": a.postal_code,
                    }
                )
                for a in p.addresses
            ],
            "maritalStatus": {"text": p.marital_status} if p.marital_status else None,
            "communication": [{"language": {"text": p.language}}] if p.language else [],
        }
    )


def _encounter(e: Encounter) -> dict[str, Any]:
    return _prune(
        {
            "resourceType": "Encounter",
            "id": e.id,
            "extension": _exts(
                e,
                {
                    "encounter_type": e.encounter_type,
                    "signed_by_id": e.signed_by_id,
                    "signed_at": _date(e.signed_at),
                    "last_modified_at": _date(e.last_modified_at),
                },
            ),
            "status": "finished",
            "class": {
                "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
                "code": "AMB",
            },
            "type": [{"text": e.note_type}] if e.note_type else [],
            "subject": _ref(e.patient_id),
            "period": {"start": _date(e.date_of_service)} if e.date_of_service else None,
            "reasonCode": [{"text": e.chief_complaint}] if e.chief_complaint else [],
            "participant": [{"individual": _ref(e.provider_id)}] if e.provider_id else [],
            "location": [{"location": _ref(e.facility_id)}] if e.facility_id else [],
            "diagnosis": [{"condition": _ref(dx)} for dx in e.diagnosis_ids],
        }
    )


def _note_html(e: Encounter) -> str:
    from anastomosis.core.textutil import html_to_text

    parts: list[str] = []
    for s in e.sections:
        attrs = f'data-kind="{escape(s.kind.value, quote=True)}"'
        if s.title:
            attrs += f' data-title="{escape(s.title, quote=True)}"'
        if s.text and s.text != html_to_text(s.html):
            attrs += f' data-text="{escape(s.text, quote=True)}"'
        parts.append(f"<section {attrs}>{s.html or ''}</section>")
    for a in e.addenda:
        attrs = 'data-kind="addendum"'
        for attr, value in (
            ("data-status", a.status),
            ("data-author", a.author_name),
            ("data-credential", a.author_credential),
            ("data-source", a.source),
            ("data-at", _date(a.at)),
        ):
            if value:
                attrs += f' {attr}="{escape(value, quote=True)}"'
        parts.append(f"<section {attrs}>{escape(a.text or '')}</section>")
    return "\n".join(parts)


def _note_docref(e: Encounter) -> dict[str, Any]:
    # Two renditions of the same note (DocumentReference.content's intended
    # use): human-readable HTML any system can display, plus an exact JSON
    # rendition Anastomosis ingest prefers — HTML parsers normalize markup
    # (<br/> vs <br>), and byte-faithful round-trips must not depend on that.
    exact = {
        "sections": [s.model_dump(mode="json") for s in e.sections],
        "addenda": [a.model_dump(mode="json") for a in e.addenda],
    }
    return _prune(
        {
            "resourceType": "DocumentReference",
            "id": f"{e.id}-doc",
            "status": "current",
            "docStatus": "final" if e.signed_at else "preliminary",
            "type": {"text": e.note_type or "Clinical note"},
            "subject": _ref(e.patient_id),
            "date": _date(e.signed_at) or _date(e.last_modified_at),
            "authenticator": _ref(e.signed_by_id) if e.signed_by_id else None,
            "context": {"encounter": [_ref(e.id)]},
            "content": [
                {
                    "attachment": {
                        "contentType": "text/html",
                        "data": base64.b64encode(_note_html(e).encode()).decode(),
                    }
                },
                {
                    "attachment": {
                        "contentType": "application/json",
                        "data": base64.b64encode(json.dumps(exact).encode()).decode(),
                    }
                },
            ],
        }
    )


def _observation(o: Observation) -> dict[str, Any]:
    quantity = None
    try:
        if o.value is not None and math.isfinite(float(o.value)):
            quantity = {"value": float(o.value), "unit": o.unit}
    except ValueError:
        quantity = None
    return _prune(
        {
            "resourceType": "Observation",
            "id": o.id,
            "extension": _exts(
                o,
                {
                    "value": o.value,
                    "unit": o.unit,
                    "recorded_at": _date(o.recorded_at),
                    "display": None if o.code else o.display,
                },
            ),
            "status": "final",
            "category": [{"coding": [{"system": OBS_CATEGORY, "code": o.category.value}]}],
            "code": (
                {"coding": [_prune({"system": LOINC, "code": o.code, "display": o.display})]}
                if o.code
                else {"text": o.display or "Observation"}
            ),
            "subject": _ref(o.patient_id),
            "encounter": _ref(o.encounter_id) if o.encounter_id else None,
            "effectiveDateTime": _date(o.effective_at),
            "valueQuantity": _prune(quantity) if quantity else None,
            "valueString": o.value if quantity is None and o.value is not None else None,
        }
    )


def _condition(c: Condition) -> dict[str, Any]:
    codings = [
        {"system": system, "code": code}
        for system, code in ((ICD10, c.icd10), (SNOMED, c.snomed))
        if code
    ]
    return _prune(
        {
            "resourceType": "Condition",
            "id": c.id,
            "extension": _exts(c, {"acuity": c.acuity}),
            "clinicalStatus": {
                "coding": [
                    {
                        "system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                        "code": "active" if c.active else "inactive",
                    }
                ]
            },
            "code": _prune({"coding": codings, "text": c.display}),
            "subject": _ref(c.patient_id),
            "onsetDateTime": _date(c.onset),
            "abatementDateTime": _date(c.stopped),
            "recordedDate": _date(c.recorded_at),
        }
    )


def _allergy(a: AllergyIntolerance) -> dict[str, Any]:
    severity = (a.severity or "").lower()
    reaction = None
    if a.reactions:
        reaction = [
            _prune(
                {
                    "manifestation": [{"text": r} for r in a.reactions],
                    "severity": severity if severity in _FHIR_SEVERITIES else None,
                }
            )
        ]
    fhir_category = _FHIR_ALLERGY_CATEGORY.get(a.category.value)
    return _prune(
        {
            "resourceType": "AllergyIntolerance",
            "id": a.id,
            "extension": _exts(
                a, {"category": a.category.value, "severity": a.severity, "reactions": a.reactions}
            ),
            "clinicalStatus": {
                "coding": [
                    {
                        "system": (
                            "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical"
                        ),
                        "code": "active" if a.active else "inactive",
                    }
                ]
            },
            "category": [fhir_category] if fhir_category else [],
            "code": {"text": a.substance} if a.substance else None,
            "patient": _ref(a.patient_id),
            "onsetDateTime": _date(a.onset),
            "reaction": reaction,
        }
    )


def _medication(m: MedicationStatement) -> dict[str, Any]:
    return _prune(
        {
            "resourceType": "MedicationStatement",
            "id": m.id,
            "extension": _exts(
                m,
                {
                    "generic_name": m.generic_name,
                    "brand_name": m.brand_name,
                    "strength": m.strength,
                    "route": m.route,
                    "dose_form": m.dose_form,
                    "rxnorm": m.rxnorm,
                    "display_name": m.display_name,
                    "associated_dx": m.associated_dx,
                    "last_modified_at": _date(m.last_modified_at),
                    "prescription_ids": m.prescription_ids,
                },
            ),
            "status": "active" if m.active else "stopped",
            "medicationCodeableConcept": {"text": m.display_name or "Unknown"},
            "subject": _ref(m.patient_id),
            "effectivePeriod": _prune({"start": _date(m.start), "end": _date(m.stop)}) or None,
            "dosage": [{"text": m.sig}] if m.sig else [],
        }
    )


def _prescription(rx: Prescription) -> dict[str, Any]:
    return _prune(
        {
            "resourceType": "MedicationRequest",
            "id": rx.id,
            "extension": _exts(
                rx,
                {
                    "prefix": rx.prefix,
                    "status_label": rx.status_label,
                    "refills": rx.refills,
                    "quantity": rx.quantity,
                    "medication_id": rx.medication_id,
                    "display_date": _date(rx.display_date),
                    "transactions": [t.model_dump(mode="json") for t in rx.transactions],
                },
            ),
            "status": "completed",
            "intent": "order",
            "medicationCodeableConcept": {"text": rx.sig or "Prescription"},
            "subject": _ref(rx.patient_id),
            "requester": _ref(rx.prescriber_id) if rx.prescriber_id else None,
            "authoredOn": _date(rx.display_date),
            "dosageInstruction": [{"text": rx.sig}] if rx.sig else [],
        }
    )


def _immunization(i: Immunization) -> dict[str, Any]:
    return _prune(
        {
            "resourceType": "Immunization",
            "id": i.id,
            "extension": _exts(i, {"source": i.source, "vaccine": i.vaccine}),
            "status": "completed",
            "vaccineCode": {"text": i.vaccine or "Unknown"},
            "patient": _ref(i.patient_id),
            "occurrenceDateTime": _date(i.administered_on),
            "occurrenceString": None if i.administered_on else "unknown",
            "lotNumber": i.lot_number,
            "expirationDate": _date(i.expires),
            "note": [{"text": i.comment}] if i.comment else [],
        }
    )


def _family_history(f: FamilyMemberHistory) -> dict[str, Any]:
    return _prune(
        {
            "resourceType": "FamilyMemberHistory",
            "id": f.id,
            "extension": _exts(
                f,
                {
                    "relation": f.relation,
                    "diagnosis": f.diagnosis,
                    "onset_date": _date(f.onset_date),
                },
            ),
            "status": "completed",
            "patient": _ref(f.patient_id),
            "relationship": {"text": f.relation or "unknown"},
            "condition": (
                [
                    _prune(
                        {
                            "code": {"text": f.diagnosis or "Unknown"},
                            "onsetString": _date(f.onset_date),
                        }
                    )
                ]
                if f.diagnosis or f.onset_date
                else []
            ),
        }
    )


def _coverage(c: Coverage) -> dict[str, Any]:
    return _prune(
        {
            "resourceType": "Coverage",
            "id": c.id,
            "extension": _exts(
                c,
                {
                    "payer": c.payer,
                    "order_of_benefits": c.order_of_benefits,
                    "plan_name": c.plan_name,
                    "plan_type": c.plan_type,
                    "coverage_type": c.coverage_type,
                    "group_number": c.group_number,
                    "priority_label": c.priority_label,
                    "employer": c.employer,
                    "relationship_to_insured": c.relationship_to_insured,
                    "payment_type": c.payment_type,
                    "copay": c.copay,
                    "status_label": c.status_label,
                },
            ),
            "status": "active" if c.active else "cancelled",
            "subscriberId": c.member_id,
            "beneficiary": _ref(c.patient_id),
            "order": None if c.order_of_benefits is None else c.order_of_benefits + 1,
            "payor": [{"display": c.payer or "Unknown"}],
            "period": _prune({"start": _date(c.start), "end": _date(c.end)}) or None,
        }
    )


def _practitioner(p: Practitioner) -> dict[str, Any]:
    return _prune(
        {
            "resourceType": "Practitioner",
            "id": p.id,
            "extension": _exts(p, {"credential": p.credential}),
            "identifier": [{"system": NPI, "value": p.npi}] if p.npi else [],
            "name": [
                _prune(
                    {
                        "text": p.display_name,
                        "given": [p.given_name] if p.given_name else [],
                        "family": p.family_name,
                    }
                )
            ],
        }
    )


def _location(f: Facility) -> dict[str, Any]:
    return _prune(
        {
            "resourceType": "Location",
            "id": f.id,
            "extension": _exts(f, {}),
            "name": f.name,
            "telecom": [
                {"system": system, "value": value}
                for system, value in (("phone", f.phone), ("fax", f.fax))
                if value
            ],
            "address": _prune(
                {
                    "line": [line for line in (f.address_line1, f.address_line2) if line],
                    "city": f.city,
                    "state": f.state,
                    "postalCode": f.postal_code,
                }
            )
            or None,
        }
    )


def _artifact(d: DocumentArtifact) -> dict[str, Any]:
    return _prune(
        {
            "resourceType": "DocumentReference",
            "id": d.id,
            "extension": _exts(
                d,
                {
                    "artifact": True,
                    "path": d.path,
                    "sha256": d.sha256,
                    "page_count": d.page_count,
                    "pack_name": d.pack_name,
                    "encounter_id": d.encounter_id,
                    "generated_at": _date(d.generated_at),
                },
            ),
            "status": "current",
            "type": {"text": d.title or "Document"},
            "subject": _ref(d.patient_id),
            "content": [{"attachment": _prune({"contentType": d.mime_type, "title": d.title})}],
        }
    )


def to_bundle(record: PatientRecord) -> dict[str, Any]:
    """Export one PatientRecord as a FHIR R4 Bundle (type=collection)."""
    resources: list[dict[str, Any]] = [_patient(record.patient, record)]
    resources += [_practitioner(p) for p in record.practitioners]
    resources += [_location(f) for f in record.facilities]
    for encounter in record.encounters:
        resources.append(_encounter(encounter))
        if encounter.sections or encounter.addenda:
            resources.append(_note_docref(encounter))
    resources += [_observation(o) for o in record.observations]
    resources += [_condition(c) for c in record.conditions]
    resources += [_allergy(a) for a in record.allergies]
    resources += [_medication(m) for m in record.medications]
    resources += [_prescription(rx) for rx in record.prescriptions]
    resources += [_immunization(i) for i in record.immunizations]
    resources += [_family_history(f) for f in record.family_history]
    resources += [_coverage(c) for c in record.coverages]
    resources += [_artifact(d) for d in record.documents]
    return {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [{"fullUrl": f"urn:anastomosis:{r['id']}", "resource": r} for r in resources],
    }
