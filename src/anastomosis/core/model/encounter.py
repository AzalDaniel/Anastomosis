"""Encounters and the clinical narrative (the part vendors lose in migration)."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from .base import AnastBase


class SectionKind(StrEnum):
    SUBJECTIVE = "subjective"
    OBJECTIVE = "objective"
    ASSESSMENT = "assessment"
    PLAN = "plan"
    NARRATIVE = "narrative"  # free-form note body (non-SOAP sources)


class NoteSection(BaseModel):
    """One narrative section of a note. HTML is preserved verbatim from the
    source (sanitized at render time, never at ingest), with a plain-text
    shadow for search and QA."""

    model_config = ConfigDict(extra="forbid")

    kind: SectionKind
    title: str | None = None
    html: str | None = None
    text: str | None = None

    @property
    def is_empty(self) -> bool:
        return not (self.text or "").strip()


class Addendum(BaseModel):
    """An amendment appended to a signed note."""

    model_config = ConfigDict(extra="forbid")

    text: str | None = None
    status: str | None = None  # e.g. "Accepted" / "Rejected"
    author_name: str | None = None
    author_credential: str | None = None
    source: str | None = None
    at: datetime | None = None


class Encounter(AnastBase):
    patient_id: str
    # Calendar date, deliberately not a datetime: sources chart DOS as a
    # date-only field, and a midnight-UTC datetime shifts to the previous
    # day the moment it's rendered in a western timezone. Precise instants
    # (signed, seen, modified) are datetimes below.
    date_of_service: date | None = None
    chief_complaint: str | None = None
    encounter_type: str | None = None
    note_type: str | None = None  # LOINC document type lands here at to_fhir time
    provider_id: str | None = None
    facility_id: str | None = None
    signed_by_id: str | None = None
    signed_at: datetime | None = None
    last_modified_at: datetime | None = None
    sections: list[NoteSection] = []
    addenda: list[Addendum] = []
    diagnosis_ids: list[str] = []  # Conditions attached to this encounter

    def section(self, kind: SectionKind) -> NoteSection | None:
        for s in self.sections:
            if s.kind == kind:
                return s
        return None

    @property
    def has_note_content(self) -> bool:
        return any(not s.is_empty for s in self.sections)
