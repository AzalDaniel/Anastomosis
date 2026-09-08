"""PatientRecord → FHIR R4 Bundle (type=collection), plain JSON dicts."""

from __future__ import annotations

import base64
import json
import math
import uuid
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
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

from .fields import (
    ACTOR,
    ALLERGY,
    ALLERGY_CATEGORIES,
    ARTIFACT,
    CONDITION,
    COVERAGE,
    ENCOUNTER,
    EXT_NS,
    EXTRAS_NS,
    FACILITY,
    FAMILY_HISTORY,
    FIELD_NS,
    ICD10,
    IDENTIFIER_SYSTEMS,
    IMMUNIZATION,
    LOINC,
    MEDICATION,
    NPI,
    OBS_CATEGORY,
    OBSERVATION,
    PATIENT,
    PRESCRIPTION,
    RECORD_EXTRAS,
    SNOMED,
    TELECOM,
    to_extensions,
)

__all__ = ["EXT_NS", "FIELD_NS", "DeliveredAttachment", "FhirExportError", "to_bundle"]

_FHIR_GENDERS = {"male", "female", "other", "unknown"}
_FHIR_SEVERITIES = {"mild", "moderate", "severe"}


def _urn(resource_id: str) -> str:
    """Contract: the bundle-internal URN for a resource id, used for both its
    entry ``fullUrl`` and every reference to it. A UUID id gets ``urn:uuid:``
    (what a receiving FHIR server resolves); any other id shape keeps
    ``urn:anastomosis:<id>`` so ingest can still recover it, since
    ``urn:uuid:`` would be invalid for a non-UUID id.
    """
    try:
        parsed = uuid.UUID(resource_id)
    except (ValueError, AttributeError, TypeError):
        return f"urn:anastomosis:{resource_id}"
    # uuid.UUID also accepts braced/urn-prefixed/dash-less forms, but urn:uuid
    # needs a canonical 8-4-4-4-12 spelling; fall back otherwise (still round-trips).
    if str(parsed) != resource_id.lower():
        return f"urn:anastomosis:{resource_id}"
    return f"urn:uuid:{resource_id}"


def _ref(resource_id: str) -> dict[str, str]:
    return {"reference": _urn(resource_id)}


class FhirExportError(Exception):
    """A record cannot be expressed as a valid FHIR Bundle."""


@dataclass(frozen=True)
class DeliveredAttachment:
    """Contract: the FHIR ``Attachment`` fields for one
    :class:`~anastomosis.core.model.DocumentArtifact` a deliverer actually
    carried. ``size``/``sha256`` are measured off the delivered bytes, never
    copied from ``DocumentArtifact.sha256``, the source's own unverified claim.
    """

    url: str
    size: int
    sha256: str


#: FHIR-standard "should have a value and does not" extension, used on an
#: Attachment a deliverer named but did not carry — a receiving system's own
#: tooling already knows it, unlike a namespaced Anastomosis one.
_DATA_ABSENT_REASON_EXT = "http://hl7.org/fhir/StructureDefinition/data-absent-reason"


#: Fields :func:`_prune` leaves alone even when empty: a FHIR resource is
#: addressed by its id (every reference resolves through it), so an empty one
#: is a problem to refuse by name, not tidy away.
_STRUCTURAL = frozenset({"resourceType", "id"})


def _prune(resource: dict[str, Any]) -> dict[str, Any]:
    """Drop only the optional fields FHIR reads as absent. A required field
    (:data:`_STRUCTURAL`) stays even when empty, so the emptiness surfaces
    where it can be reported instead of vanishing into a resource that looks
    well-formed until something tries to address it.
    """
    return {k: v for k, v in resource.items() if k in _STRUCTURAL or v not in (None, "", [], {})}


def _date(value: Any) -> str | None:
    return value.isoformat() if value else None


# --- resources ---------------------------------------------------------------


def _patient(p: Patient, record: PatientRecord) -> dict[str, Any]:
    extras: dict[str, Any] = {
        name: [m.model_dump(mode="json") for m in getattr(record, name)]
        for name in RECORD_EXTRAS
        if getattr(record, name)
    }
    # Record id omitted deliberately: never a parse-time-minted value (RULES.md 8, #405).
    extras["__record__"] = {"extensions": record.extensions}
    extension = to_extensions(p, PATIENT)
    # `__record__` is set unconditionally above, so this never skips today; it
    # is kept so the extension is not written as a bare "{}" should that change.
    if extras:
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
            "extension": to_extensions(e, ENCOUNTER),
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
            "extension": to_extensions(o, OBSERVATION),
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
            "extension": to_extensions(c, CONDITION),
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
    fhir_category = ALLERGY_CATEGORIES.get(a.category.value)
    return _prune(
        {
            "resourceType": "AllergyIntolerance",
            "id": a.id,
            "extension": to_extensions(a, ALLERGY),
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
            "extension": to_extensions(m, MEDICATION),
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
            "extension": to_extensions(rx, PRESCRIPTION),
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
            "extension": to_extensions(i, IMMUNIZATION),
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
            "extension": to_extensions(f, FAMILY_HISTORY),
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
            "extension": to_extensions(c, COVERAGE),
            "status": "active" if c.active else "cancelled",
            "subscriberId": c.member_id,
            "beneficiary": _ref(c.patient_id),
            "order": None if c.order_of_benefits is None else c.order_of_benefits + 1,
            "payor": [{"display": c.payer or "Unknown"}],
            "period": _prune({"start": _date(c.start), "end": _date(c.end)}) or None,
        }
    )


