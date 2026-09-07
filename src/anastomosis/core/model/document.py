"""Generated artifacts: the documents this toolkit produces and tracks."""

from __future__ import annotations

from datetime import datetime

from .base import AnastBase

__all__ = ["EXT_INLINE_CONTENT", "DocumentArtifact"]

#: Extension key for an artifact's own bytes (base64), for a source (e.g. a
#: C-CDA Unstructured Document) with no separate file to copy; the pipeline
#: (``_carry_attachments``) writes these bytes to the artifact's path. Model-
#: level, not source-namespaced, because the writer must not know the adapter.
EXT_INLINE_CONTENT = "anast:inline_content"


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
