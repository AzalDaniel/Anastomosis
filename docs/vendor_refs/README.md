# `docs/vendor_refs/` — vendor-published reference material only

This folder holds **distilled, cited briefs derived from vendor-published
specification material** — data-model dictionaries, export-format
documentation, public API field guides. It exists so a source adapter can be
specced — and audited afterwards — against primary-source facts instead of
memory or web folklore.

**Provenance rule (non-negotiable):**

- Every file here is built **only** from vendor-published spec/schema content
  (e.g. Oracle Health's EHI-export data-format packages). Each brief opens
  with a source table naming every document and linking its public vendor
  URL; each factual claim then cites a source tag, and for material inside a
  specification package, the **package-relative path** within it.
- The vendor packages themselves are **not redistributed in this repo**.
  They are large, vendor-distributed artifacts; download them from the
  vendor's own documentation page to re-verify any claim. If a link rots,
  replace it with title + publisher + year and mark the entry
  "(vendor distribution; not redistributable)" rather than deleting the
  citation.
- **Never patient data.** These briefs describe *table and column structure* —
  names, types, nullability, definitions, join keys — and nothing that could
  be a patient-derived value. If a vendor sample ever contained real PHI, it
  would be reported and excluded, never transcribed (the repo PHI rule, PLAN.md
  decision 4).
- No folklore. If a fact is not in the cited specification material, it is
  listed under "Could not determine from these docs" rather than guessed —
  and the adapter that consumes the brief raises loudly instead of
  inventing vendor semantics.

Think of these as the `tests/fixtures/*/README.md` verified/inferred ledgers,
but for *upstream* vendor formats — whether or not an adapter for them has
shipped yet.