#: CDA role classes for a personal relation (spouse, emergency contact) rather
#: than a clinician. Read from the source's own ``ccda:role`` classification,
#: so a role CDA adds later lands on the safe side by default.
_PERSONAL_ROLES = frozenset({"relatedEntity", "associatedEntity"})
#: The CDA entity class that is a machine rather than a person.
_DEVICE_ENTITY = "assignedAuthoringDevice"


def _actor(p: Practitioner, patient_id: str) -> dict[str, Any]:
    """Contract: one canonical Practitioner as the FHIR resource it actually
    is — a next of kin exported as ``Practitioner`` would misattribute them to
    the care team, so ``ccda:entity``/``ccda:role`` route to ``Device`` or
    ``RelatedPerson``; a record with neither stays a Practitioner.
    """
    if p.extensions.get("ccda:entity") == _DEVICE_ENTITY:
        return _device(p)
    if p.extensions.get("ccda:role") in _PERSONAL_ROLES:
        return _related_person(p, patient_id)
    return _practitioner(p)


def _human_name(p: Practitioner) -> list[dict[str, Any]]:
    return [
        _prune(
            {
                "text": p.display_name,
                "given": [p.given_name] if p.given_name else [],
                "family": p.family_name,
            }
        )
    ]


def _practitioner(p: Practitioner) -> dict[str, Any]:
    return _prune(
        {
            "resourceType": "Practitioner",
            "id": p.id,
            "extension": to_extensions(p, ACTOR),
            "identifier": [{"system": NPI, "value": p.npi}] if p.npi else [],
            "name": _human_name(p),
        }
    )


def _related_person(p: Practitioner, patient_id: str) -> dict[str, Any]:
    """A person related to the patient; ``patient`` says whose relative this
    is. The relationship travels as the document's own words — the code
    system behind it stays in the lossless tail rather than being guessed
    into a FHIR valueset this mapping cannot verify.
    """
    relationship = p.extensions.get("ccda:code")
    return _prune(
        {
            "resourceType": "RelatedPerson",
            "id": p.id,
            "extension": to_extensions(p, ACTOR),
            "identifier": [{"system": NPI, "value": p.npi}] if p.npi else [],
            "patient": _ref(patient_id),
            "relationship": [{"text": relationship}] if isinstance(relationship, str) else [],
            "name": _human_name(p),
        }
    )


def _device(p: Practitioner) -> dict[str, Any]:
    """The system that generated a document. ``type: other``: FHIR's device-
    nametype list has no entry for a CDA ``softwareName``, and guessing the
    nearest one would state something the document did not. Both CDA element
    names survive verbatim in the lossless tail.
    """
    return _prune(
        {
            "resourceType": "Device",
            "id": p.id,
            "extension": to_extensions(p, ACTOR),
            "deviceName": ([{"name": p.display_name, "type": "other"}] if p.display_name else []),
        }
    )


def _location(f: Facility) -> dict[str, Any]:
    return _prune(
        {
            "resourceType": "Location",
            "id": f.id,
            "extension": to_extensions(f, FACILITY),
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


def _artifact(
    d: DocumentArtifact, attachments: Mapping[str, DeliveredAttachment] | None
) -> dict[str, Any]:
    # ``attachments=None``: no deliverer in the loop, so the Attachment
    # carries only what the record knows. A mapping asserts completeness, so
    # a missing entry gets a loud data-absent reason, not a silent null —
    # refusal is the run's job (``pipeline._carry_attachments``), not this
    # lower-level primitive's.
    landed = attachments.get(d.id) if attachments is not None else None
    attachment: dict[str, Any] = {"contentType": d.mime_type, "title": d.title}
    if landed is not None:
        attachment["url"] = landed.url
        attachment["size"] = landed.size
        # R4's Attachment.hash is base64Binary, not the hex spelling this
        # toolkit's other digests use — encoded here, the one FHIR crossing.
        attachment["hash"] = base64.b64encode(bytes.fromhex(landed.sha256)).decode("ascii")
    elif attachments is not None and d.path:
        attachment["extension"] = [{"url": _DATA_ABSENT_REASON_EXT, "valueCode": "error"}]
    return _prune(
        {
            "resourceType": "DocumentReference",
            "id": d.id,
            "extension": to_extensions(d, ARTIFACT),
            "status": "current",
            "type": {"text": d.title or "Document"},
            "subject": _ref(d.patient_id),
            "content": [{"attachment": _prune(attachment)}],
        }
    )


def _entries(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One bundle entry per resource; refuses (naming type and count only,
    never a value — RULES.md 2) any resource with no id, since nothing in
    the bundle could reference it.
    """
    missing = Counter(
        str(r.get("resourceType") or "(untyped)") for r in resources if not r.get("id")
    )
    if missing:
        breakdown = ", ".join(f"{count} {name}" for name, count in sorted(missing.items()))
        raise FhirExportError(
            f"{sum(missing.values())} of {len(resources)} resources have no id and cannot go "
            f"in a bundle ({breakdown}). A FHIR resource is addressed by its id, so one without "
            "an id would be delivered attached to nothing. A source row reached the mapper "
            "carrying no identifier."
        )
    return [{"fullUrl": _urn(r["id"]), "resource": r} for r in resources]


def to_bundle(
    record: PatientRecord, attachments: Mapping[str, DeliveredAttachment] | None = None
) -> dict[str, Any]:
    """Contract: export one PatientRecord as a FHIR R4 Bundle (type=collection).
    ``attachments`` is what a deliverer measured off the files it carried (see
    :class:`DeliveredAttachment`); left ``None``, an Attachment carries only
    what the record knows. A caller passing a mapping asserts completeness."""
    resources: list[dict[str, Any]] = [_patient(record.patient, record)]
    resources += [_actor(p, record.patient.id) for p in record.practitioners]
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
    resources += [_artifact(d, attachments) for d in record.documents]
    return {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": _entries(resources),
    }
