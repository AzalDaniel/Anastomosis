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
  cannot abort a whole patient. (The adapter raises only on the two structural
  failures that would otherwise lose data silently: a bundle with no Patient at
  all, and DANGLING resources it cannot attribute — see
  :class:`AmbiguousUnanchoredError`. A resource with no patient reference at
  all is not dangling: it is bundle-level and rides ``fhir_r4:shared``.)
"""

from __future__ import annotations

import base64
from collections import Counter, defaultdict
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
from anastomosis.sources.base import SourceDataError

__all__ = ["AmbiguousUnanchoredError", "records_from_resources"]

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

# Resource types that are never patient-scoped: the record's own Patient, and
# the reference resources records attach by id (:func:`_practitioner`,
# :func:`_facility`). They are grouped separately, not by a patient reference.
_SHARED_TYPES = frozenset({"Patient", "Practitioner", "Location", "Organization"})

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


def _strip_version(text: str) -> str:
    """A reference with a trailing ``/_history/<version>`` removed.

    ``Patient/x/_history/2`` is a version-specific reference to the SAME logical
    resource as ``Patient/x``; taking the last path segment without this would
    read the version number as the id (and so lose the join).
    """
    head, sep, _version = text.rpartition("/_history/")
    return head if sep and head else text


def _ref_id(ref: Any) -> str | None:
    """The bare id from a FHIR reference dict (``{"reference": "Patient/x"}``).

    Strips a ``ResourceType/`` prefix, a ``urn:uuid:`` prefix, a full URL, or a
    ``/_history/<version>`` suffix, leaving the logical id used to join
    resources within the bundle. A reference carrying only a logical
    ``identifier`` (no ``reference`` string) has no bundle-local id at all and
    yields None — it is resolved against Patient.identifier separately, by
    :func:`_resolve_patient`.
    """
    if not isinstance(ref, dict):
        return None
    value = ref.get("reference")
    if not value:
        return None
    text = _strip_version(str(value))
    if text.startswith("urn:uuid:"):
        return text[len("urn:uuid:") :]
    return text.rsplit("/", 1)[-1] if "/" in text else text


def _ref_type(text: str) -> str | None:
    """The ``ResourceType`` a reference string names, when it names one.

    ``Patient/x`` and ``http://ex.org/fhir/Patient/x`` → ``"Patient"``;
    ``urn:uuid:x`` and a bare id → None (untyped, so the target type is unknown).
    """
    base = _strip_version(text)
    if "/" not in base:
        return None
    prefix = base.rsplit("/", 2)[-2]
    return prefix if prefix.isascii() and prefix.isalpha() else None


def _patient_ref(resource: dict[str, Any]) -> dict[str, Any] | None:
    """The reference NODE this resource hangs off, across the US Core patient
    reference fields — or None when the resource names no patient at all.

    Returns the node rather than an id because the two failure modes downstream
    are different: a node whose target is missing is a DANGLING reference (the
    resource belongs to somebody the data does not contain), while no node at
    all means the resource is bundle-level (a PractitionerRole, a Provenance, a
    Medication). :func:`_resolve_patient` tells them apart.
    """
    for field in ("patient", "subject", "beneficiary"):
        node = resource.get(field)
        if isinstance(node, dict) and (node.get("reference") or node.get("identifier")):
            return node
    return None


def _patient_identifier_index(
    patients: list[dict[str, Any]],
) -> dict[tuple[str | None, str], str | None]:
    """``(system, value) -> patient id`` over every Patient.identifier.

    Each identifier is indexed twice — under its own system, for a reference
    that names one, and under a system-less key, for a reference that gives only
    a value. A key two patients share maps to None: an ambiguous identifier
    anchors nothing, because guessing between two patients is exactly the
    misattribution this module refuses.
    """
    index: dict[tuple[str | None, str], str | None] = {}
    for patient in patients:
        pid = str(patient["id"])
        for ident in patient.get("identifier", []):
            if not isinstance(ident, dict) or not ident.get("value"):
                continue
            value = str(ident["value"])
            system = ident.get("system")
            keys: list[tuple[str | None, str]] = [(None, value)]
            if system:
                keys.append((str(system), value))
            for key in keys:
                if key in index and index[key] != pid:
                    index[key] = None  # two patients claim it — ambiguous
                else:
                    index.setdefault(key, pid)
    return index


def _resolve_patient(
    node: dict[str, Any] | None,
    patient_ids: frozenset[str],
    by_identifier: dict[tuple[str | None, str], str | None],
) -> tuple[str | None, bool]:
    """Resolve one patient-reference node to ``(patient id, dangling)``.

    Three outcomes, and the third is why this returns a pair:

    * ``(pid, False)`` — the node names a patient present in the data.
    * ``(None, True)`` — the node names a PATIENT-shaped target that is absent.
      The resource belongs to somebody who is not here: a dangling reference.
    * ``(None, False)`` — the node anchors nobody the data can name, and does
      not claim to: no node at all, a typed reference to a non-patient subject
      (a Group, a Device), or a logical ``identifier`` matching no (or several)
      Patient.identifier. These are bundle-level, not dangling, so they never
      block a load.
    """
    if node is None:
        return None, False
    text = node.get("reference")
    if text:
        rid = _ref_id(node)
        if rid is not None and rid in patient_ids:
            return rid, False
        ref_type = _ref_type(str(text))
        return None, ref_type is None or ref_type == "Patient"
    ident = node.get("identifier")
    if isinstance(ident, dict) and ident.get("value"):
        # A reference that names a system must match THAT system: two assigning
        # authorities can issue the same value, so matching across them would be
        # a guess. A reference with no system matches on the value alone.
        value = str(ident["value"])
        system = ident.get("system")
        return by_identifier.get((str(system), value) if system else (None, value)), False
    return None, False


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
    """Every resource element the builder did not consume, namespaced.

    The per-field half of the lossless guarantee (mirrors pf_tebra's ``_ext``):
    a FHIR element the mapper does not lift into a typed slot is preserved
    verbatim under ``fhir_r4:<element>`` rather than dropped. This matters most
    for status/verification fields a vendor may set — ``Condition.
    verificationStatus``, ``Observation.status``, ``AllergyIntolerance.
    criticality`` — whose loss would silently *reverse* a record's clinical
    meaning (a refuted diagnosis migrating as active, a retracted value as real).

    ``consumed`` names whole elements (``"birthDate"``) and/or exact sub-paths
    inside one (``"name[0].family"`` — the C-CDA exporter's relative-path
    convention, at the index the mapper actually read). An element named only by
    sub-paths is **partially** consumed: what the mapper read is pruned out and
    the rest is preserved, so a sibling the mapper never touched
    (``name[0].prefix``, a vendor's own ``extension`` entry) cannot ride out on
    the coat-tails of one it did.
    """
    skip = consumed | _STRUCTURAL
    out: dict[str, Any] = {}
    for key, value in resource.items():
        if key in skip:
            continue
        if any(path.startswith((f"{key}.", f"{key}[")) for path in consumed):
            residue = _unconsumed(value, key, consumed)
            if residue is not None:
                out[f"{_EXT}{key}"] = residue
            continue
        out[f"{_EXT}{key}"] = value
    return out


def _unconsumed(value: Any, path: str, consumed: frozenset[str]) -> Any:
    """``value`` with every consumed sub-path pruned out, or None if none is left.

    Empty containers prune away with it (sentinel discipline: a fully-consumed
    element yields None, never an empty placeholder).
    """
    if value is None or path in consumed:
        return None
    if isinstance(value, dict):
        kept = {
            key: sub
            for key, item in value.items()
            if (sub := _unconsumed(item, f"{path}.{key}", consumed)) is not None
        }
        return kept or None
    if isinstance(value, list):
        items = [
            sub
            for index, item in enumerate(value)
            if (sub := _unconsumed(item, f"{path}[{index}]", consumed)) is not None
        ]
        return items or None
    return value


# --- resource → canonical -----------------------------------------------------


def _us_core_url(suffix: str) -> str:
    return f"http://hl7.org/fhir/us/core/StructureDefinition/us-core-{suffix}"


def _race_ethnicity(resource: dict[str, Any], suffix: str) -> tuple[list[str], set[str]]:
    """The US Core race/ethnicity displays, plus the sub-paths this lift READ.

    Returns ``(values, consumed)``. ``consumed`` names only what was actually
    read — the wrapper's ``url`` (the discriminator this matched on) and, inside
    it, either the whole ``text`` sub-extension or the ``url`` +
    ``valueCoding.display`` of each ombCategory that supplied a value. Every
    sibling the lift did not read (the ombCategory codings behind a ``text`` the
    lift preferred, a ``detailed`` sub-extension, the ``valueCoding.code``
    itself) stays unconsumed and rides ``fhir_r4:extension``.

    A lift that yields NOTHING consumes nothing — a vendor-shaped entry carrying
    the race url with a plain ``valueString`` and no sub-extensions is preserved
    whole rather than marked read and dropped.
    """
    url = _us_core_url(suffix)
    for index, ext in enumerate(resource.get("extension", [])):
        if not isinstance(ext, dict) or ext.get("url") != url:
            continue
        base = f"extension[{index}]"
        values: list[str] = []
        consumed: set[str] = set()
        for sub_index, sub in enumerate(ext.get("extension", [])):
            if not isinstance(sub, dict):
                continue
            sub_path = f"{base}.extension[{sub_index}]"
            if sub.get("url") == "text" and sub.get("valueString"):
                # url + valueString are both read: the sub-entry is fully consumed.
                return [str(sub["valueString"])], {f"{base}.url", sub_path}
            if sub.get("url") == "ombCategory":
                display = (sub.get("valueCoding") or {}).get("display")
                if display:
                    values.append(str(display))
                    consumed |= {f"{sub_path}.url", f"{sub_path}.valueCoding.display"}
        return (values, consumed | {f"{base}.url"}) if values else ([], set())
    return [], set()


def _named(entry: Any) -> bool:
    """Whether a HumanName entry actually carries a name to migrate.

    A leading ``{}`` (or a use-only) entry is a placeholder some exports emit;
    selecting it would migrate a NAMELESS patient while the real name sat one
    index further on — nothing lost, but every typed slot empty.
    """
    return isinstance(entry, dict) and bool(
        entry.get("family") or any(g for g in entry.get("given", []) if g)
    )


def _patient(resource: dict[str, Any], source_file: str | None) -> Patient:
    names = resource.get("name", [])
    # First entry that carries a name; failing that, the first dict entry at all
    # (a text-only or use-only HumanName still contributes its sub-fields).
    name_index = next(
        (i for i, n in enumerate(names) if _named(n)),
        next((i for i, n in enumerate(names) if isinstance(n, dict)), None),
    )
    name = names[name_index] if name_index is not None else {}
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
    race, race_paths = _race_ethnicity(resource, "race")
    ethnicity, ethnicity_paths = _race_ethnicity(resource, "ethnicity")
    # `name` and `extension` are consumed only in PART: the sub-paths below are
    # what this function reads, and _residual preserves the rest (HumanName.use/
    # prefix/period, a second name, a non-US-Core extension, the race codings the
    # lift did not read). The name sub-paths follow the SELECTED entry's index,
    # and the given/suffix indices are RAW list positions while the reads above
    # filter empty entries; a degenerate empty entry can therefore only duplicate
    # a value into the residue, never drop one.
    consumed = {
        "birthDate",
        "gender",
        "maritalStatus",
        "communication",
        "identifier",
        "telecom",
        "address",
        *race_paths,
        *ethnicity_paths,
    }
    if name_index is not None:
        consumed |= {
            f"name[{name_index}].{sub}" for sub in ("family", "suffix[0]", "given[0]", "given[1]")
        }
    return Patient(
        id=resource["id"],
        given_name=given[0] if given else None,
        middle_name=given[1] if len(given) > 1 else None,
        family_name=name.get("family"),
        suffix=(name.get("suffix") or [None])[0],
        birth_date=_date(resource.get("birthDate")),
        sex=resource.get("gender"),
        race=race,
        ethnicity=ethnicity,
        language=language,
        marital_status=_concept_text(resource.get("maritalStatus")),
        identifiers=identifiers,
        telecom=telecom,
        addresses=addresses,
        extensions=_residual(resource, frozenset(consumed)),
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
    patient_id: str,
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
        patient_id=patient_id,
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


def _observations(
    resource: dict[str, Any], patient_id: str, source_file: str | None
) -> list[Observation]:
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


def _condition(resource: dict[str, Any], patient_id: str, source_file: str | None) -> Condition:
    code = resource.get("code", {})
    return Condition(
        id=resource["id"],
        patient_id=patient_id,
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


def _medication(
    resource: dict[str, Any], patient_id: str, source_file: str | None
) -> MedicationStatement:
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
        patient_id=patient_id,
        display_name=_concept_text(concept),
        rxnorm=_code_in(concept, _RXNORM),
        sig=dosage[0].get("text") if dosage and isinstance(dosage[0], dict) else None,
        start=start,
        stop=_date(period.get("end")),
        active=resource.get("status") in ("active", "completed"),
        extensions=extensions,
        provenance=_prov(source_file, resource["id"]),
    )


def _allergy(
    resource: dict[str, Any], patient_id: str, source_file: str | None
) -> AllergyIntolerance:
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
        patient_id=patient_id,
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


def _immunization(
    resource: dict[str, Any], patient_id: str, source_file: str | None
) -> Immunization:
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
        patient_id=patient_id,
        vaccine=_concept_text(resource.get("vaccineCode")),
        administered_on=_date(resource.get("occurrenceDateTime")),
        lot_number=resource.get("lotNumber"),
        expires=_date(resource.get("expirationDate")),
        comment=notes[0].get("text") if notes and isinstance(notes[0], dict) else None,
        extensions=extensions,
        provenance=_prov(source_file, resource["id"]),
    )


def _coverage(resource: dict[str, Any], patient_id: str, source_file: str | None) -> Coverage:
    payors = resource.get("payor") or []
    period = resource.get("period") or {}
    classes = {
        (_codings(c.get("type")) or [{}])[0].get("code"): c for c in resource.get("class", [])
    }
    order = resource.get("order")
    return Coverage(
        id=resource["id"],
        patient_id=patient_id,
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


def _goal(resource: dict[str, Any], patient_id: str, source_file: str | None) -> Goal:
    return Goal(
        id=resource["id"],
        patient_id=patient_id,
        description=(resource.get("description") or {}).get("text"),
        effective=_date(resource.get("startDate")),
        active=resource.get("lifecycleStatus") in ("active", "accepted", "in-progress"),
        extensions=_residual(resource, frozenset({"description", "startDate", "lifecycleStatus"})),
        provenance=_prov(source_file, resource["id"]),
    )


def _family_history(
    resource: dict[str, Any], patient_id: str, source_file: str | None
) -> FamilyMemberHistory:
    condition = next((c for c in resource.get("condition", []) if isinstance(c, dict)), {})
    extensions: dict[str, Any] = _residual(resource, frozenset({"relationship", "condition"}))
    if condition.get("onsetString"):
        extensions[f"{_EXT}onset_string"] = condition["onsetString"]
    return FamilyMemberHistory(
        id=resource["id"],
        patient_id=patient_id,
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


def _artifact(
    docref: dict[str, Any], patient_id: str, source_file: str | None
) -> DocumentArtifact | None:
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
            patient_id=patient_id,
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


def _safe_resource_type(value: Any) -> str:
    """A ``resourceType`` in the shape that is safe to echo to an operator.

    A resourceType is schema, not patient data — but it arrives from a file this
    module does not author, and anything at all can sit in that slot. Only a
    plain FHIR type name is echoed; every other shape reads as ``"unknown"``, so
    a crafted export cannot smuggle patient text into a message or a log line.
    """
    text = str(value)
    return text if text.isascii() and text.isalpha() and len(text) <= 64 else "unknown"


class AmbiguousUnanchoredError(SourceDataError, ValueError):
    """Resources reference a patient the data does not contain, and the data
    describes more than one patient.

    Attaching them to an arbitrary record would misattribute one patient's data
    to another; dropping them would breach the lossless guarantee. Neither is
    acceptable, so the load is refused. The message names the resource TYPES and
    their counts — schema names and counts, never a resource id or any
    patient-derived value (:func:`_safe_resource_type` holds that line even for
    a resourceType a vendor filled with something else).
    """

    def __init__(self, counts: dict[str, int]) -> None:
        safe: Counter[str] = Counter()
        for rtype, count in counts.items():
            safe[_safe_resource_type(rtype)] += count
        self.counts = dict(sorted(safe.items()))
        detail = ", ".join(f"{rtype} ({count})" for rtype, count in self.counts.items())
        super().__init__(
            f"{sum(self.counts.values())} resource(s) reference a patient that is not "
            f"in the data, and the data holds several patients, so they cannot be "
            f"attributed to one without guessing: {detail}. Load one patient at a "
            "time, or include the referenced Patient resource(s)."
        )


def records_from_resources(
    resources: list[dict[str, Any]], *, source_file: str | None = None
) -> Iterator[PatientRecord]:
    """Group flat FHIR resources into one :class:`PatientRecord` per Patient.

    Accepts the resources from a Bundle's ``entry[].resource`` or the lines of a
    Bulk-Data ``$export`` NDJSON set (already parsed). Yields one record per
    Patient, in the order the patients appear. Raises :class:`ValueError` only
    when there is no Patient at all (the loud structural failure the lossless
    guarantee requires); a per-resource oddity is tolerated, not fatal.

    A resource the grouping cannot attach to a record goes to one of two homes,
    and telling them apart is the difference between a refusal and an ordinary
    load (:func:`_resolve_patient`):

    * **Dangling** — the resource names a patient that is NOT in the data (a
      broken reference, or an out-of-scope ``$export`` slice). It belongs to
      somebody. With a single patient it is preserved under that record's
      ``extensions["fhir_r4:unanchored"]``; with several there is no record it
      can be attributed to without guessing, so the load is refused
      (:class:`AmbiguousUnanchoredError`) — omitting it would be a silent drop,
      attaching it would misattribute one patient's data to another.
    * **Patient-less** — the resource names no patient at all (PractitionerRole,
      Provenance, Medication, a Group-subject Observation). It is bundle-level,
      exactly like the Practitioner/Location/Organization resources records
      attach by reference, so it is preserved under
      ``extensions["fhir_r4:shared"]`` on EVERY record — no attribution is
      claimed by that key, nothing is dropped, and an ordinary multi-patient
      bundle is never refused over it.
    """
    patients = [r for r in resources if r.get("resourceType") == "Patient" and r.get("id")]
    if not patients:
        raise ValueError("no Patient resource found in the FHIR data")
    patient_ids = frozenset(str(p["id"]) for p in patients)
    by_identifier = _patient_identifier_index(patients)

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
    dangling: list[dict[str, Any]] = []
    shared: list[dict[str, Any]] = []

    for resource in resources:
        rtype = resource.get("resourceType")
        if rtype in _SHARED_TYPES or not rtype:
            continue
        pid, is_dangling = _resolve_patient(_patient_ref(resource), patient_ids, by_identifier)
        if pid is None:
            # A reference to a patient the data lacks belongs to somebody (and
            # may block the load); no patient reference at all is bundle-level.
            (dangling if is_dangling else shared).append(resource)
            continue
        if rtype == "DocumentReference":
            docrefs_by_patient[pid].append(resource)
            continue
        by_patient[pid][rtype].append(resource)

    # Dangling resources are preserved only when there is exactly one patient to
    # attribute them to (see the docstring); with several, neither attaching nor
    # omitting them is safe, so the load is refused. Bundle-level resources are
    # never part of that decision — they claim no patient in the first place.
    sole_patient = patients[0]["id"] if len(patients) == 1 else None
    if dangling and sole_patient is None:
        raise AmbiguousUnanchoredError(dict(Counter(str(r.get("resourceType")) for r in dangling)))
    for patient_res in patients:
        pid = patient_res["id"]
        yield _assemble(
            patient_res,
            by_patient.get(pid, {}),
            docrefs_by_patient.get(pid, []),
            practitioners,
            facilities,
            source_file,
            dangling if pid == sole_patient else [],
            shared,
        )


def _assemble(
    patient_res: dict[str, Any],
    grp: dict[str, list[dict[str, Any]]],
    docrefs: list[dict[str, Any]],
    practitioners: dict[str, dict[str, Any]],
    facilities: dict[str, dict[str, Any]],
    source_file: str | None,
    unanchored: list[dict[str, Any]],
    shared: list[dict[str, Any]],
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
            artifact = _artifact(docref, patient_id, source_file)
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
        _medication(r, patient_id, source_file)
        for rtype in ("MedicationRequest", "MedicationStatement")
        for r in grp.get(rtype, [])
    ]
    observations: list[Observation] = []
    for r in grp.get("Observation", []):
        observations.extend(_observations(r, patient_id, source_file))
    encounters = [
        _encounter(r, patient_id, notes_for_enc, source_file) for r in grp.get("Encounter", [])
    ]
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
    # Bundle-level resources that name no patient: preserved on every record
    # under a key that claims no attribution (see records_from_resources).
    if shared:
        record_ext[f"{_EXT}shared"] = shared

    return PatientRecord(
        patient=_patient(patient_res, source_file),
        encounters=encounters,
        observations=observations,
        conditions=[_condition(r, patient_id, source_file) for r in grp.get("Condition", [])],
        allergies=[_allergy(r, patient_id, source_file) for r in grp.get("AllergyIntolerance", [])],
        medications=meds,
        immunizations=[
            _immunization(r, patient_id, source_file) for r in grp.get("Immunization", [])
        ],
        family_history=[
            _family_history(r, patient_id, source_file) for r in grp.get("FamilyMemberHistory", [])
        ],
        goals=[_goal(r, patient_id, source_file) for r in grp.get("Goal", [])],
        coverages=[_coverage(r, patient_id, source_file) for r in grp.get("Coverage", [])],
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
