"""PF/Tebra join graph → canonical PatientRecords.

The lossless rule, mechanically enforced: every table mapping declares the
columns it consumes, and **every other non-empty column** lands in the
target model's ``extensions`` under a ``pf_tebra:`` namespace. A column we
have never heard of survives the migration by construction.

Source GUIDs become canonical ids verbatim, so cross-references
(encounter → diagnosis, prescription → medication) carry over without a
translation table and provenance stays greppable.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from anastomosis.core.codes import VITALS, bmi_metric
from anastomosis.core.model import (
    Addendum,
    Address,
    AdvanceDirective,
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
    Guarantor,
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
    PrescriptionTransaction,
    Provenance,
    SectionKind,
)
from anastomosis.core.textutil import clean_cell, clean_numeric, format_phone, html_to_text
from anastomosis.core.timeutil import parse_date, parse_dt

from .escript import resolve_status, script_prefix
from .loader import Export, Row

__all__ = ["map_export"]

SOURCE = "pf_tebra"

_VITAL_BY_LOINC = {v.loinc: v for v in VITALS.values()}
_ICD10_RE = re.compile(r"\b([A-TV-Z][0-9]{2}(?:\.[0-9A-Z]{1,4})?)\b")
_SNOMED_RE = re.compile(r"\b([0-9]{6,18})\b")

# Social-history observation labels, keyed by (table, value column).
_SOCIAL_TABLES = (
    ("patient-smokingstatus", "TobaccoUseDescription", "Tobacco use"),
    ("occupation-industry", "Occupation", "Occupation"),
    ("occupation-industry", "IndustryName", "Industry"),
    ("patient-education", "EducationLevel", "Education"),
    ("patient-financial-resources", "FinancialResource", "Financial resources"),
    ("tribal-affiliation", "TribalAffiliation", "Tribal affiliation"),
)


def _s(row: Row, col: str) -> str | None:
    return clean_cell(row.get(col))


def _b(row: Row, col: str) -> bool:
    value = _s(row, col)
    return value is not None and value.lower() == "true"


def _dt(row: Row, col: str) -> Any:
    return parse_dt(_s(row, col))


def _d(row: Row, col: str) -> Any:
    return parse_date(_s(row, col))


def _ext(row: Row, mapped: frozenset[str]) -> dict[str, Any]:
    """Everything the mapping didn't consume — the lossless catch-all."""
    return {
        f"{SOURCE}:{col}": value
        for col, value in row.items()
        if col is not None and col not in mapped and clean_cell(value) is not None
    }


def _prov(table: str, source_id: str | None) -> Provenance:
    return Provenance(source_system=SOURCE, source_file=f"{table}.tsv", source_id=source_id)


def _by(rows: list[Row], col: str) -> dict[str, list[Row]]:
    grouped: dict[str, list[Row]] = {}
    for row in rows:
        key = _s(row, col)
        if key is not None:
            grouped.setdefault(key, []).append(row)
    return grouped


# --- patients ----------------------------------------------------------------

_DEMOGRAPHICS_MAPPED = frozenset(
    {
        "PatientPracticeGuid",
        "FirstName",
        "MiddleName",
        "LastName",
        "NameSuffix",
        "Gender",
        "BirthDate",
        "IsActive",
        "MothersMaidenName",
        "PreferredLanguage",
        "Address1",
        "Address2",
        "AddressCity",
        "AddressState",
        "AddressZipCode",
        "HomePhone",
        "MobilePhone",
        "OfficePhone",
        "Email",
        "SSN",
        "UnPinnedNote",
    }
)

_PHONE_COLS = (
    ("HomePhone", ContactKind.PHONE_HOME),
    ("MobilePhone", ContactKind.PHONE_MOBILE),
    ("OfficePhone", ContactKind.PHONE_WORK),
)


