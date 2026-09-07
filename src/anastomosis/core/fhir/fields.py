"""The one FHIR field table: what each canonical field is called on both sides.

Export walks it forward, ingest walks it backward, and the code systems below
are the single spelling `sources/fhir_r4` reads with.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from anastomosis.core.model import (
    Address,
    AdvanceDirective,
    AllergyCategory,
    AllergyIntolerance,
    AnastBase,
    Condition,
    Coverage,
    DocumentArtifact,
    Encounter,
    Facility,
    FamilyMemberHistory,
    Goal,
    Guarantor,
    Immunization,
    MedicationStatement,
    Observation,
    PastMedicalHistory,
    Patient,
    PatientContact,
    Practitioner,
    Prescription,
    PrescriptionTransaction,
)
from anastomosis.core.timeutil import iso_date, iso_datetime

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
#: Canonical allergy category → FHIR's own code. `sources/fhir_r4` reads it
#: inverted, so the pairing is stated once.
ALLERGY_CATEGORIES = {"drug": "medication", "food": "food", "environment": "environment"}


@dataclass(frozen=True)
class Codec:
    """Contract: one value's two crossings — ``out`` writes it into a bundle,
    ``back`` reads it out; ``back(out(v))`` is ``v`` for every ``v``."""

    out: Callable[[Any], Any]
    back: Callable[[Any], Any]


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


PLAIN = Codec(lambda v: v, lambda v: v)
DATE = Codec(_iso, iso_date)
DATETIME = Codec(_iso, iso_datetime)


def _one(cls: type[Any]) -> Codec:
    return Codec(
        lambda v: v.model_dump(mode="json") if v else None,
        lambda v: cls.model_validate(v) if v else None,
    )


def _many(cls: type[Any]) -> Codec:
    return Codec(
        lambda v: [m.model_dump(mode="json") for m in v] if v else None,
        lambda v: [cls.model_validate(m) for m in v],
    )


def _code(cls: type[Any]) -> Codec:
    return Codec(lambda v: v.value, cls)


@dataclass(frozen=True)
class Field:
    """Contract: one canonical model field riding the tail as
    ``urn:anastomosis:field:<name>``. ``value`` replaces the plain attribute
    read where the FHIR shape alone would read the value back into a neighbour,
    so the tail carries it in exactly that case; ``read`` is false only for a
    tail entry that is not a model field at all."""

    name: str
    codec: Codec = PLAIN
    value: Callable[[Any], Any] | None = None
    read: bool = True


PATIENT = (
    Field("sex"),
    Field("gender_identity"),
    Field("sexual_orientation"),
    Field("race"),
    Field("ethnicity"),
    Field("mothers_maiden_name"),
    # `name.given` is one ordered list: a middle-name-only patient would read
    # back as GIVEN "Quimby" without the tail holding it.
    Field(
        "middle_name",
        value=lambda p: p.middle_name if p.middle_name and not p.given_name else None,
    ),
    # `address.line` is ordered too: a line2-only address would read back as
    # street "Suite 400" without the list verbatim.
    Field(
        "addresses",
        _many(Address),
        lambda p: p.addresses if any(a.line2 and not a.line1 for a in p.addresses) else None,
    ),
    Field("contact_preference"),
    Field("status"),
    Field("notes"),
    Field("contacts", _many(PatientContact)),
    Field("guarantor", _one(Guarantor)),
)

ENCOUNTER = (
    Field("encounter_type"),
    Field("signed_by_id"),
    Field("signed_at", DATETIME),
    Field("last_modified_at", DATETIME),
)

OBSERVATION = (
    Field("value"),
    Field("unit"),
    Field("recorded_at", DATETIME),
    # A coded observation's display is `code.coding[0].display`; only an
    # uncoded one needs the tail to keep it off `code.text`.
    Field("display", value=lambda o: None if o.code else o.display),
)

CONDITION = (Field("acuity"),)

ALLERGY = (
    Field("category", _code(AllergyCategory)),
    Field("severity"),
    Field("reactions"),
)

MEDICATION = (
    Field("generic_name"),
    Field("brand_name"),
    Field("strength"),
    Field("route"),
    Field("dose_form"),
    Field("rxnorm"),
    Field("display_name"),
    Field("associated_dx"),
    Field("last_modified_at", DATETIME),
    Field("prescription_ids"),
)

PRESCRIPTION = (
    Field("prefix"),
    Field("status_label"),
    Field("refills"),
    Field("quantity"),
    Field("medication_id"),
    Field("display_date", DATETIME),
    Field("transactions", _many(PrescriptionTransaction)),
)

IMMUNIZATION = (Field("source"), Field("vaccine"))

FAMILY_HISTORY = (
    Field("relation"),
    Field("diagnosis"),
    Field("onset_date", DATE),
)

COVERAGE = (
    Field("payer"),
    Field("order_of_benefits"),
    Field("plan_name"),
    Field("plan_type"),
    Field("coverage_type"),
    Field("group_number"),
    Field("priority_label"),
    Field("employer"),
    Field("relationship_to_insured"),
    Field("payment_type"),
    Field("copay"),
    Field("status_label"),
)

ACTOR = (Field("credential"),)

FACILITY: tuple[Field, ...] = ()

ARTIFACT = (
    # Not a model field: the marker telling ingest this DocumentReference is a
    # carried artifact rather than an encounter's note.
    Field("artifact", value=lambda _d: True, read=False),
    Field("path"),
    Field("sha256"),
    Field("page_count"),
    Field("pack_name"),
    Field("encounter_id"),
    Field("generated_at", DATETIME),
)

#: Every tail table, by the canonical model it belongs to.
TABLES: tuple[tuple[type[AnastBase], tuple[Field, ...]], ...] = (
    (Patient, PATIENT),
    (Encounter, ENCOUNTER),
    (Observation, OBSERVATION),
    (Condition, CONDITION),
    (AllergyIntolerance, ALLERGY),
    (MedicationStatement, MEDICATION),
    (Prescription, PRESCRIPTION),
    (Immunization, IMMUNIZATION),
    (FamilyMemberHistory, FAMILY_HISTORY),
    (Coverage, COVERAGE),
    (Practitioner, ACTOR),
    (Facility, FACILITY),
    (DocumentArtifact, ARTIFACT),
)

#: Record-level lists FHIR has no resource for; they ride the Patient resource
#: under :data:`EXTRAS_NS`, in this order.
RECORD_EXTRAS: dict[str, type[AnastBase]] = {
    "past_medical_history": PastMedicalHistory,
    "advance_directives": AdvanceDirective,
    "goals": Goal,
}


def to_extensions(model: AnastBase, table: tuple[Field, ...]) -> list[dict[str, str]]:
    """The lossless tail: the source's own extensions, then every table field
    FHIR cannot hold."""
    out: list[dict[str, str]] = []
    if model.extensions:
        # default=str: a future adapter could stash a datetime/Decimal in
        # extensions, and json.dumps would otherwise raise and lose the record.
        blob = json.dumps(model.extensions, sort_keys=True, default=str)
        out.append({"url": EXT_NS, "valueString": blob})
    for field in table:
        raw = field.value(model) if field.value else getattr(model, field.name)
        value = field.codec.out(raw)
        if value is None or value == [] or value == {}:
            continue
        out.append({"url": FIELD_NS + field.name, "valueString": json.dumps(value, default=str)})
    return out


def split_extensions(resource: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """A resource's extension list split back into (source extensions, tail)."""
    source: dict[str, Any] = {}
    fields: dict[str, Any] = {}
    for ext in resource.get("extension", []):
        if ext["url"] == EXT_NS:
            source = json.loads(ext["valueString"])
        elif ext["url"].startswith(FIELD_NS):
            fields[ext["url"].removeprefix(FIELD_NS)] = json.loads(ext["valueString"])
    return source, fields


def from_extensions(fields: dict[str, Any], table: tuple[Field, ...]) -> dict[str, Any]:
    """Every table field the tail carried, converted back, as model kwargs. A
    field the tail omitted is absent here, so the model's own default stands."""
    return {f.name: f.codec.back(fields[f.name]) for f in table if f.read and f.name in fields}
