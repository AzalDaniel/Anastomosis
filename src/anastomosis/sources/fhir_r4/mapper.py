"""US Core R4 resources → canonical :class:`PatientRecord` objects.

Maps *standard* FHIR R4 / US Core resources — not the inverse of
:func:`anastomosis.core.fhir.ingest.from_bundle`, which reads
``urn:anastomosis:*`` extensions arbitrary vendors don't emit. Lossless:
an unlifted field rides ``fhir_r4:``-namespaced ``extensions`` (rule 63).
Deterministic: input order drives output order. Defensive: every accessor
tolerates a missing/malformed field, except two structural failures — no
Patient at all, and a dangling, unattributable resource
(:class:`AmbiguousUnanchoredError`)."""

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
    ``Patient/x/_history/2`` names the SAME logical resource as
    ``Patient/x``; taking the last path segment without this would read
    the version number as the id and lose the join."""
    head, sep, _version = text.rpartition("/_history/")
    return head if sep and head else text


def _ref_id(ref: Any) -> str | None:
    """The bare id from a FHIR reference dict (``{"reference": "Patient/x"}``).
    Strips a ``ResourceType/`` prefix, ``urn:uuid:``, a full URL, or a
    ``/_history/<version>`` suffix. A reference with only a logical
    ``identifier`` (no ``reference`` string) yields ``None`` — resolved
    against ``Patient.identifier`` separately, by :func:`_resolve_patient`."""
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
    """The reference NODE this resource hangs off (US Core patient
    reference fields), or ``None`` when it names no patient at all. A node
    (not an id) because the two failure modes differ downstream: a missing
    target is DANGLING, no node at all is bundle-level;
    :func:`_resolve_patient` tells them apart."""
    for field in ("patient", "subject", "beneficiary"):
        node = resource.get(field)
        if isinstance(node, dict) and (node.get("reference") or node.get("identifier")):
            return node
    return None


def _patient_identifier_index(
    patients: list[dict[str, Any]],
) -> dict[tuple[str | None, str], str | None]:
    """``(system, value) -> patient id`` over every Patient.identifier.
    Indexed twice per identifier (its own system, and system-less) so a
    reference giving only a value still resolves. A key two patients share
    maps to ``None``: ambiguous, so it anchors nobody rather than guess."""
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
    """Resolve one patient-reference node to ``(patient id, dangling)``:
    ``(pid, False)`` when the node names a present patient; ``(None,
    True)`` when it names a PATIENT-shaped target that is absent
    (dangling); ``(None, False)`` when it anchors nobody and does not
    claim to (no node, a non-patient subject, or an unresolved identifier)."""
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
        # A reference naming a system must match THAT system (two assigning
        # authorities can share a value); one with no system matches on value alone.
        value = str(ident["value"])
        system = ident.get("system")
        return by_identifier.get((str(system), value) if system else (None, value)), False
    return None, False


def _indexed_codings(concept: Any) -> list[tuple[int, dict[str, Any]]]:
    """Every ``coding`` entry of a CodeableConcept with its RAW list
    position. A consumed sub-path must name the element a read actually
    came from, and a dict-filtered view renumbers past a non-dict entry —
    so the position travels with the coding, not recovered by counting."""
    if not isinstance(concept, dict):
        return []
    return [(i, c) for i, c in enumerate(concept.get("coding", [])) if isinstance(c, dict)]


def _codings(concept: Any) -> list[dict[str, Any]]:
    return [coding for _index, coding in _indexed_codings(concept)]


def _first_coding(concept: Any) -> tuple[int | None, dict[str, Any]]:
    """The first ``coding`` with its raw index; ``(None, {})`` when there is none."""
    codings = _indexed_codings(concept)
    return codings[0] if codings else (None, {})


def _code_in_consumed(
    concept: Any, systems: tuple[str, ...], base: str
) -> tuple[str | None, set[str]]:
    """``(code, sub-paths read)`` for :func:`_code_in`, rooted at ``base``.
    Only the MATCHED coding's ``system`` and ``code`` are consumed; codings
    scanned past, and the matched coding's own ``display``/``version``,
    stay in the residue. A concept matching nothing consumes nothing."""
    for index, coding in _indexed_codings(concept):
        if coding.get("system") in systems and coding.get("code"):
            return str(coding["code"]), {
                f"{base}.coding[{index}].system",
                f"{base}.coding[{index}].code",
            }
    return None, set()


def _code_in(concept: Any, systems: tuple[str, ...]) -> str | None:
    """The first ``code`` whose ``system`` is one of ``systems`` (None if absent)."""
    return _code_in_consumed(concept, systems, "")[0]


def _concept_text_consumed(concept: Any, base: str) -> tuple[str | None, set[str]]:
    """``(text, sub-paths read)`` for :func:`_concept_text`, rooted at
    ``base``: ``{base}.text`` when ``text`` supplied the label, else
    ``{base}.coding[i].display`` of the one coding that did — never the
    codings behind a preferred ``text``, never the code beside a display."""
    if not isinstance(concept, dict):
        return None, set()
    if concept.get("text"):
        return str(concept["text"]), {f"{base}.text"}
    for index, coding in _indexed_codings(concept):
        if coding.get("display"):
            return str(coding["display"]), {f"{base}.coding[{index}].display"}
    return None, set()


def _concept_text(concept: Any) -> str | None:
    """Human label of a CodeableConcept: ``text`` first, else a coding display."""
    return _concept_text_consumed(concept, "")[0]


def _ref_id_consumed(ref: Any, base: str) -> tuple[str | None, set[str]]:
    """``(id, sub-paths read)`` for :func:`_ref_id`: ``{base}.reference`` alone.

    A Reference's ``display``, ``type`` and logical ``identifier`` are siblings
    the id lift never reads; a reference that yields no id consumes nothing.
    """
    rid = _ref_id(ref)
    return rid, {f"{base}.reference"} if rid is not None else set()


def _status_active_consumed(resource: dict[str, Any], field: str) -> tuple[bool, set[str]]:
    """``(active, sub-paths read)`` for a clinical-status CodeableConcept.
    Only a coding that SAYS active is consumed; ``False`` means this lift
    found no such coding, so whatever code is actually there stays in the
    residue — dropping it would silently reverse clinical meaning."""
    concept = resource.get(field)
    for index, coding in _indexed_codings(concept):
        if coding.get("code") == "active":
            return True, {f"{field}.coding[{index}].code"}
    return False, set()


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
    """Every resource element the builder did not consume, namespaced
    ``fhir_r4:<element>`` (mirrors pf_tebra's ``_ext``) — matters most for
    status/verification fields whose loss would reverse clinical meaning.
    ``consumed`` names whole elements or exact sub-paths; a sub-path-only
    element is partially consumed, so an untouched sibling never rides out."""
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
    """US Core race/ethnicity displays, plus the sub-paths READ:
    ``(values, consumed)``. ``consumed`` names only the wrapper ``url`` and,
    inside it, the whole ``text`` or each contributing ombCategory's
    ``url``+``valueCoding.display`` — never a sibling the lift didn't read.
    A lift yielding nothing consumes nothing."""
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
    """Whether a HumanName entry actually carries a name to migrate. A
    leading ``{}`` (or a use-only) entry is a placeholder some exports
    emit; selecting it would migrate a nameless patient while the real
    name sat one index further on."""
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
    # `name`/`extension` are consumed only in PART; _residual preserves what
    # these sub-paths miss. Indices are RAW list positions while the reads
    # above filter empty entries, so a degenerate entry only duplicates into
    # the residue, never drops a value.
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
    names = resource.get("name", [])
    name_index = next((i for i, n in enumerate(names) if isinstance(n, dict)), None)
    name: dict[str, Any] = names[name_index] if name_index is not None else {}
    given = [g for g in name.get("given", []) if g]
    # `name`/`identifier` are consumed only in PART — a second name entry,
    # extra given names, and every non-NPI identifier stay in the residue.
    consumed: set[str] = set()
    if name_index is not None:
        consumed |= {f"name[{name_index}].family", f"name[{name_index}].text"}
        given_index = next((i for i, g in enumerate(name.get("given", [])) if g), None)
        if given_index is not None:
            consumed.add(f"name[{name_index}].given[{given_index}]")
    npi: str | None = None
    for index, ident in enumerate(resource.get("identifier", [])):
        if ident.get("system") == _NPI:
            value = ident.get("value")
            if value:
                npi = str(value)
                consumed |= {f"identifier[{index}].system", f"identifier[{index}].value"}
            break
    return Practitioner(
        id=resource["id"],
        given_name=given[0] if given else None,
        family_name=name.get("family"),
        display_name=name.get("text"),
        npi=npi,
        extensions=_residual(resource, frozenset(consumed)),
        provenance=_prov(source_file, resource["id"]),
    )


def _facility(resource: dict[str, Any], source_file: str | None) -> Facility:
    """A canonical Facility from a Location or Organization resource."""
    address = resource.get("address")
    address_base = "address"
    if isinstance(address, list):  # Organization.address is a list; Location's is single
        address_base = "address[0]"
        address = address[0] if address else {}
    address = address or {}
    raw_lines = address.get("line", [])
    lines = [ln for ln in raw_lines if ln]
    # Last-wins per system: a vendor listing two phones lands the LAST one, so
    # that is the entry read.
    telecom: dict[Any, Any] = {}
    telecom_at: dict[Any, int] = {}
    for index, tel in enumerate(resource.get("telecom", [])):
        system = tel.get("system")
        telecom[system] = tel.get("value")
        telecom_at[system] = index
    # Only header-printed address elements, and the two telecom entries that
    # supplied phone/fax, are read; everything else stays in the residue.
    line_indices = [i for i, ln in enumerate(raw_lines) if ln][:2]  # the two that land
    consumed = {
        "name",
        *(f"{address_base}.{sub}" for sub in ("city", "state", "postalCode")),
        *(f"{address_base}.line[{i}]" for i in line_indices),
    }
    for system in ("phone", "fax"):
        if telecom.get(system) is not None:
            consumed |= {
                f"telecom[{telecom_at[system]}].system",
                f"telecom[{telecom_at[system]}].value",
            }
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
        extensions=_residual(resource, frozenset(consumed)),
        provenance=_prov(source_file, resource["id"]),
    )


# The exact sub-paths the two reference lifts below read, kept beside the
# navigation so :func:`_encounter`'s consumed-path bookkeeping can't drift from it.
_PROVIDER_REF_PATH = "participant[0].individual.reference"
_FACILITY_REF_PATH = "location[0].location.reference"


def _encounter_provider_id(resource: dict[str, Any]) -> str | None:
    """The Practitioner id an Encounter's FIRST participant cites, or
    None. Shared by :func:`_encounter` and the reference pre-pass in
    :func:`records_from_resources`, so the two can never disagree about
    which Practitioner a record cites."""
    participants = resource.get("participant", [])
    return _ref_id(participants[0].get("individual")) if participants else None


def _encounter_facility_id(resource: dict[str, Any]) -> str | None:
    """The Location id an Encounter's FIRST location cites, or None (see above)."""
    locations = resource.get("location", [])
    return _ref_id(locations[0].get("location")) if locations else None


def _encounter(
    resource: dict[str, Any],
    patient_id: str,
    notes: dict[str, list[NoteSection]],
    source_file: str | None,
) -> Encounter:
    period = resource.get("period") or {}
    types = resource.get("type", [])
    reasons = resource.get("reasonCode", [])
    encounter_class = resource.get("class")
    class_text, consumed = _concept_text_consumed(encounter_class, "class")
    encounter_type = class_text or (encounter_class or {}).get("code")
    if class_text is None and encounter_type:
        consumed = {"class.code"}  # the Coding's own code, not its system/display
    provider_id = _encounter_provider_id(resource)
    facility_id = _encounter_facility_id(resource)
    if provider_id is not None:
        consumed.add(_PROVIDER_REF_PATH)
    if facility_id is not None:
        consumed.add(_FACILITY_REF_PATH)
    diagnosis_ids: list[str] = []
    for index, dx in enumerate(resource.get("diagnosis", [])):
        rid, paths = _ref_id_consumed(dx.get("condition"), f"diagnosis[{index}].condition")
        if rid:
            diagnosis_ids.append(rid)
            consumed |= paths
    return Encounter(
        id=resource["id"],
        patient_id=patient_id,
        date_of_service=_date(period.get("start")),
        chief_complaint=_concept_text(reasons[0]) if reasons else None,
        encounter_type=encounter_type,
        note_type=_concept_text(types[0]) if types else None,
        provider_id=provider_id,
        facility_id=facility_id,
        sections=notes.get(resource["id"], []),
        diagnosis_ids=diagnosis_ids,
        # status, period.end, and extra reasonCode/type entries ride along;
        # class/participant/location/diagnosis are consumed only in PART
        # (a participant's type/period, a second entry, a diagnosis rank).
        extensions=_residual(resource, frozenset(consumed)),
        provenance=_prov(source_file, resource["id"]),
    )


def _observations(
    resource: dict[str, Any], patient_id: str, source_file: str | None
) -> list[Observation]:
    """One resource → one or more canonical observations (BP-style panels
    split). A component-bearing Observation with no top-level value (the
    US Core blood-pressure shape) expands to one Observation per
    component, so systolic/diastolic land as discrete vitals."""
    consumed: set[str] = set()
    category = ObservationCategory.OTHER
    for cat_index, cat in enumerate(resource.get("category", [])):
        coding_index, coding = _first_coding(cat)
        code = coding.get("code")
        if code in _OBS_CATEGORY:
            category = _OBS_CATEGORY[code]
            # Only the coding that matched is read: the categories scanned past,
            # the codings behind this one, and its system/display stay behind.
            consumed.add(f"category[{cat_index}].coding[{coding_index}].code")
            break
    code_concept = resource.get("code", {})
    loinc, loinc_paths = _code_in_consumed(code_concept, _LOINC, "code")
    code_text, code_text_paths = _concept_text_consumed(code_concept, "code")
    loinc_landed = False
    if category is ObservationCategory.OTHER and loinc == _SMOKING_LOINC:
        category = ObservationCategory.SOCIAL_HISTORY
        loinc_landed = True
    encounter_id, encounter_paths = _ref_id_consumed(resource.get("encounter"), "encounter")
    consumed |= encounter_paths
    # Only the instant that supplied the effective time is read; a period's `end`
    # is a sibling nothing lifts, and an unparseable value lands nowhere at all.
    effective_source = resource.get("effectiveDateTime")
    effective_path = "effectiveDateTime"
    if not effective_source:
        effective_source = (resource.get("effectivePeriod") or {}).get("start")
        effective_path = "effectivePeriod.start"
    effective = _datetime(effective_source)
    if effective is not None:
        consumed.add(effective_path)

    def _value_unit(node: dict[str, Any]) -> tuple[str | None, str | None, set[str]]:
        """``(value, unit, sub-paths read)``, the paths RELATIVE to
        ``node``. Only the value[x] shape that supplied the value is
        consumed, so a sibling value[x] a vendor also set — or a
        Quantity's system/comparator — is never dropped alongside it."""
        qty = node.get("valueQuantity")
        if isinstance(qty, dict):
            value = _num_str(qty.get("value"))
            paths: set[str] = {"valueQuantity.value"} if value is not None else set()
            if qty.get("unit"):
                paths.add("valueQuantity.unit")
            elif qty.get("code"):
                paths.add("valueQuantity.code")
            return value, (qty.get("unit") or qty.get("code")), paths
        concept = node.get("valueCodeableConcept")
        if isinstance(concept, dict):
            text, text_paths = _concept_text_consumed(concept, "valueCodeableConcept")
            return text, None, text_paths
        if node.get("valueString") is not None:
            return str(node["valueString"]), None, {"valueString"}
        if node.get("valueBoolean") is not None:
            return str(node["valueBoolean"]), None, {"valueBoolean"}
        return None, None, set()

    fallback_index, fallback_coding = _first_coding(code_concept)
    fallback_code = loinc or fallback_coding.get("code")
    components = [
        (index, comp)
        for index, comp in enumerate(resource.get("component", []))
        if isinstance(comp, dict)
    ]
    top_value, top_unit, top_paths = _value_unit(resource)
    # Emit the panel's own value when it has one, AND every component (US
    # Core BP is a value-less panel + systolic/diastolic components; a panel
    # may carry both). Component ids are index-qualified so two components
    # sharing a LOINC never collide; an element counts as consumed only once
    # it has actually landed on an observation this returns.
    emitted: list[tuple[str, Any, str | None, str | None, Any]] = []
    panel_code_landed = False
    if top_value is not None:
        emitted.append((resource["id"], fallback_code, code_text, top_value, top_unit))
        consumed |= top_paths
        panel_code_landed = True
    for position, (index, comp) in enumerate(components):
        base = f"component[{index}].code"
        comp_loinc, comp_loinc_paths = _code_in_consumed(comp.get("code"), _LOINC, base)
        comp_text, comp_text_paths = _concept_text_consumed(comp.get("code"), base)
        value, unit, value_paths = _value_unit(comp)
        consumed |= comp_text_paths | {f"component[{index}].{path}" for path in value_paths}
        if comp_loinc is not None:
            consumed |= comp_loinc_paths
        elif loinc is not None:
            loinc_landed = True  # the panel's LOINC is the code this component carries
        emitted.append(
            (
                f"{resource['id']}:{position}:{comp_loinc or loinc or 'c'}",
                comp_loinc or loinc,
                comp_text,
                value,
                unit,
            )
        )
    if not emitted:  # neither a value nor components (e.g. a dataAbsentReason obs)
        emitted.append((resource["id"], fallback_code, code_text, None, None))
        panel_code_landed = True
    if panel_code_landed:
        if code_text is not None:
            consumed |= code_text_paths
        if loinc is not None:
            loinc_landed = True
        elif fallback_code is not None and fallback_index is not None:
            consumed.add(f"code.coding[{fallback_index}].code")
    if loinc_landed:
        consumed |= loinc_paths
    residual = _residual(resource, frozenset(consumed))
    return [
        Observation(
            id=obs_id,
            patient_id=patient_id,
            encounter_id=encounter_id,
            category=category,
            code=obs_code,
            display=obs_display,
            value=obs_value,
            unit=obs_unit,
            effective_at=effective,
            extensions=dict(residual),
            provenance=_prov(source_file, resource["id"]),
        )
        for obs_id, obs_code, obs_display, obs_value, obs_unit in emitted
    ]


def _condition(resource: dict[str, Any], patient_id: str, source_file: str | None) -> Condition:
    code = resource.get("code", {})
    icd10, icd10_paths = _code_in_consumed(code, _ICD10, "code")
    snomed, snomed_paths = _code_in_consumed(code, _SNOMED, "code")
    display, display_paths = _concept_text_consumed(code, "code")
    active, status_paths = _status_active_consumed(resource, "clinicalStatus")
    onset = _date(resource.get("onsetDateTime"))
    stopped = _date(resource.get("abatementDateTime"))
    recorded_at = _datetime(resource.get("recordedDate"))
    consumed = icd10_paths | snomed_paths | display_paths | status_paths
    consumed |= {
        key
        for key, value in (
            ("onsetDateTime", onset),
            ("abatementDateTime", stopped),
            ("recordedDate", recorded_at),
        )
        if value is not None
    }
    return Condition(
        id=resource["id"],
        patient_id=patient_id,
        icd10=icd10,
        snomed=snomed,
        display=display,
        onset=onset,
        stopped=stopped,
        recorded_at=recorded_at,
        active=active,
        # verificationStatus, category, severity, bodySite, etc. are preserved
        # (never reverse clinical meaning), along with the codings ICD-10/SNOMED
        # didn't match.
        extensions=_residual(resource, frozenset(consumed)),
        provenance=_prov(source_file, resource["id"]),
    )


def _medication(
    resource: dict[str, Any], patient_id: str, source_file: str | None
) -> MedicationStatement:
    """A canonical med-list entry from a MedicationRequest or
    MedicationStatement — US Core conveys the active list chiefly as the
    former, but the canonical :class:`MedicationStatement` is what the
    chart renders, so both map here. The originating resourceType rides
    in extensions."""
    concept = resource.get("medicationCodeableConcept", {})
    display_name, consumed = _concept_text_consumed(concept, "medicationCodeableConcept")
    rxnorm, rxnorm_paths = _code_in_consumed(concept, _RXNORM, "medicationCodeableConcept")
    consumed |= rxnorm_paths
    # Whichever dosage spelling the vendor used, only its FIRST entry's
    # `text`; a second line and the route/timing/doseAndRate beside it stay.
    dosage_key = "dosageInstruction" if resource.get("dosageInstruction") else "dosage"
    dosage = resource.get("dosageInstruction") or resource.get("dosage") or []
    sig = dosage[0].get("text") if dosage and isinstance(dosage[0], dict) else None
    if sig is not None:
        consumed.add(f"{dosage_key}[0].text")
    period = resource.get("effectivePeriod") or {}
    # The first of the three start spellings that is present supplies the date;
    # the others were never consulted, so they stay.
    start_source, start_path = period.get("start"), "effectivePeriod.start"
    if not start_source:
        start_source, start_path = resource.get("authoredOn"), "authoredOn"
    if not start_source:
        start_source, start_path = resource.get("dateAsserted"), "dateAsserted"
    start = _date(start_source)
    if start is not None:
        consumed.add(start_path)
    stop = _date(period.get("end"))
    if stop is not None:
        consumed.add("effectivePeriod.end")
    extensions: dict[str, Any] = {
        f"{_EXT}resource_type": resource["resourceType"],
        **_residual(resource, frozenset(consumed)),
    }
    return MedicationStatement(
        id=resource["id"],
        patient_id=patient_id,
        display_name=display_name,
        rxnorm=rxnorm,
        sig=sig,
        start=start,
        stop=stop,
        active=resource.get("status") in ("active", "completed"),
        extensions=extensions,
        provenance=_prov(source_file, resource["id"]),
    )


def _allergy(
    resource: dict[str, Any], patient_id: str, source_file: str | None
) -> AllergyIntolerance:
    consumed: set[str] = set()
    category = AllergyCategory.OTHER
    for index, c in enumerate(resource.get("category") or []):
        if c in _ALLERGY_CATEGORY:
            category = _ALLERGY_CATEGORY[c]
            consumed.add(f"category[{index}]")  # the entries scanned past stay
            break
    reactions: list[str] = []
    severity: Any = None
    severity_path: str | None = None
    for r_index, r in enumerate(resource.get("reaction", [])):
        for m_index, m in enumerate(r.get("manifestation", [])):
            base = f"reaction[{r_index}].manifestation[{m_index}]"
            text, text_paths = _concept_text_consumed(m, base)
            if text:
                reactions.append(text)
                consumed |= text_paths
        if severity_path is None and r.get("severity"):
            severity, severity_path = r.get("severity"), f"reaction[{r_index}].severity"
    if severity_path is not None:
        consumed.add(severity_path)
    else:
        severity = resource.get("criticality")
    substance, substance_paths = _concept_text_consumed(resource.get("code"), "code")
    consumed |= substance_paths
    onset = _date(resource.get("onsetDateTime"))
    if onset is not None:
        consumed.add("onsetDateTime")
    active, status_paths = _status_active_consumed(resource, "clinicalStatus")
    consumed |= status_paths
    return AllergyIntolerance(
        id=resource["id"],
        patient_id=patient_id,
        substance=substance,
        category=category,
        reactions=reactions,
        severity=severity,
        onset=onset,
        active=active,
        # criticality (severity fallback), verificationStatus, type,
        # recordedDate, note, category, manifestation codings, and a
        # non-active status code all stay in the residue.
        extensions=_residual(resource, frozenset(consumed)),
        provenance=_prov(source_file, resource["id"]),
    )


def _immunization(
    resource: dict[str, Any], patient_id: str, source_file: str | None
) -> Immunization:
    notes = resource.get("note", [])
    vaccine, consumed = _concept_text_consumed(resource.get("vaccineCode"), "vaccineCode")
    cvx, cvx_paths = _code_in_consumed(resource.get("vaccineCode"), _CVX, "vaccineCode")
    if cvx:
        consumed |= cvx_paths  # the CVX lands under its own extensions key below
    administered_on = _date(resource.get("occurrenceDateTime"))
    if administered_on is not None:
        consumed.add("occurrenceDateTime")
    expires = _date(resource.get("expirationDate"))
    if expires is not None:
        consumed.add("expirationDate")
    if resource.get("lotNumber") is not None:
        consumed.add("lotNumber")
    # Only the FIRST note's text becomes the comment: a second note, and the
    # time/author beside the text, are siblings this never reads.
    comment = notes[0].get("text") if notes and isinstance(notes[0], dict) else None
    if comment is not None:
        consumed.add("note[0].text")
    extensions: dict[str, Any] = _residual(resource, frozenset(consumed))
    if cvx:
        extensions[f"{_EXT}cvx"] = cvx
    return Immunization(
        id=resource["id"],
        patient_id=patient_id,
        vaccine=vaccine,
        administered_on=administered_on,
        lot_number=resource.get("lotNumber"),
        expires=expires,
        comment=comment,
        extensions=extensions,
        provenance=_prov(source_file, resource["id"]),
    )


def _coverage(resource: dict[str, Any], patient_id: str, source_file: str | None) -> Coverage:
    consumed: set[str] = set()
    payors = resource.get("payor") or []
    payer = payors[0].get("display") if payors and isinstance(payors[0], dict) else None
    if payer is not None:
        consumed.add("payor[0].display")  # its reference, and payor[1:], stay
    period = resource.get("period") or {}
    start, end = _date(period.get("start")), _date(period.get("end"))
    consumed |= {f"period.{sub}" for sub, value in (("start", start), ("end", end)) if value}
    # Last-wins per class type code. Each entry's position rides along so a
    # consumed sub-path names the entry the value actually came from.
    classes: dict[Any, dict[str, Any]] = {}
    class_paths: dict[Any, tuple[str, set[str]]] = {}
    for index, cls in enumerate(resource.get("class", [])):
        coding_index, coding = _first_coding(cls.get("type"))
        base = f"class[{index}]"
        classes[coding.get("code")] = cls
        class_paths[coding.get("code")] = (
            base,
            {f"{base}.type.coding[{coding_index}].code"} if coding_index is not None else set(),
        )
    plan_code = "group" if classes.get("group") else "plan"
    plan_name = (classes.get(plan_code) or {}).get("name")
    group_number = (classes.get("group") or {}).get("value")
    for key, sub, value in (("group", "value", group_number), (plan_code, "name", plan_name)):
        if value is not None and key in class_paths:
            base, discriminator = class_paths[key]
            consumed |= {f"{base}.{sub}"} | discriminator
    if resource.get("subscriberId") is not None:
        consumed.add("subscriberId")
    order = resource.get("order")
    # FHIR order is a positiveInt (1 = primary) → canonical 0-based; guard a
    # non-conformant 0 so it never becomes a nonsense -1.
    order_of_benefits = (order - 1) if isinstance(order, int) and order >= 1 else None
    if order_of_benefits is not None:
        consumed.add("order")  # a non-conformant order lands nowhere, so it stays
    active = resource.get("status") == "active"
    if active:
        # A status that is not active (`cancelled`, `entered-in-error`) is never
        # lifted: dropping it would migrate a cancelled policy as merely inactive.
        consumed.add("status")
    return Coverage(
        id=resource["id"],
        patient_id=patient_id,
        payer=payer,
        plan_name=plan_name,
        group_number=group_number,
        member_id=resource.get("subscriberId"),
        order_of_benefits=order_of_benefits,
        start=start,
        end=end,
        active=active,
        extensions=_residual(resource, frozenset(consumed)),
        provenance=_prov(source_file, resource["id"]),
    )


def _goal(resource: dict[str, Any], patient_id: str, source_file: str | None) -> Goal:
    consumed: set[str] = set()
    description = (resource.get("description") or {}).get("text")
    if description is not None:
        consumed.add("description.text")  # the concept's codings are never read
    effective = _date(resource.get("startDate"))
    if effective is not None:
        consumed.add("startDate")
    active = resource.get("lifecycleStatus") in ("active", "accepted", "in-progress")
    if active:
        # A lifecycleStatus outside that set is never lifted: dropping it
        # would migrate an abandoned goal as merely inactive.
        consumed.add("lifecycleStatus")
    return Goal(
        id=resource["id"],
        patient_id=patient_id,
        description=description,
        effective=effective,
        active=active,
        extensions=_residual(resource, frozenset(consumed)),
        provenance=_prov(source_file, resource["id"]),
    )


def _family_history(
    resource: dict[str, Any], patient_id: str, source_file: str | None
) -> FamilyMemberHistory:
    conditions = resource.get("condition", [])
    # Only the FIRST condition entry is lifted; a second one, and this one's
    # note/contributedToDeath/outcome, ride the residue rather than vanish.
    condition_index = next((i for i, c in enumerate(conditions) if isinstance(c, dict)), None)
    condition: dict[str, Any] = conditions[condition_index] if condition_index is not None else {}
    base = f"condition[{condition_index}]"
    relation, consumed = _concept_text_consumed(resource.get("relationship"), "relationship")
    diagnosis, diagnosis_paths = _concept_text_consumed(condition.get("code"), f"{base}.code")
    consumed |= diagnosis_paths
    onset_date = _date(condition.get("onsetDateTime"))
    if onset_date is not None:
        consumed.add(f"{base}.onsetDateTime")
    onset_string = condition.get("onsetString")
    if onset_string:
        consumed.add(f"{base}.onsetString")
    extensions: dict[str, Any] = _residual(resource, frozenset(consumed))
    if onset_string:
        extensions[f"{_EXT}onset_string"] = onset_string
    return FamilyMemberHistory(
        id=resource["id"],
        patient_id=patient_id,
        diagnosis=diagnosis,
        relation=relation,
        onset_date=onset_date,
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
    """A narrative DocumentReference → a NARRATIVE NoteSection carrying
    TEXT only, never ``.html``: packs render ``.html`` with Jinja
    ``| safe``, and this lane ingests arbitrary external markup that must
    not reach that trusted slot. Binary content (PDF etc.) becomes a
    DocumentArtifact instead."""
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
    """A synthetic encounter for a note whose ``context.encounter`` was
    absent or dangling, so the narrative still renders. Common in an
    ``$export`` slice. The docref's own ``date`` becomes the date of
    service; the id is namespaced (``docref:<id>``) so it never collides
    with a real Encounter id."""
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
    """A ``resourceType`` in the shape safe to echo to an operator: only a
    plain FHIR type name, else ``"unknown"`` — the file this reads is not
    authored by this module, so a crafted export cannot smuggle patient
    text into a message or log line."""
    text = str(value)
    return text if text.isascii() and text.isalpha() and len(text) <= 64 else "unknown"


class AmbiguousUnanchoredError(SourceDataError, ValueError):
    """Resources reference a patient the data does not contain, and the
    data describes more than one patient — attaching them would
    misattribute, dropping them would breach losslessness, so the load
    refuses. The message names resource TYPES and counts only, never an
    id or patient-derived value (:func:`_safe_resource_type`)."""

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
    """Group flat FHIR resources into one :class:`PatientRecord` per
    Patient, in order; raises only when there is no Patient at all. An
    absent-patient reference is DANGLING (preserved on its one patient, or
    refused when several could be it); a no-patient reference is
    bundle-level, on EVERY record's ``extensions["fhir_r4:shared"]``."""
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

    # Group everything patient-scoped up front (single pass); DocumentReferences
    # are partitioned per record in _assemble, where encounter ids are known.
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

    # Which reference resources a record actually attaches, decided from
    # PATIENT-ANCHORED encounters via the same two lifts _encounter uses —
    # a dangling encounter cites nobody, so its Practitioner stays uncited.
    cited_providers: set[str] = set()
    cited_facilities: set[str] = set()
    for groups in by_patient.values():
        for encounter in groups.get("Encounter", []):
            provider = _encounter_provider_id(encounter)
            if provider is not None:
                cited_providers.add(provider)
            facility = _encounter_facility_id(encounter)
            if facility is not None:
                cited_facilities.add(facility)
    # An uncited Practitioner/Location/Organization rides the bundle-level
    # key beside the patient-less resources, in bundle order, only for the
    # entries the records themselves draw on.
    for resource in resources:
        rid = resource.get("id")
        if (practitioners.get(rid) is resource and rid not in cited_providers) or (
            facilities.get(rid) is resource and rid not in cited_facilities
        ):
            shared.append(resource)

    # Dangling resources are preserved only with exactly one patient to
    # attribute them to; with several, the load is refused instead of guessing.
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
    # Partition this patient's DocumentReferences: a narrative note whose
    # context.encounter resolves attaches to that encounter; one with no/
    # dangling encounter gets a synthetic encounter; binary content becomes
    # a DocumentArtifact.
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
                # Neither renderable narrative nor binary (empty/undecodable
                # content): preserve the whole resource so nothing is dropped.
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
