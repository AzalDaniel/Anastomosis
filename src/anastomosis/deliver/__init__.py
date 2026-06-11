"""Delivery: canonical records go where they need to live next.

Three destinations matter for the v0.1 archive vertical slice:

* :mod:`anastomosis.deliver.archive` — Archivist persona. A static,
  offline-readable browser archive (plain HTML + JSON + PDFs); openable
  from ``file://`` with zero outbound network requests; 30-year durable
  because every component is plain bytes.
* :mod:`anastomosis.deliver.bundle` — Responder persona. Per-patient
  bundles: one FHIR R4 Bundle JSON + the rendered chart PDFs + a sliced
  QA report per patient, ready to hand to whoever asked for the record.
* Browser/API destinations land in M2 (``deliver.browser`` / ``deliver.fhir_api``).
"""

from anastomosis.deliver.archive import ArchiveDeliverer, ArchiveResult
from anastomosis.deliver.bundle import BundleDeliverer, BundleResult

__all__ = ["ArchiveDeliverer", "ArchiveResult", "BundleDeliverer", "BundleResult"]
