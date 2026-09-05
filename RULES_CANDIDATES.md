# RULES_CANDIDATES — worker W3 (fhir_api, ccda_export)

Candidate rules found while sweeping prose, not already stated in
`docs/RULES.md`. One sentence each, with the file:line the prose came from.
This file does not ship; the orchestrator adjudicates it.

- A destination-attach seam takes the bearer token only as a constructor
  parameter and never reads it from the environment itself, because argv
  would make it `ps`-visible. (`src/anastomosis/deliver/fhir_api/attach.py:31`)
- A C-CDA delivery names its files by patient id and artifact id only, never
  by patient name or the source's own filename, because a C-CDA export is
  the artifact most likely to leave this tool's directory control (emailed,
  imported elsewhere) and it names its own attachments after the patient.
  (`src/anastomosis/deliver/ccda_export/deliverer.py:1`)
- A C-CDA delivery that cannot write every source document a record names
  fails (`ArtifactNotDelivered`) before reporting success, rather than
  deliver a chart referencing missing artifacts (#373).
  (`src/anastomosis/deliver/ccda_export/deliverer.py:46`)

## Loose ends

(none found in the assigned files)