def _map_patient(row: Row, export: Export) -> Patient:
    guid = _s(row, "PatientPracticeGuid")
    assert guid is not None  # loader guarantees keyed rows; join column required

    identifiers = [Identifier(kind=IdentifierKind.SOURCE_GUID, value=guid, system=SOURCE)]
    if ssn := _s(row, "SSN"):
        identifiers.append(Identifier(kind=IdentifierKind.SSN, value=ssn))

    telecom = [
        ContactPoint(kind=kind, value=phone)
        for col, kind in _PHONE_COLS
        if (phone := format_phone(_s(row, col)))
    ]
    if email := _s(row, "Email"):
        telecom.append(ContactPoint(kind=ContactKind.EMAIL, value=email))

    address = Address(
        line1=_s(row, "Address1"),
        line2=_s(row, "Address2"),
        city=_s(row, "AddressCity"),
        state=_s(row, "AddressState"),
        postal_code=_s(row, "AddressZipCode"),
    )

    notes = [_s(row, "UnPinnedNote")]
    notes += [
        f"{_s(pin, 'NoteType') or 'Note'}: {_s(pin, 'NoteText')}"
        for pin in _by(export["pinned-notes"], "PatientPracticeGuid").get(guid, [])
        if _s(pin, "NoteText")
    ]

    giso_rows = _by(
        export["patient-gender-identity-sexual-orientation"], "PatientPracticeGuid"
    ).get(guid, [])
    giso = giso_rows[0] if giso_rows else {}

    return Patient(
        id=guid,
        given_name=_s(row, "FirstName"),
        middle_name=_s(row, "MiddleName"),
        family_name=_s(row, "LastName"),
        suffix=_s(row, "NameSuffix"),
        birth_date=_d(row, "BirthDate"),
        sex=_s(row, "Gender"),
        gender_identity=_s(giso, "GenderIdentity"),
        sexual_orientation=_s(giso, "SexualOrientation"),
        race=[
            name
            for r in _by(export["patient-race"], "PatientPracticeGuid").get(guid, [])
            if (name := _s(r, "RaceName"))
        ],
        ethnicity=[
            name
            for r in _by(export["patient-ethnicity"], "PatientPracticeGuid").get(guid, [])
            if (name := _s(r, "EthnicityName"))
        ],
        language=_s(row, "PreferredLanguage"),
        mothers_maiden_name=_s(row, "MothersMaidenName"),
        status="Active" if _b(row, "IsActive") else "Inactive",
        notes="\n".join(n for n in notes if n) or None,
        identifiers=identifiers,
        telecom=telecom,
        addresses=[address] if any(address.model_dump().values()) else [],
        guarantor=_map_guarantor(export, guid),
        extensions=_ext(row, _DEMOGRAPHICS_MAPPED),
        provenance=_prov("patient-demographics", guid),
    )


_GUARANTOR_MAPPED = frozenset(
    {
        "PatientPracticeGuid",
        "FirstName",
        "LastName",
        "RelationshipToPatient",
        "Address1",
        "AddressCity",
        "AddressState",
        "AddressZipCode",
        "PhoneNumber",
    }
)


def _map_guarantor(export: Export, guid: str) -> Guarantor | None:
    rows = _by(export["patient-guarantor"], "PatientPracticeGuid").get(guid, [])
    if not rows:
        return None
    row = rows[0]
    name = " ".join(p for p in (_s(row, "FirstName"), _s(row, "LastName")) if p)
    phone = format_phone(_s(row, "PhoneNumber"))
    return Guarantor(
        name=name or None,
        relationship_to_patient=_s(row, "RelationshipToPatient"),
        address=Address(
            line1=_s(row, "Address1"),
            city=_s(row, "AddressCity"),
            state=_s(row, "AddressState"),
            postal_code=_s(row, "AddressZipCode"),
        ),
        phones=[ContactPoint(kind=ContactKind.PHONE_HOME, value=phone)] if phone else [],
    )


# --- encounters ---------------------------------------------------------------

_ENCOUNTER_MAPPED = frozenset(
    {
        "PatientPracticeGuid",
        "EncounterGuid",
        "DateOfService",
        "ChiefComplaint",
        "Subjective",
        "Objective",
        "Assessment",
        "Plan",
        "SignedByProviderGuid",
        "SignedDateTimeUtc",
        "SeenByProviderGuid",
        "FacilityGuid",
        "ChartNoteType",
        "IsSoapNote",
        "LastModifiedDateTimeUtc",
    }
)

_SOAP_COLUMNS = (
    ("Subjective", SectionKind.SUBJECTIVE, "Subjective"),
    ("Objective", SectionKind.OBJECTIVE, "Objective"),
    ("Assessment", SectionKind.ASSESSMENT, "Assessment"),
    ("Plan", SectionKind.PLAN, "Plan"),
)


