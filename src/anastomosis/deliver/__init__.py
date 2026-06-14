"""Delivery: canonical records go where they need to live next.

Three destinations matter for the v0.1 archive vertical slice:

* :mod:`anastomosis.deliver.archive` — Archivist persona. A static,
  offline-readable browser archive (plain HTML + JSON + PDFs); openable
  from ``file://`` with zero outbound network requests; 30-year durable
  because every component is plain bytes.
* :mod:`anastomosis.deliver.bundle` — Responder persona. Per-patient
  bundles: one FHIR R4 Bundle JSON + the rendered chart PDFs + a sliced
  QA report per patient, ready to hand to whoever asked for the record.
* :mod:`anastomosis.deliver.ccda_export` — Migrator persona. One C-CDA / CCD
  XML per patient for destinations that import C-CDA (the router's middle
  route, between a native write API and browser automation).
* Browser/API destinations land in M2 (``deliver.browser`` / ``deliver.fhir_api``).
"""

from anastomosis.deliver.archive import ArchiveDeliverer, ArchiveResult
from anastomosis.deliver.bundle import BundleDeliverer, BundleResult
from anastomosis.deliver.ccda_export import build_ccd, deliver_ccda

__all__ = [
    "ArchiveDeliverer",
    "ArchiveResult",
    "BundleDeliverer",
    "BundleResult",
    "build_ccd",
    "deliver_ccda",
]
