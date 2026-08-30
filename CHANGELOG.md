# Changelog

All notable changes to Anastomosis are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Until 1.0.0,
minor versions may contain breaking changes (noted here when they happen).

## [Unreleased]

Post-0.7.0 audit work: a full pass over the shipped surfaces, driving the real
CLI and the real GUI rather than reading them, with every finding raised as an
issue and fixed in its own pull request.

### Added

- **A conservation ledger for C-CDA ingest, and a corpus to run it against.**
  2,103 real documents went through the adapter, every one parsed, and eleven
  canonical collections came back empty across all of them — no practitioner,
  no facility, no coverage, no document, and not one of 12,277 encounters
  carrying a note. "It parsed" had never been asked to mean anything, because
  nothing counted what the XML OFFERED, and a count of survivors reads the same
  whether the loss was zero or total. `sources/ccda/ledger.py` is the other
  count: it walks the document independently of the parser and gives every
  section, every `<entry>` and every participation exactly one disposition —
  structurally parsed, narrative preserved, unsupported, or source-empty —
  crediting a parse only on evidence (a canonical object whose provenance names
  an id the construct carries), and balancing its books through the same
  `Conservation` primitive the render and delivery seams use.
  `tools/ccda_corpus.py` generates the documents to run it on: deterministic,
  PHI-free, 6,144 shapes spanning the six C-CDA document types against every
  combination of ten structural traps, generated at test time and never
  committed. This measures; it does not fix. (#309)

### Fixed

- **A destination pack can describe the filing dialog it actually meets.**
  Attaching the file was the whole vocabulary: a pack could name a file input
  and a submit button and nothing else, so a chart filed through the browser
  route landed uncategorised, undated, in whatever status the form defaulted
  to and under no provider. Seven optional slots now describe the dialog —
  display name, category, status, date, the patient it prefills, provider,
  note — and the page seam gained the two verbs needed to drive and read them
  (`select_option`, `input_value`). Every slot is optional and skipped when
  unset, so an already-discovered `selectors.yaml` keeps meaning exactly what
  it meant. Two of the fields are gates rather than fields: the date the form
  echoes back must be the date it was given, and the patient the dialog
  prefills must still be the one the chart banner confirmed — the last
  wrong-patient check before anything is committed, and the only one that can
  see inside the dialog. (ANV2-005)
- **A run that is happening now says so.** Every view narrated a run in one
  line on screen and none of it reached a screen reader — a click produced
  silence. One always-present polite region carries it, written together with
  the visible line and only when that line actually changed. (#198)
- **The filing calendar is a table you read, not 42 buttons you cannot press.**
  Every cell was a `<button>` with no click handler and a hand cursor, inside a
  `role="grid"` with no rows at all. (#198)
- **Escape closes what Escape opened.** About, the activity drawer and the
  error-kinds flyout were wired separately and each forgot a different part of
  the contract; one implementation now owns `aria-expanded`, Escape,
  click-elsewhere, and moving focus in and back. Teach's mode tabs answer the
  arrow keys. (#198)
- **Four attributes that named nothing**: a `<label for>` pointing at a `<div>`,
  a patients table with no `<thead>` or `scope`, a column-mapping grid that read
  as a table and said nothing about it, and an `aria-activedescendant` emptied
  rather than removed. (#198)
- **An upload that dies says so before it dies.** A `BaseException` from the
  filing engine — how it deliberately models process death — sailed through
  both safety nets, leaving a run that had started, would never finish, and
  told nobody. (#117)
- **A machine that cannot render charts says so once, with the remedy**, instead
  of one bare exception type per chart. (#202)
- **The activity shortcut fired on the wrong keys**: `Ctrl+L` opened the drawer
  instead of the address bar, and a bare `l` opened it on a chooser trigger,
  where the type-ahead had already claimed the character. (#214)
- **The Uploads search truncated twice and mentioned neither cut**, so a visit
  id past the limit looked absent. (#214)
- **The error banner never left.** It cleared on five run entry points and
  nowhere else, so a message about a fixed problem followed the operator around
  the app. It now clears on a view switch and carries a dismiss control. (#214)
- Two charts can never land on one file; a same-day collision widens its suffix
  until the name is free, and two encounters carrying one id are reported
  rather than silently merged. (#186)
- A re-rendered loss ledger keeps every narrative node. (#122)
- Two exports of one record are byte-identical again. (#193)
- The clinical note is verified to have reached the page. (#188)

### Changed

- **A selector under a name the loader does not know is now refused, loudly.**
  It used to be dropped on the floor: the loader read a closed list of slot
  names, so a typo'd or stale key was read by nobody and reported to nobody
  while the pack still announced itself ready — an operator who discovered a
  selector and watched the field stay empty had no way to find out why. This
  is a breaking change for a hand-edited `selectors.yaml` carrying such a key,
  which will now fail to load with the offending name in the message.
  (ANV2-005)
- **A layout or an export format is shown by the name its author typed.** Both
  registries carry a `display` field now; `anast info` leads with the name and
  dims the id beside it. The front end's hard-coded `ccda → "C-CDA"` exception
  is gone, because the registration carries it. (#164)
- Every command's help says what the command actually does, and names a next
  step that exists. (#196, #197, #201, #203)
- Charts already in a folder answer the question being asked. (#189)
- `anast info` says what is installed, not that it is ready — `anast doctor` is
  the command that tries things. (#190)
- The Uploads counters are named for what they count: charts in the record, and
  PDFs in the folder. They are not the same measurement and used to look like
  it. (#214)

### Release

- **One build, two doors: the Windows package job now also emits an MSIX for
  the Microsoft Store.** The installer is unsigned, so every download meets
  SmartScreen; the Store re-signs each package it ingests with Microsoft's own
  certificate, which is a trusted publisher signature this project does not
  have to buy. The new artifact is packed from the SAME Nuitka layout the Inno
  installer packages — no second build, no chance of the two drifting — and
  ships beside the installer and the SBOM on the release. `anast` stays
  invocable by name through an app-execution alias, the MSIX-native answer to
  the installer's optional PATH task. Nothing about the EXE path changed, and
  nothing here signs anything: a package this repo signed would carry a
  certificate nobody trusts. (#292)
- **The shipped SBOMs name a version.** `dynamic = ["version"]` left both
  documents describing a root component with `version: null` — and dropped the
  package itself from the inventory — so the SBOM could not answer the one
  question it exists for. Both workflows now share `tools/sbom.py`, which
  resolves the version and refuses a document that does not carry it. (#142)
- The installer's SBOM is generated **after** the smoke gate, not before: an
  SBOM for an installer that has not been shown to install is a statement about
  nothing. (#142)
- The installed-footprint measurement can no longer fail a release. It is
  informational by design and was not guarded. (#142)

## [0.7.0] — 2026-08-21

The seventh alpha (**0.7.0-alpha**). Two arcs in one release. First the
surgery arc: scope the product to what it does best, make the repo read as a
product, close the FHIR-API delivery loop, and brand the Windows app.

Then the closure of a full-depth external audit — 22 findings, verdict
REJECT for 0.7.0 — and four internal adversarial review rounds on the
fixes themselves. Every finding was reproduced against this tree before it
was fixed and re-proven after; the review rounds found and closed residual
gaps in the fixes, including two that reopened the exact wrong-patient
collision the first fix was written to reject.

### Added

- **FHIR-API delivery route**: `anast upload --fhir URL` drives the same
  engine, ledger, skiplist, and L0-L6 ladder as the browser route. Bearer
  token via environment variable only (`--fhir-token-env`, default
  `ANAST_FHIR_TOKEN`); `--create-patients` (default on) for migration
  targets; ambiguous patient matches always refused. L4/L5/L6 run on this
  route (L3 skips with "no pack provided"); proven end-to-end against a
  live HAPI server in CI.
- **Product branding**: one SVG master (`assets/icon/icon.svg`) drives the
  multi-resolution exe icon, installer wizard imagery, Add/Remove Programs
  entry, and the AppUserModelID taskbar identity (`tools/make_icons.py`
  regenerates every rendition).
- **GUI behaviour lane** (`tests/gui_e2e`, 49 tests): headless Chromium
  drives the bundled pages through a generated pywebview-bridge stub with a
  console-error recorder and a GuiApi drift guard; runs as its own CI job.
- **Installed-binary smoke** (`packaging/smoke_windows.py`): silent install
  -> installed layout -> installed `anast doctor` -> the dashboard rendered
  inside the real WebView2 window (Playwright over CDP) -> silent uninstall
  with leftover check; wired into the Windows package job.
- **One shared identity predicate** (`core/identity.py`): boundary-anchored
  name, date, and value matching behind the L2/L3/L6 delivery verifier, the
  browser pack's row and banner matchers, and the QA integrity check, so the
  wrong-match defense cannot drift into a substring-loose variant in one
  place and not another. Name boundaries treat the whole Unicode
  hyphen/dash family and all three apostrophes as intra-name joiners;
  truncated values (`"Ann Li..."`) reject as unknown identities.
- **Loud refusals that reach the operator**: `OrphanRowsError` (a row on a
  known table whose foreign key names no record), `AmbiguousUnanchoredError`
  (a dangling patient reference alongside several patients), and
  `RedirectRefusedError` (a FHIR endpoint that answers a redirect). Their
  messages carry table/resource-type names and counts only, and reach the
  CLI and GUI verbatim through a `SourceDataError` passthrough.
- **Path budgeting for delivered trees** (`core/textutil.budgeted_name`,
  `deliver/_shared`): every delivered component is capped and every full
  path budgeted, with a 64-bit distinctness tag on any cut name and a
  per-run claimed-name ledger (`DeliveredNameCollision`) so two different
  source ids can never merge into one delivered slot.
- **Spatial rendering goldens**: page-1 word bounding boxes for both packs,
  regenerated by the same `tools/regen_goldens.py` pass as the text goldens
  — a CSS regression that moves a value under the wrong label now fails the
  lane that page-count, geometry, and extracted text all pass.
- **Third-party license texts ship with every artifact**:
  `assets/licenses/APACHE-2.0.txt` and `OFL-1.1.txt` plus a top-level
  `THIRD_PARTY_LICENSES.md` inventory, carried in the wheel
  (PEP 639 `license-files`) and the installer, with a release-workflow step
  that fails the build if the wheel ever loses them.
- **`PayloadTooLarge` preflight** on the FHIR upload route: an item is
  measured before its bytes are read, so an oversized chart is refused with
  an actionable message instead of materialising several times its size in
  memory.

### Changed

- The tebra destination declares its shipped browser pack in
  `destinations/registry.yaml`, so route planning can select browser
  automation (the GUI surfaces the pack chip; not ready until selector
  discovery).
- Verify ladder opens each PDF twice per item instead of five times;
  `fuzzy_contains` is linear; shared `safe_name`/`hash_and_size`/delivery
  helpers replace copy-pasted implementations; archive/bundle CLI commands
  register from one factory; duplicated page JS consolidated in `shell.js`.
- PyMuPDF is imported as `pymupdf` (the `fitz` alias is deprecated
  upstream); packaging ships it as bytecode (MSVC heap exhaustion) and
  force-includes the modules the pack contexts import (derived from their
  own import statements).
- Vendor EHI spec binaries (~30MB, non-redistributable) removed from the
  repository and its history; `docs/vendor_refs/` cites the public Oracle
  pages instead.
- Authorship and AI-assistance attribution consolidated in `DESIGN.md`;
  per-file citation banners removed (`tools/cs50_citations.py` re-applies
  them on an academic-submission branch).
- The upload engine reads the wrong-patient banner **before** the duplicate
  scan: a chart's existing-documents list is never trusted until the open
  chart is confirmed to be the right patient.
- The engine threads its already-resolved `DestinationPatient` into
  `verify_pre`, so verification never re-resolves through a
  create-capable path (a second resolve could POST a duplicate patient).
- The PHI scanner is **default-deny** for content it cannot read: binaries
  and base64-armored payloads inside text files pass only when the whole
  file's sha256 carries a provenance entry in `tools/phi_allowlist.txt`.
- `configure_logging` brings **every** root handler into the redaction
  chain, including handlers a host installed before importing Anastomosis.
- Windows output-directory hardening is reset → grant → strip → **verify**:
  the DACL is read back and every entry checked against the granted set,
  and an unparseable or unexpected descriptor fails closed.
- CI's mypy lane installs the documented `.[dev,gui]` environment through
  `packaging/constraints.txt`; the `gui` extra is bounded
  `pywebview>=6.2,<7.0` (6.2.1 breaks Nuitka-frozen Windows builds,
  upstream #1817) and the package build pins `==6.2`.
- README states what the runtime does: `migrate` writes a structured C-CDA
  payload, the FHIR seam is the CLI upload route today, and per-route L-level
  coverage is named honestly.

### Fixed

- `anast --help` no longer advertises commands that do not exist.
- Segment toggles were mouse-dead under pointer capture; pages without a
  log strip now surface errors in the banner; an inline style refused by
  the pages' CSP is set via CSSOM (`gui/web/shell.js`).
- The exe version-info and installer copyright name the project's actual
  license (AGPL-3.0-or-later, not MIT).
- Dead surface removed (verified unreferenced): `bmi_imperial`,
  `RenderIndex.unattributed`, ~300 lines of orphaned GUI CSS, unused pack
  tokens, entry-point pack discovery, the parallel upload runner, and the
  never-populated HealthConcern/ImplantableDevice/LabOrder model family
  (their PF chart sections render statically; golden output byte-identical).
- **Wrong-patient collisions at every identity gate**: an expected
  `"Ann Li"` matched inside `"Joann Liang"`, `"Mary-Ann Li-Wong"` (any
  hyphen codepoint), and `"O'Brien"`-style compounds, while a colliding
  unpadded date of birth matched inside a longer one — each accepted the
  wrong chart at the browser row, the banner readback, and the L2 fast path.
  A reordered compound surname passed the row matcher; the resolver clicked
  row 0 regardless of which row it had matched.
- **Silent data loss across three adapters and the exporter**: rows whose
  foreign key named no record, surplus columns on demographics side rows,
  unread name sub-keys and US Core race codes, section narratives the
  structural parsers could not consume (including duplicate section codes
  that overwrote each other), and record-level extensions that never
  reached the C-CDA loss narrative. The declared-loss oracle was
  value-in-haystack and masked real losses through cross-field collisions;
  it is now path-aware.
- **The Windows GUI now starts and renders in the shipped WebView2 window**:
  pywebview 6.2.1 failed to import from the frozen build (pinned to 6.2),
  the smoke discarded the only diagnostic the dying app produced (both
  streams are captured and printed on failure), and its CDP attach could
  never work — pywebview sets WebView2's browser arguments programmatically,
  so the environment variable the smoke relied on was ignored. The
  debugging port is now an opt-in, diagnostics-only setting.
- `safe_name` returned unbounded components, so a long source id produced a
  path the filesystem refused; the chart-PDF copy was unbudgeted and its
  failure was logged and skipped past — a chart silently missing from a
  delivered tree.
- The uninstall leftover check asked only for `*.exe`, missing DLLs, fonts,
  bundled Chromium data, and logs.

### Security

- The FHIR client **refuses every redirect**. `urllib`'s default opener
  follows redirects and re-attaches request headers, so a server-chosen
  target could receive the endpoint's bearer authorization; the client now
  raises rather than follow, and the operator is told to configure the
  final URL.
- Both release workflows pass ref and tag names to shell steps as quoted
  environment values. Git permits quotes, semicolons, and backticks in a
  ref name and the `v*` filter accepts them, so interpolating a tag into a
  `run:` block was shell injection inside jobs holding `id-token: write`.
- The PyPI release refuses a tag that does not name the version the source
  builds, before anything is built — the invariant the Windows release path
  already enforced.

## [0.6.0] — 2026-07-12

The sixth alpha (**0.6.0-alpha**). Closes the external v0.5.0 review's four
P1 truth defects — every one a case of a claim exceeding the runtime, the
exact defect class this product exists to prevent — plus its confirmed P2s.

### Fixed

- **`migrate` no longer reports route availability as delivery**
  (`core/migration_status.py`) — a chosen route classified the run as
  `DELIVERED` even though `run_migration` executes no route (and Tebra's
  `ccda_import` is a manual in-product import). New three-outcome contract:
  `PREPARED` (route chosen; charts, C-CDA payload, upload manifest, and route
  plan written — delivery NOT executed) is what the flow earns today;
  `DELIVERED` is reserved for a future destination executor with a durable
  receipt, and a test pins that classification never returns it until then.
  Both frontends print an actionable prepared-notice; exit codes unchanged.
- **`--render ccda-standard` no longer bypasses requested QA**
  (`core/migrate.py`) — the mode ignored `--qa` entirely (no events, no
  report, exit 0). The document-generic checks (data-integrity leak
  detection, layout/pagination) now run per rendered patient document,
  `qa_report.json` is written, FAIL exits nonzero, and the encounter/pack-
  scoped checks are recorded as skipped with a reason.
- **GUI no longer displays failed uploads as success**
  (`core/upload_command.py`) — the CLI failed non-clean terminal counts
  while the GUI checked only the abort reason, so a wrong-chart
  `PRE_VERIFY_FAILED` run read "upload complete". The verdict (`is_clean`,
  `exit_code`, PHI-safe non-clean summary) now lives on
  `UploadCommandResult` in the shared core; both frontends consume it.
- **No operator output path in logs** (`deliver/ccda_export/deliverer.py`) —
  the export-complete log carried the full output directory; operators name
  directories after patients. Count-only now, with caplog regressions.
- **Cross-page GUI events** (`gui/events.py`) — every event now carries a
  `flow` tag and each page filters to its own flow, so navigating mid-run
  can no longer make the wizard announce another flow's completion; the
  window's close path now refuses to silently interrupt a running job.
- **Pack-trust hash gate is race-free** (`reconstruct/packtrust.py`) — the
  external-pack hash was computed from one read and the code executed from a
  second, so a local writer could swap content between check and use. The
  executable surface (`context.py`) is now execution-pinned to the same
  single-read snapshot the hash covered, and `pack.yaml` is parsed from
  pinned bytes; `template.html` contributes to the hash and is
  presence-checked but still renders from disk (a bounded, non-importing
  Jinja surface — render-from-snapshot is on the backlog), and auxiliary
  assets are documented as outside the hash. Trust-store writes re-read,
  merge, and atomically replace under the repo file lock.
- **Upload resources register with the ExitStack the instant they are
  owned** (`core/upload_command.py`) — a ledger/verifier construction
  failure used to leak the attached Playwright driver.
- **Unsourced README statistics removed** — the "73% of organizations" and
  "$5,000–$150,000" figures traced to phantom citations with no primary
  source; the problem statement now makes the qualitative case on cited,
  peer-reviewed evidence (as `paper/paper.md` always did).

### Changed

- **PF mapper builds its encounter link-table indexes once per run** (the
  two per-encounter whole-table rescans the earlier hoists never covered) —
  ~9× faster encounter mapping on a 300-encounter probe, output
  byte-identical by goldens.
- **QA extracts each PDF once per run** instead of up to four times (one
  snapshot per document cached on the context, with a fallback so
  third-party QA packs keep working) — report byte-identical.
- **FHIR Bulk ingest streams NDJSON into the grouping index** instead of
  double-buffering per file; the memory expectation (resident memory scales
  with export size; spooling is roadmap) is documented instead of implied
  away.
- C-CDA conformance claims aligned to the code: the export is validated by
  round-trip with this repo's own parser; CDA XSD structural validation is
  blocked on two now-documented exporter gaps (mandatory `author`/
  `custodian` participations, OID id roots) and recorded, with full
  Schematron conformance, on the backlog.
- `python-dateutil` removed (declared, never imported); predecessor
  line-reference comments (`gpdfs:`) and stale worklog tags rewritten as
  present-tense invariants, with the archaeology guard extended to ban the
  tokens.

## [0.5.0] — 2026-07-03

The fifth alpha (**0.5.0-alpha**). Two fixes from the external alpha-4 review
plus the piece that makes the Windows app real for users: a release path that
actually ships the installer.

### Fixed

- **Raw source identifiers no longer enter logs** (`core/logutil.py`
  `safe_log_id()`) — the log contract said "opaque ids", but the ids being
  logged were the source systems' own GUIDs (`PatientPracticeGuid`,
  `PERSON_ID`, encounter/event ids, and the upload `item_key` that embeds an
  encounter GUID) — linkable, not opaque, on the machine where the export
  lives. Every logging site that interpolated one now routes it through
  `safe_log_id()`: an HMAC-SHA256 surrogate keyed per process, so log lines
  about the same record still correlate within a run but are unlinkable
  across runs and cannot be confirmed against the export. Display surfaces
  (CLI failure lines, upload console, resumability ledger) deliberately keep
  real ids — they are operator working surfaces inside hardened directories,
  not logs. SECURITY.md states the contract precisely; the caplog tests
  assert the surrogate form and the absence of the raw id.
- **The PHI scanner works without a git checkout** (`tools/phi_scan.py`) —
  it enumerated files via `git ls-files`, so users running the test suite
  from a source ZIP or sdist got a scanner crash instead of a scan. When git
  enumeration is unavailable it now falls back to a deterministic recursive
  walk with an explicit prune set (VCS/cache/venv/build directories); under
  git, behavior is unchanged. A scanner that silently skipped non-git users
  would have been a hole, not a fallback.

### Added

- **Publish a release from the Actions tab** — the Windows-package and PyPI
  workflows gain a guarded `workflow_dispatch` publish mode (main-only,
  version-asserted) in which the release action creates the `v<version>` tag
  itself. The 0.4.0 installer was built and CI-validated but never reached
  the Releases page because publishing required a terminal tag push; that
  hard dependency is gone.
- **Installer polish** (`packaging/anastomosis.iss`) — optional desktop-icon
  task, `UninstallDisplayIcon`, and the AGPL license page in the wizard. The
  launch-Anastomosis-on-finish checkbox already existed. Known GA gaps,
  documented: code signing (needs a purchased certificate; SmartScreen
  guidance is in the README) and a bespoke application icon.

## [0.4.0] — 2026-07-03

The fourth alpha (**0.4.0-alpha**). Closes the external release review with
hardening rather than suppression, and turns the repository into a complete,
self-explaining product: real Windows PHI-at-rest protection, log redaction
that is actually installed, executable (not decorative) CodeQL policy, a
design/authorship record (`DESIGN.md`), and the remaining size hotspots split
behind stable facades. No new feature surface — this release's job is
trustworthiness.

### Added

- **Windows PHI-at-rest hardening** (`core/output.py`) — `secure_output_dir`
  now hardens every output directory on Windows NTFS: ACL inheritance
  stripped, access restricted to the current user, SYSTEM, and Administrators
  (the posture CPython adopted for `os.mkdir(mode=0o700)` in the
  CVE-2024-4030 fix, and Win32-OpenSSH uses for key material), via `icacls`
  with literal, localization-safe SIDs and fail-safe ordering (grant before
  inheritance strip — no failure mode can lock the operator out). ACL-less
  filesystems (FAT32/exFAT) degrade to a loud warning; the PHI-warning README
  lands regardless. POSIX behavior (`0o700`) is unchanged. A real ACL
  assertion runs in the Windows CI lane.
- **CodeQL, for real** (`.github/workflows/codeql.yml`) — an advanced-setup
  workflow (push / PR / weekly) with the `security-extended` suite, whose
  built-in `AlertSuppression.ql` query honors inline `# codeql[rule-id]`
  comments with no extra pack, placed at exactly the audited PHI-by-design
  write sites — no rule is excluded repo-wide. Each suppression sits beside
  a `PHI-BY-DESIGN` rationale
  comment, and a policy test pins that every suppression carries one. (The
  repository's code-scanning *default setup* must be disabled once in
  Settings for the workflow's uploads to be accepted — GitHub rejects
  advanced SARIF while default setup is on.)
- **`DESIGN.md`** — the design record: architecture, data model, the
  genuinely debated decisions, hardest problems, verification strategy, and
  the authorship record.
- **Single-sourced Playwright pin** (`packaging/constraints.txt`) — the CI
  e2e lane and the Windows packaging build both resolve Playwright through
  one constraints file (`pip install -c`); the Windows browser-cache key
  derives from the file's hash; a drift test pins the whole arrangement
  (library floor stays open for users — builds pin).
- A review-archaeology CI guard: a test that bans review-history tokens from
  src/, tests/, tools/, and workflows — comments state invariants; history
  lives in this changelog.

### Fixed

- **Log redaction is now actually installed.** `configure_logging()` — the
  only code that installs the `RedactionFilter` — existed but was never
  called, so production logging ran unredacted through Python's last-resort
  stderr handler. The redacting handler is now installed idempotently at
  both entry points (the CLI root callback and the GUI main), the filter
  learned the `MM-DD-YYYY` filename date shape, and a pipeline-level
  regression test asserts no fixture patient name can appear in any log
  record.
- **The one patient-derived log message.** The archive deliverer's
  missing-PDF warning logged a rendered filename that embeds patient name +
  date of service; it now logs the opaque patient id. Remaining messages
  that interpolated paths under an output directory were aligned with the
  repo convention (never a path under out_dir) and their tests updated.
- One vulnerability-reporting SLA: the root `SECURITY.md` became the single
  reporting policy (72-hour acknowledgement, coordinated disclosure); a stale
  unchecked CDP-attach backlog item (shipped in 0.2.0) was corrected in
  `docs/PLAN.md`.

### Changed

- **The size hotspots are split behind stable facades** (the "post-beta"
  refactor, pulled forward): `cli.py` (1,650 lines) now delegates its command
  groups to focused modules while remaining the import facade — every public
  symbol, monkeypatch seam (`cli._make_destination`, `cli.console`), entry
  point, and help string is preserved and pinned by the existing boundary
  and encoding tests; `UploadConsole.upload_start` (167 lines) is decomposed
  into its pre-flight and worker stages. The PF mapper was evaluated and
  deliberately left whole (already function-decomposed; goldens pin its
  output byte-identical).
- Review-history comments across src/, tests/, tools/, and CI were rewritten
  as invariant comments — each now states the property it pins, not the
  review that requested it.

## [0.3.0] — 2026-06-24

The third alpha. Packages the toolkit as a downloadable Windows application — a
normal installer that bundles its own Python runtime and Chromium (with no
separate `pip`/`playwright` step) and installs the Edge WebView2 runtime when it
is absent — and clears the last CLI/GUI disparities so the backend CLI and the
desktop GUI drive identical shared command cores rather than parallel
implementations. The packaging build, the installer, and a silent
install-and-self-check are produced and validated on Windows CI. PR numbers in
parentheses.

### Added

- **Downloadable Windows application** (`packaging/`,
  `.github/workflows/windows-package.yml`) — two Nuitka `--mode=standalone`
  executables (the windowed GUI app and the `anast` console CLI), each bundling
  the Python runtime, Chromium, and every data asset, packaged by Inno Setup
  into a single installer with a Start-menu shortcut, an uninstaller, an optional
  "add `anast` to PATH" task, and a silent Edge WebView2 install when the runtime
  is absent. No separate `pip install` or `playwright install` step. Built and
  self-checked on Windows CI — installed silently and re-checked end to end — and
  attached to the GitHub release on a version tag.
- **`anast doctor`** (`core/selfcheck.py`) — a bundled-asset self-check that
  resolves and reads every shipped asset (the destination registry and tebra
  pack, both built-in template packs, the GUI web pages and fonts, the HL7
  `CDA.xsl` and its siblings, the learned-source synonym and schema files, the
  archive assets) and, in a frozen build, confirms the bundled Chromium is
  present. CI runs it against the FROZEN executable before packaging and again
  against the INSTALLED executable after, so a mis-bundled asset fails the build
  instead of reaching an operator. (#63)
- A standalone GUI entry point (`anastomosis.gui.__main__` plus a `gui-scripts`
  console entry) that the installed Start-menu shortcut targets. (#63)
- **L0–L6 verification ladder around uploads** (`anast upload` + a GUI "Verify
  uploads" toggle) — the implemented `LayeredVerifier` (`deliver/verify/`) is now
  reachable from both frontends through the shared upload command: L0 file
  integrity, L1 page/size, L2 document identity (fuzzy name ≥0.88 + DOB
  hard-fail), and, after upload, L5 metadata and L6 round-trip read-back. It runs
  by default (see Fixed); the engine's live wrong-patient banner abort runs on
  every upload regardless.

### Changed

- **One shared command core per flow, consumed by both the CLI and the GUI**, so
  the two frontends cannot diverge: migration-status classification
  (`core/migration_status.py`, #57), the upload command (`core/upload_command.py`,
  #58), and the source-init command (`core/source_init_command.py`, #59). The GUI
  learn-a-source wizard now runs asynchronously, like the pipeline, migrate, and
  pack flows. (#59, #60)
- The Practice Fusion pack's `build_context` is decomposed into focused,
  output-preserving helpers, and the flowsheet's vital-by-encounter scan is built
  once per record instead of once per encounter. Rendered output is byte-identical.
  (#61, #62)

### Fixed

- **The CLI no longer crashes on a non-UTF-8 (e.g. CP-1252) Windows console.** A
  new `core/presentation.py` resolves Unicode versus ASCII glyphs from the output
  stream's encoding; the transit map and every arrow-printing line use it, and
  the ASCII markers are bracket-free so Rich does not strip them. UTF-8 output is
  unchanged. (#56)
- The GUI surfaces a no-automated-route migration as an error and manual-import
  path instead of a silent success, matching the CLI (which already writes the
  C-CDA payload and exits non-zero). (#57)
- Upload `max_attempts` is unified to 3 across the CLI and GUI, the GUI gains a
  `--skiplist`, and the GUI now acquires the busy-guard and output lock BEFORE
  reading the upload manifest, closing a lock-then-read race. (#58)
- All five GUI async methods (`run_pipeline_async`, `run_migration_async`,
  `pack_init_async`, `source_init_async`, `upload_start`) now guard the
  worker-thread spawn: if `Thread.start()` fails, they release the busy flag and
  return a clean error dict instead of propagating to the bridge and wedging the
  GUI in "Busy".
- **No silent table loss in the Practice Fusion adapter.** The loader now reads
  EVERY `*.tsv` in an export (not just the 30 it maps); the mapper preserves each
  unmapped table's rows verbatim in the owning patient's `extensions`, and refuses
  the run (`UnsupportedTablesError`) when a table's rows cannot be attributed to a
  known patient — failing closed rather than discarding clinical data (e.g. an
  unmapped `patient-procedures` table).
- **Upload verification is ON by default and fails closed.** `anast upload` (and
  the GUI) now run the L0-L6 wrong-chart/wrong-patient ladder unless the operator
  explicitly passes `--no-verify`; if the render extra the ladder needs is absent,
  the run is refused rather than filing unverified. Filing into the wrong chart is
  worse than not filing.
- **Upload lock fences the migrate layout too.** `run_upload_command` now locks
  both the output dir AND the resolved manifest root (a `migrate` writes/locks the
  manifest under `<out>/charts`, a different lock dir), closing the lock-then-read
  race for that layout.
- **Windows installer "add to PATH" correctness:** the optional task writes the
  MACHINE `Path` (the install is per-machine/elevated, so the per-user HKCU hive
  would have been the elevating admin's, not the user's); it records an
  installer-owned marker and strips the entry on uninstall ONLY when that marker
  is present (delimiter-anchored), so a pre-existing or manually-added entry is
  never removed; and `ChangesEnvironment=yes` broadcasts the change so a new shell
  sees `anast` without a logout.
- **Windows package integrity:** CI now self-checks the FROZEN GUI bundle
  (`Anastomosis.exe --self-check`, the Start-menu target), not only the CLI bundle;
  the `anast doctor` tebra-pack check targets the BUNDLED pack specifically (a user
  pack can no longer mask a missing built-in); the WebView2 bootstrapper download
  is Authenticode-verified (signer = Microsoft); the release action is pinned to a
  commit SHA; and a release tag is asserted to equal `v<version>`.
- **GUI dashboard runs can now produce the upload manifest** the upload console
  consumes (a "write upload manifest" toggle threading `write_manifest` through
  `run_pipeline_async`) — GUI parity for `pipeline run --upload-manifest`. The
  upload console also gained the `--pack-dir` parity it was missing (it had
  hard-coded `null`).
- **The GUI bridge exposes only safe methods.** A `GuiApi` facade is bound as
  pywebview's `js_api` instead of the raw controller, so the synchronous heavy
  methods (`run_pipeline`/`run_migration`/`pack_init`/`source_init`) and `doctor`
  (which can start Playwright) are no longer callable from JS and cannot freeze
  the bridge; the front end uses the `*_async` variants.
- **Per-run GUI result summaries** are keyed by an opaque `summary_id` carried on
  the `done` event, so a rapid second run can no longer overwrite the per-patient
  detail the first run's UI is about to read.
- **Browser-upload teardown owns its Playwright resources.** When a run ends,
  `run_upload_command` releases the Playwright driver + CDP connection it owns
  (`browser.close()` then `playwright.stop()`, which per Playwright only
  disconnect a `connect_over_cdp` browser) — never the operator's EHR browser,
  and distinct from the manager's per-recycle session `close()`.
- **Cross-platform CI hygiene:** `core/locking.py` type-checks cleanly under
  `mypy --platform win32` (the fcntl/msvcrt branches are `sys.platform`-guarded);
  the packgen body-font e2e test accepts Windows's serif rendering
  (`TimesNewRomanPSMT`), not only a literal "Serif".

## [0.2.0] — 2026-06-15

The second alpha. Generalizes ingest and output so a migration is "from any EHR
to any EHR": a FHIR R4 source adapter, a standard HL7 C-CDA render view, an
`anast migrate` from→to command, and — when the toolkit meets a structured export
it has never seen — the ability to learn that format from a single example. Adds
the browser-upload delivery engine (a CLI and a GUI console that drives it), a
cited destination-capability registry with a shortest-path router, the
pack-from-samples layout learner, and the desktop GUI, all on the v0.1.0
foundational pipeline. PR numbers in parentheses.

### Added

- **Learn a new source format from one example** (`anast source init`,
  `sources/learned/`, `core/sourcelearn.py`, `core/model_paths.py`) — when a flat
  CSV/TSV/JSON/NDJSON export is not recognized, teach it once: a local, PHI-safe
  analysis profiles each column (counts, inferred types, digit/letter-masked
  shapes — never a raw value) and a deterministic matcher (column-name similarity
  via `rapidfuzz` + a shipped synonym table + value-type affinity) proposes a
  mapping to the canonical model, which the operator confirms. The mapping is
  saved as declarative DATA — a validated `MappingSpec` with a closed transform
  verb table, no executable code — auto-detected thereafter by a column
  fingerprint and shareable by copying its directory. Unmapped columns are
  preserved losslessly in `extensions`, and a round-trip proves no column is
  dropped before the mapping is saved. A matching GUI wizard ships too. (#49, #50)
- **`anast migrate --from <source> --to <destination>`** (`core/migrate.py`) —
  the from→to composition: ingest any source, plan the delivery route, and emit
  BOTH the human-readable charts AND the structured C-CDA payload the target
  imports. Re-runnable migration profiles persist in `~/.anastomosis`. PF→Tebra
  becomes the special case `--from pf-tebra --to tebra`. (#46)
- **FHIR R4 / US Core source adapter** (`sources/fhir_r4/`) — ingests a FHIR R4
  Bundle or a Bulk-Data `$export` NDJSON directory into canonical records,
  deterministically and source-traced; unmapped fields → `extensions`. (#44)
- **Standard C-CDA render mode** (`reconstruct/ccda_standard/`,
  `anast migrate --render ccda-standard`) — renders the structured C-CDA payload
  through HL7's own vendored `CDA.xsl` stylesheet to a neutral, vendor-agnostic
  per-patient PDF with no network egress, so a migration is never dressed in
  another vendor's house style. (#45)
- **`anast upload` + a GUI upload console that drives it** (`cli.py`,
  `gui/web/console.{html,js}`) — file reconstructed charts into a destination EHR
  through its web UI over a loopback-only DevTools attach, resumable across a hard
  kill; the GUI console starts/stops/monitors a run against the same ledger and
  never closes the operator's browser. (#47, #54)
- **Browser-delivery safety spine** (`deliver/browser/`) — the 15-state upload
  state machine (`UploadState` + legal-transition graph) over a WAL-mode SQLite
  ledger that survives a hard kill mid-upload, with a `FakeDestination` test
  double and a kill-and-resume test. (#13)
- **Upload engine** (`deliver/browser/engine.py`) — drives one item through the
  state machine: patient resolve, duplicate scan, pre/post verification, upload,
  bounded retry, and a skiplist; loud, PHI-safe permanent vs. transient failure
  classes. (#14)
- **Parallel workers, session manager, CDP attach, and run reports**
  (`deliver/browser/{parallel,manager,cdp,reports}.py`) — bounded concurrency,
  a session/manifest manager, loopback-only Chrome DevTools Protocol attach
  (never stores credentials), and PHI-safe run reports. (#15)
- **L0–L6 verification ladder** (`deliver/verify/`) — the wrong-patient
  defense: L0 file integrity, L1 page/size, L2 identity fuzzy match (≥0.88) with
  a date-of-birth hard-fail, L3 pack-driven header fields, L4 live patient-banner
  readback, L5 destination metadata, L6 byte/identity round-trip; stacked behind
  the engine's verifier seam. (#16)
- **Capability registry + shortest-path router** (`destinations/registry.py`,
  `destinations/registry.yaml`, `deliver/router.py`) — destinations declare
  capabilities as cited data; the router picks vendor API → C-CDA import →
  browser automation, and never routes an `unverified` capability. (#17)
- **Browser destination packs + discovery wizard** (`destinations/browserpack.py`,
  `destinations/wizard.py`, `destinations/tebra/`, `anast destination init`) —
  the Tebra pack ships with DISCOVER-placeholder selectors discovered by the
  operator against their own session; no vendor DOM is ever invented. (#18)
- **FHIR R4 API pusher** (`deliver/fhir_api/`) — a stdlib-`urllib` FHIR R4 REST
  client that files charts as `DocumentReference` resources (https, or http only
  for loopback), validated against a HAPI/Medplum-style integration service. (#19)
- **C-CDA export deliverer** (`deliver/ccda_export/`) — `PatientRecord` →
  C-CDA R2.1 / CCD XML for destinations that import C-CDA, with this repo's own
  C-CDA parser as the read-back contract. (#20)
- **Golden rendering tests + Synthea e2e lane** — text-and-geometry
  golden tests pinning Chromium output, plus an end-to-end pipeline lane over a
  vendored synthetic Synthea C-CDA sample. (#21)
- **Layout-learner harvest + inference** (`packgen/extract.py`,
  `packgen/infer.py`) — PyMuPDF-only, fully offline span/drawing harvest and
  deterministic, explainable inference (type scale, column grids, design tokens,
  section taxonomy, static-text intersection). (#22)
- **Layout-learner draft-pack emitter + wizard** (`packgen/emit.py`,
  `anast pack init --from-samples`) — writes a loadable draft template pack
  (mirroring `generic_soap`) with a same-patient confirmation gate and a DRAFT
  provenance note. (#23)
- **GUI shell + headless controller + pipeline dashboard** (`gui/`) — a
  pywebview shell over a fully testable, never-raising controller and thin
  vanilla-JS pages; the liquid-glass dashboard drives the *same* pipeline core
  as the CLI with live ingest/reconstruct/QA counters. (#24)
- **Migration wizard, section-selection matrix, upload console, and
  pack-init UI** (`gui/web/`, `gui/controller.py`) — the transit map as the
  wizard centerpiece, section-flag toggles on the run form, an upload
  console over the 15-state ledger (exception-TYPE histograms only, opaque item
  keys in the Cmd+K palette), a vendor-change freshness toast, and the
  pack-init page with the same-patient confirmation gate. (#25)
- **Frontend-free pipeline core** (`pipeline.py`) — extracted from the CLI so
  the CLI and GUI drive identical code, emitting PHI-safe `StageEvent`s. (#24)
- **Oracle Health / Cerner Millennium EHI adapter** (`sources/oracle_ehi/`) —
  ingests the single-patient V500 export (`v500/{schema,activity,reference}`
  MySQL dumps) via a dependency-free, tolerant `INSERT`-statement reader that
  raises loudly on malformed SQL. Maps the PERSON/ENCOUNTER/CLINICAL_EVENT
  spine plus the §4 notes pathway (CE_BLOB local text, CE_BLOB_RESULT remote
  document *references* — never fetched), resolves `*_CD` through CODE_VALUE,
  filters to current row versions, and routes every unconsumed column to
  `oracle_ehi:` extensions. CE_BLOB compression (brief §8 could-not-determine)
  is a loud `NotImplementedError`, not a guess; PHI-safe logging throughout.
- **Practice Fusion SOAP-note template pack** (`packs/practice_fusion_soap/`) —
  the 35-section forensic PF chart replica, re-typed from the predecessor's
  gold standard: 3-column PATIENT/FACILITY/ENCOUNTER header, the unified 6-column
  demographics table, active/inactive insurance + payment, vitals + vitals
  flowsheet, diagnoses, drug/food/environmental allergies, current/historical
  medications with the ESCRIPT/SCRIPT prescription lines, immunizations, the 17
  social-history sub-categories, PMH, family/advance-directive/devices/health-
  concerns/goals, SOAP, orders, screenings, observations, quality of care, care
  plan, and the conditional addenda table. Honors the documented engine lessons
  exactly (forensic `#f1f1f1` band, `print-color-adjust: exact`, the
  border-collapse "3 lines not 4" rule, the `orphans/widows: 2` + page-break
  rules, Letter geometry with the `.6/.38/.44/.39in` margins) with all real
  clinic identity synthesized (neutral placeholder logo + footer URL, providers
  from synthetic fixtures). Ships a PF golden lane and a packgen fixed-point
  re-discovery e2e (the learner recovers the pack's section taxonomy + band fill
  from its own renders). `RULES.md` records the forensics; `tools/regen_goldens.py`
  now regenerates every pack's golden. (#4)

### Changed

- **Shared pack-init command core** (`core/packinit.py`) — `anast pack init` and
  the GUI now run one analyze→confirm→emit flow; the GUI variant runs off the
  bridge thread so the window stays responsive. (#51)
- **GUI migrate-wizard parity** — the wizard exposes the same pack-dir / trust /
  force / section / QA levers the CLI's `migrate` does, threaded through to the
  same migration core. (#52)
- **Per-record render index built once per record**, not once per encounter — a
  pure-performance change; rendered output stays byte-identical (the e2e goldens
  prove it). (#53)

### Fixed

- **Clean errors on bad / empty / no-route input** — a malformed or empty export
  now fails with a clean exit 2 (PHI-safe, exception-TYPE name only) instead of a
  raw traceback or a silent zero-document "success"; an `anast migrate` to a
  destination with no viable automated route still writes the importable C-CDA
  but exits 1 loudly; and a run locks every output directory, not just the charts
  dir. (#48)
- **Guarantor mapping read invented columns** — the `pf_tebra` adapter's
  `patient-guarantor.tsv` mapping now reads the predecessor-verified column
  set (`BillingPatientRelationshipOption`, `BillingPaymentType`,
  `DateOfBirth`, `BillingGenderOption`, `SSNumber`, bare `City`/`State`/`Zip`,
  `PrimaryPhoneNumber`/`SecondaryPhoneNumber`), so payment preference, DOB,
  sex and SSN populate on a real export instead of silently coming up empty;
  unmapped guarantor columns stash losslessly into the new
  `Guarantor.extensions`. The PF pack's payment cells render the
  predecessor's exact empty states (`-` everywhere, `Primary Insurance`
  preference default) — a present-but-sparse guarantor previously printed
  literal `None` into the PDF. (#4)
- **Windows tracking race** — set the SQLite `busy_timeout` before switching to
  WAL `journal_mode`, fixing a Windows CI race in the upload ledger. (#15)
- **Tracking busy-timeout on slow CI** — raised the ledger busy timeout to 30s
  because `synchronous=FULL` commits could starve the prior 5s window on CI. (#20)

### Security

- **CDP attach is loopback-only** — the DevTools Protocol attach refuses
  non-loopback hosts, warns on shared machines, and never stores credentials. (#15)
- **FHIR client URL guard** — the FHIR base URL must be https (or http only for
  a loopback host); errors carry status codes and resource TYPE names, never
  patient-derived values. (#19)
- **No-hallucination capability registry** — any non-`none` destination
  capability must carry a `source_url` and `verified` date or registry
  validation fails loudly; `unverified` capabilities never route. (#17)
- **No invented vendor DOM** — the Tebra browser pack ships only DISCOVER
  placeholders; real selectors are operator-discovered per tenant via the
  wizard and stored in a user overlay file. (#18)
- **PHI-safe layout learner** — sample PDFs may be named after patients and
  contain per-patient data, so `packgen` stores opaque sample indices, suppresses
  single-sample static/per-patient inference, and restates the same-patient
  caveat in the emitted `DRAFT.md`. (#22, #23)
- **Pack logo cannot reach the network or the filesystem at large** — the
  PF pack's `tokens.logo_data_uri` override accepts only inline `data:` URIs
  (an http/https/file URL would make Chromium fetch it while rendering PHI),
  and `tokens.logo_asset` refuses paths that resolve outside the pack root. (#4)

## [0.1.0] — 2026-06-11

First release: the complete foundational pipeline — one command from a raw EHI
export to verified, human-readable chart documents and a searchable offline
archive. Everything below shipped across PRs
[#1](https://github.com/AzalDaniel/Anastomosis/pull/1),
[#8](https://github.com/AzalDaniel/Anastomosis/pull/8),
[#9](https://github.com/AzalDaniel/Anastomosis/pull/9), and
[#10](https://github.com/AzalDaniel/Anastomosis/pull/10).

### Added

- **Canonical clinical model** (`core/model/`) — lossless, FHIR R4-aligned
  pydantic v2 core: Patient, Practitioner, Facility, Encounter (SOAP note
  sections + addenda), Observation (vitals + social history), Condition,
  AllergyIntolerance, MedicationStatement/Request (e-script transactions),
  Coverage, FamilyMemberHistory, Immunization, AdvanceDirective,
  DocumentArtifact, PatientRecord. Every model carries an `extensions` dict so
  no source field is ever silently dropped. (#1)
- **Core utilities** (`core/`) — sentinel-safe parsing (`\N`, `-1`,
  `1/1/0001` return `None`, never fake values), 7-format date parsing,
  zoneinfo-based local-time conversion, phone/age/HTML sanitizers, LOINC
  vitals map with unit-aware BMI auto-calculation. (#1)
- **Practice Fusion / Tebra source adapter** (`sources/pf_tebra/`) — joins the
  29-table PF EHI v9 export graph into patient records; lossless `extensions`
  enforced per table; e-script status priority resolution. Built and tested
  against a fully synthetic fixture set. (#1)
- **C-CDA / CCD source adapter** (`sources/ccda/`) — ingests C-CDA R2.1
  continuity-of-care documents: problems, medications, allergies,
  immunizations, vitals, results, encounters, notes, social history;
  unmapped sections preserved under namespaced extension keys. (#9)
- **FHIR R4 export/ingest** (`core/fhir/`) — standard resources with exact
  round-trip: export a PatientRecord to a FHIR R4 Bundle and re-ingest it
  back to an identical record, proven by tests. (#8)
- **Reconstruction engine + template packs** (`reconstruct/`, `packs/`) —
  Jinja2 + Chromium rendering with renderer recycling, crash relaunch,
  deterministic filename-collision handling, and idempotent skip; defensive
  pack registry (a broken pack is diagnosed and disabled without taking the
  system down); built-in `generic_soap` pack with user-togglable section
  flags. (#1)
- **QA engine** (`qa/`) — every rendered document is verified:
  data-integrity (placeholder/unresolved-template leak detection),
  layout/pagination, LOINC vitals presence, and date-staleness checks with
  boundary-anchored matching; mutation-corpus self-tests; `--qa` pipeline
  stage exits nonzero on FAIL. (#1)
- **Offline archive deliverer** (`deliver/archive/`) — static, zero-network
  searchable archive openable from `file://`: plain folders, per-encounter
  HTML, rendered PDFs, and FHIR R4 Bundle JSON per patient — readable for
  decades without a database. (#10)
- **Per-patient bundle deliverer** (`deliver/bundle/`) — chart bundles for
  record requests, with per-patient sliced QA reports. (#10)
- **CLI** (`anast`, alias `anastomosis`) — `anast pipeline run <export-dir>
  --out <dir>` with source auto-detection, `--pack`/`--pack-dir`, section
  flag overrides, and `--force`; `anast info` lists available sources and
  packs. (#1)
- CI across ubuntu + windows × Python 3.11/3.12 with a dedicated PHI-scan
  lane and an e2e lane. (#1)

### Security

- **PHI scanner** (`tools/phi_scan.py`) — full-tree scan with a SHA-256
  hashed deny-list and generic PHI patterns, running in pre-commit and CI
  from the first commit; untracked-file blind spot closed; allowlist ledger
  requires written justification per entry. (#1, #9)
- **Log redaction** (`core/logutil.py`) — a logging filter scrubs
  SSN/phone/email/date shapes; error paths log counts, ids, and exception
  type names via `exc_tag()`, never patient-derived values. (#1)
- **Output hygiene** (`core/output.py`) — output directories created `0o700`
  with a PHI-warning README. (#1)
- **Hardened XML parsing** — the C-CDA parser disables entity resolution,
  network access, DTD loading, and huge trees
  (`resolve_entities=False, no_network=True, load_dtd=False,
  huge_tree=False`). (#9)
- **Pack trust model v1** — built-in packs are implicitly trusted; external
  packs load only with explicit `--pack-dir` opt-in. (#1)
- Strict gates: `mypy --strict`, ruff with bandit (S) and naive-datetime
  (DTZ) rules, gitleaks pre-commit, least-privilege CI permissions. (#1)
- `SECURITY.md` — reporting policy, threat model, and security posture. (#9)

[Unreleased]: https://github.com/AzalDaniel/Anastomosis/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/AzalDaniel/Anastomosis/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/AzalDaniel/Anastomosis/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/AzalDaniel/Anastomosis/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/AzalDaniel/Anastomosis/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/AzalDaniel/Anastomosis/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/AzalDaniel/Anastomosis/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/AzalDaniel/Anastomosis/releases/tag/v0.1.0