def _map_encounter(row: Row, export: Export) -> Encounter:
    guid = _s(row, "EncounterGuid")
    patient_guid = _s(row, "PatientPracticeGuid")
    assert guid is not None and patient_guid is not None

    is_soap = _b(row, "IsSoapNote")
    sections: list[NoteSection] = []
    if is_soap:
        for col, kind, title in _SOAP_COLUMNS:
            html = _s(row, col)
            sections.append(NoteSection(kind=kind, title=title, html=html, text=html_to_text(html)))
    else:
        # SIMPLE encounters carry the whole narrative in Subjective.
        html = _s(row, "Subjective")
        sections.append(NoteSection(kind=SectionKind.NARRATIVE, html=html, text=html_to_text(html)))

    addenda = [
        Addendum(
            text=html_to_text(_s(add, "Addendum")),
            status=_s(add, "AmendmentStatus"),
            source=_s(add, "AmendmentSource"),
            at=_dt(add, "LastModifiedDateTimeUtc"),
        )
        for add in _by(export["patient-encounter-addendums"], "EncounterGuid").get(guid, [])
    ]

    diagnosis_ids = [
        dx
        for link in _by(export["patient-encounter-diagnoses"], "EncounterGuid").get(guid, [])
        if (dx := _s(link, "DiagnosisGuid"))
    ]

    return Encounter(
        id=guid,
        patient_id=patient_guid,
        date_of_service=_dt(row, "DateOfService"),
        chief_complaint=_s(row, "ChiefComplaint"),
        encounter_type="SOAP" if is_soap else "SIMPLE",
        note_type=_s(row, "ChartNoteType"),
        provider_id=_s(row, "SeenByProviderGuid"),
        facility_id=_s(row, "FacilityGuid"),
        signed_by_id=_s(row, "SignedByProviderGuid"),
        signed_at=_dt(row, "SignedDateTimeUtc"),  # year-1 sentinel → None
        last_modified_at=_dt(row, "LastModifiedDateTimeUtc"),
        sections=sections,
        addenda=addenda,
        diagnosis_ids=diagnosis_ids,
        extensions=_ext(row, _ENCOUNTER_MAPPED),
        provenance=_prov("patient-encounters", guid),
    )


# --- observations (vitals + BMI auto-calc + social history) -------------------

_OBSERVATION_MAPPED = frozenset(
    {
        "PatientPracticeGuid",
        "EncounterGuid",
        "ObservationCodeSystem",
        "ObservationCode",
        "Value",
        "UnitOfObservation",
        "ObservationDateTimeUtc",
        "LastModifiedDateTimeUtc",
    }
)


def _map_observation(row: Row) -> Observation:
    code = _s(row, "ObservationCode")
    vital = _VITAL_BY_LOINC.get(code or "")
    return Observation(
        patient_id=_s(row, "PatientPracticeGuid") or "",
        encounter_id=_s(row, "EncounterGuid"),
        category=ObservationCategory.VITAL_SIGNS if vital else ObservationCategory.OTHER,
        code=code,
        display=vital.display if vital else None,
        value=_s(row, "Value"),
        unit=_s(row, "UnitOfObservation"),
        effective_at=_dt(row, "ObservationDateTimeUtc"),
        recorded_at=_dt(row, "LastModifiedDateTimeUtc"),
        extensions=_ext(row, _OBSERVATION_MAPPED),
        provenance=_prov("patient-encounter-observations", _s(row, "ObservationSetGuid")),
    )


def _to_cm(value: float, unit: str | None) -> float:
    return value * 2.54 if (unit or "").lower().startswith("in") else value


def _to_kg(value: float, unit: str | None) -> float:
    return value * 0.45359237 if (unit or "").lower().startswith("lb") else value


def _auto_bmi(encounter_obs: list[Observation]) -> Observation | None:
    """The BMI trigger: synthesize 39156-5 when height+weight exist without it.

    Unit-aware (in/cm, lb/kg); an explicitly charted BMI always wins.
    """
    by_code = {o.code: o for o in encounter_obs}
    if VITALS["bmi"].loinc in by_code:
        return None
    height = by_code.get(VITALS["height"].loinc)
    weight = by_code.get(VITALS["weight"].loinc)
    if height is None or weight is None or not height.value or not weight.value:
        return None
    try:
        height_cm = _to_cm(float(height.value), height.unit)
        weight_kg = _to_kg(float(weight.value), weight.unit)
    except ValueError:
        return None
    value = bmi_metric(weight_kg, height_cm)
    if value is None:
        return None
    return Observation(
        patient_id=weight.patient_id,
        encounter_id=weight.encounter_id,
        category=ObservationCategory.VITAL_SIGNS,
        code=VITALS["bmi"].loinc,
        display=VITALS["bmi"].display,
        value=f"{value:.1f}",
        unit="kg/m2",
        effective_at=weight.effective_at,
        extensions={f"{SOURCE}:computed": "bmi_auto_calc"},
        provenance=Provenance(source_system=SOURCE, source_file="(derived)", source_id=None),
    )


