# `docs/vendor_refs/` — vendor-published reference material only

This folder holds **distilled, cited briefs derived from vendor-published
specification material** — data-model dictionaries, export-format
documentation, public API field guides. It exists so a future source adapter
can be specced against primary-source facts instead of memory or web folklore.

**Provenance rule (non-negotiable):**

- Every file here is built **only** from vendor-published spec/schema content
  (e.g. Oracle Health's EHI-export data-format packages committed under
  `docs/*.zip`). Each factual claim cites the source file by its
  **zip-relative path**.
- **Never patient data.** These briefs describe *table and column structure* —
  names, types, nullability, definitions, join keys — and nothing that could
  be a patient-derived value. If a vendor sample ever contained real PHI, it
  would be reported and excluded, never transcribed (the repo PHI rule, PLAN.md
  decision 4).
- No web research. If a fact is not in the committed files, it is listed under
  "Could not determine from these docs" rather than guessed.

Think of these as the `tests/fixtures/*/README.md` verified/inferred ledgers,
but for *upstream* formats the project does not yet adapt.
