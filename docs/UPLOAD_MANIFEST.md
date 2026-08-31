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

## v3 — the current version

`MANIFEST_VERSION` is 3. Beyond v2, a v3 file carries the run's REVIEWED
context: the destination route the run was prepared for, and the gates it
passed before the bundle was written. Those are what
`deliver/browser/gates.py`'s `assert_deliverable` refuses on, so the bundle an
executor moves is the bundle somebody checked.

| Field | Scope | Why it is there |
| --- | --- | --- |
| `route` | run | The destination route plan this bundle was prepared for: `{destination, kind}`, where `kind` is a `RouteKind` value or `null` when the planner found no viable automated route. A recorded plan makes the route part of what was reviewed rather than a line that scrolled past on the terminal; a `null` kind refuses delivery, because executing a route the run's own planner rejected means running something nobody reviewed. `null` for the whole object when the run named no destination (a plain `anast pipeline run`). |
| `gates` | run | What the run checked first: `{qa, conservation, layout_hash}`. `qa` is `pass`/`fail`/`not_run`; `conservation` is `balanced` when the render seam's books balanced (an unbalanced batch raises long before a manifest is written); `layout_hash` is the layout content hash `render_provenance.json` published, or `null` where no Jinja layout was involved (the ccda-standard whole-patient view). A recorded gate that is not a pass refuses delivery. |

A `null` `gates` — a v3 file written by a caller that had nothing to record —
is treated the same as a pre-v3 file: warned about, loudly, never refused. See
"Pre-v3" below.

## v2 — still loadable

Beyond v1's identity and integrity fields, a v2 file carries exactly what the
L0–L6 verification ladder needs to run in FULL on the upload path, and nothing
more. Each field group is gated on the version that INTRODUCED it
(`LADDER_VERSION` = 2, `GATE_VERSION` = 3), never on `MANIFEST_VERSION` — the
reader used to compare against the current version, which was correct only
while 2 was newest and would have silently dropped these fields out of a v2
file the moment 3 existed.

| Field | Scope | Why it is there |
| --- | --- | --- |
| `pack` | run | The template pack that rendered these charts, so the upload side can reload the pack manifest L3 reads `verify_header_fields` from. One render run renders through one pack and the verifier holds one pack for the run, so the name is recorded once rather than per item. `null` when no Jinja pack was involved (the whole-patient ccda-standard view) — a genuine "L3 has nothing to check", not a lost field. |
| `expected_pages` | item | The page count of the PDF **as rendered**, so L1 asserts "exactly N pages" instead of only "at least one". |
| `date_of_service` | item | The encounter date L3's `dos` header field is checked against, and — read from the same key on the same item — the document date a destination's filing dialog is handed when its pack discovered a date field. That is the **only** encounter field either one reads, so it is the only one carried: no sections, no clinical content. A `null` is the render run saying it had no date; a pack that needs one then refuses the item rather than filing it under whatever the form defaulted to. |

## v1 — still loadable

Operators have rendered trees on disk and refusing them would strand charts, so
a v1 file still loads — but with v1's coverage (L3 skips, L1 checks only the
page floor) and one loud, PHI-free warning per read. Never a silent downgrade.

Any version outside `SUPPORTED_MANIFEST_VERSIONS` is a defect and raises.

## Pre-v3 — what the delivery gate does about it

A manifest older than v3 records no route and no gate outcomes, so an executor
cannot tell whether those charts were ever verified. It is warned about — one
loud line naming what could not be checked — and delivered anyway. Refusing
every already-rendered tree would be a worse failure than the one the gate was
added for, and it is the same posture the reader already takes with a v1 file.
Everything a pre-v3 manifest DOES carry is still enforced: the per-item sha256
has been there since v1, so a bundle whose charts changed after review is
refused at any version.