def _social_observations(export: Export, guid: str) -> list[Observation]:
    observations: list[Observation] = []
    for table, value_col, label in _SOCIAL_TABLES:
        for row in _by(export[table], "PatientPracticeGuid").get(guid, []):
            value = _s(row, value_col)
            if value is None:
                continue
            effective = next(
                (d for c in ("EffectiveDate", "EffectiveDateFrom") if (d := _dt(row, c))), None
            )
            observations.append(
                Observation(
                    patient_id=guid,
                    category=ObservationCategory.SOCIAL_HISTORY,
                    display=label,
                    value=value,
                    effective_at=effective,
                    extensions=_ext(row, frozenset({"PatientPracticeGuid", value_col})),
                    provenance=_prov(table, guid),
                )
            )
    return observations


# --- discrete clinical tables --------------------------------------------------

_DIAGNOSIS_MAPPED = frozenset(
    {
        "PatientPracticeGuid",
        "DiagnosisGuid",
        "Diagnosis",
        "DiagnosisCodeEquivalents",
        "DiagnosisAcuity",
        "StartDate",
        "StopDate",
        "LastModifiedDateTimeUtc",
    }
)


def _map_condition(row: Row) -> Condition:
    # The serialization of DiagnosisCodeEquivalents is publicly undocumented;
    # extract by code *shape* and keep the raw string in extensions.
    equivalents = _s(row, "DiagnosisCodeEquivalents") or ""
    icd10 = _ICD10_RE.search(equivalents)
    snomed = _SNOMED_RE.search(equivalents)
    stopped = _d(row, "StopDate")
    extensions = _ext(row, _DIAGNOSIS_MAPPED)
    if equivalents:
        extensions[f"{SOURCE}:DiagnosisCodeEquivalents"] = equivalents
    return Condition(
        id=_s(row, "DiagnosisGuid") or "",
        patient_id=_s(row, "PatientPracticeGuid") or "",
        icd10=icd10.group(1) if icd10 else None,
        snomed=snomed.group(1) if snomed else None,
        display=_s(row, "Diagnosis"),
        acuity=_s(row, "DiagnosisAcuity"),
        onset=_d(row, "StartDate"),
        stopped=stopped,
        recorded_at=_dt(row, "LastModifiedDateTimeUtc"),
        active=stopped is None,
        extensions=extensions,
        provenance=_prov("patient-diagnoses", _s(row, "DiagnosisGuid")),
    )


_ALLERGY_MAPPED = frozenset(
    {
        "PatientPracticeGuid",
        "AllergyGuid",
        "AllergenCategory",
        "Substance",
        "Severity",
        "StartDate",
        "IsActive",
    }
)

_ALLERGY_CATEGORIES = {
    "drug": AllergyCategory.DRUG,
    "food": AllergyCategory.FOOD,
    "environment": AllergyCategory.ENVIRONMENT,
}


def _map_allergy(row: Row, reactions_by_allergy: dict[str, list[Row]]) -> AllergyIntolerance:
    guid = _s(row, "AllergyGuid") or ""
    return AllergyIntolerance(
        id=guid,
        patient_id=_s(row, "PatientPracticeGuid") or "",
        substance=_s(row, "Substance"),
        category=_ALLERGY_CATEGORIES.get(
            (_s(row, "AllergenCategory") or "").lower(), AllergyCategory.OTHER
        ),
        reactions=[
            reaction for r in reactions_by_allergy.get(guid, []) if (reaction := _s(r, "Reaction"))
        ],
        severity=_s(row, "Severity"),
        onset=_d(row, "StartDate"),
        active=_b(row, "IsActive"),
        extensions=_ext(row, _ALLERGY_MAPPED),
        provenance=_prov("patient-allergy", guid),
    )


