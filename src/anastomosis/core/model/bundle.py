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
    Immunization,
    MedicationStatement,
    Observation,
    PastMedicalHistory,
    Prescription,
    ScreeningEvent,
)
from .coverage import Coverage
from .document import DocumentArtifact
from .encounter import Encounter
from .patient import Patient

#: Record kinds a rendered chart should show. Vitals are excluded: they are
#: encounter-scoped, not patient-scoped, and already covered by two checks.
CHARTABLE_KINDS: tuple[str, ...] = (
    "conditions",
    "allergies",
    "medications",
    "immunizations",
    "results",
)


class PatientRecord(AnastBase):
    """Contract: the unit that flows through the pipeline, one patient, whole
    chart, mapped to a FHIR Bundle (type=collection). Practitioners and
    facilities are denormalized per record so it is always self-contained —
    archivable, bundled, or migrated alone.
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
    goals: list[Goal] = []
    # A health concern shares a goal's four fields (who, what, onset,
    # active), so it reuses Goal rather than a duplicate class.
    health_concerns: list[Goal] = []
    screening_events: list[ScreeningEvent] = []
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

    def observations_by_encounter(self) -> dict[str | None, list[Observation]]:
        """Contract: groups observations by encounter id in one pass. Order
        within each group matches ``observations``, so this equals
        ``observations_for(eid)`` per key; observations with no encounter id
        group under ``None``.
        """
        grouped: dict[str | None, list[Observation]] = {}
        for observation in self.observations:
            grouped.setdefault(observation.encounter_id, []).append(observation)
        return grouped
