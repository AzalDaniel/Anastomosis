"""Discrete clinical data: observations, problems, meds, allergies, et al."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from .base import AnastBase


class ObservationCategory(StrEnum):
    VITAL_SIGNS = "vital-signs"
    SOCIAL_HISTORY = "social-history"
    LABORATORY = "laboratory"
    SCREENING = "screening"
    OTHER = "other"


class Observation(AnastBase):
    patient_id: str
    encounter_id: str | None = None
    category: ObservationCategory = ObservationCategory.OTHER
    code: str | None = None  # LOINC where known
    display: str | None = None  # e.g. "Blood Pressure", "TOBACCO USE"
    value: str | None = None
    unit: str | None = None
    effective_at: datetime | None = None
    recorded_at: datetime | None = None


class Condition(AnastBase):
    patient_id: str
    icd10: str | None = None
    snomed: str | None = None
    display: str | None = None
    acuity: str | None = None
    onset: date | None = None
    stopped: date | None = None
    recorded_at: datetime | None = None
    active: bool = True


class AllergyCategory(StrEnum):
    DRUG = "drug"
    FOOD = "food"
    ENVIRONMENT = "environment"
    OTHER = "other"


class AllergyIntolerance(AnastBase):
    patient_id: str
    substance: str | None = None
    category: AllergyCategory = AllergyCategory.OTHER
    reactions: list[str] = []
    severity: str | None = None
    onset: date | None = None
    active: bool = True


class PrescriptionTransaction(BaseModel):
    """One status transition in a prescription's life (the raw material the
    escript status-priority resolution runs on)."""

    model_config = ConfigDict(extra="forbid")

    kind: str  # the status value, e.g. "Sent", "Verified", "Dispensed"
    description: str | None = None  # source's transaction description text
    note: str | None = None
    at: datetime | None = None
    destination_type: str | None = None


class Prescription(AnastBase):
    """A single script event attached to a medication (FHIR MedicationRequest)."""

    patient_id: str
    medication_id: str | None = None
    prescriber_id: str | None = None
    prefix: str | None = None  # "ESCRIPT" | "SCRIPT" (source convention)
    status_label: str | None = None  # resolved label, e.g. "DISPENSED"
    display_date: datetime | None = None
    sig: str | None = None
    refills: str | None = None
    quantity: str | None = None
    transactions: list[PrescriptionTransaction] = []


class MedicationStatement(AnastBase):
    patient_id: str
    generic_name: str | None = None
    brand_name: str | None = None
    strength: str | None = None
    route: str | None = None
    dose_form: str | None = None
    display_name: str | None = None  # rendered "Generic (Brand) Strength Route Form"
    sig: str | None = None
    associated_dx: str | None = None
    rxnorm: str | None = None
    start: date | None = None
    stop: date | None = None
    last_modified_at: datetime | None = None
    active: bool = True
    prescription_ids: list[str] = []


class Immunization(AnastBase):
    patient_id: str
    vaccine: str | None = None
    administered_on: date | None = None
    source: str | None = None
    lot_number: str | None = None
    expires: date | None = None
    comment: str | None = None


class FamilyMemberHistory(AnastBase):
    patient_id: str
    diagnosis: str | None = None
    relation: str | None = None
    onset_date: date | None = None


class PastMedicalHistory(AnastBase):
    """Free-prose history blocks (social / family / major events)."""

    patient_id: str
    kind: str | None = None
    text: str | None = None


class AdvanceDirective(AnastBase):
    patient_id: str
    directive: str | None = None
    recorded_at: datetime | None = None


class HealthConcern(AnastBase):
    patient_id: str
    description: str | None = None
    effective: date | None = None
    active: bool = True


class Goal(AnastBase):
    patient_id: str
    description: str | None = None
    effective: date | None = None
    active: bool = True


class ImplantableDevice(AnastBase):
    patient_id: str
    description: str | None = None
    recorded_at: datetime | None = None


class LabOrderItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    test_name: str | None = None
    note: str | None = None


class LabOrder(AnastBase):
    patient_id: str
    encounter_id: str | None = None
    lab_name: str | None = None
    ordered_at: datetime | None = None
    items: list[LabOrderItem] = []
