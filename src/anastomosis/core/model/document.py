"""Generated artifacts: the documents this toolkit produces and tracks."""

from __future__ import annotations

from datetime import datetime

from .base import AnastBase

__all__ = ["EXT_INLINE_CONTENT", "DocumentArtifact"]

#: Extension key holding an artifact's OWN BYTES, base64 as the source wrote
#: them, for a source whose export has no separate file to copy.
#:
#: A C-CDA Unstructured Document is the case that needs it: the scan is inside
#: the XML, so an artifact naming a ``path`` into the export would name a file
#: that has never existed. Delivery (``pipeline._carry_attachments``) writes
#: these bytes to that path instead of copying one, so both kinds of artifact
#: land in the same place under the same names and every reader downstream sees
#: one thing: a document on disk beside the charts.
#:
#: Model-level rather than source-namespaced because the writer is the pipeline,
#: which must not know which adapter filled it in.
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
