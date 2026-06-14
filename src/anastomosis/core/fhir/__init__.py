"""FHIR R4 interchange for canonical records.

Design contract (the lossless rule, applied to FHIR):

* Every discrete datum gets a **standard R4 element** where one exists —
  any FHIR consumer can read the chart.
* Canonical fields with no clean FHIR home travel as namespaced
  extensions: ``urn:anastomosis:field:<name>`` (JSON-encoded value), and
  the source ``extensions`` dict travels as ``urn:anastomosis:ext`` —
  Anastomosis-aware consumers reconstruct the record exactly.
* Note narrative ships as a real ``DocumentReference`` carrying
  ``text/html`` (readable anywhere); section structure is preserved with
  ``<section data-kind=...>`` wrappers so ingest can split it back.
* ``provenance`` is local lineage and is not exported (a FHIR Provenance
  mapping is future work).

The bundle is plain JSON-shaped dicts: exporting needs no optional
dependency; the ``fhir`` extra adds schema validation on top.
"""

from .export import to_bundle
from .ingest import from_bundle

__all__ = ["from_bundle", "to_bundle"]
