# 0.6.0-alpha — adjudication of the external v0.5.0 review

Date: 2026-07-12. Reviewed artifact: `v0.5.0` = `main` @ `071ddd3`, REQUEST
CHANGES. Every finding was independently re-verified against the code with
live probes before any correction was implemented; two were refuted in part.
The review's architecture verdict ("do not radically rewrite; the canonical
model, adapters, engine, ledger, and L0–L6 separation should remain") is
accepted as-is.

## P1 findings — all four CONFIRMED and fixed

### P1-1 `migrate` reported route availability as delivery — CONFIRMED
`run_migration()` writes charts, an upload manifest, and the C-CDA payload —
no code path executes a delivery route (verified: `deliver/fhir_api` is never
imported by the flow; the browser engine is never instantiated; the "ccda"
delivery command is a file export, not the registry's `ccda_import` route).
Yet `classify_migration()` returned `DELIVERED` whenever `plan_route` chose
any viable route — including Tebra's `ccda_import`, whose registry kind is a
*manual in-product facesheet import*. One precision note owed to the record:
the CLI never printed the literal word "delivered"; the falsehood lived in
the enum value, the GUI `done` event, and the absence of any
you-still-have-to-deliver notice. Fixed with a three-outcome contract:
`PREPARED` (route chosen, artifacts + route plan written — what today's flow
actually earns), `MANUAL_IMPORT` (unchanged), and `DELIVERED` retained but
documented as requiring a durable delivery receipt from a destination
executor (M6) — with a test pinning that `classify_migration` never returns
it until such an executor exists. Both frontends now surface an actionable
prepared-notice; exit codes are unchanged (0 prepared / 1 manual) so
scripting contracts hold.

### P1-2 `ccda-standard` silently bypassed requested QA — CONFIRMED (both halves)
`_run_ccda_standard()` never read `cmd.qa`: a default-QA run rendered PDFs,
exited 0, emitted no QA events, wrote no `qa_report.json` (live-probed both
ways; the neutral render path does all three). Fixed: the document-generic
checks (data-integrity leak detection, layout/pagination) now run per
rendered patient document, the report is written, FAIL exits nonzero, and
the encounter/pack-scoped checks are recorded as skipped *with a reason* —
the same skip-honesty the L0–L6 ladder uses. Conformance half: the README
implied natively importable C-CDA while the builder disclaims Schematron
conformance. An XSD validation gate was attempted honestly and NOT shipped:
the HL7 CDA R2 core schema set was fetched and compiled, and it revealed two
real structural gaps in the exporter itself — the header omits the mandatory
`author` and `custodian` participations, and source-GUID/MRN `<id>` roots
are non-OID — so every current export fails XSD for exporter reasons, not
schema-tooling reasons. Shipping a gate that warns on 100% of outputs (or
quietly vendoring schemas the code cannot satisfy) would be theater. What
shipped instead: truthful claims everywhere (builder docstring, README —
validated by own-parser round-trip; XSD/Schematron conformance explicitly
not yet), and the exporter's structural fixes (author/custodian
participations, OID id roots — the latter already prototyped round-trip-
safe) recorded as the gating precondition for the XSD lane on the backlog.
Full C-CDA 2.1 Schematron conformance stays external-tooling/backlog — the
official `.sch` is ~1 MB with heavy `extends` usage untested against lxml,
and the HL7 repos carry no in-repo license, so an in-process guarantee
would itself be an overclaim.

### P1-3 GUI displayed failed uploads as success — CONFIRMED
The CLI's exit classifier treats any non-clean terminal count as failure;
the GUI worker branched on `aborted_reason` alone. Live probe: an identical
run (counts `{'failed': 1}`, no abort) exited 1 in the CLI while the GUI
emitted `done` → "upload complete". Worse, existing tests pin that a
wrong-chart pre-verify failure lands `PRE_VERIFY_FAILED` with no abort — the
exact shape the GUI mis-displayed. Fixed at the root, per the repo's own
one-core-two-frontends rule: the verdict (`is_clean` / `exit_code`) now
lives on `UploadCommandResult` in `core/upload_command.py`; both frontends
consume it; the GUI emits an error event carrying state names and counts
(PHI-safe) for non-clean completions.

### P1-4 an operator output path still entered logs — CONFIRMED
`deliver_ccda()` logged the full output directory; an operator directory
named after a patient would enter logs verbatim, violating the SECURITY.md
contract as tightened in 0.5.0. This was a deliberate 0.5.0 carve-out
("operator-chosen top-level dir") that the reviewer's probe showed to be the
wrong call — operators name directories after patients. The path is gone
(count-only message), sibling operator-dir logs were swept the same way, and
caplog regressions pin the absence.

