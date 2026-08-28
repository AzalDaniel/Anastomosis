# The upload manifest — schema versions

Reference for `deliver/browser/persist.py`. The module's docstring states the
invariants; this is the per-version field list, which a reader needs once.

## Why the file exists

A browser upload is a separate, operator-driven step that happens *after* the
charts are reconstructed — often on a different machine, against a live EHR
session the operator logs into by hand. The upload driver cannot re-run the
pipeline, so it needs the render run's `UploadItem` manifest and the `Patient`
demographics (the resolver searches the destination by name + DOB) written to
disk, ready to read back.

## v2 — the current version

`MANIFEST_VERSION` is 2. Beyond v1's identity and integrity fields, a v2 file
carries exactly what the L0–L6 verification ladder needs to run in FULL on the
upload path, and nothing more.

| Field | Scope | Why it is there |
| --- | --- | --- |
| `pack` | run | The template pack that rendered these charts, so the upload side can reload the pack manifest L3 reads `verify_header_fields` from. One render run renders through one pack and the verifier holds one pack for the run, so the name is recorded once rather than per item. `null` when no Jinja pack was involved (the whole-patient ccda-standard view) — a genuine "L3 has nothing to check", not a lost field. |
| `expected_pages` | item | The page count of the PDF **as rendered**, so L1 asserts "exactly N pages" instead of only "at least one". |
| `date_of_service` | item | The encounter date L3's `dos` header field is checked against. That is the **only** encounter field L3 reads, so it is the only one carried: no sections, no clinical content. |

## v1 — still loadable

Operators have rendered trees on disk and refusing them would strand charts, so
a v1 file still loads — but with v1's coverage (L3 skips, L1 checks only the
page floor) and one loud, PHI-free warning per read. Never a silent downgrade.

Any version outside `SUPPORTED_MANIFEST_VERSIONS` is a defect and raises.
