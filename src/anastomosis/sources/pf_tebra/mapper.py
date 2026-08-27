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

import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from typing import Any

from anastomosis.core.codes import VITALS, bmi_metric, pain_display
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
    PastMedicalHistory,
    Patient,
    PatientRecord,
    Practitioner,
    Prescription,
    PrescriptionTransaction,
    Provenance,
    SectionKind,
)
from anastomosis.core.textutil import clean_numeric, format_phone, html_to_text, sanitize_soap_html
from anastomosis.core.timeutil import age_at
from anastomosis.sources._rowutil import clean_date, clean_dt, clean_str, group_by, residual

from .escript import resolve_display_date, resolve_prefix, resolve_status
from .loader import KNOWN_TABLES, Export, OrphanRowsError, Row, UnsupportedTablesError

__all__ = ["map_export"]

SOURCE = "pf_tebra"

logger = logging.getLogger(__name__)

# Map every LOINC edition — the primary code and its modern aliases — to the
# vital, so an observation charted under either edition categorizes as a
# vital (see codes.VitalCode.aliases).
_VITAL_BY_LOINC = {
    code: vital for vital in VITALS.values() for code in (vital.loinc, *vital.aliases)
}
_PAIN_LOINCS = frozenset({VITALS["pain_severity"].loinc, *VITALS["pain_severity"].aliases})
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


# Row-cell helpers shared with oracle_ehi/mapper.py (see sources/_rowutil.py).
_s = clean_str
_dt = clean_dt
_d = clean_date
_by = group_by


def _b(row: Row, col: str) -> bool:
    value = _s(row, col)
    return value is not None and value.lower() == "true"


def _ext(row: Row, mapped: frozenset[str], prefix: str = "") -> dict[str, Any]:
    """Everything the mapping didn't consume — the lossless catch-all.

    ``prefix`` qualifies the namespaced key for rows that are NOT the model's
    own source row (a demographics side row, say), so several tables' surplus
    columns can share one ``extensions`` dict without colliding. A prefix opens
    its own sub-namespace (``side:``) rather than starting with a table name, so
    no prefixed key can ever be spelled by an unprefixed column name.
    """
    return residual(row, mapped, SOURCE, prefix)


def _prov(table: str, source_id: str | None) -> Provenance:
    return Provenance(source_system=SOURCE, source_file=f"{table}.tsv", source_id=source_id)


def _ids(rows: list[Row], col: str) -> frozenset[str]:
    """The populated values of a key column — the set a foreign key must land in."""
    return frozenset(key for row in rows if (key := _s(row, col)) is not None)


# --- patients ----------------------------------------------------------------

_PATIENT_KEY = "PatientPracticeGuid"

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


@dataclass(frozen=True)
class _DemographicsGroups:
    """The patient-keyed tables `_map_patient` joins, each grouped by
    PatientPracticeGuid ONCE for the whole export. Building these per patient
    re-scanned every table on every patient (O(patients * rows)); grouping once
    and slicing by guid is identical output for a fraction of the work.
    """

    pinned_notes: dict[str, list[Row]]
    giso: dict[str, list[Row]]
    race: dict[str, list[Row]]
    ethnicity: dict[str, list[Row]]
    guarantor: dict[str, list[Row]]

    @classmethod
    def build(cls, export: Export) -> _DemographicsGroups:
        return cls(
            pinned_notes=_by(export["pinned-notes"], "PatientPracticeGuid"),
            giso=_by(export["patient-gender-identity-sexual-orientation"], "PatientPracticeGuid"),
            race=_by(export["patient-race"], "PatientPracticeGuid"),
            ethnicity=_by(export["patient-ethnicity"], "PatientPracticeGuid"),
            guarantor=_by(export["patient-guarantor"], "PatientPracticeGuid"),
        )


# Columns `_map_patient` lifts out of ONE demographics side row. A side row is
# not consumed wholesale by the one column the mapper wants: everything else on
# it rides `patient.extensions` (see `_side_extensions`). `_KEY_ONLY` is the set
# for a row the mapper never reads at all — only its join key is accounted for.
_KEY_ONLY = frozenset({_PATIENT_KEY})
_PINNED_MAPPED = _KEY_ONLY | {"NoteType", "NoteText"}
_GISO_MAPPED = _KEY_ONLY | {"GenderIdentity", "SexualOrientation"}
_RACE_MAPPED = _KEY_ONLY | {"RaceName"}
_ETHNICITY_MAPPED = _KEY_ONLY | {"EthnicityName"}