## P2 findings — all four CONFIRMED, fixed proportionately

- **P2-5 GUI event scoping + shutdown** — CONFIRMED: all four event
  constructors carry no flow key, one `_WindowSink` feeds whichever page is
  loaded, and dashboard/wizard emit identical event kinds — so navigating
  mid-pipeline makes the wizard announce "migration complete" for a pipeline
  run (`summary_id` defends a different race and does not scope). Workers
  are daemon threads with no close barrier. Fixed: every event now carries a
  `flow` tag stamped at the emit site, pages filter to their own flow, and
  the window's `closing` hook refuses to close silently while a job runs
  (busy-state exposed by the runner).
- **P2-6 pack-trust TOCTOU + lost update** — CONFIRMED (narrow but real: the
  hash gate could be raced between hash and exec). Fixed with a single-read
  snapshot: hash and execution now provably cover the same bytes
  (code + manifest + template pinned; auxiliary assets documented as outside
  the hash boundary), and `record()` re-reads, merges, and atomically
  replaces under the repo's existing file lock. Severity honestly noted: the
  race required a local writer on a directory the operator had already
  explicitly trusted, and the lost-update failed safe (never
  wrongly-trusted).
- **P2-7 upload resource cleanup ordering** — CONFIRMED by forced-failure
  probe (a ledger-construction failure leaked the attached Playwright
  driver). Every resource now registers with the `ExitStack` the instant it
  is owned; the manual `finally` is gone; regression tests force each
  constructor failure and assert release/close ran.
- **P2-8 performance trio** — CONFIRMED: (a) two per-encounter whole-table
  rescans in the PF mapper (tables the earlier hoists never covered) are now
  built once per run, output byte-identical by goldens; (b) QA opened and
  re-extracted each PDF up to four times — now one extraction per document
  cached on the context, report byte-identical, with a fallback so
  third-party QA packs keep working; (c) the FHIR Bulk ingest's full-export
  list accumulation is streamed into the grouping index instead — the honest
  minimal win, because grouping is inherently global (NDJSON is not
  patient-sorted), so constant-memory spooling is recorded as roadmap with
  the memory expectation documented rather than pretended away.

## P3 / architecture — mixed

- **"0.5.0 is a final PEP 440 version, not an alpha" — REJECTED again, same
  recorded rationale.** The plain-version-presented-as-alpha convention is a
  deliberate, DESIGN.md-documented trade (PEP 440 pre-releases fall out of
  default pip/pipx resolution; Windows VERSIONINFO is numeric-only). The
  reviewer restates PEP 440 without engaging the recorded trade-off. The
  decision will be revisited once — at the beta/CS50 cut — on the question
  of whether `pipx install anastomosis` should resolve to the submission
  artifact.
- **README's "73%" and "$5,000–$150,000" figures — CONFIRMED unsourced and
  removed.** Research traced the 73% figure to a phantom citation
  ("According to HIMSS...") recurring verbatim across AI-generated marketing
  blogs, with no such HIMSS publication findable; the price range matches no
  primary source. Notably, `paper/paper.md` never used either figure — its
  claims are DOI-cited. The README now makes the qualitative, cited case
  (Miake-Lye 2023 on migration risk) and drops the invented numbers. This is
  the repo's own no-hallucination rule applied to its own front page.
- **`<version>` literal breaking PyPI rendering — REFUTED.** Byte-level
  inspection shows every `<version>` occurrence repo-wide sits inside a
  Markdown code span; CommonMark escapes code-span content, so it renders
  literally. No change.
- **`python-dateutil` unused — CONFIRMED** (declared, zero imports) and
  removed.
- **`gpdfs:`/`PR-O` archaeology — CONFIRMED (63 + 2 sites) and swept**: the
  private-predecessor line references are meaningless to any reader of this
  repository; comments now state the invariant. The archaeology guard
  learned the new tokens.
- **Lazy import barrels (58 modules / ~1.2–1.8 s CLI import) — DEFERRED to
  M6**: real, measured, and worth doing — but an import-graph refactor is
  exactly the kind of churn that does not belong in the same release as
  four P1 truth fixes. Recorded on the backlog with the measurements.

## Release framing

This cut ships as **0.6.0 (the sixth alpha)**. With all four P1s fixed and
the README claims narrowed, the review's stated bar for CS50 submission is
met except for the one artifact only the author can produce — the demo
video. Beta remains reserved for that submission cut.
