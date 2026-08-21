"""People and places that appear on clinical documents."""

from __future__ import annotations

from .base import AnastBase


class Practitioner(AnastBase):
    """A provider: renders care, signs notes, prescribes."""

    given_name: str | None = None
    family_name: str | None = None
    display_name: str | None = None
    # Credential text as it appears on documents, e.g. "Nurse Practitioner".
    # Not present in most EHI exports — typically supplied via site overrides.
    credential: str | None = None
    npi: str | None = None

    @property
    def name(self) -> str:
        if self.display_name:
            return self.display_name
        return " ".join(p for p in (self.given_name, self.family_name) if p)


class Facility(AnastBase):
    """A practice location as printed on note headers."""

    name: str | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    phone: str | None = None
    fax: str | None = None
