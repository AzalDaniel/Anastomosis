"""FHIR R4 interchange for canonical records.

Contract: every discrete datum maps to a standard R4 element where one
exists; a field with no clean FHIR home rides as
``urn:anastomosis:field:<name>``, and the source's own ``extensions`` dict
as ``urn:anastomosis:ext``. Note narrative ships as a ``DocumentReference``
with ``<section data-kind=...>`` wrappers so ingest can split it back.
``provenance`` is local lineage and is never exported. Plain JSON-shaped
dicts; the ``fhir`` extra adds schema validation on top.
"""

from .export import DeliveredAttachment, to_bundle
from .ingest import from_bundle

__all__ = ["DeliveredAttachment", "from_bundle", "to_bundle"]
