"""Anastomosis canonical model.

FHIR R4-aligned, USCDI-informed, pydantic v2. Every model carries an
``extensions`` dict that preserves unmapped source fields (lossless
guarantee) and optional ``provenance`` tracing back to source rows.
"""

from .actors import Facility, Practitioner
from .base import AnastBase, Provenance
from .bundle import CHARTABLE_KINDS, PatientRecord
from .clinical import (
    AdvanceDirective,
    AllergyCategory,
    AllergyIntolerance,
    Condition,
    FamilyMemberHistory,
    Goal,
    Immunization,
    MedicationStatement,
    Observation,
    ObservationCategory,
    PastMedicalHistory,
    Prescription,
    PrescriptionTransaction,
    ScreeningEvent,
)
from .coverage import Coverage
from .document import DocumentArtifact
from .encounter import Addendum, Encounter, NoteSection, SectionKind
from .patient import (
    Address,
    ContactKind,
    ContactPoint,
    Guarantor,
    Identifier,
    IdentifierKind,
    Patient,
    PatientContact,
)

__all__ = [
    "CHARTABLE_KINDS",
    "Addendum",
    "Address",
    "AdvanceDirective",
    "AllergyCategory",
    "AllergyIntolerance",
    "AnastBase",
    "Condition",
    "ContactKind",
    "ContactPoint",
    "Coverage",
    "DocumentArtifact",
    "Encounter",
    "Facility",
    "FamilyMemberHistory",
    "Goal",
    "Guarantor",
    "Identifier",
    "IdentifierKind",
    "Immunization",
    "MedicationStatement",
    "NoteSection",
    "Observation",
    "ObservationCategory",
    "PastMedicalHistory",
    "Patient",
    "PatientContact",
    "PatientRecord",
    "Practitioner",
    "Prescription",
    "PrescriptionTransaction",
    "Provenance",
    "ScreeningEvent",
    "SectionKind",
]