_MEDICATION_MAPPED = frozenset(
    {
        "PatientPracticeGuid",
        "MedicationGuid",
        "MedicationName",
        "StartDate",
        "StopDate",
        "Sig",
        "TradeName",
        "GenericName",
        "DoseForm",
        "Route",
        "ProductStrength",
        "MedicationDiscontinuedReasonName",
        "LastModifiedDateTimeUtc",
    }
)


def _map_medication(row: Row, prescription_ids: list[str]) -> MedicationStatement:
    guid = _s(row, "MedicationGuid") or ""
    stop = _d(row, "StopDate")
    discontinued = _s(row, "MedicationDiscontinuedReasonName")
    return MedicationStatement(
        id=guid,
        patient_id=_s(row, "PatientPracticeGuid") or "",
        generic_name=_s(row, "GenericName"),
        brand_name=_s(row, "TradeName"),
        strength=_s(row, "ProductStrength"),
        route=_s(row, "Route"),
        dose_form=_s(row, "DoseForm"),
        display_name=_s(row, "MedicationName"),
        sig=_s(row, "Sig"),
        start=_d(row, "StartDate"),
        stop=stop,
        last_modified_at=_dt(row, "LastModifiedDateTimeUtc"),
        active=stop is None and discontinued is None,
        prescription_ids=prescription_ids,
        extensions=_ext(row, _MEDICATION_MAPPED),
        provenance=_prov("patient-medications", guid),
    )


_PRESCRIPTION_MAPPED = frozenset(
    {
        "PatientPracticeGuid",
        "PrescriptionGuid",
        "MedicationGuid",
        "PrescribingProviderGuid",
        "DestinationTypeCode",
        "DateOfService",
        "MedicationDisplayName",
        "Sig",
        "Quantity",
        "NumberOfRefills",
    }
)


def _map_prescription(row: Row, tx_rows: list[Row]) -> Prescription:
    guid = _s(row, "PrescriptionGuid") or ""
    transactions = sorted(
        (
            PrescriptionTransaction(
                kind=_s(tx, "Status") or _s(tx, "TransactionDescription") or "",
                description=_s(tx, "TransactionDescription"),
                note=_s(tx, "TransactionNote"),
                at=_dt(tx, "TransactionDisplayDateTimeUtc"),
            )
            for tx in tx_rows
        ),
        key=lambda t: (t.at is None, t.at),
    )
    display_date = transactions[-1].at if transactions else None
    return Prescription(
        id=guid,
        patient_id=_s(row, "PatientPracticeGuid") or "",
        medication_id=_s(row, "MedicationGuid"),
        prescriber_id=_s(row, "PrescribingProviderGuid"),
        prefix=script_prefix(_s(row, "DestinationTypeCode")),
        status_label=resolve_status(transactions),
        display_date=display_date or _dt(row, "DateOfService"),
        sig=_s(row, "Sig"),
        refills=clean_numeric(row.get("NumberOfRefills")),  # -1 sentinel → None
        quantity=_s(row, "Quantity"),
        transactions=transactions,
        extensions=_ext(row, _PRESCRIPTION_MAPPED),
        provenance=_prov("patient-prescriptions", guid),
    )


_INSURANCE_MAPPED = frozenset(
    {
        "PatientInsurancePlanGuid",
        "PatientPracticeGuid",
        "PayerName",
        "InsurancePlanName",
        "InsuranceCoverageType",
        "RelationshipToInsured",
        "MemberId",
        "GroupId",
        "OrderOfBenefits",
        "EffectiveFromDate",
        "EffectiveToDate",
        "CopayFixedAmount",
        "InsurancePlanIsActive",
        "EmployerName",
    }
)

_PLAN_TYPE_RE = re.compile(r"\((PPO|HMO|EPO|POS|HDHP|PFFS)\)", re.IGNORECASE)
_BENEFIT_ORDER = {"primary": 0, "secondary": 1, "tertiary": 2}


