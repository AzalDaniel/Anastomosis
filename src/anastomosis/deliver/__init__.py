"""Delivery: canonical records go where they need to live next.

* :mod:`.archive` — static offline-readable browser archive.
* :mod:`.bundle` — per-patient FHIR bundle + rendered charts + QA slice.
* :mod:`.ccda_export` — one C-CDA/CCD XML per patient.
* :mod:`.browser` — browser-automation upload driver.
* :mod:`.fhir_api` — FHIR R4 REST destination.
* :mod:`.verify` — the L0-L6 verification ladder.

Re-exports nothing; import stays cheap (RULES.md 75)."""