_GISO_TABLE = "patient-gender-identity-sexual-orientation"


def _side_extensions(groups: _DemographicsGroups, guid: str) -> dict[str, Any]:
    """Surplus cells of the demographics SIDE rows, as
    ``pf_tebra:side:<table>:<row index>:<column>``.

    The ``side:`` segment keeps this namespace disjoint from the demographics
    row's own ``pf_tebra:<column>`` keys: a future export column literally named
    ``patient-race:0:FutureColumn`` would otherwise land on the same key as a
    side-row cell and one would overwrite the other.

    `_map_patient` lifts one or two columns out of each side row (``RaceName``,
    ``GenderIdentity``, …) and nothing at all out of the rows it never reaches
    (a second gender-identity row, a superseded guarantor). Both are losses
    without this catch-all: `_ext` runs on the demographics row only, so a
    column added beside ``RaceName`` in a future export would vanish.
    """
    out: dict[str, Any] = {}

    def keep(table: str, index: int, row: Row, mapped: frozenset[str]) -> None:
        out.update(_ext(row, mapped, prefix=f"side:{table}:{index}:"))

    for index, row in enumerate(groups.pinned_notes.get(guid, [])):
        # A pinned note with no NoteText never reaches `notes`, so its NoteType
        # is unread on that row.
        keep("pinned-notes", index, row, _PINNED_MAPPED if _s(row, "NoteText") else _KEY_ONLY)
    for index, row in enumerate(groups.giso.get(guid, [])):
        # Only the first row supplies gender identity / sexual orientation.
        keep(_GISO_TABLE, index, row, _GISO_MAPPED if index == 0 else _KEY_ONLY)
    for index, row in enumerate(groups.race.get(guid, [])):
        keep("patient-race", index, row, _RACE_MAPPED)
    for index, row in enumerate(groups.ethnicity.get(guid, [])):
        keep("patient-ethnicity", index, row, _ETHNICITY_MAPPED)
    # The LAST guarantor row wins and carries its own `_ext` catch-all on
    # Guarantor.extensions; the superseded rows are read nowhere else.
    for index, row in enumerate(groups.guarantor.get(guid, [])[:-1]):
        keep("patient-guarantor", index, row, _KEY_ONLY)
    return out


def _map_patient(row: Row, groups: _DemographicsGroups) -> Patient:
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
        for pin in groups.pinned_notes.get(guid, [])
        if _s(pin, "NoteText")
    ]

    giso_rows = groups.giso.get(guid, [])
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
        race=[name for r in groups.race.get(guid, []) if (name := _s(r, "RaceName"))],
        ethnicity=[
            name for r in groups.ethnicity.get(guid, []) if (name := _s(r, "EthnicityName"))
        ],
        language=_s(row, "PreferredLanguage"),
        mothers_maiden_name=_s(row, "MothersMaidenName"),
        status="Active" if _b(row, "IsActive") else "Inactive",
        notes="\n".join(n for n in notes if n) or None,
        identifiers=identifiers,
        telecom=telecom,
        addresses=[address] if any(address.model_dump().values()) else [],
        guarantor=_map_guarantor(groups.guarantor, guid),
        extensions=_ext(row, _DEMOGRAPHICS_MAPPED) | _side_extensions(groups, guid),
        provenance=_prov("patient-demographics", guid),
    )


# patient-guarantor.tsv columns consumed here. NOTE the Billing* names and the
# bare City/State/Zip — this table does NOT share patient-demographics' Address*
# column names.
_GUARANTOR_MAPPED = frozenset(
    {
        "PatientPracticeGuid",
        "FirstName",
        "LastName",
        "BillingPatientRelationshipOption",
        "BillingPaymentType",
        "DateOfBirth",
        "BillingGenderOption",
        "SSNumber",
        "Address1",
        "City",
        "State",
        "Zip",
        "PrimaryPhoneNumber",
        "SecondaryPhoneNumber",
    }
)


