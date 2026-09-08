"""Delivery: canonical records go where they need to live next.

* :mod:`.archive` — the file-tree deliverer: a static offline-readable
  archive, or one per-patient bundle directory (`.bundle` re-exports it).
* :mod:`.ccda_export` — one C-CDA/CCD XML per patient.
* :mod:`.browser` — browser-automation upload driver.
* :mod:`.fhir_api` — FHIR R4 REST destination.
* :mod:`.verify` — the L0-L6 verification ladder.

Re-exports nothing; import stays cheap (RULES.md 75)."""
