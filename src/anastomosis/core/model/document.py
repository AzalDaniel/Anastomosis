"""Generated artifacts: the documents this toolkit produces and tracks."""

from __future__ import annotations

from datetime import datetime

from .base import AnastBase


class DocumentArtifact(AnastBase):
    """A rendered document (FHIR DocumentReference at export time)."""

    patient_id: str
    encounter_id: str | None = None
    path: str | None = None
    sha256: str | None = None
    mime_type: str = "application/pdf"
    title: str | None = None
    page_count: int | None = None
    pack_name: str | None = None
    generated_at: datetime | None = None