def _map_guarantor(guarantor_by: dict[str, list[Row]], guid: str) -> Guarantor | None:
    rows = guarantor_by.get(guid, [])
    if not rows:
        return None
    row = rows[-1]  # last row wins (dict-overwrite load semantics)
    name = " ".join(p for p in (_s(row, "FirstName"), _s(row, "LastName")) if p)
    phones = [
        ContactPoint(kind=kind, value=phone)
        for kind, col in (
            (ContactKind.PHONE_HOME, "PrimaryPhoneNumber"),
            (ContactKind.PHONE_OTHER, "SecondaryPhoneNumber"),
        )
        if (phone := format_phone(_s(row, col)))
    ]
    return Guarantor(
        name=name or None,
        relationship_to_patient=_s(row, "BillingPatientRelationshipOption"),
        payment_preference=_s(row, "BillingPaymentType"),
        birth_date=_d(row, "DateOfBirth"),
        sex=_s(row, "BillingGenderOption"),
        ssn=_s(row, "SSNumber"),
        address=Address(
            line1=_s(row, "Address1"),
            city=_s(row, "City"),
            state=_s(row, "State"),
            postal_code=_s(row, "Zip"),
        ),
        phones=phones,
        extensions=_ext(row, _GUARANTOR_MAPPED),
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

# patient-encounter-diagnoses columns read: EncounterGuid groups the link rows onto
# their encounter (see map_export's hoist), DiagnosisGuid feeds diagnosis_ids. The
# table's own PatientPracticeGuid (redundant with the encounter's patient_id) is
# read by neither — it and any future column ride the encounter's extensions via
# _map_encounter's per-row `side:` loop, same discipline as the demographics side
# rows in _side_extensions.
_ENCOUNTER_DX_MAPPED = frozenset({"EncounterGuid", "DiagnosisGuid"})


def _note_section(kind: SectionKind, raw: str | None, title: str | None) -> NoteSection:
    """One SOAP/narrative section: rich HTML for rendering, text shadow for QA.

    ``html`` carries the ``sanitize_soap_html`` output (the rendering path);
    ``text`` keeps the flattened plain text for search, QA, and plain-text
    consumers.
    """
    sanitized = sanitize_soap_html(raw)
    return NoteSection(
        kind=kind,
        title=title,
        html=sanitized or None,
        text=html_to_text(raw),
    )


def _map_encounter(
    row: Row,
    addenda_by_encounter: dict[str, list[Row]],
    dx_by_encounter: dict[str, list[Row]],
) -> Encounter:
    guid = _s(row, "EncounterGuid")
    patient_guid = _s(row, "PatientPracticeGuid")
    assert guid is not None and patient_guid is not None

    is_soap = _b(row, "IsSoapNote")
    sections: list[NoteSection] = []
    if is_soap:
        for col, kind, title in _SOAP_COLUMNS:
            sections.append(_note_section(kind, _s(row, col), title))
    else:
        # SIMPLE encounters carry the whole narrative in Subjective.
        sections.append(_note_section(SectionKind.NARRATIVE, _s(row, "Subjective"), None))

    # addenda/dx link tables are pre-grouped by EncounterGuid once for the
    # whole export (see map_export) and sliced here.
    addenda = [
        Addendum(
            text=html_to_text(_s(add, "Addendum")),
            status=_s(add, "AmendmentStatus"),
            source=_s(add, "AmendmentSource"),
            at=_dt(add, "LastModifiedDateTimeUtc"),
        )
        for add in addenda_by_encounter.get(guid, [])
    ]

    dx_links = dx_by_encounter.get(guid, [])
    diagnosis_ids = [dx for link in dx_links if (dx := _s(link, "DiagnosisGuid"))]
    dx_extensions: dict[str, Any] = {}
    for index, link in enumerate(dx_links):
        dx_extensions.update(
            _ext(link, _ENCOUNTER_DX_MAPPED, prefix=f"side:patient-encounter-diagnoses:{index}:")
        )

    return Encounter(
        id=guid,
        patient_id=patient_guid,
        date_of_service=_d(row, "DateOfService"),  # DateField: calendar date
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
        extensions=_ext(row, _ENCOUNTER_MAPPED) | dx_extensions,
        provenance=_prov("patient-encounters", guid),
    )


_GROWTH_CHART_AGE = 18  # growth-chart chief complaints are pediatric; skipped at age >= 18


def _skip_reason(encounter: Encounter, birth_date: date | None) -> str | None:
    """Why an encounter is excluded from rendering, or ``None`` if it renders.

    Two selection rules decide exclusion:
      - empty SOAP: all four sections strip to nothing  -> "empty_soap"
      - adult growth chart: CC contains "growth chart" and patient is >=18 at
        DOS                                             -> "adult_growth_chart"
    """
    if not encounter.has_note_content:  # empty SOAP (post-strip text)
        return "empty_soap"
    cc = (encounter.chief_complaint or "").lower()
    if "growth chart" in cc and birth_date and encounter.date_of_service:  # adult growth chart
        if age_at(birth_date, encounter.date_of_service) >= _GROWTH_CHART_AGE:
            return "adult_growth_chart"
    return None


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
    value = _s(row, "Value")
    # Pain values arrive as an LA answer code or a raw number; convert to the
    # canonical 0-10 display value.
    if code in _PAIN_LOINCS:
        value = pain_display(value)
    return Observation(
        patient_id=_s(row, "PatientPracticeGuid") or "",
        encounter_id=_s(row, "EncounterGuid"),
        category=ObservationCategory.VITAL_SIGNS if vital else ObservationCategory.OTHER,
        code=code,
        display=vital.display if vital else None,
        value=value,
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


def _find_vital(by_code: dict[str | None, Observation], kind: str) -> Observation | None:
    """Find an encounter's vital by kind, matching any LOINC alias
    (codes.VitalCode.aliases — see _VITAL_BY_LOINC)."""
    vital = VITALS[kind]
    for code in (vital.loinc, *vital.aliases):
        if code in by_code:
            return by_code[code]
    return None


def _auto_bmi(encounter_obs: list[Observation]) -> Observation | None:
    """The BMI trigger: synthesize 39156-5 when height+weight exist without it.

    Fires for either LOINC edition of weight (primary 3141-9 or the 29463-7
    alias). Unit-aware (in/cm, lb/kg); an explicitly charted BMI always wins.
    """
    by_code = {o.code: o for o in encounter_obs}
    if VITALS["bmi"].loinc in by_code:
        return None
    height = _find_vital(by_code, "height")
    weight = _find_vital(by_code, "weight")
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
        value=f"{value:.2f}",  # 2 decimal places (BMI display precision)
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
            # EffectiveDate/EffectiveDateFrom/RecordedDate in that priority order:
            # the clinical assessment date wins over the administrative entry
            # date; the other value survives in extensions.
            effective = next(
                (
                    d
                    for c in ("EffectiveDate", "EffectiveDateFrom", "RecordedDate")
                    if (d := _dt(row, c))
                ),
                None,
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


# patient-med-history is a free-prose block table (HistoryType: social/family/
# major-events, ReportedHistory: narrative). PastMedicalHistory(kind, text)
# holds all three losslessly; the PF pack renders the `social` kind. The
# structured subcategory fields (alcohol, drug use, diet, ...) have no source
# table in the export.
def _past_medical_history(export: Export, guid: str) -> list[PastMedicalHistory]:
    blocks: list[PastMedicalHistory] = []
    for row in _by(export["patient-med-history"], "PatientPracticeGuid").get(guid, []):
        text = _s(row, "ReportedHistory")
        if text is None:
            continue  # an empty narrative block carries nothing
        blocks.append(
            PastMedicalHistory(
                patient_id=guid,
                kind=_s(row, "HistoryType"),
                text=text,
                extensions=_ext(
                    row, frozenset({"PatientPracticeGuid", "HistoryType", "ReportedHistory"})
                ),
                provenance=_prov("patient-med-history", guid),
            )
        )
    return blocks


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

# patient-allergy-reactions columns read: AllergyGuid groups the link rows onto
# their allergy, Reaction feeds `reactions`. ReactionSnomedCode and the table's
# own (redundant) PatientPracticeGuid survive on the allergy's extensions instead.
_REACTION_MAPPED = frozenset({"AllergyGuid", "Reaction"})


def _map_allergy(row: Row, reactions_by_allergy: dict[str, list[Row]]) -> AllergyIntolerance:
    guid = _s(row, "AllergyGuid") or ""
    reaction_rows = reactions_by_allergy.get(guid, [])
    reaction_extensions: dict[str, Any] = {}
    for index, r in enumerate(reaction_rows):
        reaction_extensions.update(
            _ext(r, _REACTION_MAPPED, prefix=f"side:patient-allergy-reactions:{index}:")
        )
    return AllergyIntolerance(
        id=guid,
        patient_id=_s(row, "PatientPracticeGuid") or "",
        substance=_s(row, "Substance"),
        category=_ALLERGY_CATEGORIES.get(
            (_s(row, "AllergenCategory") or "").lower(), AllergyCategory.OTHER
        ),
        reactions=[reaction for r in reaction_rows if (reaction := _s(r, "Reaction"))],
        severity=_s(row, "Severity"),
        onset=_d(row, "StartDate"),
        active=_b(row, "IsActive"),
        extensions=_ext(row, _ALLERGY_MAPPED) | reaction_extensions,
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
        "Refills",
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
    prefix = resolve_prefix(transactions, _s(row, "DestinationTypeCode"))
    # Display date: Order-sent→Eastern for ESCRIPT, prescription DoS otherwise
    # (see resolve_display_date).
    display_date = resolve_display_date(transactions, prefix, _dt(row, "DateOfService"))
    # Refills: NumberOfRefills, falling back to Refills.
    refills = clean_numeric(row.get("NumberOfRefills"))
    if refills is None:
        refills = clean_numeric(row.get("Refills"))  # -1 sentinel → None
    return Prescription(
        id=guid,
        patient_id=_s(row, "PatientPracticeGuid") or "",
        medication_id=_s(row, "MedicationGuid"),
        prescriber_id=_s(row, "PrescribingProviderGuid"),
        prefix=prefix,
        status_label=resolve_status(transactions),
        display_date=display_date,
        sig=_s(row, "Sig"),
        refills=refills,
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
# Quaternary→3, Other→99 extend the primary/secondary/tertiary benefit ordering.
_BENEFIT_ORDER = {"primary": 0, "secondary": 1, "tertiary": 2, "quaternary": 3, "other": 99}

# superbill-insurances columns a WINNING join actually reads (see _matched_row):
# the PIPG/plan-name join keys and the PlanType value they resolve. Every other
# column rides the coverage's extensions via `residual` below — but only onto
# the row's OWN patient (see `_own_row`), because the name tiers match across
# patients. A row that wins no coverage of its own has NOTHING read from it; see
# `unjoined`, which keeps only its PatientPracticeGuid as the placement key.
_SUPERBILL_JOINED_MAPPED = frozenset({"PatientInsurancePlanGuid", "PlanName", "PlanType"})


class _PlanTypeLookup:
    """The PF insurance TYPE (HMO/PPO/EPO/POS/Medicare/...) three-tier join.

    PF displays the TYPE from superbill-insurances.PlanType — NOT from
    patient-insurances, which only carries the generic "Medical" coverage
    type. Resolve by PatientInsurancePlanGuid first, then lowercased plan
    name, then payer name. The plan-name "(PPO)" regex is the last-resort
    fallback only.

    superbill-insurances is read in FULL (never sliced by a foreign key — see
    _FOREIGN_KEYS), so a row's home is decided here rather than by
    _check_key_closure: `residual` parks a row's surplus columns on the coverage
    its join actually won, and `unjoined` hands back every row whose join won
    nothing, for the caller to preserve non-attributingly instead of dropping.
    """

    def __init__(self, superbill_rows: list[Row]) -> None:
        self._by_pipg: dict[str, Row] = {}
        self._by_name: dict[str, Row] = {}
        for row in superbill_rows:  # build PIPG- and plan-name-keyed lookups
            pipg = _s(row, "PatientInsurancePlanGuid")
            plan_type = _s(row, "PlanType")
            name = (_s(row, "PlanName") or "").lower()
            if pipg and plan_type and pipg not in self._by_pipg:
                self._by_pipg[pipg] = row
            if name and plan_type and name not in self._by_name:
                self._by_name[name] = row

    def _matched_row(self, ins_row: Row) -> Row | None:
        """The superbill row, if any, whose PIPG/name/payer tier wins for
        ``ins_row`` — shared by `resolve` (the TYPE value) and `residual` (that
        row's surplus columns), so the two never disagree on which row won."""
        pipg = _s(ins_row, "PatientInsurancePlanGuid")  # tier 1: exact plan GUID
        if pipg and pipg in self._by_pipg:
            return self._by_pipg[pipg]
        name = (_s(ins_row, "InsurancePlanName") or "").lower()  # tier 2: plan name
        if name and name in self._by_name:
            return self._by_name[name]
        payer = (_s(ins_row, "PayerName") or "").lower()  # tier 3: payer name
        if payer and payer in self._by_name:
            return self._by_name[payer]
        return None

    def resolve(self, ins_row: Row) -> str | None:
        if (row := self._matched_row(ins_row)) is not None:
            return _s(row, "PlanType")
        # Last resort: a "(PPO)"-style suffix some practices embed in the plan
        # name. No superbill row informs this branch.
        match = _PLAN_TYPE_RE.search(_s(ins_row, "InsurancePlanName") or "")
        return match.group(1).upper() if match else None

    def _own_row(self, ins_row: Row) -> Row | None:
        """The matched row, but only when it belongs to the SAME patient.

        The TYPE a tier resolves is a fact about the PLAN — "Evergreen Basic is
        an HMO" is true for everyone on it — so `resolve` may legitimately read
        another patient's row to learn it. Every other column of that row is a
        fact about THAT patient (their practice guid, their superbill guid), and
        the plan-name and payer-name tiers match across patients by
        construction: two patients on one plan resolve to whichever row was
        indexed first. So the surplus columns may only ever be read from the
        row's own patient, or one patient's identifiers land in another's chart.
        """
        row = self._matched_row(ins_row)
        if row is None:
            return None
        return row if _s(row, _PATIENT_KEY) == _s(ins_row, _PATIENT_KEY) else None

    def residual(self, ins_row: Row) -> dict[str, Any]:
        """Surplus columns of the superbill row that resolved this coverage's
        TYPE — empty when the regex fallback resolved it (reads no superbill
        row), when no superbill row informed this coverage at all, or when the
        row that informed it belongs to a different patient (see `_own_row`)."""
        row = self._own_row(ins_row)
        if row is None:
            return {}
        return _ext(row, _SUPERBILL_JOINED_MAPPED, prefix="side:superbill-insurances:")

    def unjoined(self, insurance_rows: list[Row], superbill_rows: list[Row]) -> list[Row]:
        """Superbill rows no patient's coverage consumed.

        Consumed means `_own_row` — a row that only lent its TYPE to another
        patient had nothing else read from it, so it is still unjoined and still
        has to be preserved for its own patient. Counting a cross-patient TYPE
        read as consumption would drop the row from the export entirely.
        """
        used = {
            id(matched)
            for ins_row in insurance_rows
            if (matched := self._own_row(ins_row)) is not None
        }
        return [row for row in superbill_rows if id(row) not in used]


def _map_coverage(row: Row, plan_types: _PlanTypeLookup) -> Coverage:
    plan_name = _s(row, "InsurancePlanName")
    order_label = _s(row, "OrderOfBenefits")
    return Coverage(
        id=_s(row, "PatientInsurancePlanGuid") or "",
        patient_id=_s(row, "PatientPracticeGuid") or "",
        payer=_s(row, "PayerName"),
        plan_name=plan_name,
        plan_type=plan_types.resolve(row),
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
        extensions=_ext(row, _INSURANCE_MAPPED) | plan_types.residual(row),
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


def _unmapped_tables(
    export: Export, patient_guids: frozenset[str]
) -> dict[str, dict[str, list[Row]]]:
    """Account for EVERY table the mapper does not consume — losslessly.

    Returns ``{patient_guid: {table_name: [rows verbatim]}}`` for unmapped tables
    whose every row attributes to a KNOWN patient via ``PatientPracticeGuid``;
    the mapper stashes those rows in the owning patient's ``extensions`` so no
    field is dropped. A table with rows that cannot ALL be attributed to a known
    patient (no patient-key column, a null key, or a guid absent from
    patient-demographics) cannot be placed in the per-patient model, so the run
    is refused (:class:`UnsupportedTablesError`) — failing closed beats silently
    discarding clinical data. Empty tables are ignored (nothing to lose).
    """
    by_patient: dict[str, dict[str, list[Row]]] = {}
    orphans: list[str] = []
    for table in sorted(set(export) - set(KNOWN_TABLES)):
        rows = export[table]
        if not rows:
            continue
        grouped = _by(rows, "PatientPracticeGuid")
        attributed = sum(len(group) for group in grouped.values())
        if attributed != len(rows) or any(guid not in patient_guids for guid in grouped):
            orphans.append(table)
            continue
        for guid, guid_rows in grouped.items():
            by_patient.setdefault(guid, {})[table] = guid_rows
    if orphans:
        raise UnsupportedTablesError(sorted(orphans))
    return by_patient


# Every KNOWN table the mapper reads by slicing a per-key grouping, with the key
# column and the parent whose ids that key must name. superbill-insurances,
# providers, and facilities are deliberately absent: they are read in full, not
# sliced by an owning record, so no row of theirs can be orphaned.
_FOREIGN_KEYS: tuple[tuple[str, str, str], ...] = (
    # patient-demographics is the patient table itself, so the only way one of
    # its rows fails is a MISSING key — which would drop that whole patient.
    ("patient-demographics", _PATIENT_KEY, "patient"),
    ("patient-race", _PATIENT_KEY, "patient"),
    ("patient-ethnicity", _PATIENT_KEY, "patient"),
    (_GISO_TABLE, _PATIENT_KEY, "patient"),
    ("pinned-notes", _PATIENT_KEY, "patient"),
    ("patient-guarantor", _PATIENT_KEY, "patient"),
    ("patient-smokingstatus", _PATIENT_KEY, "patient"),
    ("occupation-industry", _PATIENT_KEY, "patient"),
    ("patient-education", _PATIENT_KEY, "patient"),
    ("patient-financial-resources", _PATIENT_KEY, "patient"),
    ("tribal-affiliation", _PATIENT_KEY, "patient"),
    ("patient-med-history", _PATIENT_KEY, "patient"),
    ("patient-family-medical-history", _PATIENT_KEY, "patient"),
    ("patient-encounters", _PATIENT_KEY, "patient"),
    ("patient-encounter-observations", _PATIENT_KEY, "patient"),
    ("patient-diagnoses", _PATIENT_KEY, "patient"),
    ("patient-allergy", _PATIENT_KEY, "patient"),
    ("patient-medications", _PATIENT_KEY, "patient"),
    ("patient-prescriptions", _PATIENT_KEY, "patient"),
    ("patient-insurances", _PATIENT_KEY, "patient"),
    ("patient-immunizations", _PATIENT_KEY, "patient"),
    ("patient-advance-directives", _PATIENT_KEY, "patient"),
    ("patient-documents", _PATIENT_KEY, "patient"),
    ("patient-encounter-addendums", "EncounterGuid", "encounter"),
    ("patient-encounter-diagnoses", "EncounterGuid", "encounter"),
    ("patient-allergy-reactions", "AllergyGuid", "allergy"),
    ("prescription-transactions", "PrescriptionGuid", "prescription"),
    ("patient-family-history-diagnoses", "RelativeGuid", "relative"),
)


def _check_key_closure(export: Export, known: dict[str, frozenset[str]]) -> None:
    """Refuse KNOWN-table rows whose foreign key names no record in the export.

    The counterpart to :func:`_unmapped_tables` for the tables the mapper DOES
    map: those are read by slicing a grouping with the owning record's guid, so
    a row with a missing or dangling key is grouped once and never read again —
    a silent drop of clinical data. Failing closed (:class:`OrphanRowsError`) is
    the same stance an orphan table gets. Counts only; no row value is read out.
    """
    orphans: dict[str, int] = {}
    for table, column, parent in _FOREIGN_KEYS:
        keys = known[parent]
        # _s gives None for a missing or blank key, and "" is never a known id,
        # so the substitution catches the missing-key row and the dangling one.
        count = sum(1 for row in export[table] if (_s(row, column) or "") not in keys)
        if count:
            orphans[table] = count
    if orphans:
        raise OrphanRowsError(orphans)


def map_export(export: Export) -> Iterator[PatientRecord]:
    """Join the loaded tables into one PatientRecord per patient."""
    # Losslessness: account for every table the mapper does not consume, and for
    # every mapped table's foreign keys, before producing any record (so a
    # refusal happens cleanly, before partial output).
    patient_guids = _ids(export["patient-demographics"], _PATIENT_KEY)
    unmapped_by_patient = _unmapped_tables(export, patient_guids)
    _check_key_closure(
        export,
        {
            "patient": patient_guids,
            "encounter": _ids(export["patient-encounters"], "EncounterGuid"),
            "allergy": _ids(export["patient-allergy"], "AllergyGuid"),
            "prescription": _ids(export["patient-prescriptions"], "PrescriptionGuid"),
            "relative": _ids(export["patient-family-medical-history"], "RelativeGuid"),
        },
    )
    if unmapped_by_patient:
        preserved = sorted({table for tables in unmapped_by_patient.values() for table in tables})
        # Table NAMES are schema, not PHI — safe to log; row values are not logged.
        logger.info(
            "pf_tebra: preserved %d unmapped table(s) into extensions: %s",
            len(preserved),
            preserved,
        )

    practitioners = _map_practitioners(export)
    facilities = _map_facilities(export)
    plan_types = _PlanTypeLookup(export["superbill-insurances"])
    # superbill-insurances is read in full (never sliced by a foreign key — see
    # _FOREIGN_KEYS), so a row that joins no coverage at all is found here, once,
    # rather than through _check_key_closure; preserved per patient below instead
    # of being dropped (see _PlanTypeLookup.unjoined).
    unjoined_rows = plan_types.unjoined(
        export["patient-insurances"], export["superbill-insurances"]
    )
    # These are placed by their OWN PatientPracticeGuid, so a row whose guid is
    # blank or names nobody in this export has no home and would vanish here.
    # superbill-insurances sits outside _check_key_closure (it is read in full,
    # never sliced), so this is the only place that can catch it. Counts only —
    # never a guid value.
    homeless = sum(1 for row in unjoined_rows if _s(row, _PATIENT_KEY) not in patient_guids)
    if homeless:
        raise OrphanRowsError({"superbill-insurances": homeless})
    unjoined_superbill_by_patient = _by(unjoined_rows, _PATIENT_KEY)

    encounters_by_patient = _by(export["patient-encounters"], "PatientPracticeGuid")
    # Encounter-keyed link tables, pre-grouped once (see _DemographicsGroups).
    addenda_by_encounter = _by(export["patient-encounter-addendums"], "EncounterGuid")
    encounter_dx_by_encounter = _by(export["patient-encounter-diagnoses"], "EncounterGuid")
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
    demo_groups = _DemographicsGroups.build(export)  # pinned-notes/giso/race/ethnicity/guarantor

    seen_guids: set[str] = set()
    for demo_row in export["patient-demographics"]:
        guid = _s(demo_row, _PATIENT_KEY)
        assert guid is not None  # _check_key_closure refuses keyless demographics rows
        if guid in seen_guids:
            # Two demographics rows for one patient: downstream the QA lookup and
            # delivery key on the guid, so the second would silently overwrite the
            # first. Fail closed instead. PHI-safe: the guid value is never named.
            raise ValueError(
                "patient-demographics contains a duplicate PatientPracticeGuid "
                "(one demographics row per patient is expected) — the export is "
                "malformed. Resolve the duplicate before migrating."
            )
        seen_guids.add(guid)
        patient = _map_patient(demo_row, demo_groups)

        # The render SELECTION (see _skip_reason) excludes empty-SOAP and
        # adult-growth-chart encounters from `encounters`. Losslessness: rather
        # than dropping them, the skipped ones are stashed in `extensions` so
        # nothing vanishes.
        all_encounters = [
            _map_encounter(row, addenda_by_encounter, encounter_dx_by_encounter)
            for row in encounters_by_patient.get(guid, [])
        ]
        encounters: list[Encounter] = []
        skipped: list[dict[str, Any]] = []
        for encounter in all_encounters:
            reason = _skip_reason(encounter, patient.birth_date)
            if reason is None:
                encounters.append(encounter)
            else:
                skipped.append({"reason": reason, "encounter": encounter.model_dump(mode="json")})
        record_extensions: dict[str, Any] = {}
        if skipped:
            # Counts only — never log patient-derived values (PHI discipline).
            logger.info(
                "pf_tebra: excluded %d of %d encounter(s) from render for patient",
                len(skipped),
                len(all_encounters),
            )
            record_extensions[f"{SOURCE}:skipped_encounters"] = skipped

        # Preserve every unmapped table's rows verbatim (lossless), keyed by table
        # name, so no field the adapter does not yet map is dropped.
        for table_name, table_rows in unmapped_by_patient.get(guid, {}).items():
            record_extensions[f"{SOURCE}:unmapped:{table_name}"] = table_rows

        # A superbill-insurances row whose PIPG/plan-name/payer-name join won no
        # coverage: non-attributing (it fed no typed object), so it lands on the
        # record rather than a specific Coverage. _KEY_ONLY keeps only the
        # PatientPracticeGuid that placed it here; every other column survives.
        if unjoined_superbill := unjoined_superbill_by_patient.get(guid, []):
            record_extensions[f"{SOURCE}:unjoined_superbill_insurances"] = [
                _ext(row, _KEY_ONLY) for row in unjoined_superbill
            ]

        observations = [_map_observation(row) for row in obs_by_patient.get(guid, [])]
        # Single-pass index: build encounter_id -> observations once instead of
        # rescanning all observations per encounter (O(n) instead of O(n*m)).
        obs_by_encounter_id: dict[str | None, list[Observation]] = {}
        for observation in observations:
            obs_by_encounter_id.setdefault(observation.encounter_id, []).append(observation)
        for encounter in encounters:
            if bmi := _auto_bmi(obs_by_encounter_id.get(encounter.id, [])):
                observations.append(bmi)
                obs_by_encounter_id.setdefault(encounter.id, []).append(bmi)
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
            past_medical_history=_past_medical_history(export, guid),
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
            coverages=[_map_coverage(row, plan_types) for row in ins_by_patient.get(guid, [])],
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
            extensions=record_extensions,
            provenance=Provenance(source_system=SOURCE, source_id=guid),
        )
