"""PatientRecord: everything the pipeline knows about one patient."""

from __future__ import annotations

from .actors import Facility, Practitioner
from .base import AnastBase
from .clinical import (
    AdvanceDirective,
    AllergyIntolerance,
    Condition,
    FamilyMemberHistory,
    Goal,
    HealthConcern,
    Immunization,
    ImplantableDevice,
    LabOrder,
    MedicationStatement,
    Observation,
    PastMedicalHistory,
    Prescription,
)
from .coverage import Coverage
from .document import DocumentArtifact
from .encounter import Encounter
from .patient import Patient


class PatientRecord(AnastBase):
    """The unit that flows through the pipeline: one patient, whole chart.

    Maps to a FHIR Bundle (type=collection). Practitioners/facilities are
    denormalized per record so a PatientRecord is always self-contained —
    a record can be archived, bundled, or migrated alone.
    """

    patient: Patient
    encounters: list[Encounter] = []
    observations: list[Observation] = []
    conditions: list[Condition] = []
    allergies: list[AllergyIntolerance] = []
    medications: list[MedicationStatement] = []
    prescriptions: list[Prescription] = []
    immunizations: list[Immunization] = []
    family_history: list[FamilyMemberHistory] = []
    past_medical_history: list[PastMedicalHistory] = []
    advance_directives: list[AdvanceDirective] = []
    health_concerns: list[HealthConcern] = []
    goals: list[Goal] = []
    devices: list[ImplantableDevice] = []
    lab_orders: list[LabOrder] = []
    coverages: list[Coverage] = []
    documents: list[DocumentArtifact] = []
    practitioners: list[Practitioner] = []
    facilities: list[Facility] = []

    def practitioner(self, practitioner_id: str | None) -> Practitioner | None:
        for p in self.practitioners:
            if p.id == practitioner_id:
                return p
        return None

    def facility(self, facility_id: str | None) -> Facility | None:
        for f in self.facilities:
            if f.id == facility_id:
                return f
        return None

    def observations_for(self, encounter_id: str) -> list[Observation]:
        return [o for o in self.observations if o.encounter_id == encounter_id]
