"""Delivery: canonical records go where they need to live next.

* :mod:`anastomosis.deliver.archive` — a static, offline-readable browser
  archive (plain HTML + JSON + PDFs); openable from ``file://`` with zero
  outbound network requests; durable because every component is plain bytes.
* :mod:`anastomosis.deliver.bundle` — per-patient bundles: one FHIR R4
  Bundle JSON + the rendered chart PDFs + a sliced QA report per patient,
  ready to hand to whoever asked for the record.
* :mod:`anastomosis.deliver.ccda_export` — one C-CDA / CCD XML per patient
  for destinations that import C-CDA.
* :mod:`anastomosis.deliver.browser` — the browser-automation upload driver
  for destinations without an import API.
* :mod:`anastomosis.deliver.fhir_api` — the FHIR R4 REST destination for
  servers that accept DocumentReference writes.
* :mod:`anastomosis.deliver.verify` — the L0-L6 verification ladder shared
  by the upload routes.

Import deliverers from their submodules; this package intentionally
re-exports nothing, so ``import anastomosis.deliver`` stays cheap (no lxml,
no jinja2) for CLI startup.
"""
