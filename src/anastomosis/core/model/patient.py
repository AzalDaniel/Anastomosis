"""Patient identity and demographics (USCDI Patient data class)."""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from .base import AnastBase


class IdentifierKind(StrEnum):
    PRN = "prn"  # practice/patient record number as printed on notes
    MRN = "mrn"
    SSN = "ssn"
    SOURCE_GUID = "source_guid"
    OTHER = "other"


class Identifier(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: IdentifierKind = IdentifierKind.OTHER
    value: str
    system: str | None = None


class ContactKind(StrEnum):
    PHONE_HOME = "phone_home"
    PHONE_MOBILE = "phone_mobile"
    PHONE_WORK = "phone_work"
    PHONE_OTHER = "phone_other"
    EMAIL = "email"


class ContactPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ContactKind
    value: str


class Address(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line1: str | None = None
    line2: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None


class PatientContact(BaseModel):
    """Next of kin / emergency contact."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    relationship: str | None = None
    phone: str | None = None
    address: Address | None = None


class Guarantor(BaseModel):
    """Financially responsible party (payment information section)."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    relationship_to_patient: str | None = None
    birth_date: date | None = None
    sex: str | None = None
    ssn: str | None = None
    address: Address | None = None
    phones: list[ContactPoint] = []
    payment_preference: str | None = None


class Patient(AnastBase):
    given_name: str | None = None
    middle_name: str | None = None
    family_name: str | None = None
    suffix: str | None = None
    birth_date: date | None = None
    sex: str | None = None
    gender_identity: str | None = None
    sexual_orientation: str | None = None
    race: list[str] = []
    ethnicity: list[str] = []
    language: str | None = None
    marital_status: str | None = None
    mothers_maiden_name: str | None = None
    contact_preference: str | None = None
    status: str | None = None  # e.g. "Active" as shown in demographics
    notes: str | None = None  # patient-level notes block
    identifiers: list[Identifier] = []
    telecom: list[ContactPoint] = []
    addresses: list[Address] = []
    contacts: list[PatientContact] = []
    guarantor: Guarantor | None = None

    @property
    def display_name(self) -> str:
        parts = [self.given_name, self.middle_name, self.family_name, self.suffix]
        return " ".join(p for p in parts if p)

    def identifier(self, kind: IdentifierKind) -> str | None:
        for ident in self.identifiers:
            if ident.kind == kind:
                return ident.value
        return None
