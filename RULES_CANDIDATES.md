# RULES_CANDIDATES — worker W3 (fhir_api, ccda_export)

Candidate rules found while sweeping prose, not already stated in
`docs/RULES.md`. One sentence each, with the file:line the prose came from.
This file does not ship; the orchestrator adjudicates it.

- A destination-attach seam takes the bearer token only as a constructor
  parameter and never reads it from the environment itself, because argv
  would make it `ps`-visible. (`src/anastomosis/deliver/fhir_api/attach.py:31`)

## Loose ends

(none found in the assigned files)