def _map_coverage(row: Row) -> Coverage:
    plan_name = _s(row, "InsurancePlanName")
    # PlanType fallback chain: the export has no plan-type column, so derive
    # from the "(PPO)"-style suffix practices embed in plan names; absent
    # that, it stays None (never guess from payer name).
    plan_type_match = _PLAN_TYPE_RE.search(plan_name or "")
    order_label = _s(row, "OrderOfBenefits")
    return Coverage(
        id=_s(row, "PatientInsurancePlanGuid") or "",
        patient_id=_s(row, "PatientPracticeGuid") or "",
        payer=_s(row, "PayerName"),
        plan_name=plan_name,
        plan_type=plan_type_match.group(1).upper() if plan_type_match else None,
        coverage_type=_s(row, "InsuranceCoverageType"),
        member_id=_s(row, "MemberId"),
        group_number=_s(row, "GroupId"),
        order_of_benefits=_BENEFIT_ORDER.get((order_label or "").lower()),
        priority_label=f"{order_label.upper()} PAYER" if order_label else None,
        employer=_s(row, "EmployerName"),
        relationship_to_insured=_s(row, "RelationshipToInsured"),
        copay=clean_numeric(row.get("CopayFixedAmount")),
        start=_d(row, "EffectiveFromDate"),
        end=_d(row, "EffectiveToDate"),
        active=_b(row, "InsurancePlanIsActive"),
        extensions=_ext(row, _INSURANCE_MAPPED),
        provenance=_prov("patient-insurances", _s(row, "PatientInsurancePlanGuid")),
    )


def _map_family_history(export: Export, guid: str) -> list[FamilyMemberHistory]:
    diagnoses_by_relative = _by(export["patient-family-history-diagnoses"], "RelativeGuid")
    histories: list[FamilyMemberHistory] = []
    for relative in _by(export["patient-family-medical-history"], "PatientPracticeGuid").get(
        guid, []
    ):
        relative_guid = _s(relative, "RelativeGuid") or ""
        relation = _s(relative, "Relationship")
        for dx in diagnoses_by_relative.get(relative_guid, []):
            histories.append(
                FamilyMemberHistory(
                    patient_id=guid,
                    diagnosis=_s(dx, "Diagnosis") or _s(dx, "SnomedCode"),
                    relation=relation,
                    onset_date=_d(dx, "OnsetDate"),
                    extensions=_ext(dx, frozenset({"PatientPracticeGuid", "RelativeGuid"})),
                    provenance=_prov("patient-family-history-diagnoses", relative_guid),
                )
            )
    return histories


_IMMUNIZATION_MAPPED = frozenset(
    {"PatientPracticeGuid", "ImmunizationGuid", "Vaccine", "Lot", "Type", "Comment"}
)
# Date column spelling is INFERRED (not in the public dictionary) — read the
# first that exists.
_IMM_DATE_COLS = ("DateAdministered", "AdministeredDate", "AdministeredDateTimeUtc")


def _map_immunization(row: Row) -> Immunization:
    administered = next((d for c in _IMM_DATE_COLS if (d := _d(row, c))), None)
    return Immunization(
        id=_s(row, "ImmunizationGuid") or "",
        patient_id=_s(row, "PatientPracticeGuid") or "",
        vaccine=_s(row, "Vaccine"),
        administered_on=administered,
        source=_s(row, "Type"),
        lot_number=_s(row, "Lot"),
        expires=_d(row, "ExpirationDate"),
        comment=_s(row, "Comment"),
        extensions=_ext(row, _IMMUNIZATION_MAPPED | {"ExpirationDate", *_IMM_DATE_COLS}),
        provenance=_prov("patient-immunizations", _s(row, "ImmunizationGuid")),
    )


# --- shared actors -------------------------------------------------------------


def _map_practitioners(export: Export) -> list[Practitioner]:
    return [
        Practitioner(
            id=_s(row, "ProviderGuid") or "",
            given_name=_s(row, "FirstName"),
            family_name=_s(row, "LastName"),
            extensions=_ext(row, frozenset({"ProviderGuid", "FirstName", "LastName"})),
            provenance=_prov("providers", _s(row, "ProviderGuid")),
        )
        for row in export["providers"]
    ]


def _map_facilities(export: Export) -> list[Facility]:
    return [
        Facility(
            id=_s(row, "FacilityGuid") or "",
            name=_s(row, "FacilityName"),
            address_line1=_s(row, "Address1"),
            address_line2=_s(row, "Address2"),
            city=_s(row, "AddressCity"),
            state=_s(row, "AddressState"),
            postal_code=_s(row, "AddressZipCode"),
            phone=format_phone(_s(row, "PhoneNumber")),
            fax=format_phone(_s(row, "FaxNumber")),
            extensions=_ext(
                row,
                frozenset(
                    {
                        "FacilityGuid",
                        "FacilityName",
                        "Address1",
                        "Address2",
                        "AddressCity",
                        "AddressState",
                        "AddressZipCode",
                        "PhoneNumber",
                        "FaxNumber",
                    }
                ),
            ),
            provenance=_prov("facilities", _s(row, "FacilityGuid")),
        )
        for row in export["facilities"]
    ]


# --- assembly --------------------------------------------------------------------


def map_export(export: Export) -> Iterator[PatientRecord]:
    """Join the loaded tables into one PatientRecord per patient."""
    practitioners = _map_practitioners(export)
    facilities = _map_facilities(export)

    encounters_by_patient = _by(export["patient-encounters"], "PatientPracticeGuid")
    obs_by_patient = _by(export["patient-encounter-observations"], "PatientPracticeGuid")
    dx_by_patient = _by(export["patient-diagnoses"], "PatientPracticeGuid")
    allergy_by_patient = _by(export["patient-allergy"], "PatientPracticeGuid")
    reactions_by_allergy = _by(export["patient-allergy-reactions"], "AllergyGuid")
    meds_by_patient = _by(export["patient-medications"], "PatientPracticeGuid")
    rx_by_patient = _by(export["patient-prescriptions"], "PatientPracticeGuid")
    tx_by_rx = _by(export["prescription-transactions"], "PrescriptionGuid")
    ins_by_patient = _by(export["patient-insurances"], "PatientPracticeGuid")
    imm_by_patient = _by(export["patient-immunizations"], "PatientPracticeGuid")
    ad_by_patient = _by(export["patient-advance-directives"], "PatientPracticeGuid")
    docs_by_patient = _by(export["patient-documents"], "PatientPracticeGuid")

    for demo_row in export["patient-demographics"]:
        guid = _s(demo_row, "PatientPracticeGuid")
        if guid is None:
            continue
        patient = _map_patient(demo_row, export)

        encounters = [_map_encounter(row, export) for row in encounters_by_patient.get(guid, [])]

        observations = [_map_observation(row) for row in obs_by_patient.get(guid, [])]
        for encounter in encounters:
            if bmi := _auto_bmi([o for o in observations if o.encounter_id == encounter.id]):
                observations.append(bmi)
        observations.extend(_social_observations(export, guid))

        prescriptions = [
            _map_prescription(row, tx_by_rx.get(_s(row, "PrescriptionGuid") or "", []))
            for row in rx_by_patient.get(guid, [])
        ]
        rx_ids_by_med: dict[str, list[str]] = {}
        for rx in prescriptions:
            if rx.medication_id:
                rx_ids_by_med.setdefault(rx.medication_id, []).append(rx.id)

        medications = [
            _map_medication(row, rx_ids_by_med.get(_s(row, "MedicationGuid") or "", []))
            for row in meds_by_patient.get(guid, [])
        ]

        yield PatientRecord(
            patient=patient,
            encounters=encounters,
            observations=observations,
            conditions=[_map_condition(row) for row in dx_by_patient.get(guid, [])],
            allergies=[
                _map_allergy(row, reactions_by_allergy) for row in allergy_by_patient.get(guid, [])
            ],
            medications=medications,
            prescriptions=prescriptions,
            immunizations=[_map_immunization(row) for row in imm_by_patient.get(guid, [])],
            family_history=_map_family_history(export, guid),
            advance_directives=[
                AdvanceDirective(
                    patient_id=guid,
                    directive=_s(row, "Directive"),
                    recorded_at=_dt(row, "DateRecorded"),
                    extensions=_ext(
                        row, frozenset({"PatientPracticeGuid", "Directive", "DateRecorded"})
                    ),
                    provenance=_prov("patient-advance-directives", guid),
                )
                for row in ad_by_patient.get(guid, [])
            ],
            coverages=[_map_coverage(row) for row in ins_by_patient.get(guid, [])],
            documents=[
                DocumentArtifact(
                    id=_s(row, "DocumentGuid") or "",
                    patient_id=guid,
                    title=_s(row, "DocumentName"),
                    mime_type="application/octet-stream",
                    generated_at=_dt(row, "DocumentDate"),
                    extensions=_ext(
                        row, frozenset({"PatientPracticeGuid", "DocumentGuid", "DocumentName"})
                    ),
                    provenance=_prov("patient-documents", _s(row, "DocumentGuid")),
                )
                for row in docs_by_patient.get(guid, [])
            ],
            practitioners=practitioners,
            facilities=facilities,
            provenance=Provenance(source_system=SOURCE, source_id=guid),
        )
